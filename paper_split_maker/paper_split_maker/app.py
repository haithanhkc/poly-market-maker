"""Paper dashboard for the combined small_pair run.

The page is a local view of the paper engine. Fills are simulated from the
public predict.fun book. This process does not sign, does not read a key,
and does not send an order. Bind address is always the loopback interface.
"""

from __future__ import annotations

import json
import math
import sys
import threading
import time
import webbrowser
from argparse import Namespace
from datetime import datetime, timezone
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

from paper_split_maker.chain_data import series_of, window_seconds
from paper_split_maker.config import Config, load_config
from paper_split_maker.dual_bid import round_open_ms
from paper_split_maker.models import WORKING_ORDER_STATUSES
from paper_split_maker.session import open_engine, run_paper
from paper_split_maker.small_pair import round_report

PAGE = Path(__file__).with_name("app_page.html")
HOST = "127.0.0.1"
# 3:10–3:15 PM ET on 19 Nov 2026. The previous round is the settled session.
DEMO_OPEN = int(datetime(2026, 11, 19, 20, 10, tzinfo=timezone.utc).timestamp())
BEAT = Decimal("62161.22")
_DASH = "dashboard.json"
_BOT = "bot.json"


class PaperControl:
    """Flags the HTTP thread may set. The paper thread is the one that applies them."""

    def __init__(self, enabled: bool = True):
        self._lock = threading.Lock()
        self._enabled = bool(enabled)
        self._stop = False
        self._reset = False

    def enabled(self) -> bool:
        with self._lock:
            return self._enabled

    def set_enabled(self, value: bool) -> None:
        with self._lock:
            self._enabled = bool(value)

    def should_stop(self) -> bool:
        with self._lock:
            return self._stop

    def request_stop(self) -> None:
        with self._lock:
            self._stop = True

    def should_reset(self) -> bool:
        with self._lock:
            if not self._reset:
                return False
            self._reset = False
            return True

    def request_reset(self) -> None:
        with self._lock:
            self._reset = True


