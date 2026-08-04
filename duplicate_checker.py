from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
import hashlib
import re
from typing import Protocol

from PySide6.QtCore import QObject, QThread, Signal

from karakeep_client import KarakeepClient


POTENTIAL_DUPLICATE_TAG = "POTENTIAL_DUPLICATE"
PD_TAG_PREFIX = "PD: "


@dataclass(frozen=True)
class BookmarkAssetFingerprint:
    bookmark_id: str
    bookmark_title: str
    asset_id: str
    digest: str
    size: int | None = None


@dataclass(frozen=True)
class DuplicateGroup:
    group_number: int
    digest: str
    matches: tuple[BookmarkAssetFingerprint, ...]


@dataclass(frozen=True)
class DuplicateCheckConfig:
    server_url: str
    api_key: str


class DuplicateCheckClientProtocol(Protocol):
    server_url: str

    def list_bookmarks(self, *, include_content: bool = False) -> tuple[dict, ...]:
        ...

    def iter_bookmarks(self, *, include_content: bool = False):
        ...

    def list_tags(self) -> tuple[dict, ...]:
        ...

    def stream_asset_bytes(self, asset_id: str):
        ...

    def attach_tags_to_bookmark(
        self,
        *,
        bookmark_id: str,
        tag_names: tuple[str, ...],
    ) -> None:
        ...


class AssetHasher:
    """Reusable SHA-256 hasher for KaraKeep assets."""

    def __init__(self, client: DuplicateCheckClientProtocol) -> None:
        self.client = client

    def hash_asset(self, asset_id: str) -> str:
        digest = hashlib.sha256()
        for chunk in self.client.stream_asset_bytes(asset_id):
            digest.update(chunk)
        return digest.hexdigest()


