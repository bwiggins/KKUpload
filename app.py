from __future__ import annotations

import sys
import time
import re
import webbrowser
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final

import keyring
from PySide6.QtCore import QEvent, QPoint, QSettings, QSize, QThread, QTimer, Qt, Signal
from PySide6.QtGui import (
    QAction,
    QColor,
    QCloseEvent,
    QFont,
    QPainter,
    QPalette,
    QPixmap,
    QTextCharFormat,
    QTextCursor,
    QTextFormat,
)
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from karakeep_client import KarakeepClient
from list_planner import parse_list_path
from preferences import (
    AppPreferences,
    ImageResizePreferences,
    load_preferences,
    save_preferences,
)
from scanner import validate_separate_folder_tree
from timing_stats import default_timing_stats_path
from duplicate_checker import DuplicateCheckConfig, DuplicateCheckWorker
from upload_worker import MoveConflictRequest, UploadJobConfig, UploadWorker


APP_NAME: Final[str] = "KKUpload"
ORGANIZATION_NAME: Final[str] = "Brad"
DEFAULT_ROOT_LIST: Final[str] = "$ KKUpload"
DEFAULT_TAGS: Final[str] = "!!-TAGGING-!!"
MAX_RECENT_VALUES: Final[int] = 10
KEYRING_SERVICE: Final[str] = "KKUpload"
KEYRING_USERNAME: Final[str] = "karakeep_api_key"
DEFAULT_APP_PALETTE: QPalette | None = None


@dataclass(frozen=True)
class UploadConfiguration:
    upload_folder: Path
    completed_folder: Path | None
    error_folder: Path | None
    unsupported_folder: Path | None
    dont_move_completed: bool
    dont_move_failed: bool
    dont_move_unsupported: bool
    dont_preserve_move_structure: bool
    move_conflict_mode: str
    resize_images_if_needed: bool
    auto_generate_output_folders: bool
    ignore_subfolders: bool
    no_import_tags: bool
    omit_top_folder_list: bool
    import_to_root: bool
    root_list: str
    default_tags: tuple[str, ...]
    dry_run: bool


class EditableHistoryField(QWidget):
    """Editable combo box with a Browse button and persistent recent values."""

    def __init__(
        self,
        settings: QSettings,
        settings_key: str,
        dialog_title: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)

        self._settings = settings
        self._settings_key = settings_key
        self._dialog_title = dialog_title

        self.combo = QComboBox()
        self.combo.setEditable(True)
        self.combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.combo.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Fixed,
        )

        self.browse_button = QPushButton("Browse...")
        self.browse_button.clicked.connect(self._browse)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.combo, 1)
        layout.addWidget(self.browse_button)

        self._load_history()

    def text(self) -> str:
        return self.combo.currentText().strip()

    def set_text(self, value: str) -> None:
        self.combo.setCurrentText(value)

    def save_current_value(self) -> None:
        current = self.text()
        if not current:
            return

        existing = [
            self.combo.itemText(index)
            for index in range(self.combo.count())
            if self.combo.itemText(index)
        ]

        history = [current]
        history.extend(item for item in existing if item != current)
        history = history[:MAX_RECENT_VALUES]

        self._settings.setValue(self._settings_key, history)

        self.combo.clear()
        self.combo.addItems(history)
        self.combo.setCurrentText(current)

    def _load_history(self) -> None:
        stored = self._settings.value(self._settings_key, [])

        if isinstance(stored, str):
            history = [stored] if stored else []
        else:
            history = [str(item) for item in stored if str(item)]

        self.combo.addItems(history)

        if history:
            self.combo.setCurrentText(history[0])

    def _browse(self) -> None:
        starting_directory = self.text()

        if not starting_directory or not Path(starting_directory).exists():
            starting_directory = str(Path.home())

        selected = QFileDialog.getExistingDirectory(
            self,
            self._dialog_title,
            starting_directory,
        )

        if selected:
            self.set_text(selected)


class ConnectionSettingsDialog(QDialog):
    """Collects and tests the Karakeep connection settings."""

    def __init__(
        self,
        settings: QSettings,
        parent: QWidget | None = None,
        log_callback: Callable[[str, str], None] | None = None,
    ) -> None:
        super().__init__(parent)

        self.settings = settings
        self.log_callback = log_callback
        self.setWindowTitle("Connection Settings")
        self.setModal(True)
        self.setMinimumWidth(380)

        self.server_url_field = QLineEdit()
        self.server_url_field.setPlaceholderText("https://bookmarks.example.com:8290")
        self.server_url_field.setText(
            str(self.settings.value("connection/server_url", "") or "")
        )

        self.api_key_field = QLineEdit()
        self.api_key_field.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key_field.setText(self._load_api_key())

        self.show_key_checkbox = QCheckBox("Show")
        self.show_key_checkbox.toggled.connect(self._update_key_visibility)

        api_key_layout = QHBoxLayout()
        api_key_layout.setContentsMargins(0, 0, 0, 0)
        api_key_layout.addWidget(self.api_key_field, 1)
        api_key_layout.addWidget(self.show_key_checkbox)

        form_layout = QFormLayout()
        form_layout.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow
        )
        form_layout.addRow("Karakeep server:", self.server_url_field)
        form_layout.addRow("API key:", api_key_layout)

        self.button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Cancel
            | QDialogButtonBox.StandardButton.Save
        )
        self.test_button = QPushButton("Test Connection")
        self.button_box.addButton(
            self.test_button,
            QDialogButtonBox.ButtonRole.ActionRole,
        )

        self.test_button.clicked.connect(self._test_connection)
        self.button_box.accepted.connect(self._save_and_accept)
        self.button_box.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form_layout)
        layout.addWidget(self.button_box)
        self.setFixedHeight(self.sizeHint().height())

    @staticmethod
    def normalized_server_url(server_url: str) -> str:
        return KarakeepClient.normalized_server_url(server_url)

    @staticmethod
    def test_connection(server_url: str, api_key: str) -> tuple[bool, str]:
        return KarakeepClient(server_url, api_key).test_connection()

    def values(self) -> tuple[str, str]:
        return (
            self.normalized_server_url(self.server_url_field.text()),
            self.api_key_field.text().strip(),
        )

    def _load_api_key(self) -> str:
        try:
            return keyring.get_password(KEYRING_SERVICE, KEYRING_USERNAME) or ""
        except keyring.errors.KeyringError:
            return ""

    def _update_key_visibility(self, show_key: bool) -> None:
        if show_key:
            self.api_key_field.setEchoMode(QLineEdit.EchoMode.Normal)
        else:
            self.api_key_field.setEchoMode(QLineEdit.EchoMode.Password)

    def _test_connection(self) -> None:
        server_url, api_key = self.values()
        if self.log_callback is not None:
            self.log_callback(f"Testing Karakeep connection: {server_url}")
        QApplication.processEvents()

        success, message = self.test_connection(server_url, api_key)
        if self.log_callback is not None:
            level = "SUCCESS" if success else "ERROR"
            self.log_callback(message, level)

    def _save_and_accept(self) -> None:
        server_url, api_key = self.values()

        if not server_url:
            self._show_error("Enter a Karakeep server URL.")
            return

        if not (
            server_url.startswith("http://")
            or server_url.startswith("https://")
        ):
            self._show_error("Server URL must start with http:// or https://.")
            return

        if not api_key:
            self._show_error("Enter a Karakeep API key.")
            return

        try:
            keyring.set_password(
                KEYRING_SERVICE,
                KEYRING_USERNAME,
                api_key,
            )
        except keyring.errors.KeyringError as exc:
            self._show_error(f"Unable to save API key:\n\n{exc}")
            return

        self.settings.setValue("connection/server_url", server_url)
        self.settings.sync()
        self.accept()

    def _show_error(self, message: str) -> None:
        QMessageBox.critical(
            self,
            "Invalid Connection Settings",
            message,
        )


class PreferencesDialog(QDialog):
    """Edits JSON-backed application preferences."""

    def __init__(
        self,
        preferences: AppPreferences,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)

        self.preferences = preferences

        self.setWindowTitle("Preferences")
        self.setModal(True)
        self.setMinimumWidth(460)

        image_resize = preferences.image_resize

        self.view_mode_combo = QComboBox()
        self.view_mode_combo.addItem("Auto", "auto")
        self.view_mode_combo.addItem("Light", "light")
        self.view_mode_combo.addItem("Dark", "dark")
        view_mode_index = self.view_mode_combo.findData(preferences.view_mode)
        self.view_mode_combo.setCurrentIndex(max(view_mode_index, 0))

        self.maximum_allowed_size_spin = QDoubleSpinBox()
        self.maximum_allowed_size_spin.setRange(0.1, 100000.0)
        self.maximum_allowed_size_spin.setDecimals(1)
        self.maximum_allowed_size_spin.setSuffix(" MB")
        self.maximum_allowed_size_spin.setValue(
            image_resize.maximum_allowed_image_size_mb
        )

        self.desired_goal_spin = QDoubleSpinBox()
        self.desired_goal_spin.setRange(0.1, 100000.0)
        self.desired_goal_spin.setDecimals(1)
        self.desired_goal_spin.setSuffix(" MB")
        self.desired_goal_spin.setValue(
            image_resize.desired_resize_goal_mb
        )

        self.maximum_attempts_spin = QSpinBox()
        self.maximum_attempts_spin.setRange(1, 100)
        self.maximum_attempts_spin.setValue(image_resize.maximum_attempts)

        self.acceptable_distance_spin = QDoubleSpinBox()
        self.acceptable_distance_spin.setRange(0.1, 99.9)
        self.acceptable_distance_spin.setDecimals(1)
        self.acceptable_distance_spin.setSuffix("%")
        self.acceptable_distance_spin.setValue(
            image_resize.acceptable_distance_percent
        )

        self.fail_if_not_within_goal_checkbox = QCheckBox(
            "Fail if resized image is not within goal range"
        )
        self.fail_if_not_within_goal_checkbox.setChecked(
            image_resize.fail_if_not_within_goal
        )

        resize_form = QFormLayout()
        resize_form.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow
        )
        resize_form.addRow(
            "Maximum allowed image size:",
            self.maximum_allowed_size_spin,
        )
        resize_form.addRow("Desired resize goal:", self.desired_goal_spin)
        resize_form.addRow("Maximum resize attempts:", self.maximum_attempts_spin)
        resize_form.addRow(
            "Acceptable distance from goal:",
            self.acceptable_distance_spin,
        )
        resize_form.addRow("", self.fail_if_not_within_goal_checkbox)

        resize_group = QGroupBox("Image resizing")
        resize_group.setLayout(resize_form)

        view_form = QFormLayout()
        view_form.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow
        )
        view_form.addRow("View mode:", self.view_mode_combo)

        view_group = QGroupBox("Appearance")
        view_group.setLayout(view_form)

        helper_text = QLabel(
            "Images larger than the maximum allowed size, or rejected by "
            "Karakeep as too large, can be resized toward the desired goal."
        )
        helper_text.setWordWrap(True)

        self.button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Cancel
            | QDialogButtonBox.StandardButton.Save
        )
        self.button_box.accepted.connect(self._validate_and_accept)
        self.button_box.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(view_group)
        layout.addWidget(resize_group)
        layout.addWidget(helper_text)
        layout.addWidget(self.button_box)

    def _validate_and_accept(self) -> None:
        maximum_allowed = self.maximum_allowed_size_spin.value()
        desired_goal = self.desired_goal_spin.value()

        if desired_goal >= maximum_allowed:
            QMessageBox.critical(
                self,
                "Invalid Preferences",
                (
                    "Desired resize goal must be lower than the maximum "
                    "allowed image size."
                ),
            )
            return

        self.preferences = AppPreferences(
            view_mode=str(self.view_mode_combo.currentData()),
            image_resize=ImageResizePreferences(
                maximum_allowed_image_size_mb=maximum_allowed,
                desired_resize_goal_mb=desired_goal,
                maximum_attempts=self.maximum_attempts_spin.value(),
                acceptable_distance_percent=(
                    self.acceptable_distance_spin.value()
                ),
                fail_if_not_within_goal=(
                    self.fail_if_not_within_goal_checkbox.isChecked()
                ),
            )
        )
        self.accept()


