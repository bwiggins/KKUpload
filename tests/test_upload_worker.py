from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from list_planner import ListRecord
from upload_worker import UploadJobConfig, UploadWorker


class FailingCreateClient:
    def list_lists(self) -> tuple[ListRecord, ...]:
        return ()

    def create_manual_list(
        self,
        *,
        name: str,
        parent_id: str | None,
    ) -> ListRecord:
        raise RuntimeError("create failed")


class EmptyDryRunClient:
    def list_lists(self) -> tuple[ListRecord, ...]:
        return ()

    def create_manual_list(
        self,
        *,
        name: str,
        parent_id: str | None,
    ) -> ListRecord:
        raise AssertionError("Dry-run should not create lists.")


class SuccessfulUploadClient:
    def __init__(self) -> None:
        self.records: dict[tuple[str | None, str], ListRecord] = {}
        self.created_lists: list[tuple[str, str | None]] = []
        self.uploaded_files: list[Path] = []
        self.created_bookmarks: list[tuple[str, str]] = []
        self.assigned_lists: list[tuple[str, str]] = []
        self.attached_tags: list[tuple[str, tuple[str, ...]]] = []
        self.bookmark_id = "bookmark-1"
        self.asset_id = "asset-1"

    def list_lists(self) -> tuple[ListRecord, ...]:
        return tuple(self.records.values())

    def create_manual_list(
        self,
        *,
        name: str,
        parent_id: str | None,
    ) -> ListRecord:
        record = ListRecord(
            id=f"list-{len(self.records) + 1}",
            name=name,
            parent_id=parent_id,
            type="manual",
        )
        self.records[(parent_id, name)] = record
        self.created_lists.append((name, parent_id))
        return record

    def upload_asset(self, file_path: Path) -> dict:
        self.uploaded_files.append(file_path)
        return {"assetId": self.asset_id}

    def create_asset_bookmark(
        self,
        *,
        asset_id: str,
        file_name: str,
    ) -> dict:
        self.created_bookmarks.append((asset_id, file_name))
        return {"id": self.bookmark_id}

    def add_bookmark_to_list(self, *, list_id: str, bookmark_id: str) -> None:
        self.assigned_lists.append((list_id, bookmark_id))

    def attach_tags_to_bookmark(
        self,
        *,
        bookmark_id: str,
        tag_names: tuple[str, ...],
    ) -> None:
        self.attached_tags.append((bookmark_id, tag_names))

    def get_bookmark(self, bookmark_id: str) -> dict:
        return {
            "id": bookmark_id,
            "content": {"assetId": self.asset_id},
            "tags": [
                {"name": tag_name}
                for _, tag_names in self.attached_tags
                for tag_name in tag_names
            ],
        }

    def get_bookmark_lists(self, bookmark_id: str) -> tuple[ListRecord, ...]:
        list_ids = {
            list_id
            for list_id, assigned_bookmark_id in self.assigned_lists
            if assigned_bookmark_id == bookmark_id
        }
        return tuple(
            record
            for record in self.records.values()
            if record.id in list_ids
        )


class FailingUploadClient(SuccessfulUploadClient):
    def upload_asset(self, file_path: Path) -> dict:
        raise RuntimeError("upload failed")