class Hub:
    """Chart, book, blotter, and equity derived from engine events."""

    def __init__(
        self,
        out_dir: Path,
        *,
        mode: str,
        config: Optional[Config] = None,
        env: Optional[dict] = None,
    ):
        self.out = Path(out_dir)
        self.out.mkdir(parents=True, exist_ok=True)
        self.mode = mode
        self.config = config
        self.env = env
        self.lock = threading.Lock()
        self.engine = None
        self.control = PaperControl(self._read_bot())
        self.sessions: dict[str, dict] = {}
        self.order: list[str] = []
        self.blotter: list[dict] = []
        self.equity: list[dict] = []
        self.now_ms = 0
        self.seq = 0
        self.subs: list = []
        self.thread: Optional[threading.Thread] = None
        self._load_dashboard()
        if not self.equity:
            self.equity = [{"t": 0, "v": self._capital()}]

    def attach(self, engine, config: Optional[Config] = None) -> None:
        self.engine = engine
        if config is not None:
            self.config = config
        engine.paper_control = self.control
        engine.quoting_enabled = self.control.enabled()
        if self.ingest not in engine.listeners:
            engine.listeners.append(self.ingest)
        engine.dashboard = self

    def start_live(self) -> None:
        """Run the public-data paper loop until stop or reset.

        ``env`` is the same map the CLI passes to the public client. Without
        it the client reads only the process environment and defaults to
        testnet, which 404s the mainnet series.
        """
        self.mode = "live"
        env = self.env

        def loop() -> None:
            while not self.control.should_stop():
                summary = run_paper(
                    self.config,
                    self.out,
                    duration_seconds=0,
                    max_markets=0,
                    env=env,
                    on_engine=self.attach,
                )
                if summary.get("stop_reason") == "reset":
                    self._wipe_files()
                    with self.lock:
                        self._clear_memory()
                    continue
                if summary.get("stop_reason") == "stall":
                    time.sleep(1.0)
                    continue
                break

        self.thread = threading.Thread(target=loop, name="paper-dashboard", daemon=True)
        self.thread.start()

    def live_running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def set_enabled(self, enabled: bool) -> None:
        self.control.set_enabled(enabled)
        self._write_bot(enabled)
        engine = self.engine
        if engine is None:
            self._notify()
            return
        if self.live_running():
            self._notify()
            return
        engine.quoting_enabled = bool(enabled)
        self._pulse(engine, quote=bool(enabled))
        self._notify()

    def reset(self, confirm: str) -> dict:
        if confirm != "RESET":
            return {"ok": False, "error": "confirm"}
        if self.live_running():
            self.control.request_reset()
            return {"ok": True, "pending": True}
        self._wipe_files()
        with self.lock:
            self._clear_memory()
        if self.config is not None:
            engine = open_engine(self.config, self.out)
            self.attach(engine)
        self.save()
        self._notify()
        return {"ok": True}

    def ingest(self, row: dict) -> None:
        """Memory only. The disk writer flushes the dashboard off the decision thread."""
        with self.lock:
            self._apply(row)
            self._dirty = True

    def flush_disk(self) -> None:
        with self.lock:
            if not getattr(self, "_dirty", False):
                return
            try:
                text = json.dumps(self._body())
            except RuntimeError:
                return
            self._dirty = False
        self._write_dashboard(text)
        self._notify()

    def snapshot(self) -> dict:
        with self.lock:
            body = self._body()
            sessions = []
            for market_id in self.order:
                sessions.append(self._session_view(market_id))
            equity = list(self.equity)
            blotter = list(reversed(self.blotter[-200:]))
            enabled = self.control.enabled()
            mode = self.mode
            now_ms = self.now_ms
            capital = self._capital()
        stats = _stats(self.engine)
        return {
            "paper": True,
            "mode": mode,
            "bot_enabled": enabled,
            "capital": capital,
            "now_ms": now_ms,
            "wall_ms": int(time.time() * 1000),
            "stats": stats,
            "equity": equity,
            "blotter": blotter,
            "sessions": sessions,
            "listener_error": getattr(self.engine, "listener_error", None),
            "feed": _feed_view(self.engine, self.config),
        }

    def save(self) -> None:
        with self.lock:
            self._dirty = True
        self.flush_disk()

    def _capital(self) -> float:
        config = self.config
        if config is not None:
            return float(config.initial_capital)
        if self.engine is not None:
            return float(self.engine.state.initial_capital)
        return 100.0

    def _clear_memory(self) -> None:
        self.sessions.clear()
        self.order.clear()
        self.blotter.clear()
        self.equity = [{"t": 0, "v": self._capital()}]
        self.now_ms = 0

    def _wipe_files(self) -> None:
        for name in (_DASH, "state.json", "summary.json", "sessions.csv", "STOP"):
            path = self.out / name
            if path.is_file():
                path.unlink()
        logs = self.out / "logs"
        if logs.is_dir():
            for child in logs.iterdir():
                if child.is_file():
                    child.unlink()

    def _apply(self, row: dict) -> None:
        kind = str(row.get("kind") or "")
        ts = int(row.get("ts") or 0)
        if ts:
            self.now_ms = ts
        market_id = str(row.get("market_id") or "")
        market = None
        if self.engine is not None and market_id:
            market = self.engine.state.markets.get(market_id)
        if market is not None:
            self._ensure(market)
            self._refresh(market, kind)
        if kind == "quote_submit":
            self._mark(market, row, "place")
            self._blotter(row, "live")
        elif kind == "cancel_request":
            self._mark(market, row, "cancel")
            self._blotter(row, "cancelled")
        elif kind == "fill":
            self._mark(market, row, "fill")
            self._blotter(row, "filled")
            if market is not None:
                self._note_equity(market)
        elif kind == "resolution" and str(row.get("detail") or "") in {"up", "down"}:
            self._exit_marks(market, str(row["detail"]))
        elif kind == "redeem" and market is not None:
            self._note_equity(market)

    def _ensure(self, market) -> None:
        market_id = market.market_id
        if market_id in self.sessions:
            return
        window = window_seconds(series_of(market.slug))
        open_ms = round_open_ms(market.slug, market.end_ts_ms, window)
        self.sessions[market_id] = {
            "id": market_id,
            "slug": market.slug,
            "question": market.question or "Bitcoin Up or Down – 5M",
            "open_ms": open_ms,
            "end_ms": int(market.end_ts_ms),
            "window_s": int(window),
            "points": [],
            "marks": [],
            "books": {"up": _empty_book(), "down": _empty_book()},
            "last": {"up": None, "down": None},
            "winner": "",
            "status": market.status,
        }
        self.order.append(market_id)
        self.order.sort(key=lambda item: self.sessions[item]["open_ms"])

    def _refresh(self, market, kind: str) -> None:
        sess = self.sessions[market.market_id]
        sess["status"] = market.status
        if market.winner:
            sess["winner"] = market.winner
        if market.reference_price:
            sess["price_to_beat"] = format(market.reference_price, "f")
        for outcome in ("up", "down"):
            book = market.book(outcome)
            sess["books"][outcome] = {
                "bids": [[float(price), float(size)] for price, size in book.bids[:8]],
                "asks": [[float(price), float(size)] for price, size in book.asks[:8]],
            }
        if kind in {
            "book",
            "binance_lean",
            "clock",
            "fill",
            "quote_submit",
            "quote_skipped",
        }:
            self._touch_point(market)
        if kind in {"fill", "redeem", "resolution", "quote_submit"}:
            self._touch_point(market)

    def _touch_point(self, market) -> None:
        sess = self.sessions[market.market_id]
        age = self._age(market, self.now_ms)
        up = _mid(market.up_book)
        down = _mid(market.down_book)
        if up is None and down is not None:
            up = 1.0 - down
        if down is None and up is not None:
            down = 1.0 - up
        btc = market.last_combined if market.last_combined is not None else market.last_binance
        points = sess["points"]
        if points and points[-1]["s"] == age:
            point = points[-1]
        else:
            point = {"s": age, "up": None, "down": None, "btc": None}
            points.append(point)
            if len(points) > 400:
                del points[:-400]
        if up is not None:
            point["up"] = up
        if down is not None:
            point["down"] = down
        if btc is not None:
            point["btc"] = float(btc)

    def _age(self, market, ts: int) -> int:
        sess = self.sessions[market.market_id]
        return int((int(ts) - int(sess["open_ms"])) / 1000)

    def _mark(self, market, row: dict, kind: str) -> None:
        if market is None:
            return
        price = _float_or_none(row.get("price"))
        if price is None:
            return
        size = _float_or_none(row.get("size")) or 0.0
        side = str(row.get("outcome") or "")
        if side not in {"up", "down"}:
            return
        sess = self.sessions[market.market_id]
        if kind == "fill":
            sess["last"][side] = price
        sess["marks"].append(
            {
                "kind": kind,
                "side": side,
                "price": price,
                "size": size,
                "s": self._age(market, int(row.get("ts") or self.now_ms)),
                "order_id": str(row.get("order_id") or ""),
            }
        )
        if len(sess["marks"]) > 500:
            del sess["marks"][:-500]

    def _exit_marks(self, market, winner: str) -> None:
        if market is None:
            return
        sess = self.sessions[market.market_id]
        sess["winner"] = winner
        age = int(sess["window_s"])
        for side in ("up", "down"):
            if any(mark["kind"] == "exit" and mark["side"] == side for mark in sess["marks"]):
                continue
            sess["marks"].append(
                {
                    "kind": "exit",
                    "side": side,
                    "price": 1.0 if side == winner else 0.0,
                    "size": 1.0,
                    "s": age,
                    "order_id": f"exit-{side}",
                }
            )

    def _blotter(self, row: dict, status: str) -> None:
        order_id = str(row.get("order_id") or "")
        found = None
        for item in self.blotter:
            if item["order_id"] == order_id:
                found = item
                break
        if found is None:
            found = {
                "order_id": order_id,
                "ts": int(row.get("ts") or 0),
                "market_id": str(row.get("market_id") or ""),
                "side": str(row.get("outcome") or ""),
                "price": str(row.get("price") or ""),
                "size": str(row.get("size") or ""),
                "status": status,
            }
            self.blotter.append(found)
        else:
            found["status"] = status
            if status == "filled":
                found["size"] = str(row.get("size") or found["size"])
                found["ts"] = int(row.get("ts") or found["ts"])
        if len(self.blotter) > 200:
            del self.blotter[:-200]

    def _note_equity(self, market) -> None:
        if self.engine is None:
            return
        value = float(self.engine.state.cash)
        for item in self.engine.state.markets.values():
            if item.status == "redeemed":
                continue
            up_mid = _mid(item.up_book) or 0.0
            down_mid = _mid(item.down_book) or 0.0
            value += float(item.up) * up_mid
            value += float(item.down) * down_mid
        point = {"t": int(self.now_ms), "v": round(value, 4), "market_id": market.market_id}
        if self.equity and self.equity[-1]["t"] == point["t"]:
            self.equity[-1] = point
        else:
            self.equity.append(point)
        if len(self.equity) > 800:
            del self.equity[:-800]

    def _session_view(self, market_id: str) -> dict:
        sess = self.sessions[market_id]
        view = {
            "id": sess["id"],
            "slug": sess["slug"],
            "question": sess["question"],
            "open_ms": sess["open_ms"],
            "end_ms": sess["end_ms"],
            "window_s": sess["window_s"],
            "price_to_beat": sess.get("price_to_beat"),
            "status": sess.get("status") or "open",
            "winner": sess.get("winner") or "",
            "points": sess["points"],
            "marks": sess["marks"],
            "books": sess["books"],
            "last": sess["last"],
            "resting": [],
            "position": None,
            "fair_up": None,
        }
        market = None
        if self.engine is not None:
            market = self.engine.state.markets.get(market_id)
        if market is None:
            return view
        view["status"] = market.status
        view["winner"] = market.winner or view["winner"]
        if market.reference_price:
            view["price_to_beat"] = format(market.reference_price, "f")
        if market.last_fair_up is not None:
            view["fair_up"] = format(market.last_fair_up, "f")
        view["position"] = _position(market)
        for order in self.engine.state.orders.values():
            if order.market_id != market_id or order.status not in WORKING_ORDER_STATUSES:
                continue
            if getattr(order, "side", "sell") != "buy":
                continue
            view["resting"].append(
                {
                    "order_id": order.order_id,
                    "side": order.outcome,
                    "price": format(order.price, "f"),
                    "size": format(order.remaining, "f"),
                    "status": order.status,
                }
            )
        return view

    def _body(self) -> dict:
        return {
            "sessions": self.sessions,
            "order": self.order,
            "blotter": self.blotter,
            "equity": self.equity,
            "now_ms": self.now_ms,
        }

    def _write_dashboard(self, text: str) -> None:
        path = self.out / _DASH
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(text, encoding="utf-8")
        last = None
        for attempt in range(6):
            try:
                tmp.replace(path)
                return
            except PermissionError as exc:
                last = exc
                time.sleep(0.05 * (attempt + 1))
        if last is not None:
            if self.engine is not None:
                self.engine.listener_error = f"PermissionError: {last}"
            print(f"dashboard save skipped: {last}", file=sys.stderr, flush=True)

    def _load_dashboard(self) -> None:
        path = self.out / _DASH
        if not path.is_file():
            return
        try:
            body = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        sessions = body.get("sessions") or {}
        if isinstance(sessions, dict):
            self.sessions = sessions
        order = body.get("order") or list(self.sessions)
        self.order = [item for item in order if item in self.sessions]
        self.blotter = list(body.get("blotter") or [])
        self.equity = list(body.get("equity") or [])
        self.now_ms = int(body.get("now_ms") or 0)

    def _read_bot(self) -> bool:
        path = self.out / _BOT
        if not path.is_file():
            return True
        try:
            body = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return True
        return bool(body.get("enabled", True))

    def _write_bot(self, enabled: bool) -> None:
        (self.out / _BOT).write_text(
            json.dumps({"enabled": bool(enabled)}), encoding="utf-8"
        )

    def _pulse(self, engine, *, quote: bool) -> None:
        """Apply a demo toggle on the engine. The live loop does this itself."""
        ts = max(int(engine.state.now_ms), int(self.now_ms)) + 1
        opens = [
            market
            for market in engine.state.markets.values()
            if market.status == "open"
        ]
        for market in opens:
            for _ in range(2):
                self.seq += 1
                engine.handle_event(
                    {
                        "type": "heartbeat",
                        "ts": ts,
                        "event_id": f"app-pulse-{self.seq}",
                        "market_id": market.market_id,
                    }
                )
                ts += 1
            if quote:
                break

    def subscribe(self):
        import queue

        box: queue.Queue = queue.Queue(maxsize=8)
        with self.lock:
            self.subs.append(box)
        return box

    def unsubscribe(self, box) -> None:
        with self.lock:
            if box in self.subs:
                self.subs.remove(box)

    def _notify(self) -> None:
        payload = json.dumps(self.snapshot())
        with self.lock:
            boxes = list(self.subs)
        for box in boxes:
            try:
                box.put_nowait(payload)
            except Exception:
                pass


