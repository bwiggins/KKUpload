from __future__ import annotations

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
