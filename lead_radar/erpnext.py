from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import requests


@dataclass(frozen=True)
class ERPNextClient:
    base_url: str
    api_key: str
    api_secret: str
    timeout_seconds: int = 30

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"token {self.api_key}:{self.api_secret}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _url(self, path: str) -> str:
        return self.base_url.rstrip("/") + path

    def get(self, path: str, **kwargs: Any) -> requests.Response:
        return requests.get(
            self._url(path),
            headers=self._headers,
            timeout=self.timeout_seconds,
            **kwargs,
        )

    def post(self, path: str, **kwargs: Any) -> requests.Response:
        return requests.post(
            self._url(path),
            headers=self._headers,
            timeout=self.timeout_seconds,
            **kwargs,
        )

    def find_lead_by_name(self, lead_name: str) -> str | None:
        resp = self.get(
            "/api/resource/Lead",
            params={
                "fields": '["name","lead_name"]',
                "filters": f'[["Lead","lead_name","=","{lead_name}"]]',
                "limit_page_length": 1,
            },
        )
        if resp.status_code != 200:
            return None
        data = resp.json().get("data") or []
        if not data:
            return None
        return str(data[0]["name"])

    def create_lead(self, lead_name: str, notes: str | None = None) -> str:
        payload: dict[str, Any] = {"lead_name": lead_name}
        if notes:
            payload["notes"] = notes
        resp = self.post("/api/resource/Lead", json=payload)
        resp.raise_for_status()
        return str(resp.json()["data"]["name"])