def _empty_book() -> dict:
    return {"bids": [], "asks": []}


def _mid(book) -> Optional[float]:
    bid = book.best_bid()
    ask = book.best_ask()
    if bid is None or ask is None or ask <= bid:
        return None
    return float((bid + ask) / 2)


def _float_or_none(value) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _position(market) -> dict:
    row = round_report(market)
    return {
        "shares_up": row["shares_up"],
        "shares_down": row["shares_down"],
        "avg_up": row["avg_price_up"],
        "avg_down": row["avg_price_down"],
        "pair_cost": row["pair_cost"],
        "excess_shares": row["excess_shares"],
        "excess_side": row["excess_side"],
        "matched": row["pnl_matched"],
        "excess": row["pnl_excess"],
        "pnl": row["pnl"],
        "status": row["status"],
        "winner": row["winner"],
    }


def _stats(engine) -> dict:
    matched = Decimal("0")
    excess = Decimal("0")
    total = Decimal("0")
    wins = 0
    losses = 0
    redeemed = 0
    if engine is None:
        return _stat_body(matched, excess, total, wins, losses, redeemed, Decimal("0"))
    for market in engine.state.markets.values():
        if market.status != "redeemed":
            continue
        redeemed += 1
        row = round_report(market)
        pnl = Decimal(row["pnl"] or "0")
        total += pnl
        matched += Decimal(row["pnl_matched"] or "0")
        excess += Decimal(row["pnl_excess"] or "0")
        if pnl > 0:
            wins += 1
        elif pnl < 0:
            losses += 1
    return _stat_body(
        matched, excess, total, wins, losses, redeemed, engine.state.cash
    )


