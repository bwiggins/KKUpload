from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

import httpx

from karakeep_client import KarakeepClient


class RecordingClient(KarakeepClient):
    def __init__(self) -> None:
        super().__init__("https://karakeep.example.test", "token")
        self.last_request: dict[str, Any] | None = None

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        self.last_request = {
            "method": method,
            "path": path,
            "kwargs": kwargs,
        }
        if path == "/assets":
            return httpx.Response(
                200,
                json={
                    "assetId": "asset-1",
                    "contentType": "image/jpeg",
                    "size": 1,
                    "fileName": "image.jpg",
                },
            )

        if path == "/bookmarks" and method == "GET":
            return httpx.Response(
                200,
                json={
                    "bookmarks": [
                        {"id": "bookmark-1"},
                        {"id": "bookmark-2"},
                    ],
                    "nextCursor": None,
                },
            )

        if path == "/tags" and method == "GET":
            return httpx.Response(
                200,
                json={
                    "tags": [
                        {"id": "tag-1", "name": "POTENTIAL_DUPLICATE"},
                        {"id": "tag-2", "name": "PD: 4"},
                    ],
                    "nextCursor": None,
                },
            )

        if path == "/tags/tag-2/bookmarks" and method == "GET":
            return httpx.Response(
                200,
                json={
                    "bookmarks": [{"id": "bookmark-1"}],
                    "nextCursor": None,
                },
            )

        if method == "DELETE":
            return httpx.Response(204)

        if method == "PATCH" and path.startswith("/bookmarks/"):
            return httpx.Response(
                200,
                json={
                    "id": path.rsplit("/", 1)[-1],
                    **kwargs["json"],
                },
            )

        if path == "/bookmarks":
            return httpx.Response(
                201,
                json={
                    "id": "bookmark-1",
                    "content": {
                        "type": "asset",
                        "assetType": "image",
                        "assetId": kwargs["json"]["assetId"],
                    },
                    "tags": [],
                    "assets": [],
                },
            )

        if path.endswith("/tags"):
            return httpx.Response(200, json={"attached": []})

        return httpx.Response(
            200,
            json={
                "id": "list-1",
                "name": kwargs["json"]["name"],
                "parentId": kwargs["json"].get("parentId"),
                "type": "manual",
            },
        )