class UploadDialog(QDialog):
    """Collects and validates the settings for one upload batch."""

    def __init__(
        self,
        settings: QSettings,
        parent: QWidget | None = None,
        locked_upload_folder: Path | None = None,
    ) -> None:
        super().__init__(parent)

        self.settings = settings
        self.locked_upload_folder = locked_upload_folder
        self.configuration: UploadConfiguration | None = None
        self._manual_completed_folder = ""
        self._manual_error_folder = ""
        self._manual_unsupported_folder = ""

        self.setWindowTitle("Upload Configuration")
        self.setModal(True)
        self.setMinimumWidth(650)

        self.upload_field = EditableHistoryField(
            settings,
            "history/upload_folders",
            "Select Upload Folder",
        )
        if locked_upload_folder is not None:
            self.upload_field.set_text(str(locked_upload_folder))
            self.upload_field.setEnabled(False)

        self.completed_field = EditableHistoryField(
            settings,
            "history/completed_folders",
            "Select Completed Folder",
        )
        self.error_field = EditableHistoryField(
            settings,
            "history/error_folders",
            "Select Error Folder",
        )
        self.unsupported_field = EditableHistoryField(
            settings,
            "history/unsupported_folders",
            "Select Unsupported Folder",
        )
        self._manual_completed_folder = self.completed_field.text()
        self._manual_error_folder = self.error_field.text()
        self._manual_unsupported_folder = self.unsupported_field.text()

        self.resize_images_if_needed_checkbox = QCheckBox(
            "Allow img resize"
        )
        self.resize_images_if_needed_checkbox.setChecked(
            self.settings.value(
                "upload/resize_images_if_needed",
                True,
                type=bool,
            )
        )

        self.auto_generate_output_folders_checkbox = QCheckBox(
            "Auto output folders"
        )
        self.auto_generate_output_folders_checkbox.setChecked(
            self.settings.value(
                "upload/auto_generate_output_folders",
                False,
                type=bool,
            )
        )
        self.auto_generate_output_folders_checkbox.toggled.connect(
            self._handle_auto_generate_output_folders_toggled
        )
        self.upload_field.combo.currentTextChanged.connect(
            self._update_generated_output_folders
        )
        self.upload_field.combo.editTextChanged.connect(
            self._update_generated_output_folders
        )

        self.omit_top_folder_list_checkbox = QCheckBox("Omit top folder")
        self.omit_top_folder_list_checkbox.setChecked(
            self.settings.value(
                "upload/omit_top_folder_list",
                False,
                type=bool,
            )
        )

        self.ignore_subfolders_checkbox = QCheckBox("Ignore subfolders")
        self.ignore_subfolders_checkbox.setChecked(
            self.settings.value(
                "upload/ignore_subfolders",
                False,
                type=bool,
            )
        )
        self.ignore_subfolders_checkbox.toggled.connect(
            self._update_ignore_subfolders_state
        )

        self.dont_move_completed_checkbox = QCheckBox("Don't move")
        self.dont_move_completed_checkbox.setChecked(
            self.settings.value(
                "upload/dont_move_completed",
                False,
                type=bool,
            )
        )
        self.dont_move_completed_checkbox.toggled.connect(
            self._update_move_folder_state
        )

        self.dont_move_failed_checkbox = QCheckBox("Don't move")
        self.dont_move_failed_checkbox.setChecked(
            self.settings.value(
                "upload/dont_move_failed",
                False,
                type=bool,
            )
        )
        self.dont_move_failed_checkbox.toggled.connect(
            self._update_move_folder_state
        )

        self.dont_move_unsupported_checkbox = QCheckBox("Don't move")
        self.dont_move_unsupported_checkbox.setChecked(
            self.settings.value(
                "upload/dont_move_unsupported",
                False,
                type=bool,
            )
        )
        self.dont_move_unsupported_checkbox.toggled.connect(
            self._update_move_folder_state
        )

        self.dont_preserve_move_structure_checkbox = QCheckBox(
            "Don't preserve subfolders when moving"
        )
        self.dont_preserve_move_structure_checkbox.setChecked(
            self.settings.value(
                "upload/dont_preserve_move_structure",
                False,
                type=bool,
            )
        )

        self.move_conflict_combo = QComboBox()
        self.move_conflict_combo.addItem("Ask", "ask")
        self.move_conflict_combo.addItem("Rename", "rename")
        self.move_conflict_combo.addItem("Overwrite", "overwrite")
        self.move_conflict_combo.addItem("STOP!", "stop")
        self._load_move_conflict_mode()

        move_options_layout = QHBoxLayout()
        move_options_layout.setContentsMargins(0, 0, 0, 0)
        move_options_layout.addWidget(self.dont_preserve_move_structure_checkbox)
        move_options_layout.addStretch(1)
        move_options_layout.addWidget(QLabel("On file move conflict:"))
        move_options_layout.addWidget(self.move_conflict_combo)

        self.root_list_combo = QComboBox()
        self.root_list_combo.setEditable(True)
        self.root_list_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self._load_text_history(
            self.root_list_combo,
            "history/root_lists",
            DEFAULT_ROOT_LIST,
        )

        self.import_to_root_checkbox = QCheckBox("No import list")
        self.import_to_root_checkbox.setChecked(
            self.settings.value(
                "upload/import_to_root",
                False,
                type=bool,
            )
        )
        self.import_to_root_checkbox.toggled.connect(
            self._update_root_list_state
        )

        self.no_import_tags_checkbox = QCheckBox("No import tags")
        self.no_import_tags_checkbox.setChecked(
            self.settings.value(
                "upload/no_import_tags",
                False,
                type=bool,
            )
        )
        self.no_import_tags_checkbox.toggled.connect(
            self._update_tags_state
        )

        self.tags_combo = QComboBox()
        self.tags_combo.setEditable(True)
        self.tags_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.tags_combo.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Fixed,
        )
        self._load_text_history(
            self.tags_combo,
            "history/default_tags",
            DEFAULT_TAGS,
        )

        self.dry_run_checkbox = QCheckBox("Dry-run")
        self.dry_run_checkbox.setChecked(True)

        import_to_list_label = QLabel(
            "Import to list:<br><span style='font-size: 8pt; font-style: italic;'>"
            "&nbsp;( / for sublists)</span>"
        )
        import_to_list_label.setTextFormat(Qt.TextFormat.RichText)

        default_tags_label = QLabel(
            "Default tags:<br><span style='font-size: 8pt; font-style: italic;'>"
            "&nbsp;(comma separated)</span>"
        )
        default_tags_label.setTextFormat(Qt.TextFormat.RichText)

        form_layout = QFormLayout()
        form_layout.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow
        )
        completed_layout = QHBoxLayout()
        completed_layout.setContentsMargins(0, 0, 0, 0)
        completed_layout.addWidget(self.completed_field, 1)
        completed_layout.addWidget(self.dont_move_completed_checkbox)

        error_layout = QHBoxLayout()
        error_layout.setContentsMargins(0, 0, 0, 0)
        error_layout.addWidget(self.error_field, 1)
        error_layout.addWidget(self.dont_move_failed_checkbox)

        unsupported_layout = QHBoxLayout()
        unsupported_layout.setContentsMargins(0, 0, 0, 0)
        unsupported_layout.addWidget(self.unsupported_field, 1)
        unsupported_layout.addWidget(self.dont_move_unsupported_checkbox)

        upload_options_layout = QHBoxLayout()
        upload_options_layout.setContentsMargins(0, 0, 0, 0)
        upload_options_layout.addWidget(self.resize_images_if_needed_checkbox)
        upload_options_layout.addWidget(self.auto_generate_output_folders_checkbox)
        upload_options_layout.addWidget(self.omit_top_folder_list_checkbox)
        upload_options_layout.addWidget(self.ignore_subfolders_checkbox)
        upload_options_layout.addStretch(1)

        form_layout.addRow("Folder to upload:", self.upload_field)
        form_layout.addRow("", upload_options_layout)
        form_layout.addRow(QLabel(" "))
        form_layout.addRow(self._section_label("Move files after processing"))
        form_layout.addRow("Completed folder:", completed_layout)
        form_layout.addRow("Error folder:", error_layout)
        form_layout.addRow("Unsupported folder:", unsupported_layout)
        form_layout.addRow("", move_options_layout)

        root_list_layout = QHBoxLayout()
        root_list_layout.setContentsMargins(0, 0, 0, 0)
        root_list_layout.addWidget(self.root_list_combo, 1)
        root_list_layout.addWidget(self.import_to_root_checkbox)

        tags_layout = QHBoxLayout()
        tags_layout.setContentsMargins(0, 0, 0, 0)
        tags_layout.addWidget(self.tags_combo, 1)
        tags_layout.addWidget(self.no_import_tags_checkbox)

        form_layout.addRow(QLabel(" "))
        form_layout.addRow(self._section_label("New import organization"))
        form_layout.addRow(
            import_to_list_label,
            root_list_layout,
        )
        form_layout.addRow(
            default_tags_label,
            tags_layout,
        )

        self.button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Cancel
        )
        self.start_button = self.button_box.addButton(
            "Start Upload",
            QDialogButtonBox.ButtonRole.AcceptRole,
        )

        self.start_button.clicked.connect(self._validate_and_accept)
        self.button_box.rejected.connect(self.reject)
        self.dry_run_checkbox.toggled.connect(
            self._update_start_button_text
        )

        self._update_start_button_text(
            self.dry_run_checkbox.isChecked()
        )

        layout = QVBoxLayout(self)
        layout.addLayout(form_layout)
        layout.addSpacing(8)

        bottom_layout = QHBoxLayout()
        bottom_layout.addWidget(self.dry_run_checkbox)
        bottom_layout.addStretch(1)
        bottom_layout.addWidget(self.button_box)

        layout.addLayout(bottom_layout)

        self._update_root_list_state(
            self.import_to_root_checkbox.isChecked()
        )
        self._update_generated_output_folders()
        self._update_move_folder_state()
        self._update_ignore_subfolders_state(
            self.ignore_subfolders_checkbox.isChecked()
        )
        self._update_tags_state(self.no_import_tags_checkbox.isChecked())

    def _update_root_list_state(self, import_to_root: bool) -> None:
        self.root_list_combo.setEnabled(not import_to_root)

    def _handle_auto_generate_output_folders_toggled(self, checked: bool) -> None:
        if checked:
            self._manual_completed_folder = self.completed_field.text()
            self._manual_error_folder = self.error_field.text()
            self._manual_unsupported_folder = self.unsupported_field.text()
            self._update_generated_output_folders()
        else:
            self.completed_field.set_text(self._manual_completed_folder)
            self.error_field.set_text(self._manual_error_folder)
            self.unsupported_field.set_text(self._manual_unsupported_folder)

        self._update_move_folder_state()

    def _update_move_folder_state(self) -> None:
        auto_generate = self.auto_generate_output_folders_checkbox.isChecked()
        if auto_generate:
            self._update_generated_output_folders()

        self.completed_field.setEnabled(
            not self.dont_move_completed_checkbox.isChecked()
            and not auto_generate
        )
        self.error_field.setEnabled(
            not self.dont_move_failed_checkbox.isChecked()
            and not auto_generate
        )
        self.unsupported_field.setEnabled(
            not self.dont_move_unsupported_checkbox.isChecked()
            and not auto_generate
        )
        self._update_ignore_subfolders_state(
            self.ignore_subfolders_checkbox.isChecked()
        )

    def _update_ignore_subfolders_state(self, ignore_subfolders: bool) -> None:
        self.dont_preserve_move_structure_checkbox.setEnabled(
            not ignore_subfolders
        )

    def _update_generated_output_folders(self) -> None:
        if not self.auto_generate_output_folders_checkbox.isChecked():
            return

        upload_text = self.upload_field.text()
        if not upload_text:
            self.completed_field.set_text("")
            self.error_field.set_text("")
            self.unsupported_field.set_text("")
            return

        upload_folder = Path(upload_text).expanduser()
        self.completed_field.set_text(
            str(self._generated_output_folder(upload_folder, "SUCCESS"))
        )
        self.error_field.set_text(
            str(self._generated_output_folder(upload_folder, "FAIL"))
        )
        self.unsupported_field.set_text(
            str(self._generated_output_folder(upload_folder, "UNSUPPORTED"))
        )

    def _update_tags_state(self, no_import_tags: bool) -> None:
        self.tags_combo.setEnabled(not no_import_tags)

    @staticmethod
    def _generated_output_folder(upload_folder: Path, category: str) -> Path:
        return upload_folder.parent / "KKU" / category / upload_folder.name

    @staticmethod
    def _section_label(text: str) -> QLabel:
        label = QLabel(f"<b>{text}</b>")
        label.setTextFormat(Qt.TextFormat.RichText)
        return label

    @staticmethod
    def _helper_label(text: str) -> QLabel:
        label = QLabel(text)
        label.setWordWrap(True)
        label.setStyleSheet("color: palette(text); opacity: 0.72;")
        return label

    def _update_start_button_text(self, dry_run: bool) -> None:
        if dry_run:
            self.start_button.setText("Start Dry-run")
        else:
            self.start_button.setText("Start Upload")

    def _validate_and_accept(self) -> None:
        upload_text = self.upload_field.text()
        completed_text = self.completed_field.text()
        error_text = self.error_field.text()
        unsupported_text = self.unsupported_field.text()
        root_list = self.root_list_combo.currentText().strip()
        import_to_root = self.import_to_root_checkbox.isChecked()
        dont_move_completed = self.dont_move_completed_checkbox.isChecked()
        dont_move_failed = self.dont_move_failed_checkbox.isChecked()
        dont_move_unsupported = self.dont_move_unsupported_checkbox.isChecked()
        dont_preserve_move_structure = (
            self.dont_preserve_move_structure_checkbox.isChecked()
        )
        move_conflict_mode = str(self.move_conflict_combo.currentData())
        resize_images_if_needed = (
            self.resize_images_if_needed_checkbox.isChecked()
        )
        auto_generate_output_folders = (
            self.auto_generate_output_folders_checkbox.isChecked()
        )
        ignore_subfolders = self.ignore_subfolders_checkbox.isChecked()
        no_import_tags = self.no_import_tags_checkbox.isChecked()
        omit_top_folder_list = self.omit_top_folder_list_checkbox.isChecked()

        if auto_generate_output_folders:
            self._update_generated_output_folders()
            completed_text = self.completed_field.text()
            error_text = self.error_field.text()
            unsupported_text = self.unsupported_field.text()

        if not upload_text:
            self._show_error("Folder to upload cannot be empty.")
            return

        if not dont_move_completed and not completed_text:
            self._show_error(
                "Completed folder cannot be empty unless "
                "\"Don't move completed\" is checked."
            )
            return

        if not dont_move_failed and not error_text:
            self._show_error(
                "Error folder cannot be empty unless "
                "\"Don't move failed\" is checked."
            )
            return

        if not dont_move_unsupported and not unsupported_text:
            self._show_error(
                "Unsupported folder cannot be empty unless "
                "\"Don't move unsupported\" is checked."
            )
            return

        upload_folder = Path(upload_text).expanduser()
        completed_folder = (
            None
            if dont_move_completed
            else Path(completed_text).expanduser()
        )
        error_folder = (
            None
            if dont_move_failed
            else Path(error_text).expanduser()
        )
        unsupported_folder = (
            None
            if dont_move_unsupported
            else Path(unsupported_text).expanduser()
        )

        if not upload_folder.exists():
            self._show_error(
                f"The Upload folder does not exist:\n\n{upload_folder}"
            )
            return

        if not upload_folder.is_dir():
            self._show_error(
                f"The Upload path is not a folder:\n\n{upload_folder}"
            )
            return

        if not import_to_root and not root_list:
            self._show_error(
                "Enter an Import to list value or select No import list."
            )
            return

        if not import_to_root:
            try:
                parse_list_path(root_list)
            except ValueError as exc:
                self._show_error(str(exc))
                return

        try:
            resolved_upload = upload_folder.resolve()
            resolved_paths = {"Upload": resolved_upload}
            if completed_folder is not None:
                resolved_paths["Completed"] = completed_folder.resolve()
            if error_folder is not None:
                resolved_paths["Error"] = error_folder.resolve()
            if unsupported_folder is not None:
                resolved_paths["Unsupported"] = unsupported_folder.resolve()
        except OSError as exc:
            self._show_error(f"Unable to resolve folder paths:\n\n{exc}")
            return

        try:
            validate_separate_folder_tree(
                resolved_paths,
                allowed_equal_pairs={
                    frozenset(("Completed", "Error")),
                    frozenset(("Completed", "Unsupported")),
                    frozenset(("Error", "Unsupported")),
                },
            )
        except ValueError as exc:
            self._show_error(str(exc))
            return

        tags = (
            ()
            if no_import_tags
            else tuple(
                tag.strip()
                for tag in self.tags_combo.currentText().split(",")
                if tag.strip()
            )
        )

        self.configuration = UploadConfiguration(
            upload_folder=upload_folder.resolve(),
            completed_folder=(
                completed_folder.resolve()
                if completed_folder is not None
                else None
            ),
            error_folder=(
                error_folder.resolve()
                if error_folder is not None
                else None
            ),
            unsupported_folder=(
                unsupported_folder.resolve()
                if unsupported_folder is not None
                else None
            ),
            dont_move_completed=dont_move_completed,
            dont_move_failed=dont_move_failed,
            dont_move_unsupported=dont_move_unsupported,
            dont_preserve_move_structure=dont_preserve_move_structure,
            move_conflict_mode=move_conflict_mode,
            resize_images_if_needed=resize_images_if_needed,
            auto_generate_output_folders=auto_generate_output_folders,
            ignore_subfolders=ignore_subfolders,
            no_import_tags=no_import_tags,
            omit_top_folder_list=omit_top_folder_list,
            import_to_root=import_to_root,
            root_list=root_list,
            default_tags=tags,
            dry_run=self.dry_run_checkbox.isChecked(),
        )

        self._save_values()
        self.accept()

    def _save_values(self) -> None:
        self.upload_field.save_current_value()
        if (
            not self.dont_move_completed_checkbox.isChecked()
            and not self.auto_generate_output_folders_checkbox.isChecked()
        ):
            self.completed_field.save_current_value()
        if (
            not self.dont_move_failed_checkbox.isChecked()
            and not self.auto_generate_output_folders_checkbox.isChecked()
        ):
            self.error_field.save_current_value()
        if (
            not self.dont_move_unsupported_checkbox.isChecked()
            and not self.auto_generate_output_folders_checkbox.isChecked()
        ):
            self.unsupported_field.save_current_value()

        self._save_text_history(
            self.root_list_combo,
            "history/root_lists",
        )
        self._save_text_history(
            self.tags_combo,
            "history/default_tags",
        )

        self.settings.setValue(
            "upload/import_to_root",
            self.import_to_root_checkbox.isChecked(),
        )

        self.settings.setValue(
            "upload/dont_move_completed",
            self.dont_move_completed_checkbox.isChecked(),
        )
        self.settings.setValue(
            "upload/resize_images_if_needed",
            self.resize_images_if_needed_checkbox.isChecked(),
        )
        self.settings.setValue(
            "upload/auto_generate_output_folders",
            self.auto_generate_output_folders_checkbox.isChecked(),
        )
        self.settings.setValue(
            "upload/ignore_subfolders",
            self.ignore_subfolders_checkbox.isChecked(),
        )
        self.settings.setValue(
            "upload/no_import_tags",
            self.no_import_tags_checkbox.isChecked(),
        )
        self.settings.setValue(
            "upload/omit_top_folder_list",
            self.omit_top_folder_list_checkbox.isChecked(),
        )
        self.settings.setValue(
            "upload/dont_move_failed",
            self.dont_move_failed_checkbox.isChecked(),
        )
        self.settings.setValue(
            "upload/dont_move_unsupported",
            self.dont_move_unsupported_checkbox.isChecked(),
        )
        self.settings.setValue(
            "upload/dont_preserve_move_structure",
            self.dont_preserve_move_structure_checkbox.isChecked(),
        )
        self.settings.setValue(
            "upload/move_conflict_mode",
            self.move_conflict_combo.currentData(),
        )
        self.settings.sync()

    def _load_move_conflict_mode(self) -> None:
        stored_mode = str(
            self.settings.value("upload/move_conflict_mode", "") or ""
        )
        if not stored_mode:
            stored_mode = (
                "rename"
                if self.settings.value(
                    "upload/rename_move_conflicts",
                    False,
                    type=bool,
                )
                else "ask"
            )

        index = self.move_conflict_combo.findData(stored_mode)
        self.move_conflict_combo.setCurrentIndex(index if index >= 0 else 0)

    def _load_text_history(
        self,
        combo: QComboBox,
        settings_key: str,
        default_value: str,
    ) -> None:
        stored = self.settings.value(settings_key, [])

        if isinstance(stored, str):
            history = [stored] if stored else []
        else:
            history = [str(item) for item in stored if str(item)]

        if not history:
            history = [default_value]

        combo.addItems(history)
        combo.setCurrentText(history[0])

    def _save_text_history(
        self,
        combo: QComboBox,
        settings_key: str,
    ) -> None:
        current = combo.currentText().strip()

        existing = [
            combo.itemText(index)
            for index in range(combo.count())
            if combo.itemText(index)
        ]

        history: list[str] = []

        if current:
            history.append(current)

        history.extend(item for item in existing if item != current)
        history = history[:MAX_RECENT_VALUES]

        self.settings.setValue(settings_key, history)

        combo.clear()
        combo.addItems(history)

        if current:
            combo.setCurrentText(current)

    def _show_error(self, message: str) -> None:
        QMessageBox.critical(
            self,
            "Invalid Upload Configuration",
            message,
        )


