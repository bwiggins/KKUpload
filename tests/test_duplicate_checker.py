from __future__ import annotations

import unittest

import httpx

from duplicate_checker import (
    DuplicateGroup,
    DuplicateScanner,
    BookmarkAssetFingerprint,
    POTENTIAL_DUPLICATE_TAG,
)
from list_planner import ListRecord


class FakeDuplicateClient:
    server_url = "https://karakeep.example.test"

    def __init__(self) -> None:
        self.attached_tags: list[tuple[str, tuple[str, ...]]] = []
        self.detached_tags: list[tuple[str, tuple[str, ...]]] = []
        self.added_lists: list[tuple[str, str]] = []
        self.deleted_bookmarks: list[str] = []
        self.deleted_tags: list[str] = []
        self.updated_notes: list[tuple[str, str]] = []
        self.list_tags_calls = 0
        self.assets = {
            "asset-1": b"same",
            "asset-2": b"different",
            "asset-3": b"same",
        }
        self.bookmarks = {
            "bookmark-1": {
                "id": "bookmark-1",
                "title": "First",
                "description": "",
                "content": {"type": "asset", "assetId": "asset-1"},
                "tags": [{"name": "PD: 3"}, {"name": "POTENTIAL_DUPLICATE"}],
            },
            "bookmark-2": {
                "id": "bookmark-2",
                "title": "Second",
                "description": "",
                "content": {"type": "asset", "assetId": "asset-2"},
                "tags": [],
            },
            "bookmark-3": {
                "id": "bookmark-3",
                "title": "First",
                "description": "",
                "content": {"type": "asset", "assetId": "asset-3"},
                "tags": [
                    {"name": "PD: 3"},
                    {"name": "POTENTIAL_DUPLICATE"},
                    {"name": "photo"},
                ],
            },
        }
        self.bookmark_lists = {
            "bookmark-1": [ListRecord("list-1", "List 1", None, "manual")],
            "bookmark-2": [],
            "bookmark-3": [ListRecord("list-2", "List 2", None, "manual")],
        }

    def list_bookmarks(self, *, include_content: bool = False) -> tuple[dict, ...]:
        return tuple(self.iter_bookmarks(include_content=include_content))

    def iter_bookmarks(self, *, include_content: bool = False):
        yield from self.bookmarks.values()

    def list_tags(self) -> tuple[dict, ...]:
        self.list_tags_calls += 1
        return (
            {"name": "PD: 1"},
            {"name": "PD: 2"},
            {"id": "tag-99", "name": "PD: 99"},
            {"name": "other"},
        )

    def stream_asset_bytes(self, asset_id: str):
        yield self.assets[asset_id]

    def attach_tags_to_bookmark(
        self,
        *,
        bookmark_id: str,
        tag_names: tuple[str, ...],
        attached_by: str = "human",
    ) -> None:
        self.attached_tags.append((bookmark_id, tag_names, attached_by))

    def detach_tags_from_bookmark(
        self,
        *,
        bookmark_id: str,
        tag_names: tuple[str, ...],
    ) -> None:
        self.detached_tags.append((bookmark_id, tag_names))

    def get_bookmark(self, bookmark_id: str) -> dict:
        return self.bookmarks[bookmark_id]

    def get_bookmark_lists(self, bookmark_id: str) -> tuple[ListRecord, ...]:
        return tuple(self.bookmark_lists[bookmark_id])

    def update_bookmark_note(
        self,
        *,
        bookmark_id: str,
        note: str,
    ) -> dict:
        self.updated_notes.append((bookmark_id, note))
        self.bookmarks[bookmark_id]["note"] = note
        return self.bookmarks[bookmark_id]

    def add_bookmark_to_list(self, *, list_id: str, bookmark_id: str) -> None:
        self.added_lists.append((bookmark_id, list_id))
        records = self.bookmark_lists[bookmark_id]
        if all(record.id != list_id for record in records):
            records.append(ListRecord(list_id, list_id, None, "manual"))

    def delete_bookmark(self, bookmark_id: str) -> None:
        self.deleted_bookmarks.append(bookmark_id)

    def delete_tag(self, tag_id: str) -> None:
        self.deleted_tags.append(tag_id)

    def list_bookmarks_for_tag(
        self,
        tag_id: str,
        *,
        include_content: bool = False,
    ) -> tuple[dict, ...]:
        if tag_id != "tag-99":
            return ()
        return (self.bookmarks["bookmark-1"],)


