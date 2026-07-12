from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shutil
import threading
from typing import Protocol

import httpx
from PySide6.QtCore import QObject, QThread, Signal

from karakeep_client import KarakeepClient, describe_http_error
from list_planner import (
    ListIndex,
    ListRecord,
    RequiredList,
    build_upload_plan,
    format_list_path,
)
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
            client = self._client or KarakeepClient(
                self.config.server_url,
                self.config.api_key,
            )

            scan_result = self._scan()
            plan = build_upload_plan(
                scan_result.supported_files,
                import_to_root=self.config.import_to_root,
                root_list=self.config.root_list,
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
                    "Entering folder: "
                    + self._format_relative_folder(current_folder_parts),
                    message_color="START",
                )

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
    ) -> tuple[int, int, int, int, bool] | None:
        succeeded = 0
        failed = 0

        for file in planned_files:
            if self._stop_requested:
                return succeeded, failed, completed_operations, processed_files, True

            while self._paused and not self._stop_requested:
                QThread.msleep(100)

            if self._stop_requested:
                return succeeded, failed, completed_operations, processed_files, True

            result = self._upload_file(
                client,
                list_index,
                file,
                completed_operations,
                processed_files,
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
    ) -> tuple[bool, int, int] | None:
        destination = format_list_path(file.destination_path)
        self._log(f"Uploading supported file: {file.file_path}")
        self._log(f"Destination list: {destination}")

        try:
            self._log(f"#### Uploading asset: {file.file_path}")
            asset = client.upload_asset(file.file_path)
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
            self.failed_file.emit(str(file.file_path))
            self._try_move_failed_file(file)
            processed_files += 1
            completed_operations += 1
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
            completed_operations += 1
            self.progress.emit(
                completed_operations,
                f"Handled failed file: {file.file_path}",
                processed_files,
            )
            return False, completed_operations, processed_files

        return True, completed_operations, processed_files

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