class MoveConflictDialog(QDialog):
    """Asks how to handle a destination-file conflict during a move."""

    def __init__(
        self,
        request: MoveConflictRequest,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)

        self.choice = "stop"
        self.setWindowTitle("Move Conflict")
        self.setModal(True)
        self.setMinimumWidth(700)

        message = QLabel(
            "A file already exists where KKUpload wants to move this file."
        )
        message.setWordWrap(True)

        source_label = QLabel(
            "<b>Moving file:</b><br>"
            f"{request.source_path.name}<br>"
            f"<b>From:</b> {request.source_path.parent}"
        )
        source_label.setTextFormat(Qt.TextFormat.RichText)
        source_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        source_label.setWordWrap(True)

        existing_label = QLabel(
            "<b>Existing file:</b><br>"
            f"{request.existing_path.name}<br>"
            f"<b>In:</b> {request.existing_path.parent}"
        )
        existing_label.setTextFormat(Qt.TextFormat.RichText)
        existing_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        existing_label.setWordWrap(True)

        destination_label = QLabel(
            f"<b>Requested destination:</b> {request.destination_path}"
        )
        destination_label.setTextFormat(Qt.TextFormat.RichText)
        destination_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        destination_label.setWordWrap(True)

        self.apply_to_all_checkbox = QCheckBox(
            "Do this operation for all future conflicts"
        )

        overwrite_button = QPushButton("Overwrite")
        rename_button = QPushButton("Rename")
        stop_button = QPushButton("Stop Upload")

        overwrite_button.clicked.connect(lambda: self._choose("overwrite"))
        rename_button.clicked.connect(lambda: self._choose("rename"))
        stop_button.clicked.connect(lambda: self._choose("stop"))

        button_layout = QHBoxLayout()
        button_layout.addStretch(1)
        button_layout.addWidget(overwrite_button)
        button_layout.addWidget(rename_button)
        button_layout.addWidget(stop_button)

        layout = QVBoxLayout(self)
        layout.addWidget(message)
        layout.addSpacing(8)
        layout.addWidget(source_label)
        layout.addSpacing(8)
        layout.addWidget(existing_label)
        layout.addSpacing(8)
        layout.addWidget(destination_label)
        layout.addSpacing(8)
        layout.addWidget(self.apply_to_all_checkbox)
        layout.addLayout(button_layout)

    def _choose(self, choice: str) -> None:
        self.choice = choice
        self.accept()


