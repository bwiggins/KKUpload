from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
import shutil
from typing import Any

from app_paths import default_log_dir, user_data_dir


PREFERENCES_FILE_NAME = "preferences.json"
BYTES_PER_MB = 1024 * 1024


@dataclass(frozen=True)
class ImageResizePreferences:
    maximum_allowed_image_size_mb: float = 49.0
    desired_resize_goal_mb: float = 25.0
    maximum_attempts: int = 4
    acceptable_distance_percent: float = 10.0
    fail_if_not_within_goal: bool = False

    @property
    def maximum_allowed_bytes(self) -> int:
        return int(self.maximum_allowed_image_size_mb * BYTES_PER_MB)

    @property
    def desired_goal_bytes(self) -> int:
        return int(self.desired_resize_goal_mb * BYTES_PER_MB)

    @property
    def minimum_acceptable_bytes(self) -> int:
        distance = self.acceptable_distance_percent / 100
        return int(self.desired_goal_bytes * (1 - distance))


@dataclass(frozen=True)
class AppPreferences:
    image_resize: ImageResizePreferences
    view_mode: str = "auto"
    log_folder: str = field(default_factory=lambda: str(default_log_dir()))


@dataclass(frozen=True)
class PreferenceLoadResult:
    preferences: AppPreferences
    messages: tuple[tuple[str, str], ...]


def default_preferences() -> AppPreferences:
    return AppPreferences(
        image_resize=ImageResizePreferences(),
        log_folder=str(default_log_dir()),
    )


def preferences_path(app_path: Path) -> Path:
    del app_path
    return user_data_dir() / PREFERENCES_FILE_NAME


def save_preferences(app_path: Path, preferences: AppPreferences) -> Path:
    path = preferences_path(app_path)
    _write_preferences(path, preferences)
    return path


def load_preferences(app_path: Path) -> PreferenceLoadResult:
    path = preferences_path(app_path)
    defaults = default_preferences()
    messages: list[tuple[str, str]] = []
    rewrite_needed = False
    migrated_legacy_preferences = False

    legacy_path = app_path.with_name(PREFERENCES_FILE_NAME)
    if not path.exists() and legacy_path.exists() and legacy_path != path:
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(legacy_path, path)
        try:
            legacy_path.unlink()
            migration_action = "Moved"
        except OSError:
            migration_action = "Copied"
        messages.append(
            (
                f"{migration_action} legacy preferences to: {path}",
                "INFO",
            )
        )
        migrated_legacy_preferences = True

    if not path.exists():
        _write_preferences(path, defaults)
        messages.append(
            (
                f"Preferences JSON missing. Created defaults and loaded successfully: {path}",
                "INFO",
            )
        )
        return PreferenceLoadResult(defaults, tuple(messages))

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _write_preferences(path, defaults)
        messages.append(
            (
                "Preferences file could not be read. "
                f"Rewrote defaults and loaded successfully: {path} ({exc})",
                "WARNING",
            )
        )
        return PreferenceLoadResult(defaults, tuple(messages))

    if not isinstance(raw, dict):
        raw = {}
        rewrite_needed = True
        messages.append(
            (
                "Preferences file root was not an object. "
                "Using defaults where needed.",
                "WARNING",
            )
        )

    image_resize_raw = raw.get("image_resize", {})
    if not isinstance(image_resize_raw, dict):
        image_resize_raw = {}
        rewrite_needed = True
        messages.append(
            (
                "Preferences image_resize section was invalid. "
                "Using resize defaults.",
                "WARNING",
            )
        )

    image_resize, validation_messages = _load_image_resize_preferences(
        image_resize_raw,
        defaults.image_resize,
    )
    messages.extend(validation_messages)
    view_mode = _view_mode_value(raw, "view_mode", defaults.view_mode, messages)
    log_folder = _log_folder_value(raw, "log_folder", defaults.log_folder, messages)
    if migrated_legacy_preferences and log_folder == "logs":
        log_folder = defaults.log_folder
        rewrite_needed = True
    preferences = AppPreferences(
        image_resize=image_resize,
        view_mode=view_mode,
        log_folder=log_folder,
    )

    if (
        validation_messages
        or "view_mode" not in raw
        or view_mode != raw.get("view_mode", defaults.view_mode)
        or "log_folder" not in raw
        or log_folder != raw.get("log_folder", defaults.log_folder)
    ):
        rewrite_needed = True

    if rewrite_needed:
        _write_preferences(path, preferences)
        messages.append(
            (
                f"Rewrote preferences file with valid values: {path}",
                "WARNING",
            )
        )

    messages.append((f"Preferences JSON loaded successfully: {path}", "INFO"))
    return PreferenceLoadResult(preferences, tuple(messages))


