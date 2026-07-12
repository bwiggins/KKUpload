from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final

from PySide6.QtCore import QSettings, QTimer, Qt
from PySide6.QtGui import (
    QColor,
    QCloseEvent,
    QFont,
    QPainter,
    QPixmap,
    QTextCharFormat,
    QTextCursor,
)
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)


APP_NAME: Final[str] = "KKUpload"
ORGANIZATION_NAME: Final[str] = "Brad"
DEFAULT_ROOT_LIST: Final[str] = "IMPORT SORTING"
DEFAULT_TAGS: Final[str] = "!!-TAGGING-!!"
MAX_RECENT_VALUES: Final[int] = 10

SUPPORTED_EXTENSIONS: Final[set[str]] = {
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".gif",
    ".bmp",
    ".tif",
    ".tiff",
}


@dataclass(frozen=True)
class UploadConfiguration:
    upload_folder: Path
    completed_folder: Path
    error_folder: Path
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


class UploadDialog(QDialog):
    """Collects and validates the settings for one upload batch."""

    def __init__(
        self,
        settings: QSettings,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)

        self.settings = settings
        self.configuration: UploadConfiguration | None = None

        self.setWindowTitle("Upload Configuration")
        self.setModal(True)
        self.setMinimumWidth(650)

        self.upload_field = EditableHistoryField(
            settings,
            "history/upload_folders",
            "Select Upload Folder",
        )
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

        self.root_list_combo = QComboBox()
        self.root_list_combo.setEditable(True)
        self.root_list_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self._load_text_history(
            self.root_list_combo,
            "history/root_lists",
            DEFAULT_ROOT_LIST,
        )

        self.import_to_root_checkbox = QCheckBox("Import to root")
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
        self.dry_run_checkbox.setChecked(
            self.settings.value(
                "upload/dry_run",
                True,
                type=bool,
            )
        )

        default_tags_label = QLabel(
            "Default tags:<br><span style='font-size: 9pt; font-style: italic;'>"
            "(comma separated)</span>"
        )
        default_tags_label.setTextFormat(Qt.TextFormat.RichText)

        form_layout = QFormLayout()
        form_layout.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow
        )
        form_layout.addRow("Folder to upload:", self.upload_field)
        form_layout.addRow("Completed folder:", self.completed_field)
        form_layout.addRow("Error folder:", self.error_field)

        root_list_layout = QHBoxLayout()
        root_list_layout.setContentsMargins(0, 0, 0, 0)
        root_list_layout.addWidget(self.root_list_combo, 1)
        root_list_layout.addWidget(self.import_to_root_checkbox)

        form_layout.addRow(QLabel(" "))
        form_layout.addRow("Import to list:", root_list_layout)
        form_layout.addRow(
            default_tags_label,
            self.tags_combo,
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

    def _update_root_list_state(self, import_to_root: bool) -> None:
        self.root_list_combo.setEnabled(not import_to_root)

    def _update_start_button_text(self, dry_run: bool) -> None:
        if dry_run:
            self.start_button.setText("Start Dry-run")
        else:
            self.start_button.setText("Start Upload")

    def _validate_and_accept(self) -> None:
        upload_text = self.upload_field.text()
        completed_text = self.completed_field.text()
        error_text = self.error_field.text()
        root_list = self.root_list_combo.currentText().strip()
        import_to_root = self.import_to_root_checkbox.isChecked()

        if not upload_text:
            self._show_error("An Upload folder is required.")
            return

        if not completed_text:
            self._show_error("A Completed folder is required.")
            return

        if not error_text:
            self._show_error("An Error folder is required.")
            return

        upload_folder = Path(upload_text).expanduser()
        completed_folder = Path(completed_text).expanduser()
        error_folder = Path(error_text).expanduser()

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
                "Enter an Import to list value or select Import to root."
            )
            return

        try:
            resolved_upload = upload_folder.resolve()
            resolved_completed = completed_folder.resolve()
            resolved_error = error_folder.resolve()
        except OSError as exc:
            self._show_error(f"Unable to resolve folder paths:\n\n{exc}")
            return

        if len(
            {
                resolved_upload,
                resolved_completed,
                resolved_error,
            }
        ) != 3:
            self._show_error(
                "Upload, Completed, and Error must be different folders."
            )
            return

        if self._is_inside(resolved_completed, resolved_upload):
            self._show_error(
                "The Completed folder cannot be inside the Upload folder."
            )
            return

        if self._is_inside(resolved_error, resolved_upload):
            self._show_error(
                "The Error folder cannot be inside the Upload folder."
            )
            return

        try:
            completed_folder.mkdir(parents=True, exist_ok=True)
            error_folder.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._show_error(
                f"Unable to create a destination folder:\n\n{exc}"
            )
            return

        tags = tuple(
            tag.strip()
            for tag in self.tags_combo.currentText().split(",")
            if tag.strip()
        )

        self.configuration = UploadConfiguration(
            upload_folder=upload_folder.resolve(),
            completed_folder=completed_folder.resolve(),
            error_folder=error_folder.resolve(),
            import_to_root=import_to_root,
            root_list=root_list,
            default_tags=tags,
            dry_run=self.dry_run_checkbox.isChecked(),
        )

        self._save_values()
        self.accept()

    def _save_values(self) -> None:
        self.upload_field.save_current_value()
        self.completed_field.save_current_value()
        self.error_field.save_current_value()

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
            "upload/dry_run",
            self.dry_run_checkbox.isChecked(),
        )
        self.settings.sync()

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

    @staticmethod
    def _is_inside(candidate: Path, parent: Path) -> bool:
        try:
            candidate.relative_to(parent)
            return True
        except ValueError:
            return False

    def _show_error(self, message: str) -> None:
        QMessageBox.critical(
            self,
            "Invalid Upload Configuration",
            message,
        )


