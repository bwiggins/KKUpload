from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any


PREFERENCES_FILE_NAME = "preferences.json"
BYTES_PER_MB = 1024 * 1024


@dataclass(frozen=True)
class ImageResizePreferences:
    maximum_allowed_image_size_mb: float = 49.0
    desired_resize_goal_mb: float = 25.0
    maximum_attempts: int = 3
    acceptable_distance_percent: float = 10.0

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


@dataclass(frozen=True)
class PreferenceLoadResult:
    preferences: AppPreferences
    messages: tuple[tuple[str, str], ...]


def default_preferences() -> AppPreferences:
    return AppPreferences(image_resize=ImageResizePreferences())


def preferences_path(app_path: Path) -> Path:
    return app_path.with_name(PREFERENCES_FILE_NAME)


def load_preferences(app_path: Path) -> PreferenceLoadResult:
    path = preferences_path(app_path)
    defaults = default_preferences()
    messages: list[tuple[str, str]] = []
    rewrite_needed = False

    if not path.exists():
        _write_preferences(path, defaults)
        messages.append(
            (
                f"Created preferences file with defaults: {path}",
                "INFO",
            )
        )
        messages.append((_format_preferences(defaults), "INFO"))
        return PreferenceLoadResult(defaults, tuple(messages))

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _write_preferences(path, defaults)
        messages.append(
            (
                "Preferences file could not be read. "
                f"Rewrote defaults: {path} ({exc})",
                "WARNING",
            )
        )
        messages.append((_format_preferences(defaults), "INFO"))
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
    preferences = AppPreferences(image_resize=image_resize)

    if validation_messages:
        rewrite_needed = True

    if rewrite_needed:
        _write_preferences(path, preferences)
        messages.append(
            (
                f"Rewrote preferences file with valid values: {path}",
                "WARNING",
            )
        )

    messages.append((_format_preferences(preferences), "INFO"))
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


def _write_preferences(path: Path, preferences: AppPreferences) -> None:
    path.write_text(
        json.dumps(_preferences_to_json(preferences), indent=2) + "\n",
        encoding="utf-8",
    )


def _preferences_to_json(preferences: AppPreferences) -> dict[str, Any]:
    image_resize = preferences.image_resize
    return {
        "image_resize": {
            "maximum_allowed_image_size_mb": (
                image_resize.maximum_allowed_image_size_mb
            ),
            "desired_resize_goal_mb": image_resize.desired_resize_goal_mb,
            "maximum_attempts": image_resize.maximum_attempts,
            "acceptable_distance_percent": (
                image_resize.acceptable_distance_percent
            ),
        }
    }


def _format_preferences(preferences: AppPreferences) -> str:
    image_resize = preferences.image_resize
    return (
        "Image resize preferences: "
        f"max allowed {image_resize.maximum_allowed_image_size_mb:g} MB; "
        f"goal {image_resize.desired_resize_goal_mb:g} MB; "
        f"attempts {image_resize.maximum_attempts}; "
        f"acceptable distance {image_resize.acceptable_distance_percent:g}%."
    )
