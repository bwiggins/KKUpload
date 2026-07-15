from __future__ import annotations

import unittest
from pathlib import Path

from list_planner import (
    ListIndex,
    ListRecord,
    build_upload_plan,
    format_list_path,
    parse_list_path,
)
from scanner import ScannedFile


class ListPlannerTests(unittest.TestCase):
    def test_import_to_list_places_folders_under_configured_root(self) -> None:
        files = (
            ScannedFile(
                path=Path("Upload/root.jpg"),
                relative_path=Path("root.jpg"),
                folder_parts=(),
            ),
            ScannedFile(
                path=Path("Upload/Models/Rose/pose.jpg"),
                relative_path=Path("Models/Rose/pose.jpg"),
                folder_parts=("Models", "Rose"),
            ),
        )

        plan = build_upload_plan(
            files,
            import_to_root=False,
            root_list="IMPORT SORTING",
            top_folder_name="Upload",
        )

        self.assertEqual(
            [item.path for item in plan.required_lists],
            [
                ("IMPORT SORTING",),
                ("IMPORT SORTING", "Upload"),
                ("IMPORT SORTING", "Upload", "Models"),
                ("IMPORT SORTING", "Upload", "Models", "Rose"),
            ],
        )
        self.assertEqual(
            plan.planned_files[0].destination_path,
            ("IMPORT SORTING", "Upload"),
        )
        self.assertEqual(
            plan.planned_files[1].destination_path,
            ("IMPORT SORTING", "Upload", "Models", "Rose"),
        )

    def test_can_omit_top_folder_list(self) -> None:
        files = (
            ScannedFile(
                path=Path("Upload/root.jpg"),
                relative_path=Path("root.jpg"),
                folder_parts=(),
            ),
            ScannedFile(
                path=Path("Upload/Models/Rose/pose.jpg"),
                relative_path=Path("Models/Rose/pose.jpg"),
                folder_parts=("Models", "Rose"),
            ),
        )

        plan = build_upload_plan(
            files,
            import_to_root=False,
            root_list="IMPORT SORTING",
            top_folder_name="Upload",
            omit_top_folder_list=True,
        )

        self.assertEqual(
            [item.path for item in plan.required_lists],
            [
                ("IMPORT SORTING",),
                ("IMPORT SORTING", "Models"),
                ("IMPORT SORTING", "Models", "Rose"),
            ],
        )
        self.assertEqual(
            plan.planned_files[0].destination_path,
            ("IMPORT SORTING",),
        )
        self.assertEqual(
            plan.planned_files[1].destination_path,
            ("IMPORT SORTING", "Models", "Rose"),
        )

    def test_import_to_list_can_be_nested_path(self) -> None:
        files = (
            ScannedFile(
                path=Path("Upload/root.jpg"),
                relative_path=Path("root.jpg"),
                folder_parts=(),
            ),
        )

        plan = build_upload_plan(
            files,
            import_to_root=False,
            root_list="zOther / $ KKUpload",
            top_folder_name="Upload",
        )

        self.assertEqual(
            [item.path for item in plan.required_lists],
            [
                ("zOther",),
                ("zOther", "$ KKUpload"),
                ("zOther", "$ KKUpload", "Upload"),
            ],
        )
        self.assertEqual(
            plan.planned_files[0].destination_path,
            ("zOther", "$ KKUpload", "Upload"),
        )

    def test_parse_list_path_rejects_empty_parts(self) -> None:
        with self.assertRaises(ValueError):
            parse_list_path("zOther//Uploads")

    def test_import_to_root_leaves_root_files_unlisted(self) -> None:
        files = (
            ScannedFile(
                path=Path("Upload/root.jpg"),
                relative_path=Path("root.jpg"),
                folder_parts=(),
            ),
            ScannedFile(
                path=Path("Upload/Models/pose.jpg"),
                relative_path=Path("Models/pose.jpg"),
                folder_parts=("Models",),
            ),
        )

        plan = build_upload_plan(
            files,
            import_to_root=True,
            root_list="ignored",
            top_folder_name="Upload",
        )

        self.assertEqual(
            [item.path for item in plan.required_lists],
            [("Upload",), ("Upload", "Models")],
        )
        self.assertEqual(plan.planned_files[0].destination_path, ("Upload",))
        self.assertEqual(
            plan.planned_files[1].destination_path,
            ("Upload", "Models"),
        )

    def test_list_index_resolves_same_name_under_different_parents(self) -> None:
        records = (
            ListRecord(id="a", name="A", parent_id=None),
            ListRecord(id="b", name="B", parent_id=None),
            ListRecord(id="a-shared", name="Shared", parent_id="a"),
            ListRecord(id="b-shared", name="Shared", parent_id="b"),
        )
        index = ListIndex(records)

        self.assertEqual(index.find_path(("A", "Shared")).id, "a-shared")
        self.assertEqual(index.find_path(("B", "Shared")).id, "b-shared")
        self.assertEqual(format_list_path(("A", "Shared")), "A / Shared")


if __name__ == "__main__":
    unittest.main()