class StatusConsole(QPlainTextEdit):
    """Read-only log console with a subtle bottom-right watermark."""

    def __init__(self, watermark_path: Path, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        self._watermark = QPixmap(str(watermark_path))

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


class MainWindow(QMainWindow):
    """Main KKUpload application window."""

    DUMMY_TOTAL_FILES: Final[int] = 20

    def __init__(self) -> None:
        super().__init__()

        self.settings = QSettings(
            ORGANIZATION_NAME,
            APP_NAME,
        )

        self.configuration: UploadConfiguration | None = None
        self.current_index = 0
        self.succeeded_count = 0
        self.failed_count = 0
        self.stop_requested = False
        self.is_running = False

        self.timer = QTimer(self)
        self.timer.setInterval(250)
        self.timer.timeout.connect(self._process_dummy_step)

        self.setWindowTitle(APP_NAME)
        self.resize(900, 650)

        self.upload_button = QPushButton("Configure Upload")
        self.upload_button.clicked.connect(self._open_upload_dialog)

        self.stop_button = QPushButton("Stop")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self._request_stop)

        button_layout = QHBoxLayout()
        button_layout.addWidget(self.upload_button)
        button_layout.addWidget(self.stop_button)
        button_layout.addStretch(1)

        self.console = StatusConsole(Path(__file__).with_name("naut.png"))
        self.console.setReadOnly(True)
        self.console.setPlaceholderText(
            "Upload activity will appear here."
        )

        console_font = QFont("Consolas")
        console_font.setStyleHint(QFont.StyleHint.Monospace)
        self.console.setFont(console_font)

        self.current_file_label = QLabel("Current file: —")
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("%p%")

        self.summary_label = QLabel(
            "Succeeded: 0    Failed: 0    Remaining: 0"
        )

        central_widget = QWidget()
        layout = QVBoxLayout(central_widget)
        layout.addLayout(button_layout)
        layout.addWidget(self.console, 1)
        layout.addWidget(self.current_file_label)
        layout.addWidget(self.progress_bar)
        layout.addWidget(self.summary_label)

        self.setCentralWidget(central_widget)

        self._restore_window_state()
        self._log("KKUpload ready.")

    def _open_upload_dialog(self) -> None:
        dialog = UploadDialog(self.settings, self)

        if dialog.exec() != QDialog.DialogCode.Accepted:
            self._log("Upload configuration canceled.")
            return

        if dialog.configuration is None:
            self._log("Upload configuration was not returned.")
            return

        self.configuration = dialog.configuration
        self._start_dummy_upload()

    def _start_dummy_upload(self) -> None:
        config = self.configuration
        if config is None:
            self._log("Upload configuration was not returned.")
            return

        if config.dry_run:
            self._log("Starting simulated dry-run.")
        else:
            self._log("Starting simulated upload.")

        self.current_index = 0
        self.succeeded_count = 0
        self.failed_count = 0
        self.stop_requested = False
        self.is_running = True

        self.upload_button.setEnabled(False)
        self.stop_button.setEnabled(True)

        self.progress_bar.setRange(0, self.DUMMY_TOTAL_FILES)
        self.progress_bar.setValue(0)
        self.current_file_label.setText("Current file: preparing...")
        self._update_summary()

        self._log(f"Found upload folder: {config.upload_folder}")

        if config.completed_folder.exists():
            self._log(
                f"Found completed folder: {config.completed_folder}"
            )
        else:
            self._log(
                f"Created completed folder: {config.completed_folder}"
            )

        if config.error_folder.exists():
            self._log(f"Found error folder: {config.error_folder}")
        else:
            self._log(f"Created error folder: {config.error_folder}")

        if config.import_to_root:
            self._log("Destination mode: import to Karakeep root.")
        else:
            self._log(
                f"Destination root list: {config.root_list}"
            )

        if config.default_tags:
            self._log(
                "Default tags: " + ", ".join(config.default_tags)
            )
        else:
            self._log("Default tags: none")

        if config.dry_run:
            self._log(
                f"Dry-run will inspect {self.DUMMY_TOTAL_FILES} simulated files."
            )
        else:
            self._log(
                f"Simulating upload of {self.DUMMY_TOTAL_FILES} files."
            )

        self.timer.start()

    def _process_dummy_step(self) -> None:
        if self.stop_requested:
            self._finish_batch(stopped=True)
            return

        if self.current_index >= self.DUMMY_TOTAL_FILES:
            self._finish_batch(stopped=False)
            return

        self.current_index += 1
        filename = f"sample-image-{self.current_index:03d}.jpg"

        self.current_file_label.setText(
            f"Current file: {filename}"
        )
        self._log(
            f"Simulated upload: {filename}"
        )

        # Produce one predictable dummy failure so the failure display
        # can be reviewed during this first UI milestone.
        if self.current_index == 7:
            self.failed_count += 1
            self._log(
                f"Simulated failure: {filename}",
                level="ERROR",
            )
        else:
            self.succeeded_count += 1
            self._log(
                f"Simulated success: {filename}",
                level="SUCCESS",
            )

        self.progress_bar.setValue(self.current_index)
        self._update_summary()

        if self.current_index >= self.DUMMY_TOTAL_FILES:
            self._finish_batch(stopped=False)

    def _request_stop(self) -> None:
        if not self.is_running:
            return

        self.stop_requested = True
        self.stop_button.setEnabled(False)
        self.stop_button.setText("Stopping...")
        self._log(
            "Stop requested. The simulated batch will stop at "
            "the next safe checkpoint.",
            level="WARNING",
        )

    def _finish_batch(self, stopped: bool) -> None:
        self.timer.stop()
        self.is_running = False

        self.upload_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        self.stop_button.setText("Stop")

        if stopped:
            self.current_file_label.setText("Current file: stopped")
            self._log("Upload batch stopped.", level="WARNING")
        else:
            self.current_file_label.setText("Current file: complete")
            self._log("Upload batch complete.", level="SUCCESS")

        self._update_summary()

    def _update_summary(self) -> None:
        remaining = max(
            self.DUMMY_TOTAL_FILES - self.current_index,
            0,
        )

        self.summary_label.setText(
            f"Succeeded: {self.succeeded_count}    "
            f"Failed: {self.failed_count}    "
            f"Remaining: {remaining}"
        )

    def _log(self, message: str, level: str = "INFO") -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        colors = self._console_log_colors()
        default_format = QTextCharFormat()

        timestamp_format = QTextCharFormat()
        timestamp_format.setForeground(QColor(colors["timestamp"]))

        level_format = QTextCharFormat()
        level_format.setForeground(
            QColor(colors.get(level, colors["default"]))
        )

        cursor = self.console.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)

        if not self.console.document().isEmpty():
            cursor.insertBlock()

        cursor.insertText(f"[{timestamp}] ", timestamp_format)
        cursor.insertText(f"[{level}] ", level_format)
        cursor.insertText(message, default_format)

        scrollbar = self.console.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def _console_log_colors(self) -> dict[str, str]:
        base_color = self.console.palette().base().color()
        is_dark_mode = base_color.lightness() < 128

        if is_dark_mode:
            return {
                "timestamp": "#9ca3af",
                "default": "#e5e7eb",
                "SUCCESS": "#4ade80",
                "ERROR": "#f87171",
            }

        return {
            "timestamp": "#6b7280",
            "default": "#111827",
            "SUCCESS": "#15803d",
            "ERROR": "#b91c1c",
        }

    def _restore_window_state(self) -> None:
        geometry = self.settings.value("window/geometry")
        window_state = self.settings.value("window/state")

        if geometry is not None:
            self.restoreGeometry(geometry)

        if window_state is not None:
            self.restoreState(window_state)

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.is_running:
            response = QMessageBox.question(
                self,
                "Upload in Progress",
                (
                    "A simulated upload is still running.\n\n"
                    "Stop it and close KKUpload?"
                ),
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )

            if response != QMessageBox.StandardButton.Yes:
                event.ignore()
                return

            self.timer.stop()
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
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName(ORGANIZATION_NAME)

    window = MainWindow()
    window.show()

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