class KarakeepClientTests(unittest.TestCase):
    def test_create_manual_list_does_not_send_folder_icon_text(self) -> None:
        client = RecordingClient()

        created = client.create_manual_list(
            name="Rose",
            parent_id="parent-1",
        )

        self.assertEqual(created.name, "Rose")
        self.assertIsNotNone(client.last_request)
        payload = client.last_request["kwargs"]["json"]
        self.assertEqual(
            payload,
            {
                "name": "Rose",
                "icon": "\U0001f4c1",
                "type": "manual",
                "parentId": "parent-1",
            },
        )
        self.assertNotEqual(payload["icon"], "folder")

    def test_upload_asset_sends_multipart_file(self) -> None:
        client = RecordingClient()

        with tempfile.TemporaryDirectory() as temp_dir:
            file_path = Path(temp_dir) / "image.jpg"
            file_path.write_bytes(b"x")

            uploaded = client.upload_asset(file_path)

        self.assertEqual(uploaded["assetId"], "asset-1")
        self.assertIsNotNone(client.last_request)
        self.assertEqual(client.last_request["method"], "POST")
        self.assertEqual(client.last_request["path"], "/assets")
        files = client.last_request["kwargs"]["files"]
        self.assertIn("file", files)
        self.assertEqual(files["file"][0], "image.jpg")
        self.assertEqual(files["file"][2], "image/jpeg")

    def test_create_asset_bookmark_payload(self) -> None:
        client = RecordingClient()

        created = client.create_asset_bookmark(
            asset_id="asset-1",
            file_name="image.jpg",
        )

        self.assertEqual(created["id"], "bookmark-1")
        self.assertIsNotNone(client.last_request)
        self.assertEqual(client.last_request["method"], "POST")
        self.assertEqual(client.last_request["path"], "/bookmarks")
        self.assertEqual(
            client.last_request["kwargs"]["json"],
            {
                "type": "asset",
                "assetType": "image",
                "assetId": "asset-1",
                "fileName": "image.jpg",
                "title": "image.jpg",
                "source": "api",
            },
        )

    def test_attach_tags_payload(self) -> None:
        client = RecordingClient()

        client.attach_tags_to_bookmark(
            bookmark_id="bookmark-1",
            tag_names=("!!-TAGGING-!!", "figure"),
        )

        self.assertIsNotNone(client.last_request)
        self.assertEqual(client.last_request["method"], "POST")
        self.assertEqual(
            client.last_request["path"],
            "/bookmarks/bookmark-1/tags",
        )
        self.assertEqual(
            client.last_request["kwargs"]["json"],
            {
                "tags": [
                    {
                        "tagName": "!!-TAGGING-!!",
                        "attachedBy": "human",
                    },
                    {
                        "tagName": "figure",
                        "attachedBy": "human",
                    },
                ]
            },
        )

    def test_list_bookmarks_requests_content(self) -> None:
        client = RecordingClient()

        bookmarks = client.list_bookmarks(include_content=True)

        self.assertEqual(
            [bookmark["id"] for bookmark in bookmarks],
            ["bookmark-1", "bookmark-2"],
        )
        self.assertIsNotNone(client.last_request)
        self.assertEqual(client.last_request["method"], "GET")
        self.assertEqual(client.last_request["path"], "/bookmarks")
        self.assertEqual(
            client.last_request["kwargs"]["params"],
            {
                "limit": 100,
                "includeContent": True,
            },
        )

    def test_list_tags_returns_tag_records(self) -> None:
        client = RecordingClient()

        tags = client.list_tags()

        self.assertEqual(
            [tag["name"] for tag in tags],
            ["POTENTIAL_DUPLICATE", "PD: 4"],
        )
        self.assertIsNotNone(client.last_request)
        self.assertEqual(client.last_request["method"], "GET")
        self.assertEqual(client.last_request["path"], "/tags")

    def test_detach_tags_payload(self) -> None:
        client = RecordingClient()

        client.detach_tags_from_bookmark(
            bookmark_id="bookmark-1",
            tag_names=("POTENTIAL_DUPLICATE", "PD: 3"),
        )

        self.assertIsNotNone(client.last_request)
        self.assertEqual(client.last_request["method"], "DELETE")
        self.assertEqual(
            client.last_request["path"],
            "/bookmarks/bookmark-1/tags",
        )
        self.assertEqual(
            client.last_request["kwargs"]["json"],
            {
                "tags": [
                    {
                        "tagName": "POTENTIAL_DUPLICATE",
                        "attachedBy": "human",
                    },
                    {
                        "tagName": "PD: 3",
                        "attachedBy": "human",
                    },
                ]
            },
        )

    def test_delete_bookmark_uses_bookmark_endpoint(self) -> None:
        client = RecordingClient()

        client.delete_bookmark("bookmark-1")

        self.assertIsNotNone(client.last_request)
        self.assertEqual(client.last_request["method"], "DELETE")
        self.assertEqual(client.last_request["path"], "/bookmarks/bookmark-1")

    def test_update_bookmark_note_uses_patch_endpoint(self) -> None:
        client = RecordingClient()

        client.update_bookmark_note(
            bookmark_id="bookmark-1",
            note="DUPLICATE TITLES:\nFirst\nSecond",
        )

        self.assertIsNotNone(client.last_request)
        self.assertEqual(client.last_request["method"], "PATCH")
        self.assertEqual(client.last_request["path"], "/bookmarks/bookmark-1")
        self.assertEqual(
            client.last_request["kwargs"]["json"],
            {"note": "DUPLICATE TITLES:\nFirst\nSecond"},
        )

    def test_delete_tag_uses_tag_endpoint(self) -> None:
        client = RecordingClient()

        client.delete_tag("tag-2")

        self.assertIsNotNone(client.last_request)
        self.assertEqual(client.last_request["method"], "DELETE")
        self.assertEqual(client.last_request["path"], "/tags/tag-2")

    def test_list_bookmarks_for_tag_requests_tag_bookmarks(self) -> None:
        client = RecordingClient()

        bookmarks = client.list_bookmarks_for_tag("tag-2", include_content=True)

        self.assertEqual([bookmark["id"] for bookmark in bookmarks], ["bookmark-1"])
        self.assertIsNotNone(client.last_request)
        self.assertEqual(client.last_request["method"], "GET")
        self.assertEqual(client.last_request["path"], "/tags/tag-2/bookmarks")
        self.assertEqual(
            client.last_request["kwargs"]["params"],
            {
                "limit": 100,
                "includeContent": True,
            },
        )


if __name__ == "__main__":
    unittest.main()
