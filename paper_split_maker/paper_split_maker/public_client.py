"""Read-only client for the documented public Polymarket endpoints.

Hosts are fixed to the official Gamma, CLOB, and Data API origins. Requests
are GET with no body and no credential header. This module has no order,
cancel, or signing method.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional, Protocol

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
DATA = "https://data-api.polymarket.com"
ALLOWED_PREFIXES = (GAMMA, CLOB, DATA)
USER_AGENT = "paper-split-maker/0.1 (read-only public data; no orders)"


class PublicDataError(RuntimeError):
    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


class Transport(Protocol):
    def get_json(self, url: str) -> object:
        ...


class UrllibTransport:
    def __init__(self, timeout: float = 8.0):
        self.timeout = timeout

    def get_json(self, url: str) -> object:
        if not url.startswith(ALLOWED_PREFIXES):
            raise PublicDataError("refusing non-official host")
        request = urllib.request.Request(
            url,
            headers={"Accept": "application/json", "User-Agent": USER_AGENT},
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            raise PublicDataError(f"http {exc.code} for {url}", status=exc.code) from exc
        except urllib.error.URLError as exc:
            raise PublicDataError(f"network error for {url}: {exc.reason}") from exc
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise PublicDataError(f"invalid json from {url}") from exc


class PublicReadClient:
    def __init__(self, transport: Optional[Transport] = None):
        self.transport = transport or UrllibTransport()

    def discover_btc_updown(self, now_s: int, limit: int, slugs: Optional[list[str]] = None) -> list[dict]:
        if slugs:
            candidates = list(slugs)
        else:
            start = (int(now_s) // 300) * 300
            candidates = [f"btc-updown-5m-{start + offset * 300}" for offset in range(4)]
        found: list[dict] = []
        for slug in candidates:
            if len(found) >= limit:
                break
            url = f"{GAMMA}/markets/slug/{urllib.parse.quote(slug, safe='')}"
            try:
                payload = self.transport.get_json(url)
            except PublicDataError as exc:
                if exc.status == 404:
                    continue
                raise
            if isinstance(payload, list):
                payload = payload[0] if payload else None
            if isinstance(payload, dict):
                found.append(payload)
        return found

    def fetch_book(self, token_id: str) -> dict:
        query = urllib.parse.urlencode({"token_id": token_id})
        payload = self.transport.get_json(f"{CLOB}/book?{query}")
        if not isinstance(payload, dict):
            raise PublicDataError("book payload is not an object")
        return payload

    def fetch_trades(self, condition_id: str, limit: int = 50) -> dict:
        query = urllib.parse.urlencode({"condition": condition_id, "limit": str(limit)})
        payload = self.transport.get_json(f"{DATA}/v2/trades?{query}")
        if not isinstance(payload, dict):
            raise PublicDataError("trade payload is not an object")
        return payload

    def fetch_resolution(self, condition_id: str) -> dict:
        query = urllib.parse.urlencode({"condition": condition_id})
        payload = self.transport.get_json(f"{DATA}/v2/resolutions?{query}")
        if not isinstance(payload, dict):
            raise PublicDataError("resolution payload is not an object")
        return payload
