from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
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
    import_to_root: bool
    root_list: str
    dry_run: bool


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


class UploadWorker(QObject):
    log = Signal(str, str, object)
    progress_range = Signal(int)
    progress = Signal(int, str)
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
            self.progress_range.emit(total_files)

            self._log("Retrieving Karakeep lists.")
            existing_lists = client.list_lists()
            self._log(f"Retrieved {len(existing_lists)} Karakeep lists.")
            list_index = ListIndex(existing_lists)

            if not self._resolve_lists(client, list_index, plan.required_lists):
                failed = 1
                not_processed = total_files
                self.finished.emit(True, succeeded, failed, not_processed)
                return

            current_folder_parts: tuple[str, ...] | None = None
            for index, file in enumerate(plan.planned_files, start=1):
                if self._stop_requested:
                    not_processed = total_files - index + 1
                    self.finished.emit(True, succeeded, failed, not_processed)
                    return

                while self._paused and not self._stop_requested:
                    QThread.msleep(100)

                if self._stop_requested:
                    not_processed = total_files - index + 1
                    self.finished.emit(True, succeeded, failed, not_processed)
                    return

                if file.relative_path.parent.parts != current_folder_parts:
                    current_folder_parts = file.relative_path.parent.parts
                    self._log(
                        "Entering folder: "
                        + self._format_relative_folder(current_folder_parts),
                        message_color="START",
                    )

                destination = format_list_path(file.destination_path)
                self._log(
                    f"Supported file: {file.relative_path} -> {destination}"
                )
                self.progress.emit(index, str(file.relative_path))
                succeeded += 1

            self.finished.emit(False, succeeded, failed, not_processed)
        except Exception as exc:  # noqa: BLE001
            self._log(f"Batch failed: {exc}", level="ERROR", message_color="ERROR")
            failed += 1
            self.finished.emit(True, succeeded, failed, not_processed)

    def _scan(self):
        self._log(f"Scanning upload folder: {self.config.upload_folder}")
        scan_result = scan_upload_folder(self.config.upload_folder)

        for folder in scan_result.scanned_folders:
            self._log(f"Scanning folder: {folder}")

        self._log(f"Found {len(scan_result.supported_files)} supported files.")
        return scan_result

    @staticmethod
    def _format_relative_folder(folder_parts: tuple[str, ...]) -> str:
        if not folder_parts or folder_parts == (".",):
            return "."
        return str(Path(*folder_parts))

    def _resolve_lists(
        self,
        client: ClientProtocol,
        list_index: ListIndex,
        required_lists: tuple[RequiredList, ...],
    ) -> bool:
        for required_list in required_lists:
            if self._stop_requested:
                return False

            while self._paused and not self._stop_requested:
                QThread.msleep(100)

            parent_record = list_index.find_path(required_list.parent_path)
            parent_id = parent_record.id if parent_record is not None else None

            path_label = format_list_path(required_list.path)
            self._log(f"Checking list: {path_label}")

            existing = list_index.find_path(required_list.path)
            if existing is not None:
                self._log(f"List exists: {path_label}")
                continue

            if self.config.dry_run:
                self._log(f"Would create list: {path_label}")
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
                return False
            except Exception as exc:  # noqa: BLE001
                self._log(
                    f"List creation failed: {path_label} ({exc})",
                    level="ERROR",
                    message_color="ERROR",
                )
                return False

            list_index.add(created)
            self._log(f"List created: {path_label}", level="SUCCESS")

        return True

    def _log(
        self,
        message: str,
        *,
        level: str = "INFO",
        message_color: str | None = None,
    ) -> None:
        self.log.emit(message, level, message_color)
