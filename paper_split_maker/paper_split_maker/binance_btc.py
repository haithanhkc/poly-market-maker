"""Public Binance BTCUSDT prices for the paper lean. No key and no orders.

The feed prefers ``wss://stream.binance.com:9443/ws/btcusdt@trade``. If that
socket drops, it polls ``https://api.binance.com/api/v3/ticker/price`` about
once a second and then reconnects. Prices stay in a rolling buffer.

Fair value is the Black-Scholes digital:

    P(Up) = Phi( ln(S / S0) / (sigma * sqrt(T)) )

``sigma`` is the realized per-second log-return volatility. ``T`` is the
seconds left in the round, and never less than one second.
"""

from __future__ import annotations

import base64
import json
import math
import os
import socket
import ssl
import threading
import time
import urllib.request
from decimal import Decimal

BINANCE_TRADE_WS = "wss://stream.binance.com:9443/ws/btcusdt@trade"
BINANCE_REST_URL = "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT"
_USER_AGENT = "paper-split-maker/0.1 (read-only; no orders)"


def parse_price_message(payload, now_ms: int = 0) -> tuple[int, Decimal] | None:
    """Trade, aggTrade, bookTicker, or a combined-stream wrapper."""
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            return None
    if not isinstance(payload, dict):
        return None
    if isinstance(payload.get("data"), dict):
        payload = payload["data"]
    kind = str(payload.get("e") or "")
    if kind in {"trade", "aggTrade"} or "p" in payload and "T" in payload:
        return _trade_price(payload)
    if "b" in payload and "a" in payload:
        mid = _book_mid(payload)
        if mid is None:
            return None
        stamp = _stamp(payload, now_ms)
        return stamp, mid
    return None


def parse_rest_ticker(payload, now_ms: int = 0) -> tuple[int, Decimal] | None:
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            return None
    if not isinstance(payload, dict):
        return None
    try:
        price = Decimal(str(payload["price"]))
    except (KeyError, ValueError, ArithmeticError):
        return None
    if price <= 0:
        return None
    return int(now_ms), price


def normal_cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def fair_up(
    spot,
    open_price,
    sigma,
    remaining_s,
    sigma_floor,
) -> Decimal | None:
    """P(Up) from the spot, the round open, and per-second volatility."""
    try:
        s = float(spot)
        s0 = float(open_price)
        floor = float(sigma_floor)
        sig = float(sigma) if sigma is not None else 0.0
        remaining = float(remaining_s)
    except (TypeError, ValueError):
        return None
    if s <= 0 or s0 <= 0 or floor <= 0:
        return None
    if sig < floor:
        sig = floor
    horizon = remaining if remaining > 1.0 else 1.0
    z = math.log(s / s0) / (sig * math.sqrt(horizon))
    if z > 12.0:
        probability = 1.0
    elif z < -12.0:
        probability = 0.0
    else:
        probability = normal_cdf(z)
    if probability <= 0.0:
        probability = 0.000001
    if probability >= 1.0:
        probability = 0.999999
    return Decimal(f"{probability:.6f}")


def realized_vol(points: list[tuple[int, Decimal]], now_ms: int, lookback_s: int) -> float | None:
    """Root-mean-square log return per sqrt(second) over the lookback."""
    start = int(now_ms) - int(lookback_s) * 1000
    window = [(int(ts), price) for ts, price in points if int(ts) >= start and price > 0]
    if len(window) < 3:
        return None
    total = 0.0
    count = 0
    previous = window[0]
    for point in window[1:]:
        dt = (point[0] - previous[0]) / 1000.0
        if dt > 0 and previous[1] > 0 and point[1] > 0:
            change = math.log(float(point[1]) / float(previous[1]))
            total += (change * change) / dt
            count += 1
        previous = point
    if count == 0:
        return None
    return math.sqrt(total / count)


