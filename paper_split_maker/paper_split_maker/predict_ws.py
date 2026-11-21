"""Public predict.fun book socket for the paper bot.

Subscribes to ``predictOrderbook/{id}`` and ``predictTopOfBook/{id}`` on
``wss://ws.predict.fun/ws``. The API key is sent only as the ``x-api-key``
handshake header and is never logged. This module does not place orders.

There is no public trade topic. ``lastOrderSettled`` has no size, so prints
stay on the REST matches poll. While the socket is down, the paper loop
uses ``GET /v1/markets/orderbooks``.

Ack and cancel latency are not changed here. The paper config keeps
``ack_latency_ms`` at 250 and ``cancel_latency_ms`` at 500. A short clock
in the fast loop promotes those timers instead of waiting for the next
REST poll.
"""

from __future__ import annotations

import base64
import json
import os
import socket
import ssl
import threading
from decimal import Decimal
from typing import Optional

from paper_split_maker.binance_btc import _send_frame
from paper_split_maker.feeds import heartbeat_reply
from paper_split_maker.normalize import clob_book_event, complement_levels
from paper_split_maker.public_client import PublicDataError, yes_book

_USER_AGENT = "paper-split-maker/0.1 (read-only predict.fun book; no orders)"


def plain_market_id(value) -> Optional[str]:
    """Decimal id with no leading zero and no sign. The server rejects the rest."""
    text = str(value if value is not None else "").strip()
    if not text.isdigit():
        return None
    if len(text) > 1 and text.startswith("0"):
        return None
    return str(int(text))


def book_topics(market_id: str) -> list[str]:
    ident = plain_market_id(market_id)
    if ident is None:
        return []
    return [f"predictOrderbook/{ident}", f"predictTopOfBook/{ident}"]


def subscribe_request(topic: str, request_id: int) -> dict:
    return {"method": "subscribe", "requestId": int(request_id), "params": [topic]}


def parse_frame(frame: dict) -> Optional[dict]:
    """One documented ``type: M`` book or top-of-book message, or None.

    Heartbeats, subscribe responses, and anything else return None. The
    caller echoes heartbeats separately.
    """
    if not isinstance(frame, dict) or frame.get("type") != "M":
        return None
    topic = str(frame.get("topic") or "")
    data = frame.get("data")
    if not isinstance(data, dict):
        return None
    if topic.startswith("predictOrderbook/"):
        market_id = plain_market_id(topic.split("/", 1)[1]) or plain_market_id(
            data.get("marketId")
        )
        if market_id is None:
            return None
        try:
            book = yes_book(data if "bids" in data else {"data": data})
        except PublicDataError:
            return None
        book["market_id"] = market_id
        book["kind"] = "book"
        settled = data.get("lastOrderSettled")
        if isinstance(settled, dict):
            book["last_order_settled"] = {
                "id": str(settled.get("id") or ""),
                "price": str(settled.get("price") or ""),
                "side": str(settled.get("side") or ""),
                "outcome": str(settled.get("outcome") or ""),
            }
        return book
    if topic.startswith("predictTopOfBook/"):
        market_id = plain_market_id(topic.split("/", 1)[1]) or plain_market_id(
            data.get("marketId")
        )
        if market_id is None:
            return None
        return {
            "kind": "top",
            "market_id": market_id,
            "bids": _one_level(data.get("bestBid")),
            "asks": _one_level(data.get("bestAsk")),
            "timestamp": None,
            "hash": "",
            "last_order_settled": None,
        }
    return None


def merge_side(levels: list, touch: list, *, higher_is_better: bool) -> list:
    """Replace the touch and keep strictly worse depth."""
    if not touch:
        return list(levels or [])
    price = Decimal(str(touch[0]["price"]))
    kept = []
    for level in levels or []:
        level_price = Decimal(str(level["price"]))
        worse = level_price < price if higher_is_better else level_price > price
        if worse:
            kept.append(level)
    return [touch[0]] + kept


