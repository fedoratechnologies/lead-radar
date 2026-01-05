from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

from .github_api import GitHubTarget, upsert_file
from .render_configmap import render_lead_radar_configmap


def _env_required(name: str) -> str:
    val = os.getenv(name, "").strip()
    if not val:
        raise RuntimeError(f"{name} is required")
    return val


def _env_int(name: str, default: int) -> int:
    val = os.getenv(name, "").strip()
    if not val:
        return default
    return int(val)


def _json_response(handler: BaseHTTPRequestHandler, status: int, payload: dict[str, Any]) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


class PublisherHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        # keep logs concise
        super().log_message(format, *args)

    def do_GET(self) -> None:  # noqa: N802
        if self.path in {"/healthz", "/"}:
            _json_response(self, 200, {"ok": True})
            return
        _json_response(self, 404, {"error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/publish":
            _json_response(self, 404, {"error": "not_found"})
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            _json_response(self, 400, {"error": "invalid_content_length"})
            return

        raw_body = self.rfile.read(length)
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except Exception:
            _json_response(self, 400, {"error": "invalid_json"})
            return

        if not isinstance(payload, dict):
            _json_response(self, 400, {"error": "invalid_payload"})
            return

        scoring = payload.get("scoring")
        sources = payload.get("sources")
        keyword_packs = payload.get("keyword_packs")
        if not isinstance(scoring, dict) or not isinstance(sources, list) or not isinstance(keyword_packs, list):
            _json_response(self, 400, {"error": "missing_or_invalid_fields"})
            return

        try:
            token = _env_required("LEAD_RADAR_GITHUB_TOKEN")
            target = GitHubTarget(
                owner=_env_required("LEAD_RADAR_GITHUB_OWNER"),
                repo=_env_required("LEAD_RADAR_GITHUB_REPO"),
                branch=os.getenv("LEAD_RADAR_GITHUB_BRANCH", "main").strip() or "main",
                path=os.getenv("LEAD_RADAR_GITHUB_PATH", "k8s/lead-radar-configmap.yaml").strip()
                or "k8s/lead-radar-configmap.yaml",
            )
        except Exception as e:
            _json_response(self, 500, {"error": "server_misconfigured", "detail": str(e)})
            return

        configmap_yaml = render_lead_radar_configmap(
            scoring=scoring,
            sources=sources,
            keyword_packs=keyword_packs,
        ).encode("utf-8")

        message = payload.get("message")
        if not isinstance(message, str) or not message.strip():
            ts = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            message = f"Lead Radar publish ({ts})"

        try:
            result = upsert_file(target=target, token=token, content=configmap_yaml, message=message)
        except Exception as e:
            _json_response(self, 502, {"error": "github_write_failed", "detail": str(e)})
            return

        commit_sha = (
            ((result.get("commit") or {}).get("sha"))
            if isinstance(result.get("commit"), dict)
            else None
        )
        html_url = (
            ((result.get("commit") or {}).get("html_url"))
            if isinstance(result.get("commit"), dict)
            else None
        )
        _json_response(self, 200, {"ok": True, "commit_sha": commit_sha, "commit_url": html_url})


def main() -> None:
    host = os.getenv("LEAD_RADAR_PUBLISHER_HOST", "0.0.0.0").strip() or "0.0.0.0"
    port = _env_int("LEAD_RADAR_PUBLISHER_PORT", 8080)
    server = HTTPServer((host, port), PublisherHandler)
    server.serve_forever()

