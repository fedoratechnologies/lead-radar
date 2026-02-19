from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import requests


def _env(name: str) -> str | None:
    value = os.getenv(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


@dataclass(frozen=True)
class HubDirectoryClient:
    base_url: str
    ingest_secret: str | None = None
    timeout_seconds: int = 30

    def ingest_organizations(self, organizations: list[dict[str, Any]]) -> dict[str, Any]:
        url = self.base_url.rstrip("/") + "/api/ingest/lead-radar"
        headers = {"Content-Type": "application/json"}
        if self.ingest_secret:
            headers["X-Radar-Ingest-Secret"] = self.ingest_secret

        response = requests.post(
            url,
            json={"organizations": organizations},
            headers=headers,
            timeout=int(self.timeout_seconds),
            allow_redirects=True,
        )

        if response.status_code not in {200, 201}:
            body = (response.text or "").strip()
            if len(body) > 600:
                body = body[:597].rstrip() + "..."
            raise RuntimeError(f"HubDirectory ingest failed ({response.status_code}): {body}")

        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("HubDirectory ingest returned non-object JSON.")
        return payload


def hubdirectory_client_from_env() -> HubDirectoryClient | None:
    base_url = _env("LEAD_RADAR_HUB_BASE_URL") or _env("HUBDIRECTORY_BASE_URL")
    if not base_url:
        return None
    ingest_secret = _env("LEAD_RADAR_HUB_INGEST_SECRET") or _env("HUBDIRECTORY_INGEST_SECRET")
    timeout = int(_env("LEAD_RADAR_HUB_TIMEOUT_SECONDS") or "30")
    return HubDirectoryClient(base_url=base_url, ingest_secret=ingest_secret, timeout_seconds=timeout)