class BookCache:
    """Latest Yes book per market. A top-of-book message updates the touch only."""

    def __init__(self):
        self._books: dict[str, dict] = {}

    def apply(self, parsed: dict) -> Optional[dict]:
        market_id = str(parsed.get("market_id") or "")
        if not market_id:
            return None
        current = self._books.get(market_id)
        if parsed.get("kind") == "top":
            bids = merge_side(
                current["bids"] if current else [],
                parsed.get("bids") or [],
                higher_is_better=True,
            )
            asks = merge_side(
                current["asks"] if current else [],
                parsed.get("asks") or [],
                higher_is_better=False,
            )
            stamp = current.get("timestamp") if current else None
            book = {
                "market_id": market_id,
                "bids": bids,
                "asks": asks,
                "timestamp": stamp,
                "hash": "",
                "kind": "top",
            }
            book["hash"] = _touch_hash(book)
        else:
            book = {
                "market_id": market_id,
                "bids": list(parsed.get("bids") or []),
                "asks": list(parsed.get("asks") or []),
                "timestamp": parsed.get("timestamp"),
                "hash": str(parsed.get("hash") or ""),
                "kind": "book",
            }
            if not book["hash"]:
                book["hash"] = _touch_hash(book)
        if current is not None and current.get("hash") == book["hash"]:
            return None
        self._books[market_id] = book
        return book


def yes_to_events(
    market_id: str,
    yes: dict,
    local_stamp: int,
    precision: int,
    seq: str,
) -> list[dict]:
    """Up book plus the Down complement. Both use the local receipt time.

    Exchange timestamps can sit behind the receipt clock. Driving ``book_ts``
    with the receipt keeps a newer socket message from being dropped as
    out of order.
    """
    bids = list(yes.get("bids") or [])
    asks = list(yes.get("asks") or [])
    digest = str(yes.get("hash") or seq)
    payloads = {
        "up": {"timestamp": local_stamp, "hash": f"{digest}:up", "bids": bids, "asks": asks},
        "down": {
            "timestamp": local_stamp,
            "hash": f"{digest}:down",
            "bids": complement_levels(asks, precision),
            "asks": complement_levels(bids, precision),
        },
    }
    events = []
    for outcome, payload in payloads.items():
        event = clob_book_event(
            market_id,
            outcome,
            payload,
            event_id=f"book:{seq}:{outcome}",
        )
        if event is None:
            return []
        event["book_ts"] = local_stamp
        event["ts"] = local_stamp
        events.append(event)
    return events


class PredictBookFeed:
    """One socket, many markets. ``drain`` is what the paper loop reads."""

    def __init__(self, url: str, api_key: str = ""):
        self.url = url
        self._api_key = str(api_key or "")
        self.connected = False
        self.last_error = ""
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._sock = None
        self._lock = threading.Lock()
        self._wanted: list[str] = []
        self._queue: list[dict] = []
        self._cache = BookCache()
        self._request_id = 0

    def __repr__(self) -> str:
        return f"PredictBookFeed(connected={self.connected})"

    def set_markets(self, market_ids) -> None:
        cleaned = []
        for item in market_ids or []:
            ident = plain_market_id(item)
            if ident and ident not in cleaned:
                cleaned.append(ident)
        with self._lock:
            self._wanted = cleaned

    def drain(self) -> list[dict]:
        with self._lock:
            rows = self._queue
            self._queue = []
        return rows

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="predict-book", daemon=True)
        self._thread.start()

    def drop(self) -> None:
        """Close the socket so a stuck reader unblocks and the loop uses REST."""
        self.connected = False
        sock = self._sock
        self._sock = None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    def stop(self) -> None:
        self._stop.set()
        sock = self._sock
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._session()
            except Exception as exc:
                self.connected = False
                self.last_error = self._safe(str(exc) or exc.__class__.__name__)
            if self._stop.is_set():
                break
            self._stop.wait(1.0)

    def _session(self) -> None:
        headers = {}
        if self._api_key:
            headers["x-api-key"] = self._api_key
        sock, leftover = open_socket(self.url, headers)
        self._sock = sock
        self.connected = True
        self.last_error = ""
        sent: set[str] = set()
        buf = bytearray(leftover)
        try:
            sock.settimeout(0.2)
            while not self._stop.is_set():
                self._send_wanted(sock, sent)
                try:
                    text = read_message(sock, buf)
                except socket.timeout:
                    continue
                if text is None:
                    break
                self._on_text(sock, text)
        finally:
            self.connected = False
            self._sock = None
            try:
                sock.close()
            except OSError:
                pass

    def _send_wanted(self, sock, sent: set[str]) -> None:
        with self._lock:
            wanted = list(self._wanted)
        for market_id in wanted:
            for topic in book_topics(market_id):
                if topic in sent:
                    continue
                self._request_id += 1
                payload = subscribe_request(topic, self._request_id)
                _send_frame(sock, 0x1, json.dumps(payload).encode("utf-8"))
                sent.add(topic)

    def _on_text(self, sock, text: str) -> None:
        try:
            frame = json.loads(text)
        except json.JSONDecodeError:
            return
        if not isinstance(frame, dict):
            return
        reply = heartbeat_reply(frame)
        if reply is not None:
            _send_frame(sock, 0x1, json.dumps(reply).encode("utf-8"))
            return
        parsed = parse_frame(frame)
        if parsed is None:
            return
        book = self._cache.apply(parsed)
        if book is None:
            return
        with self._lock:
            self._queue.append(book)

    def _safe(self, text: str) -> str:
        key = self._api_key
        if key and key in text:
            text = text.replace(key, "redacted")
        return text[:240]


