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
                    import_to_root=False,
                    root_list="IMPORT SORTING",
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


if __name__ == "__main__":
    unittest.main()