def _stat_body(matched, excess, total, wins, losses, redeemed, cash) -> dict:
    return {
        "matched": format(matched, "f"),
        "excess": format(excess, "f"),
        "pnl": format(total, "f"),
        "wins": wins,
        "losses": losses,
        "redeemed": redeemed,
        "cash": format(cash, "f"),
    }


def demo_config() -> Config:
    """Combined small_pair settings for the recorded tape. No environment file."""
    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "config.paper.json")
    config.strategy = "small_pair"
    config.small_pair_binance = True
    config.price_source = "combined"
    config.initial_capital = Decimal("100")
    config.max_loss_fraction = Decimal("0.99")
    config.stale_after_ms = 500_000
    config.ack_latency_ms = 0
    config.cancel_latency_ms = 0
    config.quote_cooldown_ms = 0
    config.max_open_markets = 2
    config.round_budget = Decimal("25")
    config.per_market_budget = Decimal("25")
    config.session_max_loss = Decimal("0")
    config.validate()
    return config


class _Tape:
    def __init__(self) -> None:
        self.px = BEAT
        self.ts = 0

    def latest(self):
        return (self.ts, self.px)

    def price_at(self, ts_ms):
        return self.px

    def vol(self, now_ms):
        return 0.00025


def play_demo(out_dir: Path, *, replay: bool = False) -> Hub:
    """Drive the real engine through one settled round and one open round."""
    out_dir = Path(out_dir)
    if replay:
        hub = Hub(out_dir, mode="demo", config=demo_config())
        hub._wipe_files()
    config = demo_config()
    if not replay and (out_dir / "state.json").exists():
        hub = Hub(out_dir, mode="demo", config=config)
        hub.attach(open_engine(config, out_dir))
        return hub
    out_dir.mkdir(parents=True, exist_ok=True)
    engine = open_engine(config, out_dir)
    hub = Hub(out_dir, mode="demo", config=config)
    hub.attach(engine)
    tape = _Tape()
    engine.binance_feed = tape
    engine.chainlink_feed = tape
    _play_tape(engine, tape)
    _polish_live_chart(hub)
    hub.save()
    return hub