def open_socket(url: str, headers: Optional[dict] = None):
    """Handshake one websocket. Returns the socket and any bytes past the HTTP response."""
    host, port, path, secure = _split_ws_url(url)
    raw = socket.create_connection((host, port), timeout=10)
    sock = raw
    try:
        if secure:
            sock = ssl.create_default_context().wrap_socket(raw, server_hostname=host)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        extra = ""
        for name, value in (headers or {}).items():
            if value in (None, ""):
                continue
            extra += f"{name}: {value}\r\n"
        host_header = host
        if not ((secure and port == 443) or (not secure and port == 80)):
            host_header = f"{host}:{port}"
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host_header}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            f"User-Agent: {_USER_AGENT}\r\n"
            f"{extra}\r\n"
        )
        sock.sendall(request.encode("ascii"))
        response = b""
        while b"\r\n\r\n" not in response:
            part = sock.recv(4096)
            if not part:
                raise ConnectionError("websocket handshake closed")
            response += part
            if len(response) > 65536:
                raise ConnectionError("websocket handshake too long")
    except Exception:
        try:
            sock.close()
        except OSError:
            pass
        raise
    head, _, leftover = response.partition(b"\r\n\r\n")
    status = head.split(b"\r\n", 1)[0]
    if b" 101 " not in status:
        try:
            sock.close()
        except OSError:
            pass
        raise ConnectionError("websocket handshake rejected")
    return sock, leftover


def read_message(sock: socket.socket, buf: bytearray) -> Optional[str]:
    """One text message. None means the server closed. Ping frames are answered."""
    parts: list[bytes] = []
    while True:
        header = _take(sock, buf, 2)
        opcode = header[0] & 0x0F
        fin = header[0] & 0x80
        length = header[1] & 0x7F
        if length == 126:
            length = int.from_bytes(_take(sock, buf, 2), "big")
        elif length == 127:
            length = int.from_bytes(_take(sock, buf, 8), "big")
        mask = _take(sock, buf, 4) if header[1] & 0x80 else b""
        payload = _take(sock, buf, length) if length else b""
        if mask:
            payload = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        if opcode == 0x8:
            return None
        if opcode == 0x9:
            _send_frame(sock, 0xA, payload)
            continue
        if opcode == 0xA:
            continue
        if opcode in (0x1, 0x0):
            parts.append(payload)
            if fin:
                return b"".join(parts).decode("utf-8", "replace")
            continue


def _take(sock: socket.socket, buf: bytearray, size: int) -> bytes:
    while len(buf) < size:
        part = sock.recv(max(size - len(buf), 1))
        if not part:
            raise ConnectionError("socket closed")
        buf.extend(part)
    out = bytes(buf[:size])
    del buf[:size]
    return out


def _split_ws_url(url: str) -> tuple[str, int, str, bool]:
    secure = str(url).startswith("wss://")
    rest = str(url).split("://", 1)[-1]
    host_path = rest.split("/", 1)
    host_port = host_path[0]
    path = "/" + host_path[1] if len(host_path) > 1 else "/"
    if ":" in host_port:
        host, port_text = host_port.rsplit(":", 1)
        port = int(port_text)
    else:
        host = host_port
        port = 443 if secure else 80
    return host, port, path, secure


def _one_level(value) -> list[dict]:
    if not isinstance(value, (list, tuple)) or len(value) < 2:
        return []
    try:
        price = format(Decimal(str(value[0])), "f")
        size = format(Decimal(str(value[1])), "f")
    except (ArithmeticError, ValueError):
        return []
    return [{"price": price, "size": size}]


def _touch_hash(book: dict) -> str:
    bid = book["bids"][0]["price"] + ":" + book["bids"][0]["size"] if book["bids"] else ""
    ask = book["asks"][0]["price"] + ":" + book["asks"][0]["size"] if book["asks"] else ""
    return f"{book['market_id']}:{book.get('timestamp')}:{bid}:{ask}:{len(book['bids'])}:{len(book['asks'])}"