class DuplicateScanner:
    """Finds exact duplicate media by grouping bookmarks with matching hashes."""

    def __init__(self, client: DuplicateCheckClientProtocol) -> None:
        self.client = client
        self.hasher = AssetHasher(client)

    def existing_pd_numbers(self) -> set[int]:
        numbers: set[int] = set()
        for tag in self.client.list_tags():
            name = self._tag_name(tag)
            if name is None:
                continue
            match = re.fullmatch(r"PD:\s*(\d+)", name.strip(), re.IGNORECASE)
            if match:
                numbers.add(int(match.group(1)))
        return numbers

    def find_duplicate_groups(
        self,
        *,
        log,
        checkpoint,
        progress,
    ) -> tuple[DuplicateGroup, ...]:
        log("Retrieving Karakeep bookmarks.")
        candidates: list[BookmarkAssetFingerprint] = []
        bookmark_count = 0
        for bookmark in self.client.iter_bookmarks(include_content=True):
            checkpoint()
            bookmark_count += 1
            bookmark_id = self._bookmark_id(bookmark) or "(missing id)"
            title = self._bookmark_title(bookmark)
            bookmark_candidates = self._asset_bookmark_records((bookmark,))
            candidates.extend(bookmark_candidates)
            asset_count = len(bookmark_candidates)
            log(
                f"Retrieved bookmark {bookmark_count}: {bookmark_id} | "
                f"{title} | assets: {asset_count}"
            )
            progress("scanned", bookmark_count, asset_count)

        log(f"Retrieved {bookmark_count} bookmarks.")
        log(f"Found {len(candidates)} bookmark assets to hash.")
        progress("hash_range", len(candidates), len(candidates))

        by_digest: dict[str, list[BookmarkAssetFingerprint]] = defaultdict(list)
        for index, candidate in enumerate(candidates, start=1):
            checkpoint()
            log(
                "Hashing asset "
                f"{index}/{len(candidates)}: bookmark {candidate.bookmark_id}, "
                f"asset {candidate.asset_id}"
            )
            digest = self.hasher.hash_asset(candidate.asset_id)
            by_digest[digest].append(
                BookmarkAssetFingerprint(
                    bookmark_id=candidate.bookmark_id,
                    bookmark_title=candidate.bookmark_title,
                    asset_id=candidate.asset_id,
                    digest=digest,
                    size=candidate.size,
                )
            )
            progress(
                "hashed",
                index,
                f"Hashed asset {index}/{len(candidates)}: {candidate.asset_id}",
            )

        existing_numbers = self.existing_pd_numbers()
        log(f"Found {len(existing_numbers)} existing PD group tags.")
        next_number = self._next_available_number(existing_numbers, 1)
        groups: list[DuplicateGroup] = []

        progress("compare_range", len(by_digest), len(by_digest))
        for index, digest in enumerate(sorted(by_digest), start=1):
            checkpoint()
            matches = tuple(by_digest[digest])
            if len(matches) < 2:
                progress("compared", index, len(groups))
                continue
            while next_number in existing_numbers:
                next_number += 1
            groups.append(
                DuplicateGroup(
                    group_number=next_number,
                    digest=digest,
                    matches=matches,
                )
            )
            existing_numbers.add(next_number)
            next_number += 1
            progress("compared", index, len(groups))

        return tuple(groups)

    def tag_duplicate_groups(
        self,
        groups: tuple[DuplicateGroup, ...],
        *,
        log,
        checkpoint,
        progress,
    ) -> None:
        completed = 0
        for group in groups:
            tags = (POTENTIAL_DUPLICATE_TAG, f"{PD_TAG_PREFIX}{group.group_number}")
            for match in group.matches:
                checkpoint()
                log(
                    "Applying duplicate tags to bookmark "
                    f"{match.bookmark_id}: {', '.join(tags)}"
                )
                self.client.attach_tags_to_bookmark(
                    bookmark_id=match.bookmark_id,
                    tag_names=tags,
                )
                completed += 1
                progress(
                    "tagged",
                    completed,
                    f"Tagged bookmark {match.bookmark_id}",
                )

    @staticmethod
    def bookmark_url(server_url: str, bookmark_id: str) -> str:
        return server_url.rstrip("/") + f"/dashboard/preview/{bookmark_id}"

    @staticmethod
    def tag_url(server_url: str, tag_id: str) -> str:
        return server_url.rstrip("/") + f"/dashboard/tags/{tag_id}"

    def tag_ids_by_name(self) -> dict[str, str]:
        tag_ids: dict[str, str] = {}
        for tag in self.client.list_tags():
            name = self._tag_name(tag)
            tag_id = self._tag_id(tag)
            if name is not None and tag_id is not None:
                tag_ids[name.casefold()] = tag_id
        return tag_ids

    @classmethod
    def _asset_bookmark_records(
        cls,
        bookmarks: tuple[dict, ...],
    ) -> tuple[BookmarkAssetFingerprint, ...]:
        records: list[BookmarkAssetFingerprint] = []
        for bookmark in bookmarks:
            bookmark_id = cls._bookmark_id(bookmark)
            if bookmark_id is None:
                continue
            title = cls._bookmark_title(bookmark)
            for asset_id, size in cls._extract_asset_ids(bookmark):
                records.append(
                    BookmarkAssetFingerprint(
                        bookmark_id=bookmark_id,
                        bookmark_title=title,
                        asset_id=asset_id,
                        digest="",
                        size=size,
                    )
                )
        return tuple(records)

    @staticmethod
    def _bookmark_id(bookmark: dict) -> str | None:
        ident = bookmark.get("id")
        if ident is None:
            return None
        return str(ident)

    @staticmethod
    def _bookmark_title(bookmark: dict) -> str:
        for key in ("title", "fileName", "name"):
            value = bookmark.get(key)
            if value:
                return str(value)

        content = bookmark.get("content")
        if isinstance(content, dict):
            for key in ("title", "fileName", "url"):
                value = content.get(key)
                if value:
                    return str(value)

        return "(untitled bookmark)"

    @classmethod
    def _extract_asset_ids(cls, bookmark: dict) -> tuple[tuple[str, int | None], ...]:
        found: list[tuple[str, int | None]] = []

        def add_from_mapping(mapping: dict) -> None:
            for key in ("assetId", "id"):
                if mapping.get(key) is not None:
                    found.append((str(mapping[key]), cls._asset_size(mapping)))
                    return

        content = bookmark.get("content")
        if isinstance(content, dict):
            if content.get("type") == "asset" and content.get("assetId") is not None:
                found.append((str(content["assetId"]), cls._asset_size(content)))
            assets = content.get("assets")
            if isinstance(assets, list):
                for asset in assets:
                    if isinstance(asset, dict):
                        add_from_mapping(asset)

        assets = bookmark.get("assets")
        if isinstance(assets, list):
            for asset in assets:
                if isinstance(asset, dict):
                    add_from_mapping(asset)

        if bookmark.get("assetId") is not None:
            found.append((str(bookmark["assetId"]), cls._asset_size(bookmark)))

        unique: list[tuple[str, int | None]] = []
        seen: set[str] = set()
        for asset_id, size in found:
            if asset_id in seen:
                continue
            seen.add(asset_id)
            unique.append((asset_id, size))
        return tuple(unique)

    @staticmethod
    def _asset_size(mapping: dict) -> int | None:
        value = mapping.get("size")
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _tag_name(tag: dict) -> str | None:
        for key in ("name", "tagName"):
            value = tag.get(key)
            if value is not None:
                return str(value)
        return None

    @staticmethod
    def _tag_id(tag: dict) -> str | None:
        value = tag.get("id") or tag.get("tagId")
        if value is None:
            return None
        return str(value)

    @staticmethod
    def _next_available_number(existing_numbers: set[int], start: int) -> int:
        value = start
        while value in existing_numbers:
            value += 1
        return value


