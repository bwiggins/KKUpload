from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import httpx
from list_planner import ListRecord
from preferences import ImageResizePreferences
from PySide6.QtGui import QImage
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
        self.uploaded_file_sizes: list[int] = []
        self.created_bookmarks: list[tuple[str, str]] = []
        self.assigned_lists: list[tuple[str, str]] = []
        self.attached_tags: list[tuple[str, tuple[str, ...]]] = []
        self.bookmark_counter = 0
        self.asset_counter = 0

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
        self.uploaded_file_sizes.append(file_path.stat().st_size)
        self.asset_counter += 1
        return {"assetId": f"asset-{self.asset_counter}"}

    def create_asset_bookmark(
        self,
        *,
        asset_id: str,
        file_name: str,
    ) -> dict:
        self.bookmark_counter += 1
        bookmark_id = f"bookmark-{self.bookmark_counter}"
        self.created_bookmarks.append((asset_id, file_name))
        return {"id": bookmark_id}

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
            "content": {"assetId": bookmark_id.replace("bookmark", "asset")},
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


class TooLargeOnceUploadClient(SuccessfulUploadClient):
    def upload_asset(self, file_path: Path) -> dict:
        self.uploaded_files.append(file_path)
        self.uploaded_file_sizes.append(file_path.stat().st_size)

        if len(self.uploaded_files) == 1:
            request = httpx.Request("POST", "https://karakeep.example.test")
            response = httpx.Response(
                413,
                json={"error": "Asset is too big"},
                request=request,
            )
            raise httpx.HTTPStatusError(
                "Asset is too big",
                request=request,
                response=response,
            )

        self.asset_counter += 1
        return {"assetId": f"asset-{self.asset_counter}"}