class FindDialog(QDialog):
    """Modeless log search dialog with live match counts."""

    SEARCH_DEBOUNCE_MS: Final[int] = 150

    def __init__(
        self,
        log_text_provider: Callable[[], str],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)

        self.log_text_provider = log_text_provider
        self._drag_offset: QPoint | None = None

        self.setWindowTitle("Find")
        self.setModal(False)
        self.setWindowFlags(
            self.windowFlags()
            | Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
        )
        self.setMinimumWidth(320)

        self.search_field = QLineEdit()
        self.search_field.setPlaceholderText("Find")

        self.scope_combo = QComboBox()
        self.scope_combo.addItem("All", "all")
        self.scope_combo.addItem("Issues", "issues")
        self.scope_combo.addItem("Warnings", "warnings")
        self.scope_combo.addItem("Errors", "errors")

        self.count_label = QLabel("0 instances")

        self.previous_button = QPushButton("Prev")
        self.previous_button.setEnabled(False)
        self.previous_button.setToolTip("Result navigation will be added later.")

        self.next_button = QPushButton("Next")
        self.next_button.setEnabled(False)
        self.next_button.setToolTip("Result navigation will be added later.")

        self.close_button = QPushButton("X")
        self.close_button.setFixedWidth(32)
        self.close_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.close_button.setStyleSheet(
            "QPushButton { border: none; padding: 2px 6px; }"
            "QPushButton:hover { background: palette(midlight); }"
        )
        self.close_button.clicked.connect(self.reject)

        search_layout = QHBoxLayout()
        search_layout.setContentsMargins(0, 0, 0, 0)
        search_layout.addWidget(self.search_field, 1)
        search_layout.addWidget(self.close_button)

        navigation_layout = QHBoxLayout()
        navigation_layout.setContentsMargins(0, 0, 0, 0)
        navigation_layout.addWidget(self.previous_button)
        navigation_layout.addWidget(self.scope_combo, 1)
        navigation_layout.addWidget(self.next_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.addLayout(search_layout)
        layout.addWidget(self.count_label)
        layout.addLayout(navigation_layout)

        self.count_timer = QTimer(self)
        self.count_timer.setSingleShot(True)
        self.count_timer.setInterval(self.SEARCH_DEBOUNCE_MS)
        self.count_timer.timeout.connect(self._update_match_count)

        self.search_field.textChanged.connect(self._schedule_count_update)
        self.scope_combo.currentIndexChanged.connect(self._schedule_count_update)

    def focus_search_field(self) -> None:
        self.search_field.setFocus()
        self.search_field.selectAll()
        self._schedule_count_update()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = (
                event.globalPosition().toPoint()
                - self.frameGeometry().topLeft()
            )

        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if (
            self._drag_offset is not None
            and event.buttons() & Qt.MouseButton.LeftButton
        ):
            self.move(event.globalPosition().toPoint() - self._drag_offset)

        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        self._drag_offset = None
        super().mouseReleaseEvent(event)

    def _schedule_count_update(self, *_args) -> None:  # type: ignore[no-untyped-def]
        self.count_timer.start()

    def _update_match_count(self) -> None:
        needle = self.search_field.text()
        if not needle:
            self.count_label.setText("0 instances")
            return

        count = self._count_matches(
            self.log_text_provider(),
            needle,
            str(self.scope_combo.currentData()),
        )
        instance_label = "instance" if count == 1 else "instances"
        self.count_label.setText(f"{count} {instance_label}")

    @classmethod
    def _count_matches(cls, log_text: str, needle: str, scope: str) -> int:
        if not needle:
            return 0

        haystack = cls._scoped_log_text(log_text, scope).casefold()
        return haystack.count(needle.casefold())

    @staticmethod
    def _scoped_log_text(log_text: str, scope: str) -> str:
        if scope == "all":
            return log_text

        lines = log_text.splitlines()

        if scope == "errors":
            return "\n".join(line for line in lines if "[ERROR]" in line)

        if scope == "warnings":
            return "\n".join(line for line in lines if "[WARNING]" in line)

        if scope == "issues":
            return "\n".join(
                line
                for line in lines
                if "[ERROR]" in line or "[WARNING]" in line
            )

        return log_text


class StatusConsole(QPlainTextEdit):
    """Read-only log console with a subtle bottom-right watermark."""

    line_clicked = Signal(int)

    def __init__(self, watermark_path: Path, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        self._watermark = QPixmap(str(watermark_path))

    def mousePressEvent(self, event) -> None:  # noqa: N802
        super().mousePressEvent(event)

        if event.button() != Qt.MouseButton.LeftButton:
            return

        cursor = self.cursorForPosition(event.position().toPoint())
        block_text = cursor.block().text()
        clicked_url = self._url_at_cursor(block_text, cursor.positionInBlock())
        if clicked_url is not None:
            webbrowser.open(clicked_url)
            return

        block_number = cursor.blockNumber()
        if block_number >= 0:
            self.line_clicked.emit(block_number)

    @staticmethod
    def _url_at_cursor(text: str, position: int) -> str | None:
        for match in re.finditer(r"https?://\S+", text):
            if match.start() <= position <= match.end():
                return match.group(0).rstrip(".,);]")
        return None

    def paintEvent(self, event) -> None:  # noqa: N802
        super().paintEvent(event)

        if self._watermark.isNull():
            return

        viewport_rect = self.viewport().rect()
        console_rect = self.rect()
        cropped_watermark = self._watermark.copy(
            0,
            0,
            int(self._watermark.width() * 0.90),
            int(self._watermark.height() * 0.75),
        )
        max_width = max(360, min(840, int(console_rect.width() * 0.96)))
        max_height = max(270, min(660, int(console_rect.height() * 1.35)))
        scaled = cropped_watermark.scaled(
            max_width,
            max_height,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )

        viewport_origin = self.viewport().pos()
        x = console_rect.right() - scaled.width() - 16 - viewport_origin.x()
        y = console_rect.bottom() - scaled.height() - 16 - viewport_origin.y()

        painter = QPainter(self.viewport())
        painter.setOpacity(0.16)
        painter.drawPixmap(x, y, scaled, 0, 0, scaled.width(), scaled.height())
        painter.end()


class LogMarkerRail(QWidget):
    """Full-log marker rail for warnings, errors, and major start lines."""

    marker_clicked = Signal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._markers: list[tuple[int, str]] = []
        self._total_lines = 1
        self._visible_lines = 1
        self.setMinimumWidth(12)
        self.setMaximumWidth(12)
        self.setSizePolicy(
            QSizePolicy.Policy.Fixed,
            QSizePolicy.Policy.Expanding,
        )

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(12, 120)

    def clear_markers(self) -> None:
        self._markers.clear()
        self._total_lines = 1
        self._visible_lines = 1
        self.update()

    def set_line_metrics(self, total_lines: int, visible_lines: int) -> None:
        self._total_lines = max(total_lines, 1)
        self._visible_lines = max(visible_lines, 1)
        self.update()

    def set_scroll_metrics(
        self,
        scrollbar_maximum: int,
        marker_scroll_positions: dict[int, float],
    ) -> None:
        # Kept for compatibility with the caller; marker placement intentionally
        # uses document line numbers because QPlainTextEdit scrollbar units do
        # not map reliably to wrapped-line geometry.
        self.update()

    def add_marker(self, line_number: int, marker_type: str) -> None:
        self._markers.append((max(line_number, 0), marker_type))
        self.update()

    def markers(self) -> tuple[tuple[int, str], ...]:
        return tuple(self._markers)

    def paintEvent(self, event) -> None:  # noqa: N802
        super().paintEvent(event)

        if not self._markers:
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)

        colors = self._marker_colors()
        top_inset = 16

        for y, marker_type in sorted(
            self._marker_positions(),
            key=lambda marker: self._marker_priority(marker[1]),
        ):
            marker_height = {
                "SUCCESS": 6,
                "ERROR": 5,
                "WARNING": 5,
                "START": 3,
            }.get(marker_type, 3)
            marker_width = {
                "SUCCESS": 9,
                "ERROR": 8,
                "WARNING": 8,
                "START": 5,
            }.get(marker_type, 5)
            marker_width = min(marker_width, self.width())
            marker_x = (self.width() - marker_width) // 2
            painter.fillRect(
                marker_x,
                max(top_inset, y - marker_height // 2),
                marker_width,
                marker_height,
                QColor(colors.get(marker_type, colors["START"])),
            )

        painter.end()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if not self._markers:
            return

        click_y = event.position().y()
        hit_radius = 10
        nearest_marker: tuple[int, int] | None = None

        for line_number, marker_type in self._markers:
            marker_y = self._marker_y(line_number)
            distance = abs(marker_y - click_y)
            if distance > hit_radius:
                continue
            if nearest_marker is None or distance < nearest_marker[0]:
                nearest_marker = (int(distance), line_number)

        if nearest_marker is not None:
            self.marker_clicked.emit(nearest_marker[1])

    def _marker_positions(self) -> list[tuple[int, str]]:
        return [
            (self._marker_y(line_number), marker_type)
            for line_number, marker_type in self._markers
        ]

    def _marker_y(self, line_number: int) -> int:
        top_inset = 16
        bottom_inset = 22
        usable_height = max(self.height() - top_inset - bottom_inset - 1, 1)
        denominator = max(self._total_lines - 1, 1)
        clamped_line = min(max(line_number, 0), denominator)
        return top_inset + int((clamped_line / denominator) * usable_height)

    @staticmethod
    def _marker_priority(marker_type: str) -> int:
        priorities = {
            "START": 0,
            "WARNING": 1,
            "ERROR": 2,
            "SUCCESS": 3,
        }
        return priorities.get(marker_type, 0)

    def _marker_colors(self) -> dict[str, str]:
        base_color = self.palette().base().color()
        is_dark_mode = base_color.lightness() < 128

        if is_dark_mode:
            return {
                "ERROR": "#f87171",
                "WARNING": "#facc15",
                "START": "#7dd3fc",
                "SUCCESS": "#22c55e",
            }

        return {
            "ERROR": "#b91c1c",
            "WARNING": "#ca8a04",
            "START": "#0284c7",
            "SUCCESS": "#166534",
        }


class MainWindow(QMainWindow):
    """Main KKUpload application window."""

    def __init__(self) -> None:
        super().__init__()

        self.settings = QSettings(
            ORGANIZATION_NAME,
            APP_NAME,
        )
        preference_load_result = load_preferences(Path(__file__))
        self.preferences: AppPreferences = preference_load_result.preferences
        self._apply_view_mode(self.preferences.view_mode)

        self.configuration: UploadConfiguration | None = None
        self.current_index = 0
        self.succeeded_count = 0
        self.failed_count = 0
        self.not_processed_count = 0
        self.resolved_conflict_count = 0
        self.unsupported_count = 0
        self.failed_files: list[tuple[str, str]] = []
        self.duplicate_stats: dict[str, int] = {
            "scanned": 0,
            "hashed": 0,
            "hash_remaining": 0,
            "compared": 0,
            "compare_remaining": 0,
            "matching": 0,
        }
        self.completed_operations = 0
        self.total_operations = 0
        self.total_files = 0
        self.job_started_at: float | None = None
        self.stop_requested = False
        self.is_paused = False
        self.is_running = False
        self.current_job_kind: str | None = None
        self.worker_thread: QThread | None = None
        self.worker: UploadWorker | DuplicateCheckWorker | None = None
        self.find_dialog: FindDialog | None = None
        self.current_log_path: Path | None = None
        self.connection_status_text = "Karakeep: Not configured"
        self.marker_metrics_update_scheduled = False
        self.highlighted_console_line: int | None = None
        self.current_marker_line: int | None = None
        self.pending_log_entries: deque[tuple[str, str, str | None]] = deque()
        self.pending_finish_stopped: bool | None = None
        self.pending_dry_run_estimate_lines: list[tuple[str, str, str | None]] = []
        self.log_flush_timer = QTimer(self)
        self.log_flush_timer.setInterval(5)
        self.log_flush_timer.timeout.connect(self._flush_pending_logs)

        self.setWindowTitle(APP_NAME)
        self.resize(900, 650)
        self.setAcceptDrops(True)
        self._create_menu_bar()

        self.connection_settings_button = QPushButton("Connection Settings")
        self.connection_settings_button.clicked.connect(
            self._open_connection_settings
        )

        self.upload_button = QPushButton("Configure Upload")
        self.upload_button.clicked.connect(lambda: self._open_upload_dialog())

        self.duplicate_check_button = QPushButton("Check Duplicates")
        self.duplicate_check_button.clicked.connect(self._confirm_duplicate_check)

        self.pause_button = QPushButton("Pause")
        self.pause_button.setEnabled(False)
        self.pause_button.clicked.connect(self._toggle_pause)

        self.stop_button = QPushButton("Stop")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self._request_stop)

        self.connection_status_label = QLabel("Karakeep: Not configured")
        self.connection_status_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self.connection_status_label.setMinimumWidth(180)
        self.connection_status_label.setMaximumWidth(360)
        self.connection_status_label.setSizePolicy(
            QSizePolicy.Policy.Preferred,
            QSizePolicy.Policy.Fixed,
        )

        button_layout = QHBoxLayout()
        button_layout.addWidget(self.connection_settings_button)
        button_layout.addWidget(self.upload_button)
        button_layout.addWidget(self.duplicate_check_button)
        button_layout.addSpacing(12)
        button_layout.addWidget(self.pause_button)
        button_layout.addWidget(self.stop_button)
        button_layout.addStretch(1)
        button_layout.addWidget(self.connection_status_label)

        self.console = StatusConsole(Path(__file__).with_name("naut.png"))
        self.console.setAcceptDrops(True)
        self.console.viewport().setAcceptDrops(True)
        self.console.installEventFilter(self)
        self.console.viewport().installEventFilter(self)
        self.console.line_clicked.connect(self._highlight_console_line)
        self.console.setReadOnly(True)
        self.console.setPlaceholderText(
            "Upload activity will appear here."
        )

        console_font = QFont("Consolas")
        console_font.setStyleHint(QFont.StyleHint.Monospace)
        self.console.setFont(console_font)
        self.console_marker_rail = LogMarkerRail()
        self.console_marker_rail.marker_clicked.connect(
            self._scroll_console_to_line
        )
        self.console.document().documentLayout().documentSizeChanged.connect(
            lambda _size: self._schedule_console_marker_metrics_update()
        )

        self.current_file_label = QLabel("Current file: —")
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("%p%")

        self.summary_label = QLabel(
            ""
        )
        self.summary_label.setTextFormat(Qt.TextFormat.RichText)
        self.summary_label.setMinimumWidth(0)
        self.summary_label.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Fixed,
        )
        self.clear_console_button = QPushButton("Clear Console")
        self.clear_console_button.clicked.connect(self._clear_console)

        bottom_status_layout = QHBoxLayout()
        bottom_status_layout.addWidget(self.summary_label, 1)
        bottom_status_layout.addWidget(self.clear_console_button)

        central_widget = QWidget()
        layout = QVBoxLayout(central_widget)
        layout.addLayout(button_layout)

        console_layout = QHBoxLayout()
        console_layout.setContentsMargins(0, 0, 0, 0)
        console_layout.setSpacing(4)
        console_layout.addWidget(self.console, 1)
        console_layout.addWidget(self.console_marker_rail)

        layout.addLayout(console_layout, 1)
        layout.addWidget(self.current_file_label)
        layout.addWidget(self.progress_bar)
        layout.addLayout(bottom_status_layout)

        self.setCentralWidget(central_widget)

        self._restore_window_state()
        self._update_connection_state()
        self._update_summary()
        self._log("KKUpload ready.")
        for message, level in preference_load_result.messages:
            self._log(message, level=level)
        QTimer.singleShot(0, self._prompt_for_connection_if_needed)

    def _create_menu_bar(self) -> None:
        file_menu = self.menuBar().addMenu("&File")

        configure_upload_action = QAction("Configure Upload", self)
        configure_upload_action.setShortcut("Ctrl+U")
        configure_upload_action.triggered.connect(
            lambda: self._open_upload_dialog()
        )
        file_menu.addAction(configure_upload_action)

        check_duplicates_action = QAction("Check Duplicates", self)
        check_duplicates_action.triggered.connect(self._confirm_duplicate_check)
        file_menu.addAction(check_duplicates_action)

        pause_action = QAction("Pause / Unpause", self)
        pause_action.setShortcut("Ctrl+Space")
        pause_action.triggered.connect(self._toggle_pause)
        file_menu.addAction(pause_action)

        file_menu.addSeparator()

        save_log_action = QAction("Save Log", self)
        save_log_action.setShortcut("Ctrl+S")
        save_log_action.triggered.connect(self._save_log)
        file_menu.addAction(save_log_action)

        save_log_as_action = QAction("Save Log As...", self)
        save_log_as_action.setShortcut("Ctrl+Shift+S")
        save_log_as_action.triggered.connect(self._save_log_as)
        file_menu.addAction(save_log_as_action)

        file_menu.addSeparator()

        exit_action = QAction("Exit", self)
        exit_action.triggered.connect(self.close)
        file_menu.addAction(exit_action)

        edit_menu = self.menuBar().addMenu("&Edit")
        preferences_action = QAction("Preferences", self)
        preferences_action.triggered.connect(self._open_preferences)
        edit_menu.addAction(preferences_action)

        search_menu = self.menuBar().addMenu("&Search")
        find_action = QAction("Find", self)
        find_action.setShortcut("Ctrl+F")
        find_action.triggered.connect(self._open_find_dialog)
        search_menu.addAction(find_action)

        search_menu.addSeparator()

        self._add_navigation_menu_action(
            search_menu,
            "Prev Issue",
            "Ctrl+Comma",
            "Ctrl+,",
            "ISSUE",
            -1,
        )

        self._add_navigation_menu_action(
            search_menu,
            "Next Issue",
            "Ctrl+Period",
            "Ctrl+.",
            "ISSUE",
            1,
        )

        self._add_navigation_menu_action(
            search_menu,
            "Prev Error",
            "Ctrl+Shift+Comma",
            "Ctrl+Shift+,",
            "ERROR",
            -1,
        )

        self._add_navigation_menu_action(
            search_menu,
            "Next Error",
            "Ctrl+Shift+Period",
            "Ctrl+Shift+.",
            "ERROR",
            1,
        )

        help_menu = self.menuBar().addMenu("&Help")
        keyboard_shortcuts_action = QAction("Keyboard Shortcuts", self)
        keyboard_shortcuts_action.triggered.connect(self._show_keyboard_shortcuts)
        help_menu.addAction(keyboard_shortcuts_action)

        help_menu.addSeparator()

        about_action = QAction("About", self)
        about_action.triggered.connect(self._show_about)
        help_menu.addAction(about_action)

    def _add_navigation_menu_action(
        self,
        menu,
        label: str,
        display_shortcut: str,
        keyboard_shortcut: str,
        marker_group: str,
        direction: int,
    ) -> None:
        def navigate() -> None:
            self._navigate_console_marker(marker_group, direction)

        menu_action = QAction(f"{label}\t{display_shortcut}", self)
        menu_action.triggered.connect(navigate)
        menu.addAction(menu_action)

        shortcut_action = QAction(self)
        shortcut_action.setShortcut(keyboard_shortcut)
        shortcut_action.setShortcutContext(Qt.ShortcutContext.ApplicationShortcut)
        shortcut_action.triggered.connect(navigate)
        self.addAction(shortcut_action)

    def _open_find_dialog(self) -> None:
        if self.find_dialog is None:
            self.find_dialog = FindDialog(self.console.toPlainText, self)
            self.find_dialog.finished.connect(self._clear_find_dialog_reference)

        self.find_dialog.show()
        self._position_find_dialog()
        self.find_dialog.raise_()
        self.find_dialog.activateWindow()
        self.find_dialog.focus_search_field()

    def _position_find_dialog(self) -> None:
        if self.find_dialog is None:
            return

        horizontal_inset = 132
        vertical_inset = 8
        viewport = self.console.viewport()
        viewport_top_left = viewport.mapToGlobal(viewport.rect().topLeft())
        dialog_size = self.find_dialog.sizeHint()
        x = (
            viewport_top_left.x()
            + viewport.width()
            - dialog_size.width()
            - horizontal_inset
        )
        y = viewport_top_left.y() + vertical_inset
        self.find_dialog.move(
            max(x, viewport_top_left.x() + horizontal_inset),
            y,
        )

    def _clear_find_dialog_reference(self, *_args) -> None:  # type: ignore[no-untyped-def]
        self.find_dialog = None

    def _open_preferences(self) -> bool:
        dialog = PreferencesDialog(self.preferences, self)

        if dialog.exec() != QDialog.DialogCode.Accepted:
            self._log("Preferences canceled.")
            return False

        try:
            path = save_preferences(Path(__file__), dialog.preferences)
        except OSError as exc:
            QMessageBox.critical(
                self,
                "Unable to Save Preferences",
                f"Could not save preferences:\n\n{exc}",
            )
            self._log(f"Unable to save preferences: {exc}", level="ERROR")
            return False

        self.preferences = dialog.preferences
        self._apply_view_mode(self.preferences.view_mode)
        self._log(f"Preferences saved: {path}", level="SUCCESS")
        return True

    def _apply_view_mode(self, view_mode: str) -> None:
        app = QApplication.instance()
        if app is None:
            return

        normalized = view_mode.lower()
        if normalized == "dark":
            app.setPalette(self._dark_palette())
            app.setStyleSheet("")
        elif normalized == "light":
            app.setPalette(self._light_palette())
            app.setStyleSheet(self._light_stylesheet())
        else:
            if DEFAULT_APP_PALETTE is not None:
                app.setPalette(DEFAULT_APP_PALETTE)
            app.setStyleSheet("")

        if hasattr(self, "console_marker_rail"):
            self.console_marker_rail.update()

    @staticmethod
    def _light_palette() -> QPalette:
        palette = QPalette()
        palette.setColor(QPalette.ColorRole.Window, QColor("#f5f5f5"))
        palette.setColor(QPalette.ColorRole.WindowText, QColor("#202124"))
        palette.setColor(QPalette.ColorRole.Base, QColor("#ffffff"))
        palette.setColor(QPalette.ColorRole.AlternateBase, QColor("#f1f3f4"))
        palette.setColor(QPalette.ColorRole.Text, QColor("#202124"))
        palette.setColor(QPalette.ColorRole.Button, QColor("#f1f3f4"))
        palette.setColor(QPalette.ColorRole.ButtonText, QColor("#202124"))
        palette.setColor(QPalette.ColorRole.ToolTipBase, QColor("#ffffff"))
        palette.setColor(QPalette.ColorRole.ToolTipText, QColor("#202124"))
        palette.setColor(QPalette.ColorRole.Highlight, QColor("#b9d8ee"))
        palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#000000"))
        palette.setColor(QPalette.ColorRole.Link, QColor("#0b57d0"))
        return palette

    @staticmethod
    def _dark_palette() -> QPalette:
        palette = QPalette()
        palette.setColor(QPalette.ColorRole.Window, QColor("#1f1f1f"))
        palette.setColor(QPalette.ColorRole.WindowText, QColor("#f1f5f9"))
        palette.setColor(QPalette.ColorRole.Base, QColor("#242424"))
        palette.setColor(QPalette.ColorRole.AlternateBase, QColor("#303030"))
        palette.setColor(QPalette.ColorRole.Text, QColor("#f1f5f9"))
        palette.setColor(QPalette.ColorRole.Button, QColor("#303030"))
        palette.setColor(QPalette.ColorRole.ButtonText, QColor("#f1f5f9"))
        palette.setColor(QPalette.ColorRole.ToolTipBase, QColor("#303030"))
        palette.setColor(QPalette.ColorRole.ToolTipText, QColor("#f1f5f9"))
        palette.setColor(QPalette.ColorRole.Highlight, QColor("#2f6f9f"))
        palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
        palette.setColor(QPalette.ColorRole.Link, QColor("#7dd3fc"))
        return palette

    @staticmethod
    def _light_stylesheet() -> str:
        return """
            QMenuBar {
                background: #f5f5f5;
                color: #202124;
            }
            QMenuBar::item {
                background: transparent;
                color: #202124;
                padding: 4px 10px;
            }
            QMenuBar::item:selected {
                background: #e8eaed;
            }
            QMenu {
                background: #ffffff;
                color: #202124;
                border: 1px solid #dadce0;
                min-width: 230px;
            }
            QMenu::item {
                color: #202124;
                padding: 5px 48px 5px 14px;
            }
            QMenu::item:selected {
                background: #e8f0fe;
                color: #202124;
            }
            QPushButton {
                background: #f1f3f4;
                color: #202124;
                border: 1px solid #c7c7c7;
                border-radius: 4px;
                padding: 4px 10px;
            }
            QPushButton:hover {
                background: #e8eaed;
            }
            QPushButton:disabled {
                color: #6b7280;
                background: #eeeeee;
                border-color: #d7d7d7;
            }
            QLineEdit,
            QComboBox,
            QSpinBox,
            QDoubleSpinBox {
                background: #ffffff;
                color: #202124;
                border: 1px solid #b8b8b8;
                border-radius: 4px;
                padding: 3px 6px;
                selection-background-color: #b9d8ee;
                selection-color: #000000;
            }
            QComboBox:disabled,
            QLineEdit:disabled,
            QSpinBox:disabled,
            QDoubleSpinBox:disabled {
                background: #eeeeee;
                color: #6b7280;
                border-color: #d7d7d7;
            }
            QComboBox QAbstractItemView {
                background: #ffffff;
                color: #202124;
                selection-background-color: #b9d8ee;
                selection-color: #000000;
            }
            QPlainTextEdit {
                selection-background-color: #b9d8ee;
                selection-color: #000000;
            }
        """

    @staticmethod
    def _dark_stylesheet() -> str:
        return """
            QMenuBar {
                background: #1f1f1f;
                color: #f1f5f9;
            }
            QMenuBar::item {
                background: transparent;
                color: #f1f5f9;
                padding: 4px 10px;
            }
            QMenuBar::item:selected {
                background: #303030;
            }
            QMenu {
                background: #242424;
                color: #f1f5f9;
                border: 1px solid #3f3f46;
                min-width: 230px;
            }
            QMenu::item {
                color: #f1f5f9;
                padding: 5px 48px 5px 14px;
            }
            QMenu::item:selected {
                background: #2f6f9f;
                color: #ffffff;
            }
            QPushButton {
                background: #303030;
                color: #f1f5f9;
                border: 1px solid #52525b;
                border-radius: 4px;
                padding: 4px 10px;
            }
            QPushButton:hover {
                background: #3f3f46;
            }
            QPushButton:disabled {
                color: #a1a1aa;
                background: #27272a;
                border-color: #3f3f46;
            }
        """

    def _open_connection_settings(self) -> bool:
        dialog = ConnectionSettingsDialog(
            self.settings,
            self,
            log_callback=self._log,
        )

        if dialog.exec() != QDialog.DialogCode.Accepted:
            self._log("Connection settings canceled.")
            self._update_connection_state()
            return False

        self._log("Connection settings saved.")
        self._update_connection_state()
        return True

    def _save_log(self) -> None:
        if self.current_log_path is None:
            self._save_log_as()
            return

        self._write_log_to_path(self.current_log_path)

    def _save_log_as(self) -> None:
        default_name = (
            f"kkupload-log-{datetime.now().strftime('%Y%m%d-%H%M%S')}.txt"
        )
        default_path = str(Path.home() / "Documents" / default_name)
        selected, _selected_filter = QFileDialog.getSaveFileName(
            self,
            "Save Log As",
            default_path,
            "Text files (*.txt);;All files (*.*)",
        )

        if not selected:
            return

        self.current_log_path = Path(selected)
        self._write_log_to_path(self.current_log_path)

    def _write_log_to_path(self, path: Path) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(self.console.toPlainText(), encoding="utf-8")
        except OSError as exc:
            QMessageBox.critical(
                self,
                "Unable to Save Log",
                f"Could not save the log file:\n\n{path}\n\n{exc}",
            )
            return

        self._log(f"Log saved: {path}", level="SUCCESS")

    def _show_not_implemented(self, feature_name: str) -> None:
        QMessageBox.information(
            self,
            feature_name,
            f"{feature_name} is not implemented yet.",
        )

    def _show_about(self) -> None:
        QMessageBox.about(
            self,
            "About KKUpload",
            (
                "KKUpload\n\n"
                "A desktop uploader for organizing local media in Karakeep.\n\n"
                "Designed by Brad Wiggins."
            ),
        )

    def _show_keyboard_shortcuts(self) -> None:
        message = (
            "<table>"
            "<tr><td><b>Configure Upload</b></td><td>&nbsp;&nbsp;Ctrl+U</td></tr>"
            "<tr><td><b>Pause / Unpause</b></td><td>&nbsp;&nbsp;Ctrl+Space</td></tr>"
            "<tr><td><b>Save Log</b></td><td>&nbsp;&nbsp;Ctrl+S</td></tr>"
            "<tr><td><b>Save Log As</b></td><td>&nbsp;&nbsp;Ctrl+Shift+S</td></tr>"
            "<tr><td><b>Find</b></td><td>&nbsp;&nbsp;Ctrl+F</td></tr>"
            "<tr><td><b>Prev Issue</b></td><td>&nbsp;&nbsp;Ctrl+Comma</td></tr>"
            "<tr><td><b>Next Issue</b></td><td>&nbsp;&nbsp;Ctrl+Period</td></tr>"
            "<tr><td><b>Prev Error</b></td><td>&nbsp;&nbsp;Ctrl+Shift+Comma</td></tr>"
            "<tr><td><b>Next Error</b></td><td>&nbsp;&nbsp;Ctrl+Shift+Period</td></tr>"
            "</table>"
        )
        QMessageBox.information(
            self,
            "Keyboard Shortcuts",
            message,
        )

    def _prompt_for_connection_if_needed(self) -> None:
        if self._has_connection_settings():
            return

        self._log("Connection settings are required before uploading.")
        self._open_connection_settings()

    def _has_connection_settings(self) -> bool:
        server_url = str(
            self.settings.value("connection/server_url", "") or ""
        ).strip()

        try:
            api_key = (
                keyring.get_password(KEYRING_SERVICE, KEYRING_USERNAME)
                or ""
            )
        except keyring.errors.KeyringError:
            api_key = ""

        return bool(server_url and api_key)

    def _update_connection_state(self) -> None:
        has_settings = self._has_connection_settings()
        self.connection_settings_button.setEnabled(not self.is_running)
        self.upload_button.setEnabled(has_settings and not self.is_running)
        self.duplicate_check_button.setEnabled(has_settings and not self.is_running)

        if has_settings:
            server_url = str(
                self.settings.value("connection/server_url", "") or ""
            ).strip()
            self.connection_status_text = server_url
            self.connection_status_label.setToolTip(server_url)
            self.connection_status_label.setContentsMargins(0, 0, 18, 0)
        else:
            self.connection_status_text = "Karakeep: Not configured"
            self.connection_status_label.setToolTip("")
            self.connection_status_label.setContentsMargins(0, 0, 0, 0)
        self._update_connection_status_label_text()

    def _update_connection_status_label_text(self) -> None:
        available_width = max(self.connection_status_label.width() - 8, 40)
        elided = self.connection_status_label.fontMetrics().elidedText(
            self.connection_status_text,
            Qt.TextElideMode.ElideLeft,
            available_width,
        )
        self.connection_status_label.setText(elided)

    def eventFilter(self, watched, event) -> bool:  # type: ignore[no-untyped-def]
        if watched in {self.console, self.console.viewport()}:
            if event.type() in {
                QEvent.Type.DragEnter,
                QEvent.Type.DragMove,
            }:
                if event.mimeData().hasUrls():
                    event.acceptProposedAction()
                    return True

            if event.type() == QEvent.Type.Drop:
                self._handle_upload_folder_drop(event)
                return True

        return super().eventFilter(watched, event)

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dragMoveEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:  # noqa: N802
        self._handle_upload_folder_drop(event)

    def _handle_upload_folder_drop(self, event) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

        paths = self._local_paths_from_drop_event(event)

        if self.is_running or self.is_paused:
            QMessageBox.information(
                self,
                "Batch Already Running",
                (
                    "A batch is already running. Wait until the current "
                    "batch is complete before starting another."
                ),
            )
            return

        if len(paths) != 1:
            QMessageBox.information(
                self,
                "Drop One Folder",
                "Drop a single folder to configure an upload batch.",
            )
            return

        dropped_path = paths[0]
        if not dropped_path.is_dir():
            QMessageBox.information(
                self,
                "Drop a Folder",
                "KKUpload drag-and-drop currently accepts folders only.",
            )
            return

        self._open_upload_dialog(locked_upload_folder=dropped_path.resolve())

    @staticmethod
    def _local_paths_from_drop_event(event) -> list[Path]:  # type: ignore[no-untyped-def]
        paths: list[Path] = []
        for url in event.mimeData().urls():
            if not url.isLocalFile():
                continue
            local_file = url.toLocalFile()
            if local_file:
                paths.append(Path(local_file))
        return paths

    def _open_upload_dialog(
        self,
        locked_upload_folder: Path | None = None,
    ) -> None:
        if not self._has_connection_settings():
            self._log("Configure Karakeep connection settings before uploading.")
            self._open_connection_settings()
            return

        dialog = UploadDialog(
            self.settings,
            self,
            locked_upload_folder=locked_upload_folder,
        )

        if dialog.exec() != QDialog.DialogCode.Accepted:
            self._log("Upload configuration canceled.")
            return

        if dialog.configuration is None:
            self._log("Upload configuration was not returned.")
            return

        self.configuration = dialog.configuration
        self._start_upload_job()

    def _confirm_duplicate_check(self) -> None:
        if not self._has_connection_settings():
            self._log("Configure Karakeep connection settings before checking duplicates.")
            self._open_connection_settings()
            return

        response = QMessageBox.question(
            self,
            "Check Duplicates",
            (
                "This feature connects to the Karakeep server and highlights "
                "possible duplicate bookmarks and media for review.\n\n"
                "It compares the hash of each server asset, so it can take a "
                "long time on large libraries. Exact media/file duplicates are "
                "reliable; whole-bookmark duplicates only make sense as a "
                "separate URL/text comparison and are not included in this "
                "scan yet.\n\n"
                "Start checking for duplicates?"
            ),
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if response != QMessageBox.StandardButton.Ok:
            self._log("Duplicate check canceled.")
            return

        self._start_duplicate_check_job()

    def _start_duplicate_check_job(self) -> None:
        server_url = str(
            self.settings.value("connection/server_url", "") or ""
        ).strip()

        try:
            api_key = (
                keyring.get_password(KEYRING_SERVICE, KEYRING_USERNAME)
                or ""
            )
        except keyring.errors.KeyringError as exc:
            self._log(f"Unable to read API key: {exc}", level="ERROR")
            return

        self._log(
            "================================",
            message_color="START",
            include_level=False,
        )
        self._log("STARTING DUPLICATE CHECK.", message_color="START")
        self._log(
            "================================",
            message_color="START",
            include_level=False,
        )

        self.configuration = None
        self.current_index = 0
        self.succeeded_count = 0
        self.failed_count = 0
        self.not_processed_count = 0
        self.resolved_conflict_count = 0
        self.unsupported_count = 0
        self.failed_files = []
        self.duplicate_stats = {
            "scanned": 0,
            "hashed": 0,
            "hash_remaining": 0,
            "compared": 0,
            "compare_remaining": 0,
            "matching": 0,
        }
        self.pending_dry_run_estimate_lines = []
        self.completed_operations = 0
        self.total_operations = 0
        self.total_files = 0
        self.job_started_at = time.monotonic()
        self.stop_requested = False
        self.is_paused = False
        self.is_running = True
        self.current_job_kind = "duplicates"

        self.upload_button.setEnabled(False)
        self.duplicate_check_button.setEnabled(False)
        self.connection_settings_button.setEnabled(False)
        self.pause_button.setEnabled(True)
        self.pause_button.setText("Pause")
        self.stop_button.setEnabled(True)
        self.clear_console_button.setEnabled(False)

        self.progress_bar.setRange(0, 0)
        self.progress_bar.setValue(0)
        self.current_file_label.setText("Current operation: preparing duplicate scan...")
        self._update_summary()
        self._log(f"Karakeep server: {server_url}")
        self._log("Duplicate tags: POTENTIAL_DUPLICATE and PD: X")

        job_config = DuplicateCheckConfig(
            server_url=server_url,
            api_key=api_key,
        )

        self.worker_thread = QThread(self)
        self.worker = DuplicateCheckWorker(job_config)
        self.worker.moveToThread(self.worker_thread)

        self.worker_thread.started.connect(self.worker.run)
        self.worker.log.connect(self._handle_worker_log)
        self.worker.progress_range.connect(self._set_progress_range)
        self.worker.progress.connect(self._set_progress)
        self.worker.stats.connect(self._handle_duplicate_stats)
        self.worker.finished.connect(self._finish_batch)
        self.worker.finished.connect(self.worker_thread.quit)
        self.worker.finished.connect(self.worker.deleteLater)
        self.worker_thread.finished.connect(self.worker_thread.deleteLater)
        self.worker_thread.finished.connect(self._clear_worker_references)

        self.worker_thread.start()

    def _start_upload_job(self) -> None:
        config = self.configuration
        if config is None:
            self._log("Upload configuration was not returned.")
            return

        if (
            not config.dry_run
            and config.dont_move_completed
            and not self._confirm_start_with_completed_files_left_in_place()
        ):
            self._log("Upload canceled before start.")
            return

        server_url = str(
            self.settings.value("connection/server_url", "") or ""
        ).strip()

        try:
            api_key = (
                keyring.get_password(KEYRING_SERVICE, KEYRING_USERNAME)
                or ""
            )
        except keyring.errors.KeyringError as exc:
            self._log(f"Unable to read API key: {exc}", level="ERROR")
            return

        self._log(
            "================================",
            message_color="START",
            include_level=False,
        )
        if config.dry_run:
            self._log(
                "STARTING DRY-RUN.",
                message_color="START",
            )
        else:
            self._log(
                "STARTING UPLOAD.",
                message_color="START",
            )
        self._log(
            "================================",
            message_color="START",
            include_level=False,
        )

        self.current_index = 0
        self.succeeded_count = 0
        self.failed_count = 0
        self.not_processed_count = 0
        self.resolved_conflict_count = 0
        self.unsupported_count = 0
        self.failed_files = []
        self.pending_dry_run_estimate_lines = []
        self.completed_operations = 0
        self.total_operations = 0
        self.total_files = 0
        self.job_started_at = time.monotonic()
        self.stop_requested = False
        self.is_paused = False
        self.is_running = True
        self.current_job_kind = "upload"

        self.upload_button.setEnabled(False)
        self.duplicate_check_button.setEnabled(False)
        self.connection_settings_button.setEnabled(False)
        self.pause_button.setEnabled(True)
        self.pause_button.setText("Pause")
        self.stop_button.setEnabled(True)
        self.clear_console_button.setEnabled(False)

        self.progress_bar.setRange(0, 0)
        self.progress_bar.setValue(0)
        self.current_file_label.setText("Current file: preparing...")
        self._update_summary()

        self._log(f"Found upload folder: {config.upload_folder}")

        if config.dont_move_completed:
            self._log("Completed files will not be moved.")
        elif config.completed_folder is not None and config.completed_folder.exists():
            self._log(
                f"Found completed folder: {config.completed_folder}"
            )
        else:
            self._log(
                "Completed folder will be created when needed: "
                f"{config.completed_folder}"
            )

        if config.dont_move_failed:
            self._log("Failed files will not be moved.")
        elif config.error_folder is not None and config.error_folder.exists():
            self._log(f"Found error folder: {config.error_folder}")
        else:
            self._log(
                f"Error folder will be created when needed: {config.error_folder}"
            )

        if config.dont_move_unsupported:
            self._log("Unsupported files will not be moved.")
        elif (
            config.unsupported_folder is not None
            and config.unsupported_folder.exists()
        ):
            self._log(f"Found unsupported folder: {config.unsupported_folder}")
        else:
            self._log(
                "Unsupported folder will be created when needed: "
                f"{config.unsupported_folder}"
            )

        if config.dont_preserve_move_structure:
            self._log("Move mode: drop files directly into output folders.")
        else:
            self._log("Move mode: preserve relative folder structure.")

        if config.ignore_subfolders:
            self._log("Subfolder mode: ignore child folders.")
        else:
            self._log("Subfolder mode: include child folders.")

        conflict_labels = {
            "ask": "ask before continuing",
            "rename": "rename automatically",
            "overwrite": "overwrite automatically",
            "stop": "stop the upload",
        }
        self._log(
            "Move conflict mode: "
            + conflict_labels.get(config.move_conflict_mode, "ask before continuing")
            + "."
        )

        if config.import_to_root:
            self._log("Destination mode: no import list.")
        else:
            self._log(
                f"Destination root list: {config.root_list}"
            )
        if config.omit_top_folder_list:
            self._log("Top folder list: omitted.")
        else:
            self._log(
                f"Top folder list: {config.upload_folder.name}"
            )

        if config.default_tags:
            self._log(
                "Default tags: " + ", ".join(config.default_tags)
            )
        else:
            self._log("Default tags: none")

        job_config = UploadJobConfig(
            server_url=server_url,
            api_key=api_key,
            upload_folder=config.upload_folder,
            completed_folder=config.completed_folder,
            error_folder=config.error_folder,
            unsupported_folder=config.unsupported_folder,
            dont_move_completed=config.dont_move_completed,
            dont_move_failed=config.dont_move_failed,
            dont_move_unsupported=config.dont_move_unsupported,
            dont_preserve_move_structure=config.dont_preserve_move_structure,
            move_conflict_mode=config.move_conflict_mode,
            resize_images_if_needed=config.resize_images_if_needed,
            ignore_subfolders=config.ignore_subfolders,
            import_to_root=config.import_to_root,
            root_list=config.root_list,
            default_tags=config.default_tags,
            dry_run=config.dry_run,
            omit_top_folder_list=config.omit_top_folder_list,
            image_resize_preferences=self.preferences.image_resize,
            timing_stats_path=default_timing_stats_path(Path(__file__)),
        )

        self.worker_thread = QThread(self)
        self.worker = UploadWorker(job_config)
        self.worker.moveToThread(self.worker_thread)

        self.worker_thread.started.connect(self.worker.run)
        self.worker.log.connect(self._handle_worker_log)
        self.worker.progress_range.connect(self._set_progress_range)
        self.worker.progress.connect(self._set_progress)
        self.worker.move_conflict.connect(self._handle_move_conflict)
        self.worker.conflict_resolved.connect(self._handle_conflict_resolved)
        self.worker.unsupported_found.connect(self._handle_unsupported_found)
        self.worker.failed_file.connect(self._handle_failed_file)
        self.worker.finished.connect(self._finish_batch)
        self.worker.finished.connect(self.worker_thread.quit)
        self.worker.finished.connect(self.worker.deleteLater)
        self.worker_thread.finished.connect(self.worker_thread.deleteLater)
        self.worker_thread.finished.connect(self._clear_worker_references)

        self.worker_thread.start()

    def _handle_move_conflict(self, request: MoveConflictRequest) -> None:
        dialog = MoveConflictDialog(request, self)
        result = dialog.exec()

        if result == QDialog.DialogCode.Accepted:
            request.resolve(
                dialog.choice,
                dialog.apply_to_all_checkbox.isChecked(),
            )
        else:
            request.resolve("stop", False)

    def _handle_worker_log(
        self,
        message: str,
        level: str,
        message_color: object,
    ) -> None:
        color = message_color if isinstance(message_color, str) else None
        if self._should_hold_dry_run_estimate_line(message):
            self.pending_dry_run_estimate_lines.append((message, level, color))
            return

        self.pending_log_entries.append((message, level, color))
        if not self.log_flush_timer.isActive():
            self.log_flush_timer.start()

    def _should_hold_dry_run_estimate_line(self, message: str) -> bool:
        if self.configuration is None or not self.configuration.dry_run:
            return False

        return message.startswith(
            "Estimated non-dry-run batch time:"
        ) or message.startswith("Estimate details:")

    def _handle_failed_file(self, relative_path: str, reason: str) -> None:
        self.failed_files.append((relative_path, reason))
        self.failed_count += 1
        self._update_summary()

    def _handle_conflict_resolved(self, count: int) -> None:
        self.resolved_conflict_count += count
        self._update_summary()

    def _handle_unsupported_found(self, count: int) -> None:
        self.unsupported_count = count
        self._update_summary()

    def _handle_duplicate_stats(self, stats: object) -> None:
        if not isinstance(stats, dict):
            return

        for key in self.duplicate_stats:
            value = stats.get(key)
            if isinstance(value, int):
                self.duplicate_stats[key] = value
        self._update_summary()

    def _set_progress_range(self, total_operations: int, total_files: int) -> None:
        self.total_operations = total_operations
        self.total_files = total_files
        self.progress_bar.setRange(0, total_operations)
        self.progress_bar.setValue(0)
        self._update_summary()

    def _set_progress(
        self,
        completed_operations: int,
        current_operation: str,
        processed_files: int,
    ) -> None:
        self.completed_operations = completed_operations
        self.current_index = processed_files
        self.succeeded_count = max(processed_files - self.failed_count, 0)
        self.progress_bar.setValue(completed_operations)
        self.current_file_label.setText(
            f"Current operation: {current_operation}"
        )
        self._update_summary()

    def _toggle_pause(self) -> None:
        if not self.is_running or self.stop_requested:
            return

        self.is_paused = not self.is_paused
        if self.worker is not None:
            self.worker.set_paused(self.is_paused)

        if self.is_paused:
            self.pause_button.setText("Unpause")
            self.current_file_label.setText("Current operation: paused")
            self._log(
                "Pause requested. No new operations will start until unpaused.",
                level="WARNING",
            )
        else:
            self.pause_button.setText("Pause")
            self._log(f"{self._current_job_label()} unpaused.")

    def _request_stop(self) -> None:
        if not self.is_running:
            return

        if (
            self.current_job_kind == "upload"
            and self.configuration is not None
            and not self.configuration.dry_run
            and self.configuration.dont_move_completed
            and not self._confirm_stop_with_completed_files_left_in_place()
        ):
            self._log("Stop canceled.")
            return

        self.stop_requested = True
        if self.worker is not None:
            self.worker.request_stop()
        self.pause_button.setEnabled(False)
        self.stop_button.setEnabled(False)
        self.stop_button.setText("Stopping...")
        self._log(
            "Stop requested. The current job will stop at "
            "the next safe checkpoint.",
            level="WARNING",
        )

    def _confirm_start_with_completed_files_left_in_place(self) -> bool:
        return (
            QMessageBox.warning(
                self,
                "Do Not Stop This Batch Midway",
                (
                    "This batch is configured to leave successfully uploaded "
                    "files in the upload folder.\n\n"
                    "That is usually fine if the batch runs to completion. "
                    "However, if you stop it before it finishes and later run "
                    "the same folder again, KKUpload cannot reliably tell "
                    "which files were already uploaded. The next run may "
                    "create duplicate uploads.\n\n"
                    "Only start this upload if you intend to let it finish, "
                    "or if you are comfortable handling duplicates yourself.\n\n"
                    "Start this upload anyway?"
                ),
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            == QMessageBox.StandardButton.Yes
        )

    def _confirm_stop_with_completed_files_left_in_place(self) -> bool:
        return (
            QMessageBox.warning(
                self,
                "Stopping Can Cause Duplicate Uploads Later",
                (
                    "This batch is leaving successfully uploaded files in the "
                    "upload folder.\n\n"
                    "Stopping before the batch finishes is unsafe because a "
                    "later run of this same folder may scan those successful "
                    "files again. KKUpload cannot reliably tell which files "
                    "were already uploaded, so the next run may create "
                    "duplicates.\n\n"
                    "Stop this upload anyway?"
                ),
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            == QMessageBox.StandardButton.Yes
        )

    def _clear_console(self) -> None:
        self.pending_log_entries.clear()
        self.pending_dry_run_estimate_lines.clear()
        self.log_flush_timer.stop()
        self.console.clear()
        self.console_marker_rail.clear_markers()
        self.highlighted_console_line = None
        self.current_marker_line = None
        self.current_index = 0
        self.succeeded_count = 0
        self.failed_count = 0
        self.not_processed_count = 0
        self.resolved_conflict_count = 0
        self.unsupported_count = 0
        self.failed_files.clear()
        self.duplicate_stats = {
            "scanned": 0,
            "hashed": 0,
            "hash_remaining": 0,
            "compared": 0,
            "compare_remaining": 0,
            "matching": 0,
        }
        self.completed_operations = 0
        self.total_operations = 0
        self.total_files = 0
        self.job_started_at = None
        self.current_file_label.setText("Current file: -")
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.current_job_kind = None
        self.console.setExtraSelections([])
        self._update_summary()
        self._write_pending_finish_if_ready()

    def _finish_batch(
        self,
        stopped: bool,
        succeeded: int,
        failed: int,
        not_processed: int,
    ) -> None:
        self.is_running = False
        self.succeeded_count = succeeded
        self.failed_count = failed
        self.not_processed_count = not_processed
        self.current_index = succeeded + failed

        self._update_connection_state()
        self.is_paused = False
        self.pause_button.setEnabled(False)
        self.pause_button.setText("Pause")
        self.stop_button.setEnabled(False)
        self.stop_button.setText("Stop")

        self.pending_finish_stopped = stopped
        self.current_file_label.setText("Current file: finalizing log output...")
        self._update_summary()
        self._write_pending_finish_if_ready()

    def _clear_worker_references(self) -> None:
        self.worker = None
        self.worker_thread = None
        self._write_pending_finish_if_ready()

    def _current_job_label(self) -> str:
        if self.current_job_kind == "duplicates":
            return "Duplicate check"
        if self.current_job_kind == "upload":
            return "Upload batch"
        return "Current job"

    def _update_summary(self) -> None:
        if self.current_job_kind == "duplicates":
            self._update_duplicate_summary()
            return

        remaining = max(
            self.total_files - self.current_index,
            0,
        )
        colors = self._console_log_colors()
        failed_text = str(self.failed_count)
        if self.failed_count > 0:
            failed_text = (
                f"<span style='color: {colors['ERROR']};'>"
                f"{self.failed_count}</span>"
            )

        eta_text = self._format_eta()
        eta_color = colors["timestamp"]

        self.summary_label.setText(
            f"Succeeded: {self.succeeded_count}&nbsp;&nbsp;&nbsp;&nbsp;"
            f"Failed: {failed_text}&nbsp;&nbsp;&nbsp;&nbsp;"
            f"Conflicts: {self._format_warning_count(self.resolved_conflict_count)}"
            "&nbsp;&nbsp;&nbsp;&nbsp;"
            f"Unsupported: {self._format_unsupported_count()}&nbsp;&nbsp;&nbsp;&nbsp;"
            f"Remaining: {remaining}&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;"
            f"<span style='color: {eta_color};'>ETA: {eta_text}</span>"
        )

    def _update_duplicate_summary(self) -> None:
        colors = self._console_log_colors()
        eta_text = self._format_eta()
        eta_color = colors["timestamp"]
        matching_text = self._format_warning_count(self.duplicate_stats["matching"])

        self.summary_label.setText(
            f"Scanned: {self.duplicate_stats['scanned']}&nbsp;&nbsp;&nbsp;&nbsp;"
            f"Hashed: {self.duplicate_stats['hashed']}&nbsp;&nbsp;&nbsp;&nbsp;"
            "Remaining to hash: "
            f"{self.duplicate_stats['hash_remaining']}&nbsp;&nbsp;&nbsp;&nbsp;"
            f"Compared: {self.duplicate_stats['compared']}&nbsp;&nbsp;&nbsp;&nbsp;"
            "Remaining to compare: "
            f"{self.duplicate_stats['compare_remaining']}&nbsp;&nbsp;&nbsp;&nbsp;"
            f"Matching: {matching_text}&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;"
            f"<span style='color: {eta_color};'>ETA: {eta_text}</span>"
        )

    def _format_eta(self) -> str:
        if (
            not self.is_running
            or self.job_started_at is None
            or self.completed_operations <= 0
            or self.total_operations <= 0
        ):
            return "--"

        remaining_operations = max(
            self.total_operations - self.completed_operations,
            0,
        )
        if remaining_operations <= 0:
            return "00:00"

        elapsed = max(time.monotonic() - self.job_started_at, 0.1)
        seconds_per_operation = elapsed / self.completed_operations
        remaining_seconds = int(round(seconds_per_operation * remaining_operations))
        return self._format_duration(remaining_seconds)

    @staticmethod
    def _format_duration(total_seconds: int) -> str:
        total_seconds = max(total_seconds, 0)
        hours, remainder = divmod(total_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        if hours:
            return f"{hours:d}:{minutes:02d}:{seconds:02d}"
        return f"{minutes:02d}:{seconds:02d}"

    def _format_total_elapsed_time(self) -> str:
        if self.job_started_at is None:
            return "--"

        elapsed_seconds = int(round(max(time.monotonic() - self.job_started_at, 0)))
        return self._format_duration(elapsed_seconds)

    def _format_unsupported_count(self) -> str:
        return self._format_warning_count(self.unsupported_count)

    def _format_warning_count(self, count: int) -> str:
        if count <= 0:
            return "0"

        warning_color = self._console_log_colors()["WARNING"]
        return (
            f"<span style='color: {warning_color};'>"
            f"{count}</span>"
        )

    def _log_completion_summary(
        self,
        include_not_processed: bool = False,
    ) -> None:
        if self.current_job_kind == "duplicates":
            self._log(f"Potential duplicate groups: {self.succeeded_count}")
            if self.failed_count > 0:
                self._log(
                    f"Duplicate check errors: {self.failed_count}",
                    message_color="ERROR",
                )
            self._log_blank_lines(2)
            return

        total = self.total_files
        self._log(f"Successful: {self.succeeded_count} / {total}")

        if self.failed_count > 0:
            self._log(
                f"Failed: {self.failed_count} / {total}",
                message_color="ERROR",
            )
        else:
            self._log(f"Failed: {self.failed_count} / {total}")

        if self.pending_dry_run_estimate_lines:
            for message, level, color in self.pending_dry_run_estimate_lines:
                self._log(message, level=level, message_color=color)
            self.pending_dry_run_estimate_lines.clear()

        if include_not_processed:
            self._log(
                f"Not processed: {self.not_processed_count} / {total}"
            )

        if self.failed_files:
            self._log("Failed files:", message_color="ERROR")
            for failed_file, reason in self.failed_files:
                self._log(
                    f"- {failed_file} ({reason})",
                    message_color="ERROR",
                )

        self._log_blank_lines(2)

    def _log_blank_lines(self, count: int) -> None:
        should_auto_scroll = self._console_should_auto_scroll()
        cursor = self.console.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)

        for _ in range(count):
            cursor.insertBlock()

        self._schedule_console_marker_metrics_update()
        if should_auto_scroll:
            scrollbar = self.console.verticalScrollBar()
            scrollbar.setValue(scrollbar.maximum())

    def _flush_pending_logs(self, flush_all: bool = False) -> None:
        if flush_all:
            entries_to_flush = len(self.pending_log_entries)
        else:
            entries_to_flush = min(len(self.pending_log_entries), 500)

        for _ in range(entries_to_flush):
            message, level, color = self.pending_log_entries.popleft()
            self._log(
                message,
                level=level,
                message_color=color,
                include_level=bool(level),
            )

        if not self.pending_log_entries:
            self.log_flush_timer.stop()
            self._schedule_console_marker_metrics_update()
            self._write_pending_finish_if_ready()

    def _write_pending_finish_if_ready(self) -> None:
        if self.pending_log_entries:
            if not self.log_flush_timer.isActive():
                self.log_flush_timer.start()
            return

        if self.pending_finish_stopped is None:
            return

        stopped = self.pending_finish_stopped
        self.pending_finish_stopped = None

        if stopped:
            self.current_file_label.setText("Current file: stopped")
            self._log(
                f"{self._current_job_label().upper()} STOPPED!",
                level="WARNING",
                message_color="WARNING",
            )
            self._log(f"Total time: {self._format_total_elapsed_time()}")
            self._log_completion_summary(include_not_processed=True)
            self.clear_console_button.setEnabled(True)
        else:
            self.current_file_label.setText("Current file: complete")
            if self.current_job_kind == "duplicates":
                complete_message = "DUPLICATE CHECK COMPLETE!"
            else:
                complete_message = (
                    "DRY-RUN COMPLETE!"
                    if self.configuration is not None and self.configuration.dry_run
                    else "UPLOAD BATCH COMPLETE!"
                )
            self._log(
                complete_message,
                level="SUCCESS",
                message_color="SUCCESS",
            )
            self._log(f"Total time: {self._format_total_elapsed_time()}")
            self._log_completion_summary()
            self.clear_console_button.setEnabled(True)

        self.current_job_kind = None

    def _log(
        self,
        message: str,
        level: str = "INFO",
        message_color: str | None = None,
        include_prefix: bool = True,
        include_level: bool = True,
    ) -> None:
        should_auto_scroll = self._console_should_auto_scroll()
        timestamp = datetime.now().strftime("%H:%M:%S")
        colors = self._console_log_colors()
        default_format = QTextCharFormat()

        timestamp_format = QTextCharFormat()
        timestamp_format.setForeground(QColor(colors["timestamp"]))

        level_format = QTextCharFormat()
        level_format.setForeground(
            QColor(colors.get(level, colors["default"]))
        )

        message_format = QTextCharFormat()
        if message_color is not None:
            message_format.setForeground(
                QColor(colors.get(message_color, colors["default"]))
            )

        cursor = self.console.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)

        if not self.console.document().isEmpty():
            cursor.insertBlock()

        if include_prefix:
            cursor.insertText(f"[{timestamp}] ", timestamp_format)
            if include_level:
                cursor.insertText(f"[{level}] ", level_format)

        if message.startswith("###### "):
            cursor.insertText("###### ", timestamp_format)
            cursor.insertText(
                message.removeprefix("###### "),
                message_format if message_color is not None else default_format,
            )
        else:
            cursor.insertText(
                message,
                message_format if message_color is not None else default_format,
            )

        total_lines = self.console.document().blockCount()
        marker_type = self._marker_type_for_log(message, level, message_color)
        if marker_type is not None:
            self.console_marker_rail.add_marker(total_lines - 1, marker_type)
        self._schedule_console_marker_metrics_update()

        if should_auto_scroll:
            scrollbar = self.console.verticalScrollBar()
            scrollbar.setValue(scrollbar.maximum())

    def _console_should_auto_scroll(self) -> bool:
        scrollbar = self.console.verticalScrollBar()
        return scrollbar.value() >= scrollbar.maximum() - 2

    def _scroll_console_to_line(self, line_number: int) -> None:
        document = self.console.document()
        block = document.findBlockByNumber(line_number)
        if not block.isValid():
            return

        self.current_marker_line = line_number
        cursor = QTextCursor(block)
        self.console.setTextCursor(cursor)
        self.console.centerCursor()
        self._highlight_console_line(line_number)

    def _highlight_console_line(self, line_number: int) -> None:
        document = self.console.document()
        block = document.findBlockByNumber(line_number)
        if not block.isValid():
            self.highlighted_console_line = None
            self.console.setExtraSelections([])
            return

        self.highlighted_console_line = line_number
        self.current_marker_line = line_number
        selection = QTextEdit.ExtraSelection()
        selection.cursor = QTextCursor(block)
        selection.cursor.clearSelection()

        highlight_format = QTextCharFormat()
        highlight_format.setBackground(QColor(self._console_highlight_color()))
        highlight_format.setProperty(
            QTextFormat.Property.FullWidthSelection,
            True,
        )
        selection.format = highlight_format
        self.console.setExtraSelections([selection])

    def _console_highlight_color(self) -> str:
        base_color = self.console.palette().base().color()
        if base_color.lightness() < 128:
            return "#2f6f9f"

        return "#b9d8ee"

    def _navigate_console_marker(self, marker_group: str, direction: int) -> None:
        markers = self._markers_for_navigation(marker_group)
        if not markers:
            return

        direction = 1 if direction >= 0 else -1

        current_line = self.current_marker_line
        if current_line is None:
            next_index = 0 if direction > 0 else len(markers) - 1
        elif direction > 0:
            next_index = next(
                (
                    index
                    for index, marker in enumerate(markers)
                    if marker[0] > current_line
                ),
                0,
            )
        else:
            next_index = next(
                (
                    index
                    for index in range(len(markers) - 1, -1, -1)
                    if markers[index][0] < current_line
                ),
                len(markers) - 1,
            )

        line_number = markers[next_index][0]
        self._scroll_console_to_line(line_number)

    def _markers_for_navigation(
        self,
        marker_group: str,
    ) -> tuple[tuple[int, str], ...]:
        if marker_group == "ERROR":
            wanted = {"ERROR"}
        else:
            wanted = {"ERROR", "WARNING"}

        seen_lines: set[int] = set()
        markers: list[tuple[int, str]] = []
        for line_number, marker_type in self.console_marker_rail.markers():
            if marker_type not in wanted or line_number in seen_lines:
                continue
            seen_lines.add(line_number)
            markers.append((line_number, marker_type))

        return tuple(sorted(markers, key=lambda marker: marker[0]))

    @staticmethod
    def _marker_type_for_log(
        message: str,
        level: str,
        message_color: str | None,
    ) -> str | None:
        if level == "ERROR" or message_color == "ERROR":
            return "ERROR"
        if level == "WARNING" or message_color == "WARNING":
            return "WARNING"
        if (
            message_color == "SUCCESS"
            and message
            in {
                "UPLOAD BATCH COMPLETE!",
                "DRY-RUN COMPLETE!",
            }
        ):
            return "SUCCESS"
        if message_color == "START":
            return "START"
        return None

    def _visible_console_lines(self) -> int:
        line_height = max(self.console.fontMetrics().lineSpacing(), 1)
        return max(self.console.viewport().height() // line_height, 1)

    def _update_console_marker_metrics(self) -> None:
        self.marker_metrics_update_scheduled = False
        self.console_marker_rail.set_line_metrics(
            self.console.document().blockCount(),
            self._visible_console_lines(),
        )
        self.console_marker_rail.update()

    def _schedule_console_marker_metrics_update(self) -> None:
        if self.marker_metrics_update_scheduled:
            return

        self.marker_metrics_update_scheduled = True
        QTimer.singleShot(0, self._update_console_marker_metrics)
        QTimer.singleShot(50, self._update_console_marker_metrics)

    def _console_log_colors(self) -> dict[str, str]:
        base_color = self.console.palette().base().color()
        is_dark_mode = base_color.lightness() < 128

        if is_dark_mode:
            return {
                "timestamp": "#9ca3af",
                "default": "#e5e7eb",
                "INFO": "#e5e7eb",
                "SUCCESS": "#4ade80",
                "ERROR": "#f87171",
                "WARNING": "#facc15",
                "GOLD": "#facc15",
                "START": "#7dd3fc",
            }

        return {
            "timestamp": "#6b7280",
            "default": "#111827",
            "INFO": "#111827",
            "SUCCESS": "#15803d",
            "ERROR": "#b91c1c",
            "WARNING": "#ca8a04",
            "GOLD": "#b45309",
            "START": "#0284c7",
        }

    def _restore_window_state(self) -> None:
        geometry = self.settings.value("window/geometry")
        window_state = self.settings.value("window/state")

        if geometry is not None:
            self.restoreGeometry(geometry)

        if window_state is not None:
            self.restoreState(window_state)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        if hasattr(self, "connection_status_label"):
            self._update_connection_status_label_text()
        if hasattr(self, "console_marker_rail"):
            self._update_console_marker_metrics()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.is_running:
            response = QMessageBox.question(
                self,
                "Upload in Progress",
                (
                    "An upload job is still running.\n\n"
                    "Stop it and close KKUpload?"
                ),
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )

            if response != QMessageBox.StandardButton.Yes:
                event.ignore()
                return

            if self.worker is not None:
                self.worker.request_stop()
            if self.worker_thread is not None:
                self.worker_thread.quit()
                self.worker_thread.wait(3000)
            self.is_running = False

        self.settings.setValue(
            "window/geometry",
            self.saveGeometry(),
        )
        self.settings.setValue(
            "window/state",
            self.saveState(),
        )
        self.settings.sync()

        event.accept()


def main() -> int:
    global DEFAULT_APP_PALETTE

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName(ORGANIZATION_NAME)
    DEFAULT_APP_PALETTE = app.palette()

    window = MainWindow()
    window.show()

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
