from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from preferences import load_preferences, preferences_path


class PreferencesTests(unittest.TestCase):
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
            self.assertIn("Created preferences file", result.messages[0][0])

    def test_invalid_preferences_are_rewritten_with_valid_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            app_path = Path(temp_dir) / "app.py"
            path = preferences_path(app_path)
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
            self.assertEqual(rewritten["image_resize"]["maximum_attempts"], 3)
            self.assertTrue(
                any(
                    "Rewrote preferences file" in message
                    for message, _ in result.messages
                )
            )


if __name__ == "__main__":
    unittest.main()
