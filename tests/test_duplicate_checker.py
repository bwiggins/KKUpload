from __future__ import annotations

import unittest

from duplicate_checker import (
    DuplicateScanner,
    POTENTIAL_DUPLICATE_TAG,
)


class FakeDuplicateClient:
    server_url = "https://karakeep.example.test"

    def __init__(self) -> None:
        self.attached_tags: list[tuple[str, tuple[str, ...]]] = []
        self.assets = {
            "asset-1": b"same",
            "asset-2": b"different",
            "asset-3": b"same",
        }

    def list_bookmarks(self, *, include_content: bool = False) -> tuple[dict, ...]:
        return tuple(self.iter_bookmarks(include_content=include_content))

    def iter_bookmarks(self, *, include_content: bool = False):
        yield from (
            {
                "id": "bookmark-1",
                "title": "First",
                "content": {"type": "asset", "assetId": "asset-1"},
            },
            {
                "id": "bookmark-2",
                "title": "Second",
                "content": {"type": "asset", "assetId": "asset-2"},
            },
            {
                "id": "bookmark-3",
                "title": "Third",
                "content": {"type": "asset", "assetId": "asset-3"},
            },
        )

    def list_tags(self) -> tuple[dict, ...]:
        return (
            {"name": "PD: 1"},
            {"name": "PD: 2"},
            {"name": "other"},
        )

    def stream_asset_bytes(self, asset_id: str):
        yield self.assets[asset_id]

    def attach_tags_to_bookmark(
        self,
        *,
        bookmark_id: str,
        tag_names: tuple[str, ...],
    ) -> None:
        self.attached_tags.append((bookmark_id, tag_names))


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
                ("bookmark-1", (POTENTIAL_DUPLICATE_TAG, "PD: 3")),
                ("bookmark-3", (POTENTIAL_DUPLICATE_TAG, "PD: 3")),
            ],
        )


if __name__ == "__main__":
    unittest.main()
