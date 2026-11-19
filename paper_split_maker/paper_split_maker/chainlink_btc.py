"""Public Chainlink BTC/USD reads for the paper filter. No key and no orders.

predict.fun settles BTC up/down on the Chainlink BTC/USDT Data Stream. That
report API requires a key, so this module reads the on-chain BTC/USD
aggregator on BNB Chain instead. The contract is
``0x264990fbd0A4796A3E3d8E37C4d5F87a3aCa5Ebf``
(BTC/USD-RefPrice-DF-Binance-001). ``latestRoundData`` is a public
``eth_call``. The answer uses 8 decimals.
"""

from __future__ import annotations

import json
import threading
import urllib.request
from decimal import Decimal

from paper_split_maker.binance_btc import PriceBuffer

# https://data.chain.link/feeds/bsc/mainnet/btc-usd
AGGREGATOR = "0x264990fbd0A4796A3E3d8E37C4d5F87a3aCa5Ebf"
DECIMALS = 8
LATEST_ROUND_DATA = "0xfeaf968c"
DEFAULT_RPCS = (
    "https://bsc-dataseed.binance.org/",
    "https://bsc.publicnode.com",
)
_USER_AGENT = "paper-split-maker/0.1 (read-only; no orders)"


def decode_latest_round(payload) -> tuple[int, Decimal] | None:
    """``(updated_at_ms, price)`` from a latestRoundData eth_call result."""
    raw = _hex_bytes(payload)
    if raw is None or len(raw) < 160:
        return None
    answer = int.from_bytes(raw[32:64], "big", signed=True)
    updated = int.from_bytes(raw[96:128], "big")
    if answer <= 0 or updated <= 0:
        return None
    price = Decimal(answer) / (Decimal(10) ** DECIMALS)
    if price <= 0:
        return None
    return updated * 1000, price


def _hex_bytes(payload) -> bytes | None:
    if isinstance(payload, dict):
        result = payload.get("result")
        if not isinstance(result, str):
            return None
        payload = result
    if isinstance(payload, bytes):
        return payload
    if not isinstance(payload, str):
        return None
    text = payload[2:] if payload.startswith("0x") else payload
    try:
        return bytes.fromhex(text)
    except ValueError:
        return None


def rpc_request(url: str, method: str, params: list, timeout: float = 8.0) -> dict:
    body = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    ).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": _USER_AGENT},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        parsed = json.loads(response.read().decode("utf-8"))
    if not isinstance(parsed, dict):
        raise ValueError("rpc response is not an object")
    if parsed.get("error"):
        raise ValueError(str(parsed["error"]))
    return parsed


def read_btc_usd(urls: tuple[str, ...] = DEFAULT_RPCS, timeout: float = 8.0):
    """Try each public RPC until one returns a round. Raises the last error."""
    last = None
    call = [{"to": AGGREGATOR, "data": LATEST_ROUND_DATA}, "latest"]
    for url in urls:
        try:
            payload = rpc_request(url, "eth_call", call, timeout=timeout)
        except Exception as exc:
            last = exc
            continue
        parsed = decode_latest_round(payload)
        if parsed is not None:
            return parsed
        last = ValueError("empty round")
    if last is not None:
        raise last
    raise ValueError("no rpc url")


class ChainlinkBtcFeed:
    """Poll the public aggregator. ``latest`` is ``(updated_at_ms, price)``."""

    def __init__(self, *, urls: tuple[str, ...] = DEFAULT_RPCS, poll_s: float = 5.0, fetch=None):
        self.buffer = PriceBuffer(keep_seconds=7200)
        self.urls = tuple(urls)
        self.poll_s = float(poll_s) if float(poll_s) > 0 else 5.0
        self._fetch = fetch or (lambda: read_btc_usd(self.urls))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.polls = 0
        self.errors = 0

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="chainlink-btc", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=2)

    def latest(self) -> tuple[int, Decimal] | None:
        return self.buffer.latest()

    def price_at(self, ts_ms: int) -> Decimal | None:
        return self.buffer.price_at(ts_ms)

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._once()
            if self._stop.wait(self.poll_s):
                return

    def _once(self) -> None:
        try:
            parsed = self._fetch()
        except Exception:
            self.errors += 1
            return
        if parsed is None:
            self.errors += 1
            return
        self.polls += 1
        self.buffer.add(*parsed)