def _load_image_resize_preferences(
    raw: dict[str, Any],
    defaults: ImageResizePreferences,
) -> tuple[ImageResizePreferences, list[tuple[str, str]]]:
    messages: list[tuple[str, str]] = []

    maximum_allowed = _positive_float(
        raw,
        "maximum_allowed_image_size_mb",
        defaults.maximum_allowed_image_size_mb,
        messages,
    )
    desired_goal = _positive_float(
        raw,
        "desired_resize_goal_mb",
        defaults.desired_resize_goal_mb,
        messages,
    )
    maximum_attempts = _positive_int(
        raw,
        "maximum_attempts",
        defaults.maximum_attempts,
        messages,
    )
    acceptable_distance = _positive_float(
        raw,
        "acceptable_distance_percent",
        defaults.acceptable_distance_percent,
        messages,
    )
    fail_if_not_within_goal = _bool_value(
        raw,
        "fail_if_not_within_goal",
        defaults.fail_if_not_within_goal,
        messages,
    )

    if desired_goal >= maximum_allowed:
        messages.append(
            (
                "Preference desired_resize_goal_mb must be lower than "
                "maximum_allowed_image_size_mb. Using default resize goal.",
                "WARNING",
            )
        )
        desired_goal = defaults.desired_resize_goal_mb

    if acceptable_distance >= 100:
        messages.append(
            (
                "Preference acceptable_distance_percent must be below 100. "
                "Using default acceptable distance.",
                "WARNING",
            )
        )
        acceptable_distance = defaults.acceptable_distance_percent

    return (
        ImageResizePreferences(
            maximum_allowed_image_size_mb=maximum_allowed,
            desired_resize_goal_mb=desired_goal,
            maximum_attempts=maximum_attempts,
            acceptable_distance_percent=acceptable_distance,
            fail_if_not_within_goal=fail_if_not_within_goal,
        ),
        messages,
    )


def _positive_float(
    raw: dict[str, Any],
    key: str,
    default: float,
    messages: list[tuple[str, str]],
) -> float:
    value = raw.get(key, default)

    try:
        number = float(value)
    except (TypeError, ValueError):
        number = 0

    if number <= 0:
        messages.append(
            (
                f"Preference {key} was invalid. Using default {default}.",
                "WARNING",
            )
        )
        return default

    return number


def _positive_int(
    raw: dict[str, Any],
    key: str,
    default: int,
    messages: list[tuple[str, str]],
) -> int:
    value = raw.get(key, default)

    try:
        number = int(value)
    except (TypeError, ValueError):
        number = 0

    if number <= 0:
        messages.append(
            (
                f"Preference {key} was invalid. Using default {default}.",
                "WARNING",
            )
        )
        return default

    return number


def _bool_value(
    raw: dict[str, Any],
    key: str,
    default: bool,
    messages: list[tuple[str, str]],
) -> bool:
    value = raw.get(key, default)

    if isinstance(value, bool):
        return value

    messages.append(
        (
            f"Preference {key} was invalid. Using default {default}.",
            "WARNING",
        )
    )
    return default


def _view_mode_value(
    raw: dict[str, Any],
    key: str,
    default: str,
    messages: list[tuple[str, str]],
) -> str:
    value = raw.get(key, default)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"auto", "light", "dark"}:
            return normalized

    messages.append(
        (
            f"Preference {key} was invalid. Using default {default}.",
            "WARNING",
        )
    )
    return default


def _log_folder_value(
    raw: dict[str, Any],
    key: str,
    default: str,
    messages: list[tuple[str, str]],
) -> str:
    value = raw.get(key, default)
    if isinstance(value, str):
        normalized = value.strip()
        if normalized:
            return normalized

    messages.append(
        (
            f"Preference {key} was invalid. Using default {default}.",
            "WARNING",
        )
    )
    return default


def _write_preferences(path: Path, preferences: AppPreferences) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(_preferences_to_json(preferences), indent=2) + "\n",
        encoding="utf-8",
    )


def _preferences_to_json(preferences: AppPreferences) -> dict[str, Any]:
    image_resize = preferences.image_resize
    return {
        "view_mode": preferences.view_mode,
        "log_folder": preferences.log_folder,
        "image_resize": {
            "maximum_allowed_image_size_mb": (
                image_resize.maximum_allowed_image_size_mb
            ),
            "desired_resize_goal_mb": image_resize.desired_resize_goal_mb,
            "maximum_attempts": image_resize.maximum_attempts,
            "acceptable_distance_percent": (
                image_resize.acceptable_distance_percent
            ),
            "fail_if_not_within_goal": image_resize.fail_if_not_within_goal,
        }
    }
