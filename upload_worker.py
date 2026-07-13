from __future__ import annotations

from dataclasses import dataclass, field
import math
from pathlib import Path
import shutil
import tempfile
import threading
from typing import Protocol

import httpx
from PySide6.QtCore import QObject, QThread, Qt, Signal
from PySide6.QtGui import QImage

from failure_inspector import investigate_unsupported_asset
from karakeep_client import KarakeepClient, describe_http_error
from list_planner import (
    ListIndex,
    ListRecord,
    RequiredList,
    build_upload_plan,
    format_list_path,
)
from preferences import ImageResizePreferences
from scanner import scan_upload_folder


@dataclass(frozen=True)
class UploadJobConfig:
    server_url: str
    api_key: str
    upload_folder: Path
    completed_folder: Path | None
    error_folder: Path | None
    dont_move_completed: bool
    dont_move_failed: bool
    dont_preserve_move_structure: bool
    move_conflict_mode: str
    import_to_root: bool
    root_list: str
    default_tags: tuple[str, ...]
    dry_run: bool
    unsupported_folder: Path | None = None
    dont_move_unsupported: bool = False
    omit_top_folder_list: bool = False
    image_resize_preferences: ImageResizePreferences = field(
        default_factory=ImageResizePreferences
    )
    resize_images_if_needed: bool = True


@dataclass
class MoveConflictRequest:
    source_path: Path
    existing_path: Path
    destination_path: Path
    kind: str
    choice: str | None = None
    apply_to_all: bool = False

    def __post_init__(self) -> None:
        self.resolved = threading.Event()

    def resolve(self, choice: str, apply_to_all: bool) -> None:
        self.choice = choice
        self.apply_to_all = apply_to_all
        self.resolved.set()


@dataclass(frozen=True)
class LocalMoveFile:
    file_path: Path
    relative_path: Path


class ClientProtocol(Protocol):
    def list_lists(self) -> tuple[ListRecord, ...]:
        ...

    def create_manual_list(
        self,
        *,
        name: str,
        parent_id: str | None,
    ) -> ListRecord:
        ...

    def upload_asset(self, file_path: Path) -> dict:
        ...

    def create_asset_bookmark(
        self,
        *,
        asset_id: str,
        file_name: str,
    ) -> dict:
        ...

    def add_bookmark_to_list(self, *, list_id: str, bookmark_id: str) -> None:
        ...

    def attach_tags_to_bookmark(
        self,
        *,
        bookmark_id: str,
        tag_names: tuple[str, ...],
    ) -> None:
        ...

    def get_bookmark(self, bookmark_id: str) -> dict:
        ...

    def get_bookmark_lists(self, bookmark_id: str) -> tuple[ListRecord, ...]:
        ...


