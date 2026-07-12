from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scanner import scan_upload_folder, validate_separate_folder_tree


class ScannerTests(unittest.TestCase):
    def test_scan_recursively_finds_supported_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "root.jpg").write_text("x")
            (root / "ignore.txt").write_text("x")
            (root / "Models" / "Rose").mkdir(parents=True)
            (root / "Models" / "Rose" / "pose.PNG").write_text("x")

            result = scan_upload_folder(root)

            relative_paths = {
                str(file.relative_path).replace("\\", "/")
                for file in result.supported_files
            }
            self.assertEqual(relative_paths, {"root.jpg", "Models/Rose/pose.PNG"})
            self.assertEqual(result.supported_files[0].folder_parts, ())
            self.assertEqual(
                result.supported_files[1].folder_parts,
                ("Models", "Rose"),
            )
            unsupported_paths = {
                str(path.relative_to(root)).replace("\\", "/")
                for path in result.unsupported_files
            }
            self.assertEqual(unsupported_paths, {"ignore.txt"})

    def test_rejects_any_nested_folder_relationship(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            upload = root / "Upload"
            completed = upload / "Completed"
            error = root / "Errors"
            upload.mkdir()
            completed.mkdir()
            error.mkdir()

            with self.assertRaises(ValueError):
                validate_separate_folder_tree(
                    {
                        "Upload": upload,
                        "Completed": completed,
                        "Error": error,
                    }
                )

    def test_can_allow_completed_and_error_to_match(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            upload = root / "Upload"
            output = root / "Output"
            upload.mkdir()
            output.mkdir()

            validate_separate_folder_tree(
                {
                    "Upload": upload,
                    "Completed": output,
                    "Error": output,
                },
                allowed_equal_pairs={frozenset(("Completed", "Error"))},
            )


if __name__ == "__main__":
    unittest.main()