class FlakyExistingGroupClient(FakeDuplicateClient):
    def __init__(self) -> None:
        super().__init__()
        self.group_requests = 0

    def list_tags(self) -> tuple[dict, ...]:
        return (
            {"id": "tag-1", "name": "PD: 1"},
            {"id": "tag-99", "name": "PD: 99"},
        )

    def list_bookmarks_for_tag(
        self,
        tag_id: str,
        *,
        include_content: bool = False,
    ) -> tuple[dict, ...]:
        self.group_requests += 1
        if tag_id == "tag-1":
            request = httpx.Request("GET", "https://karakeep.example.test")
            raise httpx.ConnectError("[Errno 11001] getaddrinfo failed", request=request)
        return super().list_bookmarks_for_tag(
            tag_id,
            include_content=include_content,
        )


class FailingTagListClient(FakeDuplicateClient):
    def list_tags(self) -> tuple[dict, ...]:
        self.list_tags_calls += 1
        request = httpx.Request("GET", "https://karakeep.example.test")
        raise httpx.ConnectError("[WinError 10053] connection aborted", request=request)


class DuplicateScannerTests(unittest.TestCase):
    def test_find_duplicate_groups_uses_unused_pd_number(self) -> None:
        client = FakeDuplicateClient()
        scanner = DuplicateScanner(client)
        progress_events: list[tuple[str, int, object]] = []

        groups = scanner.find_duplicate_groups(
            log=lambda _message, **_kwargs: None,
            checkpoint=lambda: None,
            progress=lambda kind, value, detail: progress_events.append(
                (kind, value, detail)
            ),
        )

        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].group_number, 3)
        self.assertEqual(
            [match.bookmark_id for match in groups[0].matches],
            ["bookmark-1", "bookmark-3"],
        )
        self.assertEqual(progress_events[0], ("scanned", 1, 1))
        self.assertIn(("hash_range", 3, 3), progress_events)

    def test_tag_duplicate_groups_applies_common_and_group_tags(self) -> None:
        client = FakeDuplicateClient()
        scanner = DuplicateScanner(client)
        groups = scanner.find_duplicate_groups(
            log=lambda _message, **_kwargs: None,
            checkpoint=lambda: None,
            progress=lambda *_args: None,
        )

        scanner.tag_duplicate_groups(
            groups,
            log=lambda _message, **_kwargs: None,
            checkpoint=lambda: None,
            progress=lambda *_args: None,
        )

        self.assertEqual(
            client.attached_tags,
            [
                ("bookmark-1", (POTENTIAL_DUPLICATE_TAG, "PD: 3"), "human"),
                ("bookmark-3", (POTENTIAL_DUPLICATE_TAG, "PD: 3"), "human"),
            ],
        )

    def test_bookmark_url_uses_karakeep_preview_route(self) -> None:
        self.assertEqual(
            DuplicateScanner.bookmark_url(
                "https://karakeep.example.test/",
                "bookmark-1",
            ),
            "https://karakeep.example.test/dashboard/preview/bookmark-1",
        )

    def test_tag_ids_and_tag_url_use_dashboard_tag_route(self) -> None:
        scanner = DuplicateScanner(FakeDuplicateClient())

        self.assertEqual(scanner.tag_ids_by_name()["pd: 99"], "tag-99")
        self.assertEqual(
            DuplicateScanner.tag_url("https://karakeep.example.test", "tag-99"),
            "https://karakeep.example.test/dashboard/tags/tag-99",
        )

    def test_existing_duplicate_groups_skips_group_after_retrieval_failures(self) -> None:
        client = FlakyExistingGroupClient()
        scanner = DuplicateScanner(client)
        log_messages: list[str] = []

        groups = scanner.existing_duplicate_groups(
            log=lambda message, **_kwargs: log_messages.append(message),
            checkpoint=lambda: None,
        )

        self.assertEqual([group.group_number for group in groups], [99])
        self.assertEqual(groups[0].pd_tag_id, "tag-99")
        self.assertEqual(client.group_requests, 4)
        self.assertTrue(
            any(
                "Skipping existing duplicate group PD: 1" in message
                for message in log_messages
            )
        )

    def test_existing_duplicate_groups_returns_empty_when_tag_list_fails(self) -> None:
        client = FailingTagListClient()
        scanner = DuplicateScanner(client)
        log_messages: list[str] = []

        groups = scanner.existing_duplicate_groups(
            log=lambda message, **_kwargs: log_messages.append(message),
            checkpoint=lambda: None,
        )

        self.assertEqual(groups, ())
        self.assertEqual(client.list_tags_calls, 3)
        self.assertTrue(
            any(
                "Could not retrieve existing PD tag list" in message
                for message in log_messages
            )
        )


    def test_cleanup_replicates_lists_and_tags(self) -> None:
        client = FakeDuplicateClient()
        scanner = DuplicateScanner(client)
        groups = scanner.find_duplicate_groups(
            log=lambda _message, **_kwargs: None,
            checkpoint=lambda: None,
            progress=lambda *_args: None,
        )

        scanner.cleanup_duplicate_groups(
            groups,
            aggressive_duplicate_clearing=False,
            replicate_lists=True,
            replicate_tags=True,
            auto_cull=False,
            cleanup_resolved_duplicate_tags=True,
            log=lambda _message, **_kwargs: None,
            checkpoint=lambda: None,
        )

        self.assertIn(("bookmark-1", "list-2"), client.added_lists)
        self.assertIn(("bookmark-3", "list-1"), client.added_lists)
        self.assertIn(("bookmark-1", ("photo",), "human"), client.attached_tags)

    def test_cleanup_preserves_ai_tag_source_when_unioning_tags(self) -> None:
        client = FakeDuplicateClient()
        scanner = DuplicateScanner(client)
        client.bookmarks["bookmark-3"]["tags"] = [
            {"name": "PD: 3"},
            {"name": "POTENTIAL_DUPLICATE"},
            {"name": "ai-photo", "attachedBy": "ai"},
        ]
        groups = scanner.find_duplicate_groups(
            log=lambda _message, **_kwargs: None,
            checkpoint=lambda: None,
            progress=lambda *_args: None,
        )

        scanner.cleanup_duplicate_groups(
            groups,
            aggressive_duplicate_clearing=False,
            replicate_lists=False,
            replicate_tags=True,
            auto_cull=False,
            cleanup_resolved_duplicate_tags=True,
            log=lambda _message, **_kwargs: None,
            checkpoint=lambda: None,
        )

        self.assertIn(("bookmark-1", ("ai-photo",), "ai"), client.attached_tags)

    def test_cleanup_aggressive_keeps_first_copies_metadata_and_deletes_others(self) -> None:
        client = FakeDuplicateClient()
        scanner = DuplicateScanner(client)
        group = DuplicateGroup(
            group_number=3,
            digest="same",
            matches=(
                BookmarkAssetFingerprint("bookmark-1", "First", "asset-1", "same"),
                BookmarkAssetFingerprint("bookmark-3", "Third", "asset-3", "same"),
            ),
            pd_tag_id="tag-3",
        )
        client.bookmarks["bookmark-3"]["title"] = "Third"
        client.bookmarks["bookmark-1"]["note"] = "Existing note."

        results = scanner.cleanup_duplicate_groups(
            (group,),
            aggressive_duplicate_clearing=True,
            replicate_lists=False,
            replicate_tags=False,
            auto_cull=False,
            cleanup_resolved_duplicate_tags=False,
            log=lambda _message, **_kwargs: None,
            checkpoint=lambda: None,
        )

        self.assertEqual(results[0].deleted_count, 1)
        self.assertEqual(results[0].remaining_count, 1)
        self.assertEqual(client.deleted_bookmarks, ["bookmark-3"])
        self.assertIn(("bookmark-1", "list-2"), client.added_lists)
        self.assertIn(("bookmark-1", ("photo",), "human"), client.attached_tags)
        self.assertEqual(
            client.updated_notes,
            [("bookmark-1", "Existing note.\n\nDUPLICATE TITLES:\nFirst\nThird")],
        )
        self.assertEqual(
            client.detached_tags,
            [("bookmark-1", ("POTENTIAL_DUPLICATE",))],
        )
        self.assertEqual(client.deleted_tags, ["tag-3"])

    def test_cleanup_aggressive_writes_title_note_without_existing_note(self) -> None:
        client = FakeDuplicateClient()
        scanner = DuplicateScanner(client)
        group = DuplicateGroup(
            group_number=3,
            digest="same",
            matches=(
                BookmarkAssetFingerprint("bookmark-1", "First", "asset-1", "same"),
                BookmarkAssetFingerprint("bookmark-3", "Third", "asset-3", "same"),
            ),
        )
        client.bookmarks["bookmark-3"]["title"] = "Third"

        scanner.cleanup_duplicate_groups(
            (group,),
            aggressive_duplicate_clearing=True,
            replicate_lists=False,
            replicate_tags=False,
            auto_cull=False,
            cleanup_resolved_duplicate_tags=False,
            log=lambda _message, **_kwargs: None,
            checkpoint=lambda: None,
        )

        self.assertEqual(
            client.updated_notes,
            [("bookmark-1", "DUPLICATE TITLES:\nFirst\nThird")],
        )

    def test_cleanup_auto_culls_repeated_signature_and_removes_tags_from_single(self) -> None:
        client = FakeDuplicateClient()
        scanner = DuplicateScanner(client)
        group = DuplicateGroup(
            group_number=3,
            digest="same",
            matches=(
                BookmarkAssetFingerprint("bookmark-1", "First", "asset-1", "same"),
                BookmarkAssetFingerprint("bookmark-3", "First", "asset-3", "same"),
            ),
        )
        client.bookmark_lists["bookmark-3"] = list(client.bookmark_lists["bookmark-1"])
        client.bookmarks["bookmark-3"]["tags"] = [
            {"name": "PD: 3"},
            {"name": "POTENTIAL_DUPLICATE"},
        ]

        scanner.cleanup_duplicate_groups(
            (group,),
            aggressive_duplicate_clearing=False,
            replicate_lists=False,
            replicate_tags=False,
            auto_cull=True,
            cleanup_resolved_duplicate_tags=True,
            log=lambda _message, **_kwargs: None,
            checkpoint=lambda: None,
        )

        self.assertEqual(client.deleted_bookmarks, ["bookmark-3"])
        self.assertEqual(
            client.detached_tags,
            [("bookmark-1", ("POTENTIAL_DUPLICATE",))],
        )

    def test_cleanup_skips_missing_bookmark_after_retries(self) -> None:
        client = FakeDuplicateClient()
        scanner = DuplicateScanner(client)
        group = DuplicateGroup(
            group_number=3,
            digest="same",
            matches=(
                BookmarkAssetFingerprint("bookmark-1", "First", "asset-1", "same"),
                BookmarkAssetFingerprint("missing", "Missing", "asset-x", "same"),
            ),
        )
        log_messages: list[str] = []

        results = scanner.cleanup_duplicate_groups(
            (group,),
            aggressive_duplicate_clearing=False,
            replicate_lists=False,
            replicate_tags=False,
            auto_cull=True,
            cleanup_resolved_duplicate_tags=True,
            log=lambda message, **_kwargs: log_messages.append(message),
            checkpoint=lambda: None,
        )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].error_count, 1)
        self.assertEqual(results[0].remaining_count, 1)
        self.assertTrue(
            any("skipping this bookmark" in message for message in log_messages)
        )

    def test_cleanup_resolved_duplicate_tags_can_be_disabled(self) -> None:
        client = FakeDuplicateClient()
        scanner = DuplicateScanner(client)
        group = DuplicateGroup(
            group_number=3,
            digest="same",
            matches=(
                BookmarkAssetFingerprint("bookmark-1", "First", "asset-1", "same"),
            ),
        )

        scanner.cleanup_duplicate_groups(
            (group,),
            aggressive_duplicate_clearing=False,
            replicate_lists=False,
            replicate_tags=False,
            auto_cull=False,
            cleanup_resolved_duplicate_tags=False,
            log=lambda _message, **_kwargs: None,
            checkpoint=lambda: None,
        )

        self.assertEqual(client.detached_tags, [])
        self.assertEqual(client.deleted_tags, [])

    def test_cleanup_deletes_empty_pd_tag(self) -> None:
        client = FakeDuplicateClient()
        scanner = DuplicateScanner(client)
        group = DuplicateGroup(group_number=99, digest="empty", matches=())
        log_messages: list[str] = []

        scanner.cleanup_duplicate_groups(
            (group,),
            aggressive_duplicate_clearing=False,
            replicate_lists=False,
            replicate_tags=False,
            auto_cull=False,
            cleanup_resolved_duplicate_tags=True,
            log=lambda message, **_kwargs: log_messages.append(message),
            checkpoint=lambda: None,
        )

        self.assertEqual(client.deleted_tags, ["tag-99"])
        self.assertTrue(
            any("Deleting resolved duplicate tag PD: 99" in message for message in log_messages)
        )

    def test_cleanup_uses_existing_pd_tag_id_without_relisting_tags(self) -> None:
        client = FakeDuplicateClient()
        scanner = DuplicateScanner(client)
        groups = (
            DuplicateGroup(
                group_number=99,
                digest="empty",
                matches=(),
                pd_tag_id="tag-99",
            ),
            DuplicateGroup(
                group_number=100,
                digest="empty",
                matches=(),
                pd_tag_id="tag-100",
            ),
        )

        scanner.cleanup_duplicate_groups(
            groups,
            aggressive_duplicate_clearing=False,
            replicate_lists=False,
            replicate_tags=False,
            auto_cull=False,
            cleanup_resolved_duplicate_tags=True,
            log=lambda _message, **_kwargs: None,
            checkpoint=lambda: None,
        )

        self.assertEqual(client.list_tags_calls, 0)
        self.assertEqual(client.deleted_tags, ["tag-99", "tag-100"])


if __name__ == "__main__":
    unittest.main()
