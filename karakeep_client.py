from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import Any

import httpx

from constants import KARAKEEP_API_PREFIX
from list_planner import ListRecord


class KarakeepClient:
    def __init__(
        self,
        server_url: str,
        api_key: str,
        *,
        timeout: float = 10.0,
    ) -> None:
        self.server_url = self.normalized_server_url(server_url)
        self.api_key = api_key.strip()
        self.timeout = timeout

    @staticmethod
    def normalized_server_url(server_url: str) -> str:
        return server_url.strip().rstrip("/")

    @property
    def api_base_url(self) -> str:
        return self.server_url + KARAKEEP_API_PREFIX

    def test_connection(self) -> tuple[bool, str]:
        if not self.server_url:
            return False, "Enter a Karakeep server URL."

        if not (
            self.server_url.startswith("http://")
            or self.server_url.startswith("https://")
        ):
            return False, "Server URL must start with http:// or https://."

        if not self.api_key:
            return False, "Enter a Karakeep API key."

        try:
            self.list_lists()
        except httpx.HTTPStatusError as exc:
            return False, f"Connection failed: HTTP {exc.response.status_code}."
        except httpx.RequestError as exc:
            return False, f"Connection failed: {exc}"

        return True, "Connected to Karakeep."

    def list_lists(self) -> tuple[ListRecord, ...]:
        records: list[ListRecord] = []
        cursor: str | None = None

        while True:
            params: dict[str, str | int] = {"limit": 100}
            if cursor is not None:
                params["cursor"] = cursor

            response = self._request("GET", "/lists", params=params)
            data = response.json()

            if isinstance(data, list):
                records.extend(self._parse_list(item) for item in data)
                break

            records.extend(
                self._parse_list(item)
                for item in data.get("lists", [])
            )
            cursor = data.get("nextCursor")
            if cursor is None:
                break

        return tuple(records)

    def create_manual_list(
        self,
        *,
        name: str,
        parent_id: str | None,
    ) -> ListRecord:
        payload: dict[str, Any] = {
            "name": name,
            "icon": "\U0001f4c1",
            "type": "manual",
        }

        if parent_id is not None:
            payload["parentId"] = parent_id

        response = self._request("POST", "/lists", json=payload)
        return self._parse_list(response.json())

    def upload_asset(self, file_path: Path) -> dict[str, Any]:
        content_type = (
            mimetypes.guess_type(file_path.name)[0]
            or "application/octet-stream"
        )
        with file_path.open("rb") as file:
            response = self._request(
                "POST",
                "/assets",
                files={
                    "file": (
                        file_path.name,
                        file,
                        content_type,
                    )
                },
            )
        return response.json()

    def create_asset_bookmark(
        self,
        *,
        asset_id: str,
        file_name: str,
    ) -> dict[str, Any]:
        response = self._request(
            "POST",
            "/bookmarks",
            json={
                "type": "asset",
                "assetType": "image",
                "assetId": asset_id,
                "fileName": file_name,
                "title": file_name,
                "source": "api",
            },
        )
        return response.json()

    def add_bookmark_to_list(self, *, list_id: str, bookmark_id: str) -> None:
        self._request("PUT", f"/lists/{list_id}/bookmarks/{bookmark_id}")

    def remove_bookmark_from_list(self, *, list_id: str, bookmark_id: str) -> None:
        self._request("DELETE", f"/lists/{list_id}/bookmarks/{bookmark_id}")

    def attach_tags_to_bookmark(
        self,
        *,
        bookmark_id: str,
        tag_names: tuple[str, ...],
        attached_by: str = "human",
    ) -> None:
        if not tag_names:
            return

        self._request(
            "POST",
            f"/bookmarks/{bookmark_id}/tags",
            json={
                "tags": [
                    {
                        "tagName": tag_name,
                        "attachedBy": attached_by,
                    }
                    for tag_name in tag_names
                ]
            },
        )

    def detach_tags_from_bookmark(
        self,
        *,
        bookmark_id: str,
        tag_names: tuple[str, ...],
    ) -> None:
        if not tag_names:
            return

        self._request(
            "DELETE",
            f"/bookmarks/{bookmark_id}/tags",
            json={
                "tags": [
                    {
                        "tagName": tag_name,
                        "attachedBy": "human",
                    }
                    for tag_name in tag_names
                ]
            },
        )

    def get_bookmark(self, bookmark_id: str) -> dict[str, Any]:
        response = self._request("GET", f"/bookmarks/{bookmark_id}")
        return response.json()

    def get_bookmark_lists(self, bookmark_id: str) -> tuple[ListRecord, ...]:
        response = self._request("GET", f"/bookmarks/{bookmark_id}/lists")
        data = response.json()
        if isinstance(data, list):
            return tuple(self._parse_list(item) for item in data)
        return tuple(self._parse_list(item) for item in data.get("lists", []))

    def delete_bookmark(self, bookmark_id: str) -> None:
        self._request("DELETE", f"/bookmarks/{bookmark_id}")

    def delete_tag(self, tag_id: str) -> None:
        self._request("DELETE", f"/tags/{tag_id}")

    def list_bookmarks(self, *, include_content: bool = False) -> tuple[dict, ...]:
        return tuple(self.iter_bookmarks(include_content=include_content))

    def iter_bookmarks(self, *, include_content: bool = False):
        cursor: str | None = None

        while True:
            params: dict[str, str | int | bool] = {
                "limit": 100,
                "includeContent": include_content,
            }
            if cursor is not None:
                params["cursor"] = cursor

            response = self._request("GET", "/bookmarks", params=params)
            data = response.json()

            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict):
                        yield item
                break

            for item in data.get("bookmarks", []):
                if isinstance(item, dict):
                    yield item
            cursor = data.get("nextCursor")
            if cursor is None:
                break

    def list_tags(self) -> tuple[dict, ...]:
        tags: list[dict] = []
        cursor: str | None = None

        while True:
            params: dict[str, str | int] = {"limit": 100}
            if cursor is not None:
                params["cursor"] = cursor

            response = self._request("GET", "/tags", params=params)
            data = response.json()

            if isinstance(data, list):
                tags.extend(item for item in data if isinstance(item, dict))
                break

            tags.extend(item for item in data.get("tags", []) if isinstance(item, dict))
            cursor = data.get("nextCursor")
            if cursor is None:
                break

        return tuple(tags)

    def list_bookmarks_for_tag(
        self,
        tag_id: str,
        *,
        include_content: bool = False,
    ) -> tuple[dict, ...]:
        bookmarks: list[dict] = []
        cursor: str | None = None

        while True:
            params: dict[str, str | int | bool] = {
                "limit": 100,
                "includeContent": include_content,
            }
            if cursor is not None:
                params["cursor"] = cursor

            response = self._request(
                "GET",
                f"/tags/{tag_id}/bookmarks",
                params=params,
            )
            data = response.json()

            if isinstance(data, list):
                bookmarks.extend(item for item in data if isinstance(item, dict))
                break

            bookmarks.extend(
                item for item in data.get("bookmarks", []) if isinstance(item, dict)
            )
            cursor = data.get("nextCursor")
            if cursor is None:
                break

        return tuple(bookmarks)

    def stream_asset_bytes(self, asset_id: str):
        with httpx.stream(
            "GET",
            self.api_base_url + f"/assets/{asset_id}",
            headers={"Authorization": f"Bearer {self.api_key}"},
            timeout=self.timeout,
        ) as response:
            response.raise_for_status()
            yield from response.iter_bytes()

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        response = httpx.request(
            method,
            self.api_base_url + path,
            headers={"Authorization": f"Bearer {self.api_key}"},
            timeout=self.timeout,
            **kwargs,
        )
        response.raise_for_status()
        return response

    @staticmethod
    def _parse_list(item: dict[str, Any]) -> ListRecord:
        return ListRecord(
            id=str(item["id"]),
            name=str(item["name"]),
            parent_id=(
                str(item["parentId"])
                if item.get("parentId") is not None
                else None
            ),
            type=str(item.get("type", "manual")),
        )


def describe_http_error(exc: httpx.HTTPStatusError) -> str:
    response_text = exc.response.text.strip()
    status = f"HTTP {exc.response.status_code}"
    if not response_text:
        return status
    return f"{status}: {response_text}"