def _polish_live_chart(hub: Hub) -> None:
    """Pin the open round's last print to the reference chart's 18.5 / 81.5."""
    if len(hub.order) < 2:
        return
    sess = hub.sessions[hub.order[-1]]
    if not sess["points"]:
        return
    sess["points"][-1]["up"] = 0.185
    sess["points"][-1]["down"] = 0.815
    sess["points"][-1]["btc"] = 62110.32
    sess["last"]["up"] = sess["last"]["up"] or 0.19


def _play_tape(engine, tape: _Tape) -> None:
    first = DEMO_OPEN - 300
    _round(
        engine,
        tape,
        market_id="m1",
        open_s=first,
        ages=list(range(0, 291, 10)),
        settle="down",
        path=_settled_path,
    )
    _round(
        engine,
        tape,
        market_id="m2",
        open_s=DEMO_OPEN,
        ages=list(range(0, 74, 5)),
        settle="",
        path=_live_path,
    )


def _settled_path(age: int) -> tuple[Decimal, float]:
    span = Decimal(290)
    done = Decimal(age) / span
    px = BEAT + (Decimal("62110.32") - BEAT) * done
    prob = 0.50 - 0.22 * float(done)
    return px, prob


def _live_path(age: int) -> tuple[Decimal, float]:
    if age <= 25:
        wobble = 0.02 * math.sin(age / 3.5)
        px = BEAT - Decimal(age) * Decimal("0.40")
        return px, 0.52 + wobble
    done = (age - 25) / 48
    done = done * done
    px = BEAT + (Decimal("62110.32") - BEAT) * Decimal(str(done))
    prob = 0.52 * (1.0 - done) + 0.185 * done
    return px, prob


