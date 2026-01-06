from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from io import BytesIO
from typing import Any
from urllib.parse import urlparse

from minio import Minio


def _env(name: str) -> str | None:
    val = os.getenv(name)
    if val is None:
        return None
    val = val.strip()
    return val or None


def _parse_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    value = value.strip().lower()
    if value in {"1", "true", "t", "yes", "y", "on"}:
        return True
    if value in {"0", "false", "f", "no", "n", "off"}:
        return False
    return default


def _parse_endpoint(raw: str) -> tuple[str, bool]:
    raw = raw.strip()
    if "://" not in raw:
        return raw, False

    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("LEAD_RADAR_S3_ENDPOINT must be http(s) URL or host:port")
    if parsed.path not in {"", "/"}:
        raise ValueError("LEAD_RADAR_S3_ENDPOINT must not include a path")
    if not parsed.netloc:
        raise ValueError("LEAD_RADAR_S3_ENDPOINT missing host")
    return parsed.netloc, parsed.scheme == "https"


def json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [json_safe(v) for v in value]
    return str(value)


@dataclass(frozen=True)
class RawObjectLocation:
    bucket: str
    key: str
    etag: str | None = None
    version_id: str | None = None


@dataclass(frozen=True)
class RawStore:
    endpoint: str
    bucket: str
    prefix: str
    client: Minio

    def ensure_bucket(self) -> None:
        if not self.client.bucket_exists(self.bucket):
            self.client.make_bucket(self.bucket)

    def put_json(self, key: str, payload: Any) -> RawObjectLocation:
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        result = self.client.put_object(
            self.bucket,
            key,
            data=BytesIO(encoded),
            length=len(encoded),
            content_type="application/json; charset=utf-8",
        )
        return RawObjectLocation(
            bucket=self.bucket,
            key=key,
            etag=getattr(result, "etag", None),
            version_id=getattr(result, "version_id", None),
        )


def raw_store_from_env() -> RawStore | None:
    endpoint_raw = _env("LEAD_RADAR_S3_ENDPOINT")
    access_key = _env("LEAD_RADAR_S3_ACCESS_KEY")
    secret_key = _env("LEAD_RADAR_S3_SECRET_KEY")
    bucket = _env("LEAD_RADAR_S3_BUCKET")

    if not endpoint_raw or not access_key or not secret_key or not bucket:
        return None

    prefix = (_env("LEAD_RADAR_S3_PREFIX") or "lead-radar").strip().strip("/")
    endpoint, secure_by_scheme = _parse_endpoint(endpoint_raw)
    secure = _parse_bool(_env("LEAD_RADAR_S3_SECURE"), secure_by_scheme)

    client = Minio(endpoint, access_key=access_key, secret_key=secret_key, secure=secure)
    return RawStore(endpoint=endpoint, bucket=bucket, prefix=prefix, client=client)