class UploadWorker(QObject):
    log = Signal(str, str, object)
    progress_range = Signal(int, int)
    progress = Signal(int, str, int)
    move_conflict = Signal(object)
    conflict_resolved = Signal(int)
    unsupported_found = Signal(int)
    failed_file = Signal(str)
    finished = Signal(bool, int, int, int)

    def __init__(
        self,
        config: UploadJobConfig,
        *,
        client: ClientProtocol | None = None,
    ) -> None:
        super().__init__()
        self.config = config
        self._client = client
        self._stop_requested = False
        self._paused = False
        self._move_conflict_policy: str | None = None

    def request_stop(self) -> None:
        self._stop_requested = True

    def set_paused(self, paused: bool) -> None:
        self._paused = paused

    def run(self) -> None:
        succeeded = 0
        failed = 0
        not_processed = 0

        try:
            with tempfile.TemporaryDirectory(prefix="kkupload-") as temp_dir:
                temp_folder = Path(temp_dir)
                client = self._client or KarakeepClient(
                    self.config.server_url,
                    self.config.api_key,
                )

                scan_result = self._scan()
                plan = build_upload_plan(
                    scan_result.supported_files,
                    import_to_root=self.config.import_to_root,
                    root_list=self.config.root_list,
                    top_folder_name=self.config.upload_folder.name,
                    omit_top_folder_list=self.config.omit_top_folder_list,
                )

                total_files = len(plan.planned_files)
                required_lists = plan.required_lists
                pipeline_operation_count = total_files

                if not self.config.dry_run:
                    pipeline_operation_count = sum(
                        self._file_pipeline_operation_count(
                            planned_file,
                            move_kind="completed",
                        )
                        for planned_file in plan.planned_files
                    )

                total_operations = (
                    len(scan_result.scanned_folders)
                    + len(required_lists)
                    + pipeline_operation_count
                )
                self.progress_range.emit(total_operations, total_files)
                completed_operations = 0
                processed_files = 0

                for folder in scan_result.scanned_folders:
                    completed_operations += 1
                    self._log(f"Scanning folder: {folder}")
                    self.progress.emit(
                        completed_operations,
                        f"Scanning folder: {folder}",
                        processed_files,
                    )

                self._log("Scanning step complete.")
                self._log("================================", level="")

                self._log("Retrieving Karakeep lists.")
                existing_lists = client.list_lists()
                self._log(f"Retrieved {len(existing_lists)} Karakeep lists.")
                list_index = ListIndex(existing_lists)

                completed_operations = self._resolve_lists(
                    client,
                    list_index,
                    required_lists,
                    completed_operations,
                    processed_files,
                )
                if completed_operations is None:
                    failed = 1
                    not_processed = total_files
                    self.finished.emit(True, succeeded, failed, not_processed)
                    return

                self._log("List creation step complete.")
                self._log("================================", level="")

                if self.config.dry_run:
                    result = (
                        self._report_dry_run_files(
                            plan.planned_files,
                            total_files,
                            completed_operations,
                            processed_files,
                        )
                    )
                    if result[0] is None:
                        not_processed = total_files - processed_files
                        self.finished.emit(True, 0, failed, not_processed)
                        return

                    succeeded, completed_operations, processed_files = result
                elif plan.planned_files:
                    result = self._upload_files(
                        client,
                        list_index,
                        plan.planned_files,
                        total_files,
                        completed_operations,
                        processed_files,
                        temp_folder,
                    )
                    if result is None:
                        not_processed = max(total_files - processed_files, 0)
                        self.finished.emit(True, succeeded, failed, not_processed)
                        return

                    (
                        succeeded,
                        failed,
                        completed_operations,
                        processed_files,
                        stopped,
                    ) = result
                    not_processed = max(total_files - processed_files, 0)
                    if stopped:
                        self.finished.emit(True, succeeded, failed, not_processed)
                        return
                else:
                    self._log("No supported files found.")

            self.finished.emit(False, succeeded, failed, not_processed)
        except Exception as exc:  # noqa: BLE001
            self._log(f"Batch failed: {exc}", level="ERROR", message_color="ERROR")
            failed += 1
            self.finished.emit(True, succeeded, failed, not_processed)

    def _scan(self):
        self._log(f"Scanning upload folder: {self.config.upload_folder}")
        scan_result = scan_upload_folder(self.config.upload_folder)
        self._log(f"Found {len(scan_result.supported_files)} supported files.")
        self._log(f"Found {len(scan_result.unsupported_files)} unsupported files.")
        for unsupported_file in scan_result.unsupported_files:
            self._log(
                f"Unsupported file: {unsupported_file}",
                level="WARNING",
                message_color="WARNING",
            )
            local_file = LocalMoveFile(
                file_path=unsupported_file,
                relative_path=unsupported_file.relative_to(
                    self.config.upload_folder
                ),
            )
            if self.config.dry_run:
                self._log_planned_move(local_file, "unsupported")
            else:
                try:
                    self._move_processed_file(local_file, "unsupported")
                except Exception as exc:  # noqa: BLE001
                    self._log(
                        "Unsupported file could not be moved: "
                        f"{unsupported_file} ({exc})",
                        level="ERROR",
                        message_color="ERROR",
                    )
            if self._stop_requested:
                break
        self.unsupported_found.emit(len(scan_result.unsupported_files))
        return scan_result

    @staticmethod
    def _format_relative_folder(folder_parts: tuple[str, ...]) -> str:
        if not folder_parts or folder_parts == (".",):
            return "."
        return str(Path(*folder_parts))

    def _file_pipeline_operation_count(
        self,
        file,
        *,
        move_kind: str,
    ) -> int:
        operations = 3  # upload asset, create bookmark, verify
        if file.destination_path:
            operations += 1
        if self.config.default_tags:
            operations += 1
        if self._move_folder_for_kind(move_kind) is not None:
            operations += 1
        return operations

    def _report_dry_run_files(
        self,
        planned_files,
        total_files: int,
        completed_operations: int,
        processed_files: int,
    ) -> tuple[int, int, int] | tuple[None, int, int]:
        succeeded = 0
        current_folder_parts: tuple[str, ...] | None = None

        for index, file in enumerate(planned_files, start=1):
            if self._stop_requested:
                return None, completed_operations, processed_files

            while self._paused and not self._stop_requested:
                QThread.msleep(100)

            if self._stop_requested:
                return None, completed_operations, processed_files

            if file.relative_path.parent.parts != current_folder_parts:
                current_folder_parts = file.relative_path.parent.parts
                self._log(
                    "# Entering folder: "
                    + self._format_relative_folder(current_folder_parts),
                    message_color="START",
                )

            self._log("#### ")
            destination = format_list_path(file.destination_path)
            self._log(
                f"Supported file: {file.file_path} -> {destination}"
            )
            self._log_planned_move(file, "completed")
            succeeded += 1
            processed_files += 1
            completed_operations += 1
            self.progress.emit(
                completed_operations,
                str(file.file_path),
                processed_files,
            )

        return succeeded, completed_operations, processed_files

    def _upload_files(
        self,
        client: ClientProtocol,
        list_index: ListIndex,
        planned_files,
        total_files: int,
        completed_operations: int,
        processed_files: int,
        temp_folder: Path,
    ) -> tuple[int, int, int, int, bool] | None:
        succeeded = 0
        failed = 0
        current_folder_parts: tuple[str, ...] | None = None

        for file in planned_files:
            if self._stop_requested:
                return succeeded, failed, completed_operations, processed_files, True

            while self._paused and not self._stop_requested:
                QThread.msleep(100)

            if self._stop_requested:
                return succeeded, failed, completed_operations, processed_files, True

            if file.relative_path.parent.parts != current_folder_parts:
                current_folder_parts = file.relative_path.parent.parts
                self._log(
                    "# Entering folder: "
                    + self._format_relative_folder(current_folder_parts),
                    message_color="START",
                )

            self._log("#### ")
            result = self._upload_file(
                client,
                list_index,
                file,
                completed_operations,
                processed_files,
                temp_folder,
            )
            if result is None:
                failed += 1
                return succeeded, failed, completed_operations, processed_files, True

            file_succeeded, completed_operations, processed_files = result
            if file_succeeded:
                succeeded += 1
            else:
                failed += 1
                if self._stop_requested:
                    return (
                        succeeded,
                        failed,
                        completed_operations,
                        processed_files,
                        True,
                    )

        return succeeded, failed, completed_operations, processed_files, False

    def _upload_file(
        self,
        client: ClientProtocol,
        list_index: ListIndex,
        file,
        completed_operations: int,
        processed_files: int,
        temp_folder: Path,
    ) -> tuple[bool, int, int] | None:
        file_start_operations = completed_operations
        destination = format_list_path(file.destination_path)
        self._log(f"Uploading supported file: {file.file_path}")
        self._log(f"Destination list: {destination}")

        try:
            upload_path = self._prepare_upload_file(file.file_path, temp_folder)
            asset = self._upload_asset_with_resize_retry(
                client,
                file.file_path,
                upload_path,
                temp_folder,
            )
            asset_id = str(asset["assetId"])
            completed_operations += 1
            self.progress.emit(
                completed_operations,
                f"Uploaded asset: {file.file_path}",
                processed_files,
            )
            self._log(f"Asset uploaded: {asset_id}", level="SUCCESS")

            self._log(f"Creating asset bookmark: {file.file_path}")
            bookmark = client.create_asset_bookmark(
                asset_id=asset_id,
                file_name=file.file_path.name,
            )
            bookmark_id = str(bookmark["id"])
            completed_operations += 1
            self.progress.emit(
                completed_operations,
                f"Created bookmark: {file.file_path}",
                processed_files,
            )
            self._log(f"Bookmark created: {bookmark_id}", level="SUCCESS")

            destination_record = (
                list_index.find_path(file.destination_path)
                if file.destination_path
                else None
            )
            if file.destination_path and destination_record is None:
                raise RuntimeError(
                    "Destination list was not resolved: "
                    + format_list_path(file.destination_path)
                )

            if destination_record is not None:
                self._log(f"Adding bookmark to list: {destination}")
                client.add_bookmark_to_list(
                    list_id=destination_record.id,
                    bookmark_id=bookmark_id,
                )
                completed_operations += 1
                self.progress.emit(
                    completed_operations,
                    f"Assigned list: {destination}",
                    processed_files,
                )
                self._log(f"Bookmark added to list: {destination}", level="SUCCESS")

            if self.config.default_tags:
                tag_text = ", ".join(self.config.default_tags)
                self._log(f"Applying tags: {tag_text}")
                client.attach_tags_to_bookmark(
                    bookmark_id=bookmark_id,
                    tag_names=self.config.default_tags,
                )
                completed_operations += 1
                self.progress.emit(
                    completed_operations,
                    f"Applied tags: {tag_text}",
                    processed_files,
                )
                self._log(f"Tags applied: {tag_text}", level="SUCCESS")

            self._log(f"Verifying bookmark: {bookmark_id}")
            bookmark_after = client.get_bookmark(bookmark_id)
            bookmark_lists = client.get_bookmark_lists(bookmark_id)
            self._verify_uploaded_bookmark(
                bookmark=bookmark_after,
                bookmark_lists=bookmark_lists,
                asset_id=asset_id,
                destination_record=destination_record,
                tag_names=self.config.default_tags,
            )
            completed_operations += 1
            processed_files += 1
            self.progress.emit(
                completed_operations,
                f"Verified bookmark: {file.file_path}",
                processed_files,
            )
            self._log(f"Verification complete: {bookmark_id}", level="SUCCESS")
            move_destination = self._move_processed_file(file, "completed")
            if self._stop_requested:
                return False, completed_operations, processed_files
            if move_destination is not None:
                completed_operations += 1
                self.progress.emit(
                    completed_operations,
                    f"Moved completed file: {file.file_path}",
                    processed_files,
                )
        except httpx.HTTPStatusError as exc:
            self._log(
                "Upload failed: "
                f"{file.file_path} ({describe_http_error(exc)})",
                level="ERROR",
                message_color="ERROR",
            )
            if self._is_unsupported_asset_type_error(exc):
                self._investigate_unsupported_asset(file.file_path)
            self.failed_file.emit(str(file.file_path))
            self._try_move_failed_file(file)
            processed_files += 1
            completed_operations = file_start_operations + (
                self._file_pipeline_operation_count(
                    file,
                    move_kind="completed",
                )
            )
            self.progress.emit(
                completed_operations,
                f"Handled failed file: {file.file_path}",
                processed_files,
            )
            return False, completed_operations, processed_files
        except Exception as exc:  # noqa: BLE001
            self._log(
                f"Upload failed: {file.file_path} ({exc})",
                level="ERROR",
                message_color="ERROR",
            )
            self.failed_file.emit(str(file.file_path))
            self._try_move_failed_file(file)
            processed_files += 1
            completed_operations = file_start_operations + (
                self._file_pipeline_operation_count(
                    file,
                    move_kind="completed",
                )
            )
            self.progress.emit(
                completed_operations,
                f"Handled failed file: {file.file_path}",
                processed_files,
            )
            return False, completed_operations, processed_files

        return True, completed_operations, processed_files

    def _prepare_upload_file(self, file_path: Path, temp_folder: Path) -> Path:
        if not self.config.resize_images_if_needed:
            return file_path

        preferences = self.config.image_resize_preferences
        file_size = file_path.stat().st_size

        if file_size <= preferences.maximum_allowed_bytes:
            return file_path

        self._log(
            "File exceeds maximum allowed image size: "
            f"{file_path} ({self._format_file_size(file_path)} > "
            f"{preferences.maximum_allowed_image_size_mb:g} MB)",
            level="WARNING",
        )

        resized_path = self._resize_to_upload_limit(
            file_path,
            temp_folder,
        )
        self._log(
            "Using resized upload copy: "
            f"{resized_path} ({self._format_file_size(resized_path)})",
            level="SUCCESS",
        )
        return resized_path

    def _upload_asset_with_resize_retry(
        self,
        client: ClientProtocol,
        original_path: Path,
        upload_path: Path,
        temp_folder: Path,
    ) -> dict:
        self._log_uploading_asset(upload_path)
        try:
            return client.upload_asset(upload_path)
        except httpx.HTTPStatusError as exc:
            if (
                not self.config.resize_images_if_needed
                or not self._is_too_large_error(exc)
                or upload_path != original_path
            ):
                raise

            self._log(
                "Karakeep rejected the image as too large. "
                "Attempting resize and retry.",
                level="WARNING",
            )
            resized_path = self._resize_to_upload_limit(
                original_path,
                temp_folder,
            )
            self._log(
                "Retrying with resized upload copy: "
                f"{resized_path} ({self._format_file_size(resized_path)})",
                level="WARNING",
            )
            self._log_uploading_asset(resized_path)
            return client.upload_asset(resized_path)

    def _log_uploading_asset(self, file_path: Path) -> None:
        self._log(
            f"Uploading asset: {file_path} "
            f"({self._format_file_size(file_path)})"
        )

    def _resize_to_upload_limit(self, file_path: Path, temp_folder: Path) -> Path:
        preferences = self.config.image_resize_preferences
        image = QImage(str(file_path))
        if image.isNull():
            raise RuntimeError(f"Unable to load image for resizing: {file_path}")

        original_width = image.width()
        original_height = image.height()
        original_size = file_path.stat().st_size
        high_scale = 1.0
        scale = min(
            0.98,
            math.sqrt(preferences.desired_goal_bytes / original_size) * 0.98,
        )
        best_under_goal: Path | None = None

        for attempt in range(1, preferences.maximum_attempts + 1):
            scale = max(scale, 0.01)
            target_width = max(1, int(original_width * scale))
            target_height = max(1, int(original_height * scale))
            resized = image.scaled(
                target_width,
                target_height,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            resized_path = (
                temp_folder
                / f"{file_path.stem}.resized-{attempt}{file_path.suffix}"
            )

            self._log(
                "Resize attempt "
                f"{attempt}: {original_width}x{original_height} -> "
                f"{resized.width()}x{resized.height()}"
            )

            if not resized.save(str(resized_path)):
                raise RuntimeError(
                    f"Unable to save resized image copy: {resized_path}"
                )

            resized_size = resized_path.stat().st_size
            self._log(
                f"Resize attempt {attempt} size: "
                f"{self._format_file_size(resized_path)}"
            )

            if resized_size <= preferences.desired_goal_bytes:
                best_under_goal = resized_path
                if resized_size >= preferences.minimum_acceptable_bytes:
                    self._log(
                        "Resize result is within acceptable range: "
                        f"{self._format_file_size(resized_path)}"
                    )
                    return resized_path

                self._log(
                    "Resize result is under the goal but outside the "
                    "preferred distance. Trying closer if attempts remain.",
                    level="WARNING",
                )
                scale = (scale + high_scale) / 2
                continue

            self._log(
                "Resize result is still over the desired goal.",
                level="WARNING",
            )
            high_scale = scale
            scale *= (
                math.sqrt(preferences.desired_goal_bytes / resized_size) * 0.98
            )

        if best_under_goal is not None:
            if preferences.fail_if_not_within_goal:
                raise RuntimeError(
                    "Unable to resize image within the acceptable distance "
                    "from desired goal after "
                    f"{preferences.maximum_attempts} attempts: {file_path}"
                )

            self._log(
                "Resize attempts did not land within the preferred distance, "
                "but the best result is under the goal.",
                level="WARNING",
            )
            return best_under_goal

        raise RuntimeError(
            "Unable to resize image under desired goal after "
            f"{preferences.maximum_attempts} attempts: {file_path}"
        )

    @staticmethod
    def _is_too_large_error(exc: httpx.HTTPStatusError) -> bool:
        return exc.response.status_code == 413

    @staticmethod
    def _is_unsupported_asset_type_error(exc: httpx.HTTPStatusError) -> bool:
        return (
            exc.response.status_code == 400
            and "unsupported asset type" in exc.response.text.casefold()
        )

    def _investigate_unsupported_asset(self, file_path: Path) -> None:
        self._log(f"Investigating failed file: {file_path}")
        try:
            findings = investigate_unsupported_asset(file_path)
        except Exception as exc:  # noqa: BLE001
            self._log(
                f"Unable to inspect failed file: {exc}",
                level="WARNING",
                message_color="WARNING",
            )
            return

        for finding in findings:
            self._log(
                finding,
                level="WARNING",
                message_color="WARNING",
            )

    @staticmethod
    def _format_file_size(file_path: Path) -> str:
        try:
            size = file_path.stat().st_size
        except OSError:
            return "unknown size"

        units = ("B", "KB", "MB", "GB", "TB")
        value = float(size)
        unit_index = 0

        while value >= 1024 and unit_index < len(units) - 1:
            value /= 1024
            unit_index += 1

        if unit_index == 0:
            return f"{size} B"

        return f"{value:.1f} {units[unit_index]}"

    def _try_move_failed_file(self, file) -> None:
        try:
            self._move_processed_file(file, "failed")
        except Exception as exc:  # noqa: BLE001
            self._log(
                f"Failed file could not be moved: {file.file_path} ({exc})",
                level="ERROR",
                message_color="ERROR",
            )

    def _log_planned_move(self, file, kind: str) -> None:
        root_folder = self._move_folder_for_kind(kind)
        if root_folder is None:
            self._log(
                f"Dry-run: would leave {kind} file in place: {file.file_path}"
            )
            return

        destination = self._move_destination_for_file(file, root_folder)
        if destination.exists():
            self.conflict_resolved.emit(1)
            if self.config.move_conflict_mode == "rename":
                destination = self._available_destination_path(destination)
                self._log(
                    f"Dry-run: would rename conflicting {kind} move to: "
                    f"{destination}"
                )
            elif self.config.move_conflict_mode == "overwrite":
                self._log(
                    f"Dry-run: would overwrite conflicting {kind} file at: "
                    f"{destination}",
                    level="WARNING",
                )
            elif self.config.move_conflict_mode == "stop":
                self._log(
                    f"Dry-run: would stop because of move conflict: "
                    f"{destination}",
                    level="WARNING",
                )
            else:
                self._log(
                    f"Dry-run: would ask how to handle move conflict: "
                    f"{destination}",
                    level="WARNING",
                )
        else:
            self._log(
                f"Dry-run: would move {kind} file to: {destination}"
            )

    def _move_processed_file(self, file, kind: str) -> Path | None:
        root_folder = self._move_folder_for_kind(kind)
        if root_folder is None:
            self._log(f"Local {kind} file left in place: {file.file_path}")
            return None

        destination = self._move_destination_for_file(file, root_folder)
        destination.parent.mkdir(parents=True, exist_ok=True)
        move_choice = "move"
        if destination.exists():
            conflict_result = self._resolve_move_conflict(
                source_path=file.file_path,
                destination_path=destination,
                kind=kind,
            )
            if conflict_result is None:
                self._stop_requested = True
                self._log(
                    "Upload stopped because a move conflict was not resolved.",
                    level="WARNING",
                )
                return None

            destination, move_choice = conflict_result

        if move_choice == "overwrite":
            self._log(f"Overwriting {kind} file at: {destination}")
            destination.unlink()
        else:
            self._log(f"Moving {kind} file to: {destination}")
        shutil.move(str(file.file_path), str(destination))
        self._log(f"Moved {kind} file to: {destination}", level="SUCCESS")
        return destination

    def _resolve_move_conflict(
        self,
        *,
        source_path: Path,
        destination_path: Path,
        kind: str,
    ) -> tuple[Path, str] | None:
        if self.config.move_conflict_mode != "ask":
            self._log(
                "Move conflict detected. Applying configured policy "
                f"'{self.config.move_conflict_mode}': {destination_path}",
                level="WARNING",
            )
            result = self._apply_move_conflict_choice(
                self.config.move_conflict_mode,
                destination_path,
            )
            if result is not None:
                self.conflict_resolved.emit(1)
            return result

        if self._move_conflict_policy is not None:
            result = self._apply_move_conflict_choice(
                self._move_conflict_policy,
                destination_path,
            )
            if result is not None:
                self.conflict_resolved.emit(1)
            return result

        request = MoveConflictRequest(
            source_path=source_path,
            existing_path=destination_path,
            destination_path=destination_path,
            kind=kind,
        )
        self._log(
            f"Move conflict detected: {destination_path}",
            level="WARNING",
        )
        self.move_conflict.emit(request)

        while not request.resolved.wait(0.1):
            if self._stop_requested:
                return None

        if request.choice is None:
            return None

        if request.apply_to_all and request.choice in {"overwrite", "rename"}:
            self._move_conflict_policy = request.choice

        result = self._apply_move_conflict_choice(
            request.choice,
            destination_path,
        )
        if result is not None:
            self.conflict_resolved.emit(1)
        return result

    def _apply_move_conflict_choice(
        self,
        choice: str,
        destination_path: Path,
    ) -> tuple[Path, str] | None:
        if choice == "overwrite":
            return destination_path, "overwrite"

        if choice == "rename":
            renamed = self._available_destination_path(destination_path)
            return renamed, "rename"

        if choice == "stop":
            return None

        raise ValueError(f"Unknown conflict choice: {choice}")

    def _move_folder_for_kind(self, kind: str) -> Path | None:
        if kind == "completed":
            if self.config.dont_move_completed:
                return None
            return self.config.completed_folder

        if kind == "failed":
            if self.config.dont_move_failed:
                return None
            return self.config.error_folder

        if kind == "unsupported":
            if self.config.dont_move_unsupported:
                return None
            return self.config.unsupported_folder

        raise ValueError(f"Unknown move kind: {kind}")

    def _move_destination_for_file(self, file, root_folder: Path) -> Path:
        if self.config.dont_preserve_move_structure:
            return root_folder / file.file_path.name
        return root_folder / file.relative_path

    @staticmethod
    def _available_destination_path(destination: Path) -> Path:
        if not destination.exists():
            return destination

        stem = destination.stem
        suffix = destination.suffix
        parent = destination.parent
        counter = 1

        while True:
            candidate = parent / f"{stem} ({counter}){suffix}"
            if not candidate.exists():
                return candidate
            counter += 1

    def _verify_uploaded_bookmark(
        self,
        *,
        bookmark: dict,
        bookmark_lists: tuple[ListRecord, ...],
        asset_id: str,
        destination_record: ListRecord | None,
        tag_names: tuple[str, ...],
    ) -> None:
        found_asset_ids = set(self._extract_asset_ids(bookmark))
        if asset_id not in found_asset_ids:
            raise RuntimeError(
                f"Verification failed: bookmark does not reference asset {asset_id}."
            )

        if destination_record is not None:
            list_ids = {record.id for record in bookmark_lists}
            if destination_record.id not in list_ids:
                raise RuntimeError(
                    "Verification failed: bookmark is not attached to "
                    f"list {destination_record.name}."
                )

        if tag_names:
            existing_tags = {
                str(tag).casefold()
                for tag in self._extract_tag_names(bookmark)
            }
            missing_tags = [
                tag for tag in tag_names if tag.casefold() not in existing_tags
            ]
            if missing_tags:
                raise RuntimeError(
                    "Verification failed: missing tags "
                    + ", ".join(missing_tags)
                    + "."
                )

    @staticmethod
    def _extract_asset_ids(bookmark: dict) -> tuple[str, ...]:
        asset_ids: list[str] = []

        content = bookmark.get("content")
        if isinstance(content, dict):
            if content.get("assetId") is not None:
                asset_ids.append(str(content["assetId"]))

            assets = content.get("assets")
            if isinstance(assets, list):
                for asset in assets:
                    if isinstance(asset, dict) and asset.get("assetId") is not None:
                        asset_ids.append(str(asset["assetId"]))

        assets = bookmark.get("assets")
        if isinstance(assets, list):
            for asset in assets:
                if isinstance(asset, dict) and asset.get("assetId") is not None:
                    asset_ids.append(str(asset["assetId"]))

        if bookmark.get("assetId") is not None:
            asset_ids.append(str(bookmark["assetId"]))

        return tuple(asset_ids)

    @staticmethod
    def _extract_tag_names(bookmark: dict) -> tuple[str, ...]:
        tags = bookmark.get("tags", [])
        tag_names: list[str] = []

        if isinstance(tags, list):
            for tag in tags:
                if isinstance(tag, str):
                    tag_names.append(tag)
                elif isinstance(tag, dict):
                    if tag.get("name") is not None:
                        tag_names.append(str(tag["name"]))
                    elif tag.get("tagName") is not None:
                        tag_names.append(str(tag["tagName"]))

        return tuple(tag_names)

    def _resolve_lists(
        self,
        client: ClientProtocol,
        list_index: ListIndex,
        required_lists: tuple[RequiredList, ...],
        completed_operations: int,
        processed_files: int,
    ) -> int | None:
        for required_list in required_lists:
            if self._stop_requested:
                return None

            while self._paused and not self._stop_requested:
                QThread.msleep(100)

            parent_record = list_index.find_path(required_list.parent_path)
            parent_id = parent_record.id if parent_record is not None else None

            path_label = format_list_path(required_list.path)
            self._log(f"Checking list: {path_label}")

            existing = list_index.find_path(required_list.path)
            if existing is not None:
                self._log(f"List exists: {path_label}")
                completed_operations += 1
                self.progress.emit(
                    completed_operations,
                    f"Checked list: {path_label}",
                    processed_files,
                )
                continue

            if self.config.dry_run:
                self._log(f"Would create list: {path_label}")
                completed_operations += 1
                self.progress.emit(
                    completed_operations,
                    f"Checked list: {path_label}",
                    processed_files,
                )
                continue

            self._log(f"Creating list: {path_label}")
            try:
                created = client.create_manual_list(
                    name=required_list.name,
                    parent_id=parent_id,
                )
            except httpx.HTTPStatusError as exc:
                self._log(
                    "List creation failed: "
                    f"{path_label} ({describe_http_error(exc)})",
                    level="ERROR",
                    message_color="ERROR",
                )
                return None
            except Exception as exc:  # noqa: BLE001
                self._log(
                    f"List creation failed: {path_label} ({exc})",
                    level="ERROR",
                    message_color="ERROR",
                )
                return None

            list_index.add(created)
            self._log(f"List created: {path_label}", level="SUCCESS")
            completed_operations += 1
            self.progress.emit(
                completed_operations,
                f"Created list: {path_label}",
                processed_files,
            )

        return completed_operations

    def _log(
        self,
        message: str,
        *,
        level: str = "INFO",
        message_color: str | None = None,
    ) -> None:
        self.log.emit(message, level, message_color)