class UploadWorkerTests(unittest.TestCase):
    def test_list_creation_failure_finishes_as_stopped(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            upload_folder = Path(temp_dir)
            (upload_folder / "image.jpg").write_text("x")

            worker = UploadWorker(
                UploadJobConfig(
                    server_url="https://karakeep.example.test",
                    api_key="token",
                    upload_folder=upload_folder,
                    completed_folder=Path(temp_dir) / "completed",
                    error_folder=Path(temp_dir) / "errors",
                    dont_move_completed=False,
                    dont_move_failed=False,
                    dont_preserve_move_structure=False,
                    rename_move_conflicts=False,
                    import_to_root=False,
                    root_list="IMPORT SORTING",
                    default_tags=(),
                    dry_run=False,
                ),
                client=FailingCreateClient(),
            )

            finished_values: list[tuple[bool, int, int, int]] = []
            worker.finished.connect(
                lambda stopped, succeeded, failed, not_processed: (
                    finished_values.append(
                        (stopped, succeeded, failed, not_processed)
                    )
                )
            )

            worker.run()

            self.assertEqual(finished_values, [(True, 0, 1, 1)])

    def test_progress_range_counts_folders_lists_and_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            upload_folder = Path(temp_dir)
            (upload_folder / "root.jpg").write_text("x")
            (upload_folder / "Models").mkdir()
            (upload_folder / "Models" / "pose.jpg").write_text("x")

            worker = UploadWorker(
                UploadJobConfig(
                    server_url="https://karakeep.example.test",
                    api_key="token",
                    upload_folder=upload_folder,
                    completed_folder=Path(temp_dir) / "completed",
                    error_folder=Path(temp_dir) / "errors",
                    dont_move_completed=False,
                    dont_move_failed=False,
                    dont_preserve_move_structure=False,
                    rename_move_conflicts=False,
                    import_to_root=False,
                    root_list="IMPORT SORTING",
                    default_tags=(),
                    dry_run=True,
                ),
                client=EmptyDryRunClient(),
            )

            ranges: list[tuple[int, int]] = []
            progress_values: list[tuple[int, str, int]] = []
            worker.progress_range.connect(
                lambda total_operations, total_files: ranges.append(
                    (total_operations, total_files)
                )
            )
            worker.progress.connect(
                lambda completed_operations, label, processed_files: (
                    progress_values.append(
                        (completed_operations, label, processed_files)
                    )
                )
            )

            worker.run()

            self.assertEqual(ranges, [(6, 2)])
            self.assertEqual(progress_values[-1][0], 6)
            self.assertEqual(progress_values[-1][2], 2)

    def test_live_upload_processes_first_file_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            upload_folder = Path(temp_dir)
            completed_folder = Path(temp_dir) / "completed"
            (upload_folder / "alpha.jpg").write_text("x")
            (upload_folder / "beta.jpg").write_text("x")

            client = SuccessfulUploadClient()
            worker = UploadWorker(
                UploadJobConfig(
                    server_url="https://karakeep.example.test",
                    api_key="token",
                    upload_folder=upload_folder,
                    completed_folder=completed_folder,
                    error_folder=Path(temp_dir) / "errors",
                    dont_move_completed=False,
                    dont_move_failed=False,
                    dont_preserve_move_structure=False,
                    rename_move_conflicts=False,
                    import_to_root=False,
                    root_list="IMPORT SORTING",
                    default_tags=("!!-TAGGING-!!", "figure"),
                    dry_run=False,
                ),
                client=client,
            )

            finished_values: list[tuple[bool, int, int, int]] = []
            worker.finished.connect(
                lambda stopped, succeeded, failed, not_processed: (
                    finished_values.append(
                        (stopped, succeeded, failed, not_processed)
                    )
                )
            )

            worker.run()

            self.assertEqual(finished_values, [(False, 1, 0, 1)])
            self.assertEqual(client.created_lists, [("IMPORT SORTING", None)])
            self.assertEqual(
                client.uploaded_files,
                [(upload_folder / "alpha.jpg").resolve()],
            )
            self.assertEqual(
                client.created_bookmarks,
                [("asset-1", "alpha.jpg")],
            )
            self.assertEqual(
                client.assigned_lists,
                [("list-1", "bookmark-1")],
            )
            self.assertEqual(
                client.attached_tags,
                [("bookmark-1", ("!!-TAGGING-!!", "figure"))],
            )
            self.assertFalse((upload_folder / "alpha.jpg").exists())
            self.assertTrue((completed_folder / "alpha.jpg").exists())
            self.assertTrue((upload_folder / "beta.jpg").exists())

    def test_live_upload_preserves_relative_move_structure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            upload_folder = root / "upload"
            completed_folder = root / "completed"
            nested_folder = upload_folder / "fart" / "burp"
            nested_folder.mkdir(parents=True)
            (nested_folder / "poop.jpg").write_text("x")

            worker = UploadWorker(
                UploadJobConfig(
                    server_url="https://karakeep.example.test",
                    api_key="token",
                    upload_folder=upload_folder,
                    completed_folder=completed_folder,
                    error_folder=root / "errors",
                    dont_move_completed=False,
                    dont_move_failed=False,
                    dont_preserve_move_structure=False,
                    rename_move_conflicts=False,
                    import_to_root=False,
                    root_list="IMPORT SORTING",
                    default_tags=(),
                    dry_run=False,
                ),
                client=SuccessfulUploadClient(),
            )

            worker.run()

            self.assertFalse((nested_folder / "poop.jpg").exists())
            self.assertTrue(
                (completed_folder / "fart" / "burp" / "poop.jpg").exists()
            )

    def test_live_upload_can_flatten_move_structure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            upload_folder = root / "upload"
            completed_folder = root / "completed"
            nested_folder = upload_folder / "fart" / "burp"
            nested_folder.mkdir(parents=True)
            (nested_folder / "poop.jpg").write_text("x")

            worker = UploadWorker(
                UploadJobConfig(
                    server_url="https://karakeep.example.test",
                    api_key="token",
                    upload_folder=upload_folder,
                    completed_folder=completed_folder,
                    error_folder=root / "errors",
                    dont_move_completed=False,
                    dont_move_failed=False,
                    dont_preserve_move_structure=True,
                    rename_move_conflicts=False,
                    import_to_root=False,
                    root_list="IMPORT SORTING",
                    default_tags=(),
                    dry_run=False,
                ),
                client=SuccessfulUploadClient(),
            )

            worker.run()

            self.assertFalse((nested_folder / "poop.jpg").exists())
            self.assertTrue((completed_folder / "poop.jpg").exists())
            self.assertFalse((completed_folder / "fart").exists())

    def test_live_upload_can_auto_rename_move_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            upload_folder = root / "upload"
            completed_folder = root / "completed"
            upload_folder.mkdir()
            completed_folder.mkdir()
            (upload_folder / "poop.jpg").write_text("new")
            (completed_folder / "poop.jpg").write_text("existing")

            worker = UploadWorker(
                UploadJobConfig(
                    server_url="https://karakeep.example.test",
                    api_key="token",
                    upload_folder=upload_folder,
                    completed_folder=completed_folder,
                    error_folder=root / "errors",
                    dont_move_completed=False,
                    dont_move_failed=False,
                    dont_preserve_move_structure=False,
                    rename_move_conflicts=True,
                    import_to_root=False,
                    root_list="IMPORT SORTING",
                    default_tags=(),
                    dry_run=False,
                ),
                client=SuccessfulUploadClient(),
            )

            worker.run()

            self.assertFalse((upload_folder / "poop.jpg").exists())
            self.assertEqual((completed_folder / "poop.jpg").read_text(), "existing")
            self.assertEqual((completed_folder / "poop (1).jpg").read_text(), "new")

    def test_live_upload_moves_failed_file_to_error_folder(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            upload_folder = root / "upload"
            error_folder = root / "errors"
            nested_folder = upload_folder / "fart" / "burp"
            nested_folder.mkdir(parents=True)
            (nested_folder / "poop.jpg").write_text("x")

            worker = UploadWorker(
                UploadJobConfig(
                    server_url="https://karakeep.example.test",
                    api_key="token",
                    upload_folder=upload_folder,
                    completed_folder=root / "completed",
                    error_folder=error_folder,
                    dont_move_completed=False,
                    dont_move_failed=False,
                    dont_preserve_move_structure=False,
                    rename_move_conflicts=False,
                    import_to_root=False,
                    root_list="IMPORT SORTING",
                    default_tags=(),
                    dry_run=False,
                ),
                client=FailingUploadClient(),
            )

            finished_values: list[tuple[bool, int, int, int]] = []
            worker.finished.connect(
                lambda stopped, succeeded, failed, not_processed: (
                    finished_values.append(
                        (stopped, succeeded, failed, not_processed)
                    )
                )
            )

            worker.run()

            self.assertEqual(finished_values, [(True, 0, 1, 0)])
            self.assertFalse((nested_folder / "poop.jpg").exists())
            self.assertTrue(
                (error_folder / "fart" / "burp" / "poop.jpg").exists()
            )


if __name__ == "__main__":
    unittest.main()

