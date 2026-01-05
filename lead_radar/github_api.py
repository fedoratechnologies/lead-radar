from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any

import requests


GITHUB_API_BASE = "https://api.github.com"


@dataclass(frozen=True)
class GitHubTarget:
    owner: str
    repo: str
    branch: str
    path: str


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "lead-radar-publisher",
    }


def get_file_sha(target: GitHubTarget, token: str, timeout_seconds: int = 30) -> str | None:
    resp = requests.get(
        f"{GITHUB_API_BASE}/repos/{target.owner}/{target.repo}/contents/{target.path}",
        headers=_headers(token),
        params={"ref": target.branch},
        timeout=timeout_seconds,
    )
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    data = resp.json()
    sha = data.get("sha")
    return str(sha) if sha else None


def upsert_file(
    target: GitHubTarget,
    token: str,
    content: bytes,
    message: str,
    timeout_seconds: int = 30,
) -> dict[str, Any]:
    sha = get_file_sha(target=target, token=token, timeout_seconds=timeout_seconds)
    payload: dict[str, Any] = {
        "message": message,
        "content": base64.b64encode(content).decode("ascii"),
        "branch": target.branch,
    }
    if sha:
        payload["sha"] = sha

    resp = requests.put(
        f"{GITHUB_API_BASE}/repos/{target.owner}/{target.repo}/contents/{target.path}",
        headers=_headers(token),
        json=payload,
        timeout=timeout_seconds,
    )
    resp.raise_for_status()
    return resp.json()