def _round(engine, tape, *, market_id, open_s, ages, settle, path) -> None:
    open_ms = open_s * 1000
    end_ms = (open_s + 300) * 1000
    _push(
        engine,
        tape,
        {
            "type": "market_open",
            "event_id": f"open-{market_id}",
            "ts": open_ms,
            "market_id": market_id,
            "condition_id": f"0x{market_id}",
            "slug": f"btc-updown-5m-{open_s}",
            "question": "Bitcoin Up or Down – 5M",
            "up_token_id": f"{market_id}-up",
            "down_token_id": f"{market_id}-down",
            "up_label": "Up",
            "down_label": "Down",
            "up_index": 0,
            "down_index": 1,
            "end_ts": end_ms,
            "tick_size": "0.01",
            "min_order_size": "1",
            "synthetic": True,
            "accepting_orders": True,
            "reference_price": format(BEAT, "f"),
        },
        BEAT,
    )
    for age in ages:
        px, prob = path(age)
        ts = open_ms + age * 1000
        _books(engine, tape, market_id, ts, px, prob)
        if age in {30, 40} or (market_id == "m1" and age == 50):
            _hit_bid(engine, tape, market_id, "up" if age != 50 else "down", ts + 10, px)
    if market_id == "m2":
        _screenshot_book(engine, tape, market_id, open_ms + 73_000)
    if settle:
        _push(
            engine,
            tape,
            {
                "type": "resolution",
                "event_id": f"resolved-{market_id}",
                "ts": end_ms,
                "market_id": market_id,
                "source": "data-api-v2",
                "status": "resolved",
                "payouts": [0, 1_000_000] if settle == "down" else [1_000_000, 0],
                "market_type": "BINARY",
            },
            tape.px,
        )
        # A cancel is confirmed on the next event, and only then can the redeem land.
        _push(
            engine,
            tape,
            {
                "type": "heartbeat",
                "event_id": f"redeem-{market_id}",
                "ts": end_ms + 1,
                "market_id": market_id,
            },
            tape.px,
        )


