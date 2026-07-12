from __future__ import annotations

import unittest
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


if __name__ == "__main__":
    unittest.main()
