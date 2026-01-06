from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any
from urllib.parse import quote

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

    def put(self, path: str, **kwargs: Any) -> requests.Response:
        return requests.put(
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
                "filters": json.dumps([["Lead", "lead_name", "=", lead_name]]),
                "limit_page_length": 1,
            },
        )
        if resp.status_code != 200:
            return None
        data = resp.json().get("data") or []
        if not data:
            return None
        return str(data[0]["name"])

    def create_lead(self, lead_name: str, notes: str | None = None, status: str = "Lead") -> str:
        payload: dict[str, Any] = {"lead_name": lead_name, "status": status}
        if notes:
            payload["notes"] = notes
        resp = self.post("/api/resource/Lead", json=payload)
        resp.raise_for_status()
        return str(resp.json()["data"]["name"])

    def find_prospect_by_company_name(self, company_name: str) -> str | None:
        resp = self.get(
            "/api/resource/Prospect",
            params={
                "fields": '["name","company_name"]',
                "filters": json.dumps([["Prospect", "company_name", "=", company_name]]),
                "limit_page_length": 1,
            },
        )
        if resp.status_code != 200:
            return None
        data = resp.json().get("data") or []
        if not data:
            return None
        return str(data[0]["name"])

    def ensure_prospect(self, company_name: str, *, company: str) -> str:
        existing = self.find_prospect_by_company_name(company_name)
        if existing:
            return existing
        payload: dict[str, Any] = {
            "company_name": company_name,
            "company": company,
        }
        resp = self.post("/api/resource/Prospect", json=payload)
        if resp.status_code == 409:
            # Name collision or duplicate; re-check.
            existing = self.find_prospect_by_company_name(company_name)
            if existing:
                return existing
        resp.raise_for_status()
        return str(resp.json()["data"]["name"])

    def find_open_opportunity_for_prospect(self, prospect_name: str) -> str | None:
        resp = self.get(
            "/api/resource/Opportunity",
            params={
                "fields": '["name","status","opportunity_from","party_name"]',
                "filters": json.dumps(
                    [
                        ["Opportunity", "opportunity_from", "=", "Prospect"],
                        ["Opportunity", "party_name", "=", prospect_name],
                        ["Opportunity", "status", "in", ["Open", "Replied", "Quotation"]],
                    ]
                ),
                "limit_page_length": 1,
                "order_by": "modified desc",
            },
        )
        if resp.status_code != 200:
            return None
        data = resp.json().get("data") or []
        if not data:
            return None
        return str(data[0]["name"])

    def create_opportunity_for_prospect(
        self,
        prospect_name: str,
        *,
        company: str,
        transaction_date: str,
        title: str | None = None,
        status: str = "Open",
        naming_series: str = "CRM-OPP-.YYYY.-",
    ) -> str:
        payload: dict[str, Any] = {
            "naming_series": naming_series,
            "opportunity_from": "Prospect",
            "party_name": prospect_name,
            "status": status,
            "company": company,
            "transaction_date": transaction_date,
        }
        if title:
            payload["title"] = title
        resp = self.post("/api/resource/Opportunity", json=payload)
        resp.raise_for_status()
        return str(resp.json()["data"]["name"])

    def set_opportunity_status(self, opportunity_name: str, status: str) -> None:
        path = "/api/resource/Opportunity/" + quote(opportunity_name, safe="")
        resp = self.put(path, json={"status": status})
        resp.raise_for_status()