def _books(engine, tape, market_id, ts, px, prob) -> None:
    mid = Decimal(str(round(prob, 2)))
    if mid < Decimal("0.05"):
        mid = Decimal("0.05")
    if mid > Decimal("0.95"):
        mid = Decimal("0.95")
    bid = mid - Decimal("0.01")
    ask = mid + Decimal("0.01")
    _one_book(engine, tape, market_id, "up", ts, bid, ask, px, "up")
    down_bid = (Decimal("1") - ask).quantize(Decimal("0.01"))
    down_ask = (Decimal("1") - bid).quantize(Decimal("0.01"))
    _one_book(engine, tape, market_id, "down", ts + 1, down_bid, down_ask, px, "down")
    _push(
        engine,
        tape,
        {
            "type": "heartbeat",
            "event_id": f"hb-{market_id}-{ts}",
            "ts": ts + 2,
            "market_id": market_id,
        },
        px,
    )


def _one_book(engine, tape, market_id, outcome, ts, bid, ask, px, tag) -> None:
    bids = []
    asks = []
    for step in range(5):
        level_bid = bid - Decimal(step) * Decimal("0.01")
        level_ask = ask + Decimal(step) * Decimal("0.01")
        if level_bid >= Decimal("0.01"):
            bids.append([format(level_bid, "f"), str(180 + step * 90)])
        if level_ask <= Decimal("0.99"):
            asks.append([format(level_ask, "f"), str(140 + step * 70)])
    _push(
        engine,
        tape,
        {
            "type": "book",
            "event_id": f"book-{market_id}-{outcome}-{ts}",
            "ts": ts,
            "market_id": market_id,
            "outcome": outcome,
            "bids": bids,
            "asks": asks,
            "hash": f"{tag}-{ts}-{bid}-{ask}",
        },
        px,
    )


def _screenshot_book(engine, tape, market_id, ts) -> None:
    """Ask ladder above 21¢ and bid ladder under 19¢, matching the reference book."""
    asks = [
        ["0.21", "133"],
        ["0.22", "420"],
        ["0.23", "594"],
        ["0.24", "530"],
        ["0.25", "1710"],
    ]
    bids = [
        ["0.19", "231"],
        ["0.18", "3310"],
        ["0.17", "2880"],
        ["0.16", "844"],
        ["0.15", "963"],
    ]
    _push(
        engine,
        tape,
        {
            "type": "book",
            "event_id": f"book-shot-up-{ts}",
            "ts": ts,
            "market_id": market_id,
            "outcome": "up",
            "bids": bids,
            "asks": asks,
            "hash": f"shot-up-{ts}",
        },
        Decimal("62110.32"),
    )
    down_asks = [["0.81", "133"], ["0.82", "420"], ["0.83", "594"], ["0.84", "530"]]
    down_bids = [["0.79", "231"], ["0.78", "1200"], ["0.77", "800"], ["0.76", "640"]]
    _push(
        engine,
        tape,
        {
            "type": "book",
            "event_id": f"book-shot-down-{ts}",
            "ts": ts,
            "market_id": market_id,
            "outcome": "down",
            "bids": down_bids,
            "asks": down_asks,
            "hash": f"shot-down-{ts}",
        },
        Decimal("62110.32"),
    )


def _hit_bid(engine, tape, market_id, outcome, ts, px) -> None:
    orders = [
        order
        for order in engine.state.orders.values()
        if order.market_id == market_id
        and order.outcome == outcome
        and getattr(order, "side", "") == "buy"
        and order.status == "live"
    ]
    if not orders:
        return
    order = sorted(orders, key=lambda item: item.price)[0]
    price = order.price - Decimal("0.01")
    if price <= 0:
        price = order.price
    _push(
        engine,
        tape,
        {
            "type": "trade",
            "event_id": f"trade-{market_id}-{outcome}-{ts}",
            "ts": ts,
            "trade_ts": ts,
            "market_id": market_id,
            "outcome": outcome,
            "price": format(price, "f"),
            "size": format(order.remaining, "f"),
            "taker_side": "SELL",
            "trade_id": f"trade-{market_id}-{outcome}-{ts}",
        },
        px,
    )


def _push(engine, tape, event, px: Decimal) -> None:
    tape.px = px
    tape.ts = int(event["ts"])
    engine.handle_event(event)