class DuplicateCheckWorker(QObject):
    log = Signal(str, str, object)
    progress_range = Signal(int, int)
    progress = Signal(int, str, int)
    stats = Signal(object)
    finished = Signal(bool, int, int, int)

    def __init__(
        self,
        config: DuplicateCheckConfig,
        *,
        client: DuplicateCheckClientProtocol | None = None,
    ) -> None:
        super().__init__()
        self.config = config
        self._client = client
        self._stop_requested = False
        self._paused = False
        self._completed_operations = 0
        self._scanned_bookmarks = 0
        self._total_assets = 0
        self._hashed_assets = 0
        self._total_comparisons = 0
        self._completed_comparisons = 0
        self._matching_groups = 0

    def request_stop(self) -> None:
        self._stop_requested = True

    def set_paused(self, paused: bool) -> None:
        self._paused = paused

    def run(self) -> None:
        try:
            client = self._client or KarakeepClient(
                self.config.server_url,
                self.config.api_key,
                timeout=30.0,
            )
            scanner = DuplicateScanner(client)
            self._log("Retrieving bookmarks and preparing duplicate scan.")
            self.progress_range.emit(0, 0)

            groups = scanner.find_duplicate_groups(
                log=self._log,
                checkpoint=self._checkpoint,
                progress=self._handle_scan_progress,
            )
            if self._stop_requested:
                self.finished.emit(True, 0, 0, 0)
                return

            match_count = sum(len(group.matches) for group in groups)
            hash_operations = self._completed_operations
            total_operations = max(hash_operations + match_count, 1)
            self.progress_range.emit(total_operations, match_count)
            self.progress.emit(
                hash_operations,
                "Hashing complete.",
                0,
            )

            if not groups:
                self._log("No duplicate media hashes found.", level="SUCCESS")
                self.finished.emit(False, 0, 0, 0)
                return

            self._log(
                f"Found {len(groups)} potential duplicate groups "
                f"containing {match_count} bookmarks.",
                level="WARNING",
                message_color="WARNING",
            )
            scanner.tag_duplicate_groups(
                groups,
                log=self._log,
                checkpoint=self._checkpoint,
                progress=self._handle_tag_progress,
            )
            if self._stop_requested:
                self.finished.emit(True, 0, 0, 0)
                return

            tag_ids = scanner.tag_ids_by_name()
            self._log("================================", level="")
            self._log("Potential duplicate groups:", message_color="WARNING")
            for group in groups:
                pd_tag = f"{PD_TAG_PREFIX}{group.group_number}"
                pd_tag_id = tag_ids.get(pd_tag.casefold())
                tag_link = (
                    DuplicateScanner.tag_url(client.server_url, pd_tag_id)
                    if pd_tag_id is not None
                    else "(tag link unavailable)"
                )
                self._log(
                    "--------------------------------",
                    level="",
                    message_color="WARNING",
                )
                self._log(
                    f"Group {pd_tag} | {tag_link} | "
                    f"{len(group.matches)} matches | sha256 {group.digest}",
                    message_color="WARNING",
                )
                for match in group.matches:
                    link = DuplicateScanner.bookmark_url(
                        client.server_url,
                        match.bookmark_id,
                    )
                    size_text = (
                        f", {match.size} bytes" if match.size is not None else ""
                    )
                    self._log(
                        "- "
                        f"{match.bookmark_title} | bookmark {match.bookmark_id} "
                        f"| asset {match.asset_id}{size_text} | {link}"
                    )

            self.finished.emit(False, len(groups), 0, 0)
        except StopRequested:
            self.finished.emit(True, 0, 0, 0)
        except Exception as exc:  # noqa: BLE001
            self._log(
                f"Duplicate check failed: {exc}",
                level="ERROR",
                message_color="ERROR",
            )
            self.finished.emit(True, 0, 1, 0)

    def _checkpoint(self) -> None:
        if self._stop_requested:
            raise StopRequested
        while self._paused and not self._stop_requested:
            QThread.msleep(100)
        if self._stop_requested:
            raise StopRequested

    def _handle_scan_progress(self, kind: str, value: int, detail) -> None:
        if kind == "scanned":
            self._scanned_bookmarks = int(value)
            self._total_assets += int(detail)
            self._emit_stats()
            return

        if kind == "hash_range":
            self._completed_operations = 0
            total_operations = int(value)
            total_files = int(detail)
            self._total_assets = total_files
            self.progress_range.emit(total_operations, total_files)
            self._emit_stats()
            return

        if kind == "compare_range":
            self._total_comparisons = int(value)
            self.progress_range.emit(
                self._total_assets + self._total_comparisons,
                self._total_assets,
            )
            self._emit_stats()
            return

        if kind == "compared":
            self._completed_comparisons = int(value)
            self._matching_groups = int(detail)
            self.progress.emit(
                self._total_assets + self._completed_comparisons,
                f"Compared hash group {self._completed_comparisons}/"
                f"{self._total_comparisons}",
                self._hashed_assets,
            )
            self._emit_stats()
            return

        if kind != "hashed":
            return

        self._completed_operations = int(value)
        self._hashed_assets = int(value)
        self.progress.emit(
            self._completed_operations,
            str(detail),
            self._completed_operations,
        )
        self._emit_stats()

    def _handle_tag_progress(self, kind: str, value: int, detail) -> None:
        if kind != "tagged":
            return

        self.progress.emit(
            self._completed_operations + int(value),
            str(detail),
            int(value),
        )

    def _emit_stats(self) -> None:
        self.stats.emit(
            {
                "scanned": self._scanned_bookmarks,
                "hashed": self._hashed_assets,
                "hash_remaining": max(self._total_assets - self._hashed_assets, 0),
                "compared": self._completed_comparisons,
                "compare_remaining": max(
                    self._total_comparisons - self._completed_comparisons,
                    0,
                ),
                "matching": self._matching_groups,
            }
        )

    def _log(
        self,
        message: str,
        level: str = "INFO",
        message_color: str | None = None,
    ) -> None:
        self.log.emit(message, level, message_color)


class StopRequested(Exception):
    pass
