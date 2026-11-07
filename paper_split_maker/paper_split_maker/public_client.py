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
        return self._read(url, parse_float=None)

    def get_json_decimal(self, url: str) -> object:
        """GET JSON, keeping the original decimal literals for prices and sizes."""
        return self._read(url, parse_float=str)

    def _read(self, url: str, *, parse_float) -> object:
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
            raise PublicDataError(
                f"http {exc.code} for {url}", status=exc.code
            ) from exc
        except urllib.error.URLError as exc:
            raise PublicDataError(f"network error for {url}: {exc.reason}") from exc
        try:
            kwargs = {} if parse_float is None else {"parse_float": parse_float}
            return json.loads(raw, **kwargs)
        except json.JSONDecodeError as exc:
            raise PublicDataError(f"invalid json from {url}") from exc


class PublicReadClient:
    def __init__(self, transport: Optional[Transport] = None):
        self.transport = transport or UrllibTransport()

    def discover_btc_updown(
        self, now_s: int, limit: int, slugs: Optional[list[str]] = None
    ) -> list[dict]:
        if slugs:
            candidates = list(slugs)
        else:
            start = (int(now_s) // 300) * 300
            candidates = [
                f"btc-updown-5m-{start + offset * 300}" for offset in range(4)
            ]
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

    def fetch_trades_page(
        self, condition_id: str, limit: int = 500, cursor: Optional[str] = None
    ) -> dict:
        params = {"condition": condition_id, "limit": str(int(limit))}
        if cursor:
            params["cursor"] = cursor
        query = urllib.parse.urlencode(params)
        payload = self._get(f"{DATA}/v2/trades?{query}", decimal_floats=True)
        if not isinstance(payload, dict):
            raise PublicDataError("trade payload is not an object")
        return payload

    def fetch_price_history(
        self,
        token_id: str,
        start_s: int,
        end_s: int,
        bucket_seconds: int = 60,
        cursor: Optional[str] = None,
    ) -> dict:
        params = {
            "token_id": token_id,
            "start": str(int(start_s)),
            "end": str(int(end_s)),
            "bucket_seconds": str(int(bucket_seconds)),
        }
        if cursor:
            params["cursor"] = cursor
        query = urllib.parse.urlencode(params)
        payload = self._get(f"{DATA}/v2/prices-history?{query}", decimal_floats=True)
        if not isinstance(payload, dict):
            raise PublicDataError("price history payload is not an object")
        return payload

    def _get(self, url: str, *, decimal_floats: bool) -> object:
        if decimal_floats and hasattr(self.transport, "get_json_decimal"):
            return self.transport.get_json_decimal(url)
        return self.transport.get_json(url)
