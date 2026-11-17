"""Read-only client for the predict.fun REST API.

Testnet ``https://api-testnet.predict.fun`` needs no API key. Mainnet
``https://api.predict.fun`` needs ``x-api-key`` and is not used unless the
caller selects it. This module has no order, cancel, or signing method.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal
from typing import Optional, Protocol

from paper_split_maker.chain_data import series_of, window_seconds
from paper_split_maker.normalize import complement_levels

TESTNET = "https://api-testnet.predict.fun"
MAINNET = "https://api.predict.fun"
ALLOWED_PREFIXES = (TESTNET, MAINNET)
# Cloudflare answers the urllib default User-Agent with 403 "error code: 1010".
USER_AGENT = "paper-split-maker/0.1 (read-only predict.fun; no orders)"
UP_NAMES = {"up", "yes"}
DOWN_NAMES = {"down", "no"}


class PublicDataError(RuntimeError):
    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


class Transport(Protocol):
    def get_json(self, url: str) -> object:
        ...


class UrllibTransport:
    def __init__(self, timeout: float = 8.0, headers: Optional[dict] = None):
        self.timeout = timeout
        self.headers = dict(headers or {})
        self._opener = urllib.request.build_opener()

    def get_json(self, url: str) -> object:
        return self._read(url, parse_float=None)

    def get_json_decimal(self, url: str) -> object:
        """GET JSON, keeping the original decimal literals for prices and sizes."""
        return self._read(url, parse_float=str)

    def _read(self, url: str, *, parse_float) -> object:
        if not url.startswith(ALLOWED_PREFIXES):
            raise PublicDataError("refusing non-official host")
        headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
        headers.update(self.headers)
        request = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read(180).decode("utf-8", errors="replace").strip()
            message = f"http {exc.code} for {url}"
            if detail:
                message = f"{message}: {detail}"
            raise PublicDataError(message, status=exc.code) from exc
        except urllib.error.URLError as exc:
            raise PublicDataError(f"network error for {url}: {exc.reason}") from exc
        try:
            kwargs = {} if parse_float is None else {"parse_float": parse_float}
            return json.loads(raw, **kwargs)
        except json.JSONDecodeError as exc:
            raise PublicDataError(f"invalid json from {url}") from exc


def client_from_env(env: dict | None) -> PublicReadClient:
    """Read PREDICT_NETWORK and PREDICT_API_KEY. The key is a header, never a log line."""
    env = env or {}
    network = str(env.get("PREDICT_NETWORK") or "testnet").strip() or "testnet"
    api_key = str(env.get("PREDICT_API_KEY") or "").strip()
    return PublicReadClient(network=network, api_key=api_key)


def rest_base(network: str = "testnet") -> str:
    if str(network).lower() == "mainnet":
        return MAINNET
    return TESTNET


class PublicReadClient:
    def __init__(
        self,
        transport: Optional[Transport] = None,
        *,
        network: str = "testnet",
        api_key: str = "",
    ):
        self.network = "mainnet" if str(network).lower() == "mainnet" else "testnet"
        self.base = rest_base(self.network)
        headers = {}
        if self.network == "mainnet" and api_key:
            headers["x-api-key"] = api_key
        if transport is None:
            transport = UrllibTransport(headers=headers)
        self.transport = transport
        self._by_token: dict[str, dict] = {}
        self._by_condition: dict[str, dict] = {}
        self._yes_cache: dict[str, dict] = {}
        self.last_discovery: dict = {
            "network": self.network,
            "series": "",
            "http_status": 0,
            "rounds": 0,
            "parsed": 0,
            "matched": 0,
            "unparsed": 0,
            "starts": [],
            "error": "",
        }

    def discover_btc_updown(
        self,
        now_s: int,
        limit: int,
        slugs: Optional[list[str]] = None,
        series: Optional[str] = None,
    ) -> list[dict]:
        series_name, wanted = _wanted_slugs(now_s, slugs, series)
        url = (
            f"{self.base}/v1/series/{urllib.parse.quote(series_name, safe='')}"
            "/rounds?status=ACTIVE&first=20"
        )
        try:
            payload = self._rounds(url)
        except PublicDataError as exc:
            self.last_discovery = self._discovery(
                series_name, exc.status or 0, error=str(exc)
            )
            if exc.status == 404:
                return []
            raise
        rounds = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(rounds, list):
            self.last_discovery = self._discovery(
                series_name, 200, error="rounds payload is not a list"
            )
            raise PublicDataError("rounds payload is not a list")
        found: list[dict] = []
        starts: list[int] = []
        unparsed = 0
        for category in rounds:
            converted = category_to_market(category, series_name)
            if converted is None:
                unparsed += 1
                continue
            starts.append(int(converted.get("start_ts") or 0))
            if wanted and converted["slug"] not in wanted:
                continue
            self._remember(converted)
            found.append(converted)
            if len(found) >= limit:
                break
        found.sort(key=lambda item: int(item.get("start_ts") or 0))
        self.last_discovery = self._discovery(
            series_name,
            200,
            rounds=len(rounds),
            parsed=len(rounds) - unparsed,
            matched=len(found),
            unparsed=unparsed,
            starts=starts[:8],
        )
        return found[:limit]

    def _rounds(self, url: str) -> object:
        try:
            return self.transport.get_json(url)
        except PublicDataError as exc:
            if exc.status != 400 or "status=" not in url:
                raise
            plain = url.split("?", 1)[0] + "?first=20"
            return self.transport.get_json(plain)

    def _discovery(
        self,
        series: str,
        status: int,
        *,
        rounds: int = 0,
        parsed: int = 0,
        matched: int = 0,
        unparsed: int = 0,
        starts: list[int] | None = None,
        error: str = "",
    ) -> dict:
        return {
            "network": self.network,
            "series": series,
            "http_status": int(status or 0),
            "rounds": rounds,
            "parsed": parsed,
            "matched": matched,
            "unparsed": unparsed,
            "starts": list(starts or []),
            "error": error,
        }

    def fetch_book(self, token_id: str) -> dict:
        meta = self._by_token.get(str(token_id))
        if meta is None:
            raise PublicDataError(f"unknown outcome token {token_id}")
        yes = self._yes_book(meta["market_id"])
        if meta["outcome"] == "up":
            book = dict(yes)
        else:
            book = {
                "timestamp": yes.get("timestamp"),
                "hash": f"{yes.get('hash')}:down",
                "bids": complement_levels(yes.get("asks") or [], meta["precision"]),
                "asks": complement_levels(yes.get("bids") or [], meta["precision"]),
            }
        book["asset_id"] = str(token_id)
        book["tick_size"] = meta["tick"]
        book["min_order_size"] = "1"
        return book

    def fetch_trades(self, condition_id: str, limit: int = 50) -> dict:
        meta = self._by_condition.get(str(condition_id).lower())
        market_id = meta["market_id"] if meta else str(condition_id)
        query = urllib.parse.urlencode({"first": str(int(limit))})
        url = f"{self.base}/v1/markets/{urllib.parse.quote(str(market_id), safe='')}/matches?{query}"
        try:
            payload = self._get(url, decimal_floats=True)
        except PublicDataError as exc:
            if exc.status == 404:
                return {"data": []}
            raise
        if isinstance(payload, dict) and isinstance(payload.get("data"), list):
            rows = payload["data"]
            if rows and isinstance(rows[0], dict) and (
                "token_id" in rows[0] or "asset_id" in rows[0]
            ):
                return payload
            return {"data": _match_rows(rows, meta)}
        return {"data": []}

    def fetch_resolution(self, condition_id: str) -> dict:
        meta = self._by_condition.get(str(condition_id).lower())
        if meta is None:
            return {"data": []}
        market_id = meta["market_id"]
        url = f"{self.base}/v1/markets/{urllib.parse.quote(str(market_id), safe='')}"
        try:
            payload = self.transport.get_json(url)
        except PublicDataError as exc:
            if exc.status == 404:
                return {"data": []}
            raise
        market = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(market, dict):
            return {"data": []}
        return resolution_payload(condition_id, market)

    def _yes_book(self, market_id: str) -> dict:
        cached = self._yes_cache.get(str(market_id))
        if cached is not None:
            return cached
        url = (
            f"{self.base}/v1/markets/"
            f"{urllib.parse.quote(str(market_id), safe='')}/orderbook"
        )
        payload = self._get(url, decimal_floats=True)
        book = yes_book(payload)
        self._yes_cache[str(market_id)] = book
        return book

    def _remember(self, market: dict) -> None:
        precision = int(market.get("decimalPrecision") or 2)
        tick = str(market.get("orderPriceMinTickSize") or _tick(precision))
        condition = str(market.get("conditionId") or "")
        info = {
            "market_id": str(market.get("predictMarketId") or market.get("id")),
            "precision": precision,
            "tick": tick,
            "up_token": str(market.get("_up_token") or ""),
            "down_token": str(market.get("_down_token") or ""),
        }
        tokens = market.get("clobTokenIds")
        if isinstance(tokens, str):
            try:
                tokens = json.loads(tokens)
            except json.JSONDecodeError:
                tokens = None
        outcomes = market.get("outcomes")
        if isinstance(outcomes, str):
            try:
                outcomes = json.loads(outcomes)
            except json.JSONDecodeError:
                outcomes = None
        if isinstance(tokens, list) and isinstance(outcomes, list):
            for token, label in zip(tokens, outcomes):
                side = "up" if str(label).casefold() == "up" else "down"
                self._by_token[str(token)] = {**info, "outcome": side}
        if condition:
            self._by_condition[condition.lower()] = info

    def _get(self, url: str, *, decimal_floats: bool) -> object:
        if decimal_floats and hasattr(self.transport, "get_json_decimal"):
            return self.transport.get_json_decimal(url)
        return self.transport.get_json(url)


def _wanted_slugs(
    now_s: int, slugs: Optional[list[str]], series: Optional[str] = None
) -> tuple[str, set[str]]:
    if slugs:
        series_name = series_of(str(slugs[0]))
        return series_name, {str(item) for item in slugs}
    series_name = series_of(series or "btc-updown-5m")
    window = window_seconds(series_name)
    start = (int(now_s) // window) * window
    return series_name, {f"{series_name}-{start + offset * window}" for offset in range(4)}


def category_to_market(category: dict, series: str) -> dict | None:
    """One active round becomes the Up/Down market the strategy already consumes."""
    if not isinstance(category, dict):
        return None
    markets = category.get("markets")
    market = None
    if isinstance(markets, list):
        for item in markets:
            if isinstance(item, dict) and item.get("outcomes"):
                market = item
                break
    if market is None and category.get("outcomes"):
        market = category
    if not isinstance(market, dict):
        return None
    pair = _outcome_pair(market.get("outcomes"))
    if pair is None:
        return None
    up, down = pair
    start_ms = _parse_time_ms(category.get("startsAt") or market.get("startsAt"))
    end_ms = _parse_time_ms(
        category.get("endsAt") or market.get("endsAt") or market.get("endDate")
    )
    if start_ms is None or end_ms is None:
        return None
    start_s = start_ms // 1000
    precision = int(market.get("decimalPrecision") or category.get("decimalPrecision") or 2)
    trading = str(market.get("tradingStatus") or "").upper()
    status = str(market.get("status") or category.get("status") or "").upper()
    closed = status == "RESOLVED" or trading == "CLOSED"
    accepting = trading in {"OPEN", "MATCHING_NOT_ENABLED", ""} and not closed
    fee = market.get("feeRateBps")
    if fee is None:
        fee = 0
    neg = bool(market.get("isNegRisk") if "isNegRisk" in market else category.get("isNegRisk"))
    yield_bearing = bool(
        market.get("isYieldBearing")
        if "isYieldBearing" in market
        else category.get("isYieldBearing")
    )
    market_id = market.get("id")
    condition = (
        market.get("conditionId")
        or market.get("condition_id")
        or category.get("conditionId")
        or category.get("condition_id")
    )
    if market_id is None or not condition:
        return None
    slug = f"{series_of(series)}-{start_s}"
    question = str(market.get("question") or category.get("title") or slug)
    return {
        "id": str(market_id),
        "predictMarketId": str(market_id),
        "version": "v1",
        "conditionId": str(condition),
        "slug": slug,
        "question": question,
        "outcomes": '["Up", "Down"]',
        "clobTokenIds": json.dumps([up["onChainId"], down["onChainId"]]),
        "endDate": end_ms,
        "start_ts": start_s,
        "orderPriceMinTickSize": _tick(precision),
        "orderMinSize": "1",
        "acceptingOrders": accepting,
        "closed": closed,
        "negRisk": neg,
        "isNegRisk": neg,
        "isYieldBearing": yield_bearing,
        "feeRateBps": int(fee),
        "decimalPrecision": precision,
        "_up_token": str(up["onChainId"]),
        "_down_token": str(down["onChainId"]),
    }


def _outcome_pair(raw) -> tuple[dict, dict] | None:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return None
    if not isinstance(raw, list) or len(raw) != 2:
        return None
    records = []
    for item in raw:
        if isinstance(item, str):
            name = item.casefold()
            records.append(
                {
                    "name": item,
                    "onChainId": item,
                    "indexSet": 1 if name in UP_NAMES else 2,
                }
            )
        elif isinstance(item, dict) and (item.get("onChainId") or item.get("positionId")):
            record = dict(item)
            record["onChainId"] = record.get("onChainId") or record.get("positionId")
            records.append(record)
        else:
            return None
    up = down = None
    for record in records:
        name = str(record.get("name") or "").casefold()
        if name in UP_NAMES:
            up = record
        elif name in DOWN_NAMES:
            down = record
    if up is None or down is None:
        by_index = {int(item.get("indexSet") or 0): item for item in records}
        up = up or by_index.get(1)
        down = down or by_index.get(2)
    if not isinstance(up, dict) or not isinstance(down, dict):
        return None
    if str(up.get("onChainId")) == str(down.get("onChainId")):
        return None
    return up, down


def yes_book(payload: dict) -> dict:
    data = payload.get("data") if isinstance(payload, dict) and "bids" not in payload else payload
    if not isinstance(data, dict):
        raise PublicDataError("book payload is not an object")
    bids = _levels(data.get("bids"))
    asks = _levels(data.get("asks"))
    if bids is None or asks is None:
        raise PublicDataError("book levels are not pairs")
    stamp = data.get("updateTimestampMs") or data.get("timestamp")
    market_id = data.get("marketId") or ""
    return {
        "timestamp": stamp,
        "hash": f"{market_id}:{stamp}",
        "bids": bids,
        "asks": asks,
    }


def resolution_payload(condition_id: str, market: dict) -> dict:
    status = str(market.get("status") or "")
    if status.upper() != "RESOLVED":
        return {"data": []}
    resolution = market.get("resolution") if isinstance(market.get("resolution"), dict) else {}
    name = str(resolution.get("name") or "").casefold()
    index_set = resolution.get("indexSet")
    if name in DOWN_NAMES:
        payouts = [0, 1_000_000]
    elif name in UP_NAMES or index_set == 1:
        payouts = [1_000_000, 0]
    else:
        return {"data": []}
    return {
        "data": [
            {
                "condition_id": condition_id,
                "status": "resolved",
                "payouts": payouts,
                "market_type": "BINARY",
            }
        ]
    }


def _match_rows(rows: list, meta: dict | None) -> list[dict]:
    out = []
    up = (meta or {}).get("up_token") or ""
    down = (meta or {}).get("down_token") or ""
    for row in rows:
        if not isinstance(row, dict):
            continue
        if "token_id" in row or "asset_id" in row:
            out.append(row)
            continue
        outcome = str(row.get("outcome") or row.get("outcomeName") or "").casefold()
        token = up if outcome in UP_NAMES else down if outcome in DOWN_NAMES else ""
        if not token:
            continue
        out.append(
            {
                "token_id": token,
                "side": str(row.get("side") or row.get("quoteType") or "BUY"),
                "price": row.get("price"),
                "size": row.get("size") or row.get("quantity"),
                "timestamp": row.get("executedAt") or row.get("timestamp"),
                "transaction_hash": row.get("transactionHash") or row.get("id"),
            }
        )
    return out


def _levels(raw) -> list[dict] | None:
    if not isinstance(raw, list):
        return None
    levels = []
    for item in raw:
        if isinstance(item, dict):
            price, size = item.get("price"), item.get("size")
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            price, size = item[0], item[1]
        else:
            return None
        levels.append({"price": _dec_str(price), "size": _dec_str(size)})
    return levels


def _dec_str(value) -> str:
    return format(Decimal(str(value)), "f")


def _tick(precision: int) -> str:
    if precision < 0:
        precision = 0
    return format(Decimal(1).scaleb(-precision), "f")


def _parse_time_ms(value) -> int | None:
    from paper_split_maker.normalize import _parse_time_ms as parse

    return parse(value)


def _window(series: str) -> int:
    return window_seconds(series)
