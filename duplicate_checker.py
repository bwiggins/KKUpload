from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
import hashlib
import re
from typing import Protocol

import httpx
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
    pd_tag_id: str | None = None


@dataclass(frozen=True)
class DuplicateCleanupResult:
    group_number: int
    starting_count: int
    deleted_count: int
    remaining_count: int
    error_count: int


@dataclass(frozen=True)
class DuplicateCheckConfig:
    server_url: str
    api_key: str
    rescan: bool = True
    replicate_lists: bool = False
    replicate_tags: bool = False
    auto_cull: bool = False
    cleanup_resolved_duplicate_tags: bool = True


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
        attached_by: str = "human",
    ) -> None:
        ...

    def detach_tags_from_bookmark(
        self,
        *,
        bookmark_id: str,
        tag_names: tuple[str, ...],
    ) -> None:
        ...

    def get_bookmark(self, bookmark_id: str) -> dict:
        ...

    def get_bookmark_lists(self, bookmark_id: str) -> tuple:
        ...

    def add_bookmark_to_list(self, *, list_id: str, bookmark_id: str) -> None:
        ...

    def delete_bookmark(self, bookmark_id: str) -> None:
        ...

    def delete_tag(self, tag_id: str) -> None:
        ...

    def list_bookmarks_for_tag(
        self,
        tag_id: str,
        *,
        include_content: bool = False,
    ) -> tuple[dict, ...]:
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

    def existing_duplicate_groups(
        self,
        *,
        log,
        checkpoint,
    ) -> tuple[DuplicateGroup, ...]:
        tag_ids = self.tag_ids_by_name()
        groups: list[DuplicateGroup] = []

        for tag_name, tag_id in sorted(tag_ids.items()):
            match = re.fullmatch(r"pd:\s*(\d+)", tag_name, re.IGNORECASE)
            if match is None:
                continue

            checkpoint()
            group_number = int(match.group(1))
            log(f"Retrieving existing duplicate group PD: {group_number}.")
            bookmarks = self._try_tag_operation(
                f"retrieve existing duplicate group PD: {group_number}",
                tag_id=tag_id,
                operation=lambda tag_id=tag_id: self.client.list_bookmarks_for_tag(
                    tag_id,
                    include_content=True,
                ),
                log=log,
                checkpoint=checkpoint,
            )
            if bookmarks is None:
                log(
                    f"Skipping existing duplicate group PD: {group_number} "
                    "after repeated retrieval failures.",
                    level="ERROR",
                    message_color="ERROR",
                )
                continue
            records = self._asset_bookmark_records(bookmarks)
            groups.append(
                DuplicateGroup(
                    group_number=group_number,
                    digest="existing-pd-group",
                    matches=records,
                    pd_tag_id=tag_id,
                )
            )
            log(
                f"Existing group PD: {group_number} has "
                f"{len(records)} bookmark asset records."
            )

        return tuple(groups)

    def cleanup_duplicate_groups(
        self,
        groups: tuple[DuplicateGroup, ...],
        *,
        replicate_lists: bool,
        replicate_tags: bool,
        auto_cull: bool,
        cleanup_resolved_duplicate_tags: bool,
        log,
        checkpoint,
        progress=None,
    ) -> tuple[DuplicateCleanupResult, ...]:
        results: list[DuplicateCleanupResult] = []
        total_groups = len(groups)
        pd_tag_ids_by_name: dict[str, str] | None = None

        for group in groups:
            checkpoint()
            pd_tag = f"{PD_TAG_PREFIX}{group.group_number}"
            bookmark_ids = self._unique_bookmark_ids(group)

            log("--------------------------------", level="")
            log(f"Cleanup group {pd_tag}: {len(bookmark_ids)} bookmarks.")
            details, detail_errors = self._group_bookmark_details(
                bookmark_ids,
                log=log,
                checkpoint=checkpoint,
            )
            if not details:
                log(
                    f"Cleanup group {pd_tag}: no retrievable bookmarks remain.",
                    level="WARNING",
                    message_color="WARNING",
                )
                if cleanup_resolved_duplicate_tags:
                    pd_tag_id = group.pd_tag_id
                    if pd_tag_id is None:
                        if pd_tag_ids_by_name is None:
                            pd_tag_ids_by_name = self.tag_ids_by_name()
                        pd_tag_id = pd_tag_ids_by_name.get(pd_tag.casefold())
                    self._cleanup_resolved_duplicate_tags(
                        (),
                        pd_tag,
                        pd_tag_id,
                        log=log,
                        checkpoint=checkpoint,
                    )
                results.append(
                    DuplicateCleanupResult(
                        group_number=group.group_number,
                        starting_count=len(bookmark_ids),
                        deleted_count=0,
                        remaining_count=0,
                        error_count=detail_errors,
                    )
                )
                if progress is not None:
                    progress("cleanup", len(results), total_groups)
                continue

            if replicate_lists:
                self._replicate_lists(details, log=log, checkpoint=checkpoint)

            if replicate_tags:
                self._replicate_tags(details, log=log, checkpoint=checkpoint)

            remaining_ids = tuple(details)
            deleted_count = 0
            if auto_cull:
                remaining_ids, deleted_count = self._auto_cull_group(
                    details,
                    log=log,
                    checkpoint=checkpoint,
                )

            if cleanup_resolved_duplicate_tags and len(remaining_ids) <= 1:
                pd_tag_id = group.pd_tag_id
                if pd_tag_id is None:
                    if pd_tag_ids_by_name is None:
                        pd_tag_ids_by_name = self.tag_ids_by_name()
                    pd_tag_id = pd_tag_ids_by_name.get(pd_tag.casefold())
                self._cleanup_resolved_duplicate_tags(
                    remaining_ids,
                    pd_tag,
                    pd_tag_id,
                    log=log,
                    checkpoint=checkpoint,
                )

            result = DuplicateCleanupResult(
                group_number=group.group_number,
                starting_count=len(bookmark_ids),
                deleted_count=deleted_count,
                remaining_count=len(remaining_ids),
                error_count=detail_errors,
            )
            log(
                f"Cleanup group {pd_tag} complete: removed "
                f"{result.deleted_count} duplicate bookmark(s), "
                f"{result.remaining_count} remaining, "
                f"{result.error_count} skipped/error(s)."
            )
            results.append(result)
            if progress is not None:
                progress("cleanup", len(results), total_groups)

        return tuple(results)

    def _group_bookmark_details(
        self,
        bookmark_ids: tuple[str, ...],
        *,
        log,
        checkpoint,
    ) -> tuple[dict[str, dict], int]:
        details: dict[str, dict] = {}
        error_count = 0
        for bookmark_id in bookmark_ids:
            checkpoint()
            bookmark = self._try_bookmark_operation(
                f"retrieve bookmark {bookmark_id}",
                bookmark_id=bookmark_id,
                operation=lambda bookmark_id=bookmark_id: self.client.get_bookmark(
                    bookmark_id
                ),
                log=log,
                checkpoint=checkpoint,
            )
            if bookmark is None:
                error_count += 1
                continue

            lists = self._try_bookmark_operation(
                f"retrieve lists for bookmark {bookmark_id}",
                bookmark_id=bookmark_id,
                operation=lambda bookmark_id=bookmark_id: self.client.get_bookmark_lists(
                    bookmark_id
                ),
                log=log,
                checkpoint=checkpoint,
            )
            if lists is None:
                error_count += 1
                lists = ()

            details[bookmark_id] = {
                "bookmark": bookmark,
                "lists": tuple(lists),
                "tags": self._extract_tag_names(bookmark),
                "tag_sources": self._extract_tag_sources(bookmark),
            }
        return details, error_count

    def _replicate_lists(self, details: dict[str, dict], *, log, checkpoint) -> None:
        all_lists = {
            record.id: record
            for detail in details.values()
            for record in detail["lists"]
        }
        for bookmark_id, detail in details.items():
            existing_ids = {record.id for record in detail["lists"]}
            for list_id, record in sorted(all_lists.items(), key=lambda item: item[1].name):
                if list_id in existing_ids:
                    continue
                checkpoint()
                log(f"Unioning list '{record.name}' to bookmark {bookmark_id}.")
                added = self._try_bookmark_operation(
                    f"union list '{record.name}' to bookmark {bookmark_id}",
                    bookmark_id=bookmark_id,
                    operation=lambda list_id=list_id, bookmark_id=bookmark_id: (
                        self.client.add_bookmark_to_list(
                            list_id=list_id,
                            bookmark_id=bookmark_id,
                        )
                    ),
                    log=log,
                    checkpoint=checkpoint,
                )
                if added is not None:
                    detail["lists"] = tuple((*detail["lists"], record))

    def _replicate_tags(self, details: dict[str, dict], *, log, checkpoint) -> None:
        all_tags = sorted({
            tag
            for detail in details.values()
            for tag in detail["tags"]
        })
        for bookmark_id, detail in details.items():
            existing = {tag.casefold() for tag in detail["tags"]}
            missing = tuple(tag for tag in all_tags if tag.casefold() not in existing)
            if not missing:
                continue
            checkpoint()
            missing_by_source: dict[str, list[str]] = defaultdict(list)
            for tag in missing:
                attached_by = self._source_for_tag(details, tag)
                missing_by_source[attached_by].append(tag)
            for attached_by, tag_names in sorted(missing_by_source.items()):
                tag_tuple = tuple(tag_names)
                log(
                    f"Unioning {attached_by} tags to bookmark {bookmark_id}: "
                    f"{', '.join(tag_tuple)}."
                )
                attached = self._try_bookmark_operation(
                    f"union tags to bookmark {bookmark_id}",
                    bookmark_id=bookmark_id,
                    operation=(
                        lambda bookmark_id=bookmark_id,
                        tag_tuple=tag_tuple,
                        attached_by=attached_by: self.client.attach_tags_to_bookmark(
                            bookmark_id=bookmark_id,
                            tag_names=tag_tuple,
                            attached_by=attached_by,
                        )
                    ),
                    log=log,
                    checkpoint=checkpoint,
                )
                if attached is not None:
                    detail["tags"] = tuple((*detail["tags"], *tag_tuple))
                    tag_sources = dict(detail["tag_sources"])
                    for tag in tag_tuple:
                        tag_sources[tag.casefold()] = attached_by
                    detail["tag_sources"] = tag_sources

    def _auto_cull_group(
        self,
        details: dict[str, dict],
        *,
        log,
        checkpoint,
    ) -> tuple[tuple[str, ...], int]:
        kept_by_signature: dict[tuple, str] = {}
        deleted_ids: set[str] = set()

        for bookmark_id in sorted(details):
            signature = self._cleanup_signature(details[bookmark_id])
            if signature not in kept_by_signature:
                kept_by_signature[signature] = bookmark_id
                continue

            checkpoint()
            kept_id = kept_by_signature[signature]
            log(
                f"Culling redundant duplicate bookmark {bookmark_id}; "
                f"same cleanup signature as {kept_id}."
            )
            deleted = self._try_bookmark_operation(
                f"delete redundant bookmark {bookmark_id}",
                bookmark_id=bookmark_id,
                operation=lambda bookmark_id=bookmark_id: self.client.delete_bookmark(
                    bookmark_id
                ),
                log=log,
                checkpoint=checkpoint,
            )
            if deleted is not None:
                deleted_ids.add(bookmark_id)

        return (
            tuple(bookmark_id for bookmark_id in details if bookmark_id not in deleted_ids),
            len(deleted_ids),
        )

    def _cleanup_resolved_duplicate_tags(
        self,
        bookmark_ids: tuple[str, ...],
        pd_tag: str,
        pd_tag_id: str | None,
        *,
        log,
        checkpoint,
    ) -> None:
        for bookmark_id in bookmark_ids:
            checkpoint()
            log(
                f"Removing {POTENTIAL_DUPLICATE_TAG} from remaining bookmark "
                f"{bookmark_id} for resolved group {pd_tag}."
            )
            self._try_bookmark_operation(
                f"remove {POTENTIAL_DUPLICATE_TAG} from bookmark {bookmark_id}",
                bookmark_id=bookmark_id,
                operation=lambda bookmark_id=bookmark_id: (
                    self.client.detach_tags_from_bookmark(
                        bookmark_id=bookmark_id,
                        tag_names=(POTENTIAL_DUPLICATE_TAG,),
                    )
                ),
                log=log,
                checkpoint=checkpoint,
            )

        if pd_tag_id is None:
            log(
                f"Resolved group {pd_tag}: PD tag id was not found; "
                "could not delete the tag.",
                level="WARNING",
                message_color="WARNING",
            )
            return

        log(f"Deleting resolved duplicate tag {pd_tag}.")
        self._try_tag_operation(
            f"delete resolved duplicate tag {pd_tag}",
            tag_id=pd_tag_id,
            operation=lambda pd_tag_id=pd_tag_id: self.client.delete_tag(pd_tag_id),
            log=log,
            checkpoint=checkpoint,
        )

    @staticmethod
    def _try_bookmark_operation(
        description: str,
        *,
        bookmark_id: str,
        operation,
        log,
        checkpoint,
        attempts: int = 3,
    ):
        for attempt in range(1, attempts + 1):
            checkpoint()
            try:
                result = operation()
                return True if result is None else result
            except (httpx.HTTPStatusError, httpx.RequestError, KeyError) as exc:
                level = "WARNING" if attempt < attempts else "ERROR"
                suffix = (
                    f"retrying ({attempt + 1}/{attempts})."
                    if attempt < attempts
                    else "skipping this bookmark."
                )
                log(
                    f"Could not {description} for bookmark {bookmark_id}: "
                    f"{exc}; {suffix}",
                    level=level,
                    message_color=level,
                )
                if attempt < attempts:
                    QThread.msleep(250 * attempt)
        return None

    @staticmethod
    def _try_tag_operation(
        description: str,
        *,
        tag_id: str,
        operation,
        log,
        checkpoint,
        attempts: int = 3,
    ):
        for attempt in range(1, attempts + 1):
            checkpoint()
            try:
                result = operation()
                return True if result is None else result
            except (httpx.HTTPStatusError, httpx.RequestError, KeyError) as exc:
                level = "WARNING" if attempt < attempts else "ERROR"
                suffix = (
                    f"retrying ({attempt + 1}/{attempts})."
                    if attempt < attempts
                    else "skipping this tag."
                )
                log(
                    f"Could not {description} for tag {tag_id}: {exc}; {suffix}",
                    level=level,
                    message_color=level,
                )
                if attempt < attempts:
                    QThread.msleep(250 * attempt)
        return None

    @classmethod
    def _cleanup_signature(cls, detail: dict) -> tuple:
        bookmark = detail["bookmark"]
        tag_names = tuple(
            sorted(
                tag.casefold()
                for tag in detail["tags"]
                if not cls._is_duplicate_bookkeeping_tag(tag)
            )
        )
        list_ids = tuple(sorted(record.id for record in detail["lists"]))
        return (
            cls._normalized_text(cls._bookmark_title(bookmark)),
            cls._normalized_text(cls._bookmark_description(bookmark)),
            list_ids,
            tag_names,
        )

    @staticmethod
    def _normalized_text(value: str) -> str:
        return " ".join(value.casefold().split())

    @staticmethod
    def _bookmark_description(bookmark: dict) -> str:
        for key in ("description", "summary", "note"):
            value = bookmark.get(key)
            if value:
                return str(value)

        content = bookmark.get("content")
        if isinstance(content, dict):
            for key in ("description", "summary", "note", "text"):
                value = content.get(key)
                if value:
                    return str(value)

        return ""

    @classmethod
    def _extract_tag_names(cls, bookmark: dict) -> tuple[str, ...]:
        tags = bookmark.get("tags", [])
        tag_names: list[str] = []
        if isinstance(tags, list):
            for tag in tags:
                name: str | None = None
                if isinstance(tag, str):
                    name = tag
                elif isinstance(tag, dict):
                    name = cls._tag_name(tag)
                if name:
                    tag_names.append(name)
        return tuple(tag_names)

    @classmethod
    def _extract_tag_sources(cls, bookmark: dict) -> dict[str, str]:
        tags = bookmark.get("tags", [])
        tag_sources: dict[str, str] = {}
        if not isinstance(tags, list):
            return tag_sources

        for tag in tags:
            if not isinstance(tag, dict):
                continue
            name = cls._tag_name(tag)
            if not name:
                continue
            attached_by = (
                tag.get("attachedBy")
                or tag.get("attached_by")
                or tag.get("source")
                or "human"
            )
            tag_sources[name.casefold()] = str(attached_by)
        return tag_sources

    @staticmethod
    def _source_for_tag(details: dict[str, dict], tag_name: str) -> str:
        normalized = tag_name.casefold()
        sources = {
            str(detail.get("tag_sources", {}).get(normalized, ""))
            for detail in details.values()
        }
        sources.discard("")
        if "human" in {source.casefold() for source in sources}:
            return "human"
        return sorted(sources, key=str.casefold)[0] if sources else "human"

    @staticmethod
    def _is_duplicate_bookkeeping_tag(tag_name: str) -> bool:
        normalized = tag_name.strip().casefold()
        return (
            normalized == POTENTIAL_DUPLICATE_TAG.casefold()
            or re.fullmatch(r"pd:\s*\d+", normalized) is not None
        )

    @staticmethod
    def _unique_bookmark_ids(group: DuplicateGroup) -> tuple[str, ...]:
        seen: set[str] = set()
        ids: list[str] = []
        for match in group.matches:
            if match.bookmark_id in seen:
                continue
            seen.add(match.bookmark_id)
            ids.append(match.bookmark_id)
        return tuple(ids)

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
        self._cleanup_total_groups = 0
        self._cleanup_processed_groups = 0

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
            self._log("Preparing duplicate maintenance job.")
            self.progress_range.emit(0, 0)

            if self.config.rescan:
                self._log("Retrieving bookmarks and preparing duplicate scan.")
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
            else:
                self._log("Skipping full hash rescan by request.")
                groups = scanner.existing_duplicate_groups(
                    log=self._log,
                    checkpoint=self._checkpoint,
                )
                self._matching_groups = len(groups)
                self._emit_stats()
                if not groups:
                    self._log("No existing PD duplicate groups found.", level="SUCCESS")
                    self.finished.emit(False, 0, 0, 0)
                    return

            if self._stop_requested:
                self.finished.emit(True, 0, 0, 0)
                return

            if (
                self.config.replicate_lists
                or self.config.replicate_tags
                or self.config.auto_cull
                or self.config.cleanup_resolved_duplicate_tags
            ):
                self._log("Starting duplicate cleanup options.")
                self._cleanup_total_groups = len(groups)
                self._cleanup_processed_groups = 0
                self._matching_groups = len(groups)
                self.progress_range.emit(max(len(groups), 1), len(groups))
                self._emit_stats()
                scanner.cleanup_duplicate_groups(
                    groups,
                    replicate_lists=self.config.replicate_lists,
                    replicate_tags=self.config.replicate_tags,
                    auto_cull=self.config.auto_cull,
                    cleanup_resolved_duplicate_tags=(
                        self.config.cleanup_resolved_duplicate_tags
                    ),
                    log=self._log,
                    checkpoint=self._checkpoint,
                    progress=self._handle_cleanup_progress,
                )
                if self._stop_requested:
                    self.finished.emit(True, 0, 0, 0)
                    return
                if (
                    self.config.auto_cull
                    or self.config.cleanup_resolved_duplicate_tags
                ):
                    self._log(
                        "Cleanup complete. Skipping full duplicate refresh; "
                        "run another duplicate check to reload current server state."
                    )
                    groups = tuple(
                        group
                        for group in groups
                        if len(group.matches) > 1
                    )
                    self._matching_groups = len(groups)
                    self._emit_stats()

            tag_ids = {
                f"{PD_TAG_PREFIX}{group.group_number}".casefold(): group.pd_tag_id
                for group in groups
                if group.pd_tag_id is not None
            }
            if not tag_ids and groups:
                try:
                    tag_ids = scanner.tag_ids_by_name()
                except (httpx.HTTPStatusError, httpx.RequestError) as exc:
                    self._log(
                        f"Could not refresh tag links for final report: {exc}. "
                        "Continuing with tag links unavailable.",
                        level="WARNING",
                        message_color="WARNING",
                    )
                    tag_ids = {}
            self._log("================================", level="")
            self._log("Potential duplicate groups:", message_color="WARNING")
            if not groups:
                self._log(
                    "No potential duplicate groups remain in the local cleanup result.",
                    level="SUCCESS",
                    message_color="SUCCESS",
                )
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

    def _handle_cleanup_progress(self, kind: str, value: int, detail) -> None:
        if kind != "cleanup":
            return

        self._cleanup_processed_groups = int(value)
        self._cleanup_total_groups = int(detail)
        self._matching_groups = max(
            self._cleanup_total_groups - self._cleanup_processed_groups,
            0,
        )
        self._completed_operations = self._cleanup_processed_groups
        self.progress.emit(
            self._cleanup_processed_groups,
            f"Cleaned duplicate group "
            f"{self._cleanup_processed_groups}/{self._cleanup_total_groups}",
            self._cleanup_processed_groups,
        )
        self._emit_stats()

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
