from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from preferences import (
    AppPreferences,
    ImageResizePreferences,
    load_preferences,
    preferences_path,
    save_preferences,
)


class PreferencesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.local_app_data = tempfile.TemporaryDirectory()
        self.environment = patch.dict(
            os.environ,
            {"LOCALAPPDATA": self.local_app_data.name},
        )
        self.environment.start()

    def tearDown(self) -> None:
        self.environment.stop()
        self.local_app_data.cleanup()

    def _default_log_folder(self) -> str:
        return str(Path(self.local_app_data.name) / "KKUpload" / "logs")

    def test_missing_preferences_file_is_created_with_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            app_path = Path(temp_dir) / "app.py"

            result = load_preferences(app_path)
            path = preferences_path(app_path)

            self.assertTrue(path.exists())
            self.assertEqual(
                result.preferences.image_resize.maximum_allowed_image_size_mb,
                49.0,
            )
            self.assertEqual(result.preferences.view_mode, "auto")
<<<<<<< Updated upstream
=======
            self.assertEqual(
                result.preferences.log_folder,
                self._default_log_folder(),
            )
>>>>>>> Stashed changes
            self.assertIn("Created defaults and loaded successfully", result.messages[0][0])

    def test_invalid_preferences_are_rewritten_with_valid_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            app_path = Path(temp_dir) / "app.py"
            path = preferences_path(app_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(
                    {
                        "image_resize": {
                            "maximum_allowed_image_size_mb": -1,
                            "desired_resize_goal_mb": "nope",
                            "maximum_attempts": 0,
                            "acceptable_distance_percent": 150,
                        }
                    }
                ),
                encoding="utf-8",
            )

            result = load_preferences(app_path)
            rewritten = json.loads(path.read_text(encoding="utf-8"))

            self.assertEqual(
                result.preferences.image_resize.maximum_allowed_image_size_mb,
                49.0,
            )
            self.assertEqual(result.preferences.view_mode, "auto")
<<<<<<< Updated upstream
            self.assertEqual(rewritten["view_mode"], "auto")
=======
            self.assertEqual(
                result.preferences.log_folder,
                self._default_log_folder(),
            )
            self.assertEqual(rewritten["view_mode"], "auto")
            self.assertEqual(rewritten["log_folder"], self._default_log_folder())
>>>>>>> Stashed changes
            self.assertEqual(rewritten["image_resize"]["maximum_attempts"], 4)
            self.assertIs(
                rewritten["image_resize"]["fail_if_not_within_goal"],
                False,
            )
            self.assertTrue(
                any(
                    "Rewrote preferences file" in message
                    for message, _ in result.messages
                )
            )

    def test_save_preferences_writes_json_values(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            app_path = Path(temp_dir) / "app.py"
            preferences = AppPreferences(
                view_mode="dark",
                image_resize=ImageResizePreferences(
                    maximum_allowed_image_size_mb=80.0,
                    desired_resize_goal_mb=32.5,
                    maximum_attempts=6,
                    acceptable_distance_percent=7.5,
                    fail_if_not_within_goal=True,
                )
            )

            path = save_preferences(app_path, preferences)
            saved = json.loads(path.read_text(encoding="utf-8"))

            self.assertEqual(saved["view_mode"], "dark")
            self.assertEqual(
                saved["image_resize"]["maximum_allowed_image_size_mb"],
                80.0,
            )
            self.assertEqual(
                saved["image_resize"]["desired_resize_goal_mb"],
                32.5,
            )
            self.assertEqual(saved["image_resize"]["maximum_attempts"], 6)
            self.assertEqual(
                saved["image_resize"]["acceptable_distance_percent"],
                7.5,
            )
            self.assertIs(
                saved["image_resize"]["fail_if_not_within_goal"],
                True,
            )

    def test_load_preferences_accepts_saved_view_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            app_path = Path(temp_dir) / "app.py"
            path = preferences_path(app_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(
                    {
                        "view_mode": "LIGHT",
                        "image_resize": {
                            "maximum_allowed_image_size_mb": 49.0,
                            "desired_resize_goal_mb": 25.0,
                            "maximum_attempts": 4,
                            "acceptable_distance_percent": 10.0,
                            "fail_if_not_within_goal": False,
                        },
                    }
                ),
                encoding="utf-8",
            )

            result = load_preferences(app_path)
            rewritten = json.loads(path.read_text(encoding="utf-8"))

            self.assertEqual(result.preferences.view_mode, "light")
            self.assertEqual(rewritten["view_mode"], "light")

<<<<<<< Updated upstream
=======
    def test_load_preferences_accepts_saved_log_folder(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            app_path = Path(temp_dir) / "app.py"
            path = preferences_path(app_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(
                    {
                        "view_mode": "auto",
                        "log_folder": "A:/KKU/Logs",
                        "image_resize": {
                            "maximum_allowed_image_size_mb": 49.0,
                            "desired_resize_goal_mb": 25.0,
                            "maximum_attempts": 4,
                            "acceptable_distance_percent": 10.0,
                            "fail_if_not_within_goal": False,
                        },
                    }
                ),
                encoding="utf-8",
            )

            result = load_preferences(app_path)

            self.assertEqual(result.preferences.log_folder, "A:/KKU/Logs")

    def test_legacy_preferences_are_migrated_from_application_folder(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            app_path = Path(temp_dir) / "app.py"
            legacy_path = app_path.with_name("preferences.json")
            legacy_path.write_text(
                json.dumps(
                    {
                        "view_mode": "dark",
                        "log_folder": "logs",
                        "image_resize": {
                            "maximum_allowed_image_size_mb": 49.0,
                            "desired_resize_goal_mb": 25.0,
                            "maximum_attempts": 4,
                            "acceptable_distance_percent": 10.0,
                            "fail_if_not_within_goal": False,
                        },
                    }
                ),
                encoding="utf-8",
            )

            result = load_preferences(app_path)
            migrated_path = preferences_path(app_path)

            self.assertTrue(migrated_path.exists())
            self.assertFalse(legacy_path.exists())
            self.assertEqual(result.preferences.view_mode, "dark")
            self.assertEqual(result.preferences.log_folder, self._default_log_folder())
            self.assertTrue(
                any(
                    "Moved legacy preferences" in message
                    for message, _ in result.messages
                )
            )

>>>>>>> Stashed changes

if __name__ == "__main__":
    unittest.main()
