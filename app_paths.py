from __future__ import annotations

import os
from pathlib import Path


APP_DATA_FOLDER_NAME = "KKUpload"
LOG_FOLDER_NAME = "logs"


def user_data_dir() -> Path:
    """Return the private per-user directory for KKUpload runtime data."""
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / APP_DATA_FOLDER_NAME

    return Path.home() / "AppData" / "Local" / APP_DATA_FOLDER_NAME


def default_log_dir() -> Path:
    return user_data_dir() / LOG_FOLDER_NAME