def live_config(path: Path, *, fast: bool = True) -> Config:
    """Same bankroll and price source as run_smallpair_combo100, own output folder.

    ``fast`` is the event-driven book. Pass False for the old REST poll.
    """
    from paper_split_maker.cli import _apply_strategy, _load

    args = Namespace(
        config=path,
        max_markets=0,
        strategy="small_pair",
        series="btc-updown-5m",
        binance_lean=True,
        price_source="combined",
        pair_gate=None,
        catch_up=None,
        first_leg_seconds=None,
        fast=fast,
        capital="100",
        max_loss_fraction="0.99",
    )
    return _apply_strategy(_load(args), args)


def _feed_view(engine, config) -> dict:
    decision = int(getattr(engine, "decision_wall_ms", 0) or 0) if engine is not None else 0
    age = None
    if decision:
        age = max(0, int(time.time() * 1000) - decision)
    return {
        "book": str(getattr(engine, "book_transport", "") or "") if engine is not None else "",
        "connected": bool(getattr(engine, "book_connected", False)) if engine is not None else False,
        "decision_age_ms": age,
        "fast": bool(getattr(config, "small_pair_fast", False)),
        "stalled": bool(getattr(engine, "stalled", False)) or (age is not None and age > 3000),
    }


def serve(hub: Hub, port: int, *, open_browser: bool = False) -> None:
    handler = _handler(hub)
    server = ThreadingHTTPServer((HOST, port), handler)
    bound = server.server_address[1]
    url = f"http://{HOST}:{bound}/"
    print(f"paper dashboard {url} mode={hub.mode}", flush=True)
    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        hub.control.request_stop()
        server.server_close()


def _handler(hub: Hub):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            return

        def do_GET(self):
            path = urlsplit(self.path).path
            if path in {"/", "/index.html"}:
                body = PAGE.read_text(encoding="utf-8").encode("utf-8")
                self._bytes(200, "text/html; charset=utf-8", body)
                return
            if path == "/api/state":
                self._json(200, hub.snapshot())
                return
            if path == "/events":
                self._events()
                return
            self._json(404, {"ok": False, "error": "not_found"})

        def do_POST(self):
            path = urlsplit(self.path).path
            payload = self._read_json()
            if path == "/api/bot":
                hub.set_enabled(bool(payload.get("enabled")))
                self._json(200, {"ok": True, "bot_enabled": hub.control.enabled()})
                return
            if path == "/api/reset":
                self._json(200, hub.reset(str(payload.get("confirm") or "")))
                return
            self._json(404, {"ok": False, "error": "not_found"})

        def _read_json(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if length < 0 or length > 10_000:
                return {}
            raw = self.rfile.read(length) if length else b""
            if not raw:
                return {}
            try:
                body = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return {}
            return body if isinstance(body, dict) else {}

        def _json(self, status: int, payload: dict) -> None:
            body = json.dumps(payload).encode("utf-8")
            self._bytes(status, "application/json; charset=utf-8", body)

        def _bytes(self, status: int, content_type: str, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'",
            )
            self.end_headers()
            self.wfile.write(body)

        def _events(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            box = hub.subscribe()
            try:
                self.wfile.write(b"data: " + json.dumps(hub.snapshot()).encode("utf-8") + b"\n\n")
                self.wfile.flush()
                while True:
                    try:
                        payload = box.get(timeout=15)
                    except Exception:
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                        continue
                    self.wfile.write(b"data: " + payload.encode("utf-8") + b"\n\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionError, OSError):
                return
            finally:
                hub.unsubscribe(box)

    return Handler


def main(argv: Optional[list[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Paper dashboard for combined small_pair")
    parser.add_argument("--config", default="config.paper.json")
    parser.add_argument("--out", default="runs/app_combo")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--demo", action="store_true")
    parser.add_argument("--replay", action="store_true")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument(
        "--fast",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "event-driven book and pair take. Default on. "
            "--no-fast is the old REST poll, for a side-by-side comparison."
        ),
    )
    args = parser.parse_args(argv)
    out = Path(args.out)
    if args.demo:
        hub = play_demo(out, replay=args.replay)
    else:
        path = Path(args.config)
        from paper_split_maker.cli import _credential_env

        config = live_config(path, fast=bool(args.fast))
        hub = Hub(out, mode="live", config=config, env=_credential_env(path))
        hub.start_live()
    if args.port == 8765:
        raise SystemExit("port 8765 is the other local page; use 8766")
    serve(hub, args.port, open_browser=not args.no_browser)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