class PriceBuffer:
    """Timestamped prices. Readers and the feed thread share one lock."""

    def __init__(self, keep_seconds: int = 1200):
        self.keep_seconds = max(int(keep_seconds), 60)
        self._points: list[tuple[int, Decimal]] = []
        self._lock = threading.Lock()

    def add(self, ts_ms: int, price: Decimal) -> None:
        if price <= 0:
            return
        stamp = int(ts_ms)
        with self._lock:
            if self._points and stamp < self._points[-1][0]:
                return
            if self._points and stamp == self._points[-1][0]:
                self._points[-1] = (stamp, price)
            else:
                self._points.append((stamp, price))
            cutoff = stamp - self.keep_seconds * 1000
            if len(self._points) > 4 and self._points[0][0] < cutoff:
                self._points = [item for item in self._points if item[0] >= cutoff]

    def latest(self) -> tuple[int, Decimal] | None:
        with self._lock:
            if not self._points:
                return None
            return self._points[-1]

    def price_at(self, ts_ms: int) -> Decimal | None:
        """Last price at or before ``ts_ms``, else the earliest sample."""
        with self._lock:
            if not self._points:
                return None
            chosen = None
            for stamp, price in self._points:
                if stamp <= int(ts_ms):
                    chosen = price
                else:
                    break
            if chosen is None:
                return self._points[0][1]
            return chosen

    def points(self) -> list[tuple[int, Decimal]]:
        with self._lock:
            return list(self._points)

    def vol(self, now_ms: int, lookback_s: int) -> float | None:
        return realized_vol(self.points(), now_ms, lookback_s)


class BinanceBtcFeed:
    """Background trade stream with a REST fallback. Call ``start`` once."""

    def __init__(
        self,
        *,
        lookback_s: int = 600,
        url: str = BINANCE_TRADE_WS,
        rest_url: str = BINANCE_REST_URL,
        fetch_rest=None,
        now_ms=None,
    ):
        self.buffer = PriceBuffer(keep_seconds=max(int(lookback_s) * 2, 1200))
        self.lookback_s = int(lookback_s)
        self.url = url
        self.rest_url = rest_url
        self._fetch_rest = fetch_rest or _fetch_rest_ticker
        self._now_ms = now_ms or (lambda: int(time.time() * 1000))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._sock: socket.socket | None = None
        self.connected = False
        self.rest_polls = 0

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="binance-btc", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        sock = self._sock
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        thread = self._thread
        if thread is not None:
            thread.join(timeout=2)

    def latest(self) -> tuple[int, Decimal] | None:
        return self.buffer.latest()

    def price_at(self, ts_ms: int) -> Decimal | None:
        return self.buffer.price_at(ts_ms)

    def vol(self, now_ms: int) -> float | None:
        return self.buffer.vol(now_ms, self.lookback_s)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._ws_session()
            except Exception:
                self.connected = False
            if self._stop.is_set():
                break
            self._rest_until_retry()

    def _rest_until_retry(self) -> None:
        for _ in range(5):
            if self._stop.is_set():
                return
            self._rest_once()
            if self._stop.wait(1.0):
                return

    def _rest_once(self) -> None:
        try:
            payload = self._fetch_rest(self.rest_url)
        except Exception:
            return
        parsed = parse_rest_ticker(payload, self._now_ms())
        if parsed is None:
            return
        self.rest_polls += 1
        self.buffer.add(*parsed)

    def _ws_session(self) -> None:
        sock = _open_websocket(self.url)
        self._sock = sock
        self.connected = True
        try:
            sock.settimeout(1.0)
            while not self._stop.is_set():
                try:
                    opcode, payload = _read_frame(sock)
                except socket.timeout:
                    continue
                if opcode == 0x8:
                    break
                if opcode == 0x9:
                    _send_frame(sock, 0xA, payload)
                    continue
                if opcode != 0x1 or not payload:
                    continue
                parsed = parse_price_message(payload.decode("utf-8", "replace"), self._now_ms())
                if parsed is not None:
                    self.buffer.add(*parsed)
        finally:
            self.connected = False
            self._sock = None
            try:
                sock.close()
            except OSError:
                pass


def decode_frame(header_and_body: bytes) -> tuple[int, bytes]:
    """One unmasked server frame. Used by tests and the socket reader."""
    if len(header_and_body) < 2:
        raise ValueError("short frame")
    opcode = header_and_body[0] & 0x0F
    masked = header_and_body[1] & 0x80
    length = header_and_body[1] & 0x7F
    offset = 2
    if length == 126:
        length = int.from_bytes(header_and_body[2:4], "big")
        offset = 4
    elif length == 127:
        length = int.from_bytes(header_and_body[2:10], "big")
        offset = 10
    mask = None
    if masked:
        mask = header_and_body[offset : offset + 4]
        offset += 4
    payload = bytearray(header_and_body[offset : offset + length])
    if mask:
        for index, value in enumerate(payload):
            payload[index] = value ^ mask[index % 4]
    return opcode, bytes(payload)