class UnsupportedAssetTypeClient(SuccessfulUploadClient):
    def upload_asset(self, file_path: Path) -> dict:
        request = httpx.Request("POST", "https://karakeep.example.test")
        response = httpx.Response(
            400,
            json={"error": "Unsupported asset type"},
            request=request,
        )
        raise httpx.HTTPStatusError(
            "Unsupported asset type",
            request=request,
            response=response,
        )


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
                    move_conflict_mode="ask",
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
                    move_conflict_mode="ask",
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

            self.assertEqual(ranges, [(7, 2)])
            self.assertEqual(progress_values[-1][0], 7)
            self.assertEqual(progress_values[-1][2], 2)

    def test_dry_run_counts_move_conflicts_as_resolved(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            upload_folder = root / "upload"
            completed_folder = root / "completed"
            upload_folder.mkdir()
            completed_folder.mkdir()
            (upload_folder / "image.jpg").write_text("new")
            (completed_folder / "image.jpg").write_text("existing")

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
                    move_conflict_mode="ask",
                    import_to_root=False,
                    root_list="IMPORT SORTING",
                    default_tags=(),
                    dry_run=True,
                ),
                client=EmptyDryRunClient(),
            )

            resolved_conflicts: list[int] = []
            worker.conflict_resolved.connect(resolved_conflicts.append)

            worker.run()

            self.assertEqual(sum(resolved_conflicts), 1)
            self.assertTrue((upload_folder / "image.jpg").exists())
            self.assertEqual((completed_folder / "image.jpg").read_text(), "existing")

    def test_live_upload_processes_all_files(self) -> None:
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
                    move_conflict_mode="ask",
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

            self.assertEqual(finished_values, [(False, 2, 0, 0)])
            self.assertEqual(
                client.created_lists,
                [
                    ("IMPORT SORTING", None),
                    (upload_folder.name, "list-1"),
                ],
            )
            self.assertEqual(
                client.uploaded_files,
                [
                    (upload_folder / "alpha.jpg").resolve(),
                    (upload_folder / "beta.jpg").resolve(),
                ],
            )
            self.assertEqual(
                client.created_bookmarks,
                [
                    ("asset-1", "alpha.jpg"),
                    ("asset-2", "beta.jpg"),
                ],
            )
            self.assertEqual(
                client.assigned_lists,
                [
                    ("list-2", "bookmark-1"),
                    ("list-2", "bookmark-2"),
                ],
            )
            self.assertEqual(
                client.attached_tags,
                [
                    ("bookmark-1", ("!!-TAGGING-!!", "figure")),
                    ("bookmark-2", ("!!-TAGGING-!!", "figure")),
                ],
            )
            self.assertFalse((upload_folder / "alpha.jpg").exists())
            self.assertTrue((completed_folder / "alpha.jpg").exists())
            self.assertFalse((upload_folder / "beta.jpg").exists())
            self.assertTrue((completed_folder / "beta.jpg").exists())

    def test_worker_can_omit_top_folder_list(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            upload_folder = root / "upload"
            upload_folder.mkdir()
            (upload_folder / "alpha.jpg").write_text("x")

            client = SuccessfulUploadClient()
            worker = UploadWorker(
                UploadJobConfig(
                    server_url="https://karakeep.example.test",
                    api_key="token",
                    upload_folder=upload_folder,
                    completed_folder=root / "completed",
                    error_folder=root / "errors",
                    dont_move_completed=False,
                    dont_move_failed=False,
                    dont_preserve_move_structure=False,
                    move_conflict_mode="ask",
                    import_to_root=False,
                    root_list="IMPORT SORTING",
                    default_tags=(),
                    dry_run=False,
                    omit_top_folder_list=True,
                ),
                client=client,
            )

            worker.run()

            self.assertEqual(client.created_lists, [("IMPORT SORTING", None)])
            self.assertEqual(client.assigned_lists, [("list-1", "bookmark-1")])

    def test_live_upload_resizes_oversized_image_copy(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            upload_folder = root / "upload"
            completed_folder = root / "completed"
            upload_folder.mkdir()
            image_path = upload_folder / "large.bmp"

            image = QImage(300, 300, QImage.Format.Format_RGB32)
            image.fill(0xFF336699)
            self.assertTrue(image.save(str(image_path)))

            client = SuccessfulUploadClient()
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
                    move_conflict_mode="ask",
                    import_to_root=False,
                    root_list="IMPORT SORTING",
                    default_tags=(),
                    dry_run=False,
                    image_resize_preferences=ImageResizePreferences(
                        maximum_allowed_image_size_mb=0.1,
                        desired_resize_goal_mb=0.05,
                        maximum_attempts=3,
                        acceptable_distance_percent=25,
                    ),
                ),
                client=client,
            )

            worker.run()

            self.assertEqual(len(client.uploaded_files), 1)
            self.assertNotEqual(client.uploaded_files[0], image_path.resolve())
            self.assertLess(
                client.uploaded_file_sizes[0],
                (completed_folder / "large.bmp").stat().st_size,
            )
            self.assertFalse(image_path.exists())
            self.assertTrue((completed_folder / "large.bmp").exists())

    def test_live_upload_retries_with_resize_after_too_large_response(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            upload_folder = root / "upload"
            completed_folder = root / "completed"
            upload_folder.mkdir()
            image_path = upload_folder / "large.bmp"

            image = QImage(300, 300, QImage.Format.Format_RGB32)
            image.fill(0xFF663399)
            self.assertTrue(image.save(str(image_path)))

            client = TooLargeOnceUploadClient()
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
                    move_conflict_mode="ask",
                    import_to_root=False,
                    root_list="IMPORT SORTING",
                    default_tags=(),
                    dry_run=False,
                    image_resize_preferences=ImageResizePreferences(
                        maximum_allowed_image_size_mb=10,
                        desired_resize_goal_mb=0.05,
                        maximum_attempts=4,
                        acceptable_distance_percent=25,
                    ),
                    resize_images_if_needed=True,
                ),
                client=client,
            )

            worker.run()

            self.assertEqual(len(client.uploaded_files), 2)
            self.assertEqual(client.uploaded_files[0], image_path.resolve())
            self.assertNotEqual(client.uploaded_files[1], image_path.resolve())
            self.assertLess(
                client.uploaded_file_sizes[1],
                client.uploaded_file_sizes[0],
            )
            self.assertFalse(image_path.exists())
            self.assertTrue((completed_folder / "large.bmp").exists())

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
                    move_conflict_mode="ask",
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
                    move_conflict_mode="ask",
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
                    move_conflict_mode="rename",
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

    def test_live_upload_can_auto_overwrite_move_conflict(self) -> None:
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
                    move_conflict_mode="overwrite",
                    import_to_root=False,
                    root_list="IMPORT SORTING",
                    default_tags=(),
                    dry_run=False,
                ),
                client=SuccessfulUploadClient(),
            )

            worker.run()

            self.assertFalse((upload_folder / "poop.jpg").exists())
            self.assertEqual((completed_folder / "poop.jpg").read_text(), "new")
            self.assertFalse((completed_folder / "poop (1).jpg").exists())

    def test_live_upload_can_stop_on_move_conflict(self) -> None:
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
                    move_conflict_mode="stop",
                    import_to_root=False,
                    root_list="IMPORT SORTING",
                    default_tags=(),
                    dry_run=False,
                ),
                client=SuccessfulUploadClient(),
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
            self.assertTrue((upload_folder / "poop.jpg").exists())
            self.assertEqual((completed_folder / "poop.jpg").read_text(), "existing")

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
                    move_conflict_mode="ask",
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

            self.assertEqual(finished_values, [(False, 0, 1, 0)])
            self.assertFalse((nested_folder / "poop.jpg").exists())
            self.assertTrue(
                (error_folder / "fart" / "burp" / "poop.jpg").exists()
            )

    def test_live_upload_moves_unsupported_file_to_unsupported_folder(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            upload_folder = root / "upload"
            unsupported_folder = root / "unsupported"
            nested_folder = upload_folder / "fart" / "burp"
            nested_folder.mkdir(parents=True)
            unsupported_file = nested_folder / "poop.txt"
            unsupported_file.write_text("x")

            worker = UploadWorker(
                UploadJobConfig(
                    server_url="https://karakeep.example.test",
                    api_key="token",
                    upload_folder=upload_folder,
                    completed_folder=root / "completed",
                    error_folder=root / "errors",
                    dont_move_completed=False,
                    dont_move_failed=False,
                    dont_preserve_move_structure=False,
                    move_conflict_mode="ask",
                    import_to_root=False,
                    root_list="IMPORT SORTING",
                    default_tags=(),
                    dry_run=False,
                    unsupported_folder=unsupported_folder,
                    dont_move_unsupported=False,
                ),
                client=EmptyDryRunClient(),
            )

            worker.run()

            self.assertFalse(unsupported_file.exists())
            self.assertTrue(
                (unsupported_folder / "fart" / "burp" / "poop.txt").exists()
            )

    def test_dry_run_reports_unsupported_move_without_moving(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            upload_folder = root / "upload"
            unsupported_folder = root / "unsupported"
            upload_folder.mkdir()
            unsupported_file = upload_folder / "poop.txt"
            unsupported_file.write_text("x")

            worker = UploadWorker(
                UploadJobConfig(
                    server_url="https://karakeep.example.test",
                    api_key="token",
                    upload_folder=upload_folder,
                    completed_folder=root / "completed",
                    error_folder=root / "errors",
                    dont_move_completed=False,
                    dont_move_failed=False,
                    dont_preserve_move_structure=False,
                    move_conflict_mode="ask",
                    import_to_root=False,
                    root_list="IMPORT SORTING",
                    default_tags=(),
                    dry_run=True,
                    unsupported_folder=unsupported_folder,
                    dont_move_unsupported=False,
                ),
                client=EmptyDryRunClient(),
            )

            log_messages: list[str] = []
            worker.log.connect(
                lambda message, level, message_color: log_messages.append(message)
            )

            worker.run()

            self.assertTrue(unsupported_file.exists())
            self.assertFalse((unsupported_folder / "poop.txt").exists())
            self.assertIn(
                f"Dry-run: would move unsupported file to: {unsupported_folder / 'poop.txt'}",
                "\n".join(log_messages),
            )

    def test_unsupported_asset_type_investigates_failed_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            upload_folder = root / "upload"
            error_folder = root / "errors"
            upload_folder.mkdir()
            html_file = upload_folder / "fake.jpg"
            html_file.write_text(
                """
                <!DOCTYPE html>
                <html>
                <head>
                    <meta property="og:image" content="https://example.test/fake.jpg">
                </head>
                <body>not really an image</body>
                </html>
                """,
                encoding="utf-8",
            )

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
                    move_conflict_mode="ask",
                    import_to_root=False,
                    root_list="IMPORT SORTING",
                    default_tags=(),
                    dry_run=False,
                ),
                client=UnsupportedAssetTypeClient(),
            )

            log_messages: list[str] = []
            worker.log.connect(
                lambda message, level, message_color: log_messages.append(message)
            )

            worker.run()

            joined_logs = "\n".join(log_messages)
            self.assertIn("Investigating failed file:", joined_logs)
            self.assertIn("Detected failed file content: HTML document.", joined_logs)
            self.assertIn(
                "File extension looks like media, but the file content appears "
                "to be HTML.",
                joined_logs,
            )
            self.assertIn(
                "Possible image URL found: https://example.test/fake.jpg",
                joined_logs,
            )
            self.assertTrue((error_folder / "fake.jpg").exists())


if __name__ == "__main__":
    unittest.main()