def _read_frame(sock: socket.socket) -> tuple[int, bytes]:
    header = _recv_exact(sock, 2)
    length = header[1] & 0x7F
    extra = b""
    if length == 126:
        extra = _recv_exact(sock, 2)
    elif length == 127:
        extra = _recv_exact(sock, 8)
    masked = header[1] & 0x80
    mask = _recv_exact(sock, 4) if masked else b""
    size = length
    if length == 126:
        size = int.from_bytes(extra, "big")
    elif length == 127:
        size = int.from_bytes(extra, "big")
    payload = _recv_exact(sock, size) if size else b""
    return decode_frame(header + extra + mask + payload)


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    chunks = []
    remaining = size
    while remaining:
        part = sock.recv(remaining)
        if not part:
            raise ConnectionError("socket closed")
        chunks.append(part)
        remaining -= len(part)
    return b"".join(chunks)


def _send_frame(sock: socket.socket, opcode: int, payload: bytes) -> None:
    mask = os.urandom(4)
    masked = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
    header = bytearray([0x80 | (opcode & 0x0F)])
    size = len(payload)
    if size < 126:
        header.append(0x80 | size)
    elif size < 65536:
        header.append(0x80 | 126)
        header.extend(size.to_bytes(2, "big"))
    else:
        header.append(0x80 | 127)
        header.extend(size.to_bytes(8, "big"))
    header.extend(mask)
    sock.sendall(bytes(header) + masked)


def _open_websocket(url: str) -> socket.socket:
    host, port, path = _split_ws_url(url)
    raw = socket.create_connection((host, port), timeout=10)
    context = ssl.create_default_context()
    sock = context.wrap_socket(raw, server_hostname=host)
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    request = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        f"User-Agent: {_USER_AGENT}\r\n"
        "\r\n"
    )
    sock.sendall(request.encode("ascii"))
    response = b""
    while b"\r\n\r\n" not in response:
        part = sock.recv(4096)
        if not part:
            sock.close()
            raise ConnectionError("websocket handshake closed")
        response += part
        if len(response) > 8192:
            sock.close()
            raise ConnectionError("websocket handshake too long")
    status = response.split(b"\r\n", 1)[0]
    if b" 101 " not in status:
        sock.close()
        raise ConnectionError("websocket handshake rejected")
    return sock


def _split_ws_url(url: str) -> tuple[str, int, str]:
    rest = url.split("://", 1)[-1]
    host_path = rest.split("/", 1)
    host_port = host_path[0]
    path = "/" + host_path[1] if len(host_path) > 1 else "/"
    if ":" in host_port:
        host, port_text = host_port.rsplit(":", 1)
        port = int(port_text)
    else:
        host = host_port
        port = 443
    return host, port, path


def _fetch_rest_ticker(url: str):
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def _trade_price(payload: dict) -> tuple[int, Decimal] | None:
    try:
        price = Decimal(str(payload["p"]))
    except (KeyError, ValueError, ArithmeticError):
        return None
    if price <= 0:
        return None
    return _stamp(payload, 0), price


def _book_mid(payload: dict) -> Decimal | None:
    try:
        bid = Decimal(str(payload["b"]))
        ask = Decimal(str(payload["a"]))
    except (KeyError, ValueError, ArithmeticError):
        return None
    if bid <= 0 or ask <= 0 or ask < bid:
        return None
    return (bid + ask) / 2


def _stamp(payload: dict, now_ms: int) -> int:
    for key in ("T", "E"):
        if payload.get(key) not in (None, ""):
            try:
                return int(payload[key])
            except (TypeError, ValueError):
                continue
    return int(now_ms)


def reference_price_from_category(category: dict, market: dict) -> Decimal | None:
    """Strike or open printed by predict.fun, when the payload has one."""
    keys = (
        "priceToBeat",
        "strikePrice",
        "openPrice",
        "startPrice",
        "referencePrice",
    )
    for source in (market, category):
        if not isinstance(source, dict):
            continue
        found = _positive(source, keys)
        if found is not None:
            return found
        for nested_key in ("metadata", "variantData", "oracle"):
            nested = source.get(nested_key)
            if isinstance(nested, dict):
                found = _positive(nested, keys)
                if found is not None:
                    return found
    return None


def _positive(source: dict, keys: tuple[str, ...]) -> Decimal | None:
    for key in keys:
        if source.get(key) in (None, ""):
            continue
        try:
            value = Decimal(str(source[key]))
        except (ValueError, ArithmeticError):
            continue
        if value > 0:
            return value
    return None
