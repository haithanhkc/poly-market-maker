"""Bounded demo, replay, and public-data paper sessions."""

from __future__ import annotations

import csv
import json
import sys
import time
from pathlib import Path
from typing import Callable, Optional

from paper_split_maker.chain_data import window_seconds
from paper_split_maker.config import Config, ConfigError
from paper_split_maker.engine import Engine
from paper_split_maker.decimal_utils import ZERO, money
from paper_split_maker.logs import write_summary
from paper_split_maker.risk import worst_case_equity
from paper_split_maker.small_pair import can_fund_session
from paper_split_maker.normalize import (
    clob_book_event,
    data_api_resolution_event,
    data_api_trade_events,
    gamma_market_event,
)
from paper_split_maker.public_client import (
    PublicDataError,
    PublicReadClient,
    client_from_env,
)


def load_scenario(path: Path) -> tuple[dict, list[dict]]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("events"), list):
        raise ConfigError("scenario must be an object with an events list")
    label = str(data.get("label") or "")
    if "synthetic" not in label.casefold() and "fixture" not in label.casefold():
        raise ConfigError(
            "scenario label must say synthetic or fixture; unlabeled files are refused"
        )
    for event in data["events"]:
        if not isinstance(event, dict):
            raise ConfigError("scenario events must be objects")
    return data, data["events"]


def open_engine(config: Config, out_dir: Path) -> Engine:
    out_dir.mkdir(parents=True, exist_ok=True)
    state_path = out_dir / "state.json"
    log_dir = out_dir / "logs"
    if state_path.exists():
        engine = Engine.load(config, state_path, log_dir)
    else:
        engine = Engine.create(config, state_path, log_dir)
    return engine


def run_events(engine: Engine, events: list[dict]) -> None:
    for event in events:
        engine.handle_event(event)


def run_replay(config: Config, scenario_path: Path, out_dir: Path) -> dict:
    scenario, events = load_scenario(scenario_path)
    engine = open_engine(config, out_dir)
    engine.mode = "replay"
    run_events(engine, events)
    notes = [
        "Replay is deterministic given the file and the ledger.",
        "Synthetic or fixture resolution is not an official settlement.",
        str(scenario.get("description") or ""),
    ]
    summary = engine.summary(notes=[note for note in notes if note])
    summary["scenario"] = str(scenario_path)
    summary["scenario_label"] = scenario.get("label")
    write_summary(out_dir, summary)
    return summary


def run_demo(config: Config, out_dir: Path, scenario_path: Path) -> dict:
    summary = run_replay(config, scenario_path, out_dir)
    summary["mode"] = "demo"
    write_summary(out_dir, summary)
    return summary


def run_paper(
    config: Config,
    out_dir: Path,
    *,
    duration_seconds: int,
    max_markets: int,
    client: Optional[PublicReadClient] = None,
    now_s: Optional[Callable[[], float]] = None,
    sleep: Optional[Callable[[float], None]] = None,
    env: Optional[dict] = None,
    on_engine: Optional[Callable[[Engine], None]] = None,
) -> dict:
    import time

    if duration_seconds < 0:
        raise ConfigError("duration_seconds must be zero or positive")
    if max_markets < 0:
        raise ConfigError("max_markets must be zero or positive")
    clock = now_s or time.time
    sleeper = sleep or time.sleep
    reader = client or client_from_env(env)
    engine = open_engine(config, out_dir)
    engine.mode = "paper"
    if on_engine is not None:
        on_engine(engine)
    started = clock()
    deadline = None if duration_seconds == 0 else started + duration_seconds
    bound = (
        "Open-ended until the session cannot be funded or the run is stopped."
        if duration_seconds == 0 and max_markets == 0
        else "Bounded by duration_seconds and max_markets. Zero means no limit."
    )
    notes = [
        "Public read-only paper session. No orders were sent.",
        bound,
        f"discovery network={reader.network} series={getattr(config, 'series_slug', '') or 'btc-updown-5m'}",
    ]
    if getattr(config, "small_pair_fast", False):
        notes.append(
            "Fast paper loop. Books come from the predict.fun websocket "
            "(predictOrderbook and predictTopOfBook) and fall back to "
            "GET /v1/markets/orderbooks while the socket is down. "
            "Decisions run on each book and each BTC tick. "
            f"Ack latency stays {config.ack_latency_ms} ms and cancel latency "
            f"stays {config.cancel_latency_ms} ms; a 100 ms clock promotes them. "
            "An unpaired lot takes the opposite ask when its average cost plus "
            f"that ask is at most {config.small_pair_take_sum}. "
            "There is no public trade stream, so matches are polled every 250 ms. "
            "Quotes start at "
            f"+{int(getattr(config, 'small_pair_fast_start_seconds', 1))}s."
        )
    seen: set[str] = set()
    opened_slugs: set[str] = set()

    def note(text: str) -> None:
        if not text or text in seen:
            return
        seen.add(text)
        notes.append(text)
        _stderr(text)

    feeds = _start_price_feeds(config, engine, note)
    public_ok = True
    polls = 0
    try:
        return _paper_loop(
            config,
            out_dir,
            engine,
            reader,
            clock,
            sleeper,
            deadline,
            duration_seconds,
            max_markets,
            note,
            notes,
            opened_slugs,
            public_ok,
            polls,
        )
    finally:
        for feed in feeds:
            feed.stop()


def _paper_loop(
    config,
    out_dir,
    engine,
    reader,
    clock,
    sleeper,
    deadline,
    duration_seconds,
    max_markets,
    note,
    notes,
    opened_slugs,
    public_ok,
    polls,
):
    stop_reason = "running"
    redeemed_ids = {
        market.market_id
        for market in engine.state.markets.values()
        if market.status == "redeemed"
    }
    try:
        if getattr(config, "small_pair_fast", False) and str(
            getattr(config, "strategy", "")
        ) == "small_pair":
            return _paper_fast_loop(
                config,
                out_dir,
                engine,
                reader,
                clock,
                sleeper,
                deadline,
                duration_seconds,
                max_markets,
                note,
                notes,
                opened_slugs,
                public_ok,
                polls,
            )
        while True:
            if deadline is not None and clock() >= deadline:
                stop_reason = "duration"
                break
            if _hit_cap(len(opened_slugs), max_markets) and not _session_busy(engine):
                stop_reason = "max_markets"
                break
            if not _session_busy(engine) and _should_bust(config, engine):
                stop_reason = "bust"
                break
            if (out_dir / "STOP").exists():
                stop_reason = "manual"
                break
            control = getattr(engine, "paper_control", None)
            if control is not None:
                engine.quoting_enabled = bool(control.enabled())
                if control.should_reset():
                    stop_reason = "reset"
                    break
                if control.should_stop():
                    stop_reason = "manual"
                    break
            polls += 1
            stamp = int(clock() * 1000)
            begin = getattr(reader, "begin_poll", None)
            if begin is not None:
                begin()
            opened = _discover_due(
                engine, reader, config, stamp, max_markets, note, opened_slugs
            )
            if reader.last_discovery.get("error"):
                public_ok = False
            failed = False
            for market in list(engine.state.markets.values()):
                if market.status == "redeemed":
                    continue
                view = {
                    "market_id": market.market_id,
                    "condition_id": market.condition_id,
                    "up_token_id": market.up_token_id,
                    "down_token_id": market.down_token_id,
                }
                try:
                    for outcome, token in (
                        ("up", market.up_token_id),
                        ("down", market.down_token_id),
                    ):
                        try:
                            book = reader.fetch_book(token)
                        except PublicDataError:
                            market.book_fetches_failed += 1
                            raise
                        if _stamp_missing(book.get("timestamp")):
                            book = dict(book)
                            book["timestamp"] = stamp
                        event = clob_book_event(
                            market.market_id,
                            outcome,
                            book,
                            event_id=f"book:{market.market_id}:{outcome}:{book.get('hash')}:{book.get('timestamp')}:{polls}",
                        )
                        if event is None:
                            market.book_fetches_failed += 1
                            failed = True
                            continue
                        market.book_fetches_ok += 1
                        # Exchange timestamps can lead the local clock. Keep them on the
                        # book, and drive the ledger clock with the session clock.
                        event["book_ts"] = event["ts"]
                        event["ts"] = stamp
                        engine.handle_event(event)
                    trades_payload = _poll_trades(reader, market, note)
                    for trade in data_api_trade_events(view, trades_payload):
                        trade["trade_ts"] = trade["ts"]
                        trade["ts"] = stamp
                        engine.handle_event(trade)
                    resolution = data_api_resolution_event(
                        view, reader.fetch_resolution(market.condition_id)
                    )
                    if resolution is not None:
                        engine.handle_event(resolution)
                except PublicDataError as exc:
                    failed = True
                    note(f"poll failed: {exc}")
                    break
            if failed:
                public_ok = False
                engine.handle_event(
                    {
                        "type": "feed",
                        "status": "down",
                        "ts": stamp,
                        "event_id": f"feed-down-{polls}",
                    }
                )
            elif not engine.state.feed_up:
                engine.handle_event(
                    {
                        "type": "feed",
                        "status": "up",
                        "ts": stamp,
                        "event_id": f"feed-up-{polls}",
                    }
                )
            engine.book_transport = "rest"
            engine.book_connected = False
            engine.handle_event(
                {"type": "heartbeat", "ts": stamp, "event_id": f"hb-{polls}-{stamp}"}
            )
            engine.decision_wall_ms = int(time.time() * 1000)
            if getattr(config, "strategy", "") in {"dual_bid", "small_pair"}:
                label = str(config.strategy)
                for market in engine.state.markets.values():
                    note(_status_line(label, market))
            fresh = {
                market.market_id
                for market in engine.state.markets.values()
                if market.status == "redeemed"
            }
            if fresh - redeemed_ids:
                redeemed_ids = fresh
                _write_run_files(
                    out_dir,
                    engine,
                    notes,
                    public_ok,
                    duration_seconds,
                    max_markets,
                    polls,
                    len(opened_slugs),
                    dict(getattr(reader, "last_discovery", {}) or {}),
                    "running",
                )
            if deadline is None:
                sleeper(config.poll_interval_seconds)
                continue
            remaining = deadline - clock()
            if remaining <= 0:
                stop_reason = "duration"
                break
            sleeper(min(config.poll_interval_seconds, remaining))
    except KeyboardInterrupt:
        stop_reason = "manual"
    opened = len(opened_slugs)
    stats = dict(getattr(reader, "last_discovery", {}) or {})
    note(_discovery_line(stats))
    note(f"polls={polls} opened={opened} stop={stop_reason}")
    return _write_run_files(
        out_dir,
        engine,
        notes,
        public_ok,
        duration_seconds,
        max_markets,
        polls,
        opened,
        stats,
        stop_reason,
    )


# Wake at least this often so a book that arrived during the sleep is
# handled inside the 1s decision budget. Ack is still 250ms and cancel
# is still 500ms; the clock only promotes those timers.
_FAST_SLEEP_S = 0.05
_FAST_CLOCK_S = 0.10
_FAST_TRADE_S = 0.25
_FAST_DISCOVER_S = 0.5


def _paper_fast_loop(
    config,
    out_dir,
    engine,
    reader,
    clock,
    sleeper,
    deadline,
    duration_seconds,
    max_markets,
    note,
    notes,
    opened_slugs,
    public_ok,
    polls,
    book_feed=None,
):
    from paper_split_maker.feeds import market_ws
    from paper_split_maker.predict_ws import PredictBookFeed, yes_to_events

    own_feed = book_feed is None
    feed = book_feed
    if own_feed:
        feed = PredictBookFeed(
            market_ws(getattr(reader, "network", "mainnet")),
            str(getattr(reader, "api_key", "") or ""),
        )
        feed.start()
    stop_reason = "running"
    redeemed_ids = {
        market.market_id
        for market in engine.state.markets.values()
        if market.status == "redeemed"
    }
    last_clock = 0.0
    last_trades = 0.0
    last_discover = -1e9
    last_price = None
    try:
        while True:
            if deadline is not None and clock() >= deadline:
                stop_reason = "duration"
                break
            if _hit_cap(len(opened_slugs), max_markets) and not _session_busy(engine):
                stop_reason = "max_markets"
                break
            if not _session_busy(engine) and _should_bust(config, engine):
                stop_reason = "bust"
                break
            if (out_dir / "STOP").exists():
                stop_reason = "manual"
                break
            control = getattr(engine, "paper_control", None)
            if control is not None:
                engine.quoting_enabled = bool(control.enabled())
                if control.should_reset():
                    stop_reason = "reset"
                    break
                if control.should_stop():
                    stop_reason = "manual"
                    break
            now = clock()
            stamp = int(now * 1000)
            if now - last_discover >= _FAST_DISCOVER_S:
                begin = getattr(reader, "begin_poll", None)
                if begin is not None:
                    begin()
                _discover_due(
                    engine, reader, config, stamp, max_markets, note, opened_slugs
                )
                last_discover = now
            routes = _predict_routes(reader, engine)
            try:
                feed.set_markets(list(routes))
                frames = list(feed.drain() or [])
            except Exception as exc:
                frames = []
                note(f"book socket read failed: {exc}")
            applied = False
            failed = False
            if frames:
                applied, failed, polls = _apply_yes_frames(
                    engine, frames, routes, stamp, polls, yes_to_events
                )
                if applied:
                    engine.book_transport = "ws"
                    engine.book_connected = True
                    polls += 1
                    _heartbeat(engine, stamp, polls)
            elif not getattr(feed, "connected", False) and now - last_trades >= _FAST_TRADE_S:
                rest_applied, rest_failed, polls = _apply_rest_books(
                    engine, reader, routes, stamp, polls, note, yes_to_events
                )
                applied = rest_applied
                failed = rest_failed
                if applied:
                    engine.book_transport = "rest"
                    engine.book_connected = False
                    polls += 1
                    _heartbeat(engine, stamp, polls)
            if getattr(feed, "connected", False):
                engine.book_transport = "ws"
                engine.book_connected = True
            else:
                engine.book_connected = False
            if failed and not engine.book_connected:
                public_ok = False
                polls += 1
                engine.handle_event(
                    {
                        "type": "feed",
                        "status": "down",
                        "ts": stamp,
                        "event_id": f"feed-down-{polls}",
                    }
                )
            elif applied and not engine.state.feed_up:
                polls += 1
                engine.handle_event(
                    {
                        "type": "feed",
                        "status": "up",
                        "ts": stamp,
                        "event_id": f"feed-up-{polls}",
                    }
                )
            price = _btc_token(engine)
            if last_price is not None and price != last_price and price:
                polls += 1
                _heartbeat(engine, stamp, polls)
            last_price = price
            if now - last_clock >= _FAST_CLOCK_S:
                polls += 1
                engine.handle_event(
                    {
                        "type": "clock",
                        "ts": stamp,
                        "event_id": f"clock-{polls}-{stamp}",
                    }
                )
                last_clock = now
            if now - last_trades >= _FAST_TRADE_S:
                _poll_open_trades(engine, reader, stamp, note)
                _poll_resolutions(engine, reader)
                last_trades = now
                if str(getattr(config, "strategy", "")) == "small_pair":
                    for market in engine.state.markets.values():
                        note(_status_line("small_pair", market))
            fresh = {
                market.market_id
                for market in engine.state.markets.values()
                if market.status == "redeemed"
            }
            if fresh - redeemed_ids:
                redeemed_ids = fresh
                _write_run_files(
                    out_dir,
                    engine,
                    notes,
                    public_ok,
                    duration_seconds,
                    max_markets,
                    polls,
                    len(opened_slugs),
                    dict(getattr(reader, "last_discovery", {}) or {}),
                    "running",
                )
            if deadline is None:
                sleeper(_FAST_SLEEP_S)
                continue
            remaining = deadline - clock()
            if remaining <= 0:
                stop_reason = "duration"
                break
            sleeper(min(_FAST_SLEEP_S, remaining))
    except KeyboardInterrupt:
        stop_reason = "manual"
    finally:
        if own_feed and feed is not None:
            feed.stop()
    opened = len(opened_slugs)
    stats = dict(getattr(reader, "last_discovery", {}) or {})
    note(_discovery_line(stats))
    note(f"polls={polls} opened={opened} stop={stop_reason}")
    return _write_run_files(
        out_dir,
        engine,
        notes,
        public_ok,
        duration_seconds,
        max_markets,
        polls,
        opened,
        stats,
        stop_reason,
    )


def _heartbeat(engine, stamp: int, polls: int) -> None:
    engine.handle_event(
        {"type": "heartbeat", "ts": stamp, "event_id": f"hb-{polls}-{stamp}"}
    )
    engine.decision_wall_ms = int(time.time() * 1000)


def _btc_token(engine):
    bits = []
    for name in ("binance_feed", "chainlink_feed"):
        feed = getattr(engine, name, None)
        latest = feed.latest() if feed is not None and hasattr(feed, "latest") else None
        if not latest:
            bits.append(None)
            continue
        bits.append((int(latest[0]), str(latest[1])))
    return tuple(bits)


def _predict_routes(reader, engine) -> dict:
    routes = {}
    table = getattr(reader, "_by_condition", {}) or {}
    for market in engine.state.markets.values():
        if market.status == "redeemed":
            continue
        info = table.get(str(market.condition_id or "").lower())
        if not isinstance(info, dict):
            continue
        ident = str(info.get("market_id") or "")
        if not ident:
            continue
        routes[ident] = (market, info)
    return routes


def _apply_yes_frames(engine, frames, routes, stamp, polls, yes_to_events):
    applied = False
    failed = False
    for yes in frames:
        pair = routes.get(str(yes.get("market_id") or ""))
        if pair is None:
            continue
        market, info = pair
        polls += 1
        events = yes_to_events(
            market.market_id,
            yes,
            stamp,
            int(info.get("precision") or 2),
            f"{polls}",
        )
        if not events:
            market.book_fetches_failed += 1
            failed = True
            continue
        for event in events:
            engine.handle_event(event)
            market.book_fetches_ok += 1
        applied = True
    return applied, failed, polls


def _apply_rest_books(engine, reader, routes, stamp, polls, note, yes_to_events):
    if not routes:
        return False, False, polls
    fetch_many = getattr(reader, "fetch_orderbooks", None)
    books = None
    try:
        if fetch_many is not None:
            books = fetch_many(list(routes))
        else:
            books = {}
            for ident in routes:
                books[ident] = reader.fetch_book(routes[ident][0].up_token_id)
    except PublicDataError as exc:
        note(f"orderbooks failed: {exc}")
        return False, True, polls
    if not isinstance(books, dict):
        return False, True, polls
    frames = []
    for ident, yes in books.items():
        if not isinstance(yes, dict):
            continue
        row = dict(yes)
        row["market_id"] = str(ident)
        frames.append(row)
    return _apply_yes_frames(engine, frames, routes, stamp, polls, yes_to_events)


def _poll_open_trades(engine, reader, stamp, note) -> None:
    for market in list(engine.state.markets.values()):
        if market.status == "redeemed":
            continue
        view = {
            "market_id": market.market_id,
            "condition_id": market.condition_id,
            "up_token_id": market.up_token_id,
            "down_token_id": market.down_token_id,
        }
        payload = _poll_trades(reader, market, note)
        for trade in data_api_trade_events(view, payload):
            trade["trade_ts"] = trade["ts"]
            trade["ts"] = stamp
            engine.handle_event(trade)


def _poll_resolutions(engine, reader) -> None:
    for market in list(engine.state.markets.values()):
        if market.status != "open":
            continue
        view = {
            "market_id": market.market_id,
            "condition_id": market.condition_id,
            "up_token_id": market.up_token_id,
            "down_token_id": market.down_token_id,
        }
        try:
            payload = reader.fetch_resolution(market.condition_id)
        except PublicDataError:
            continue
        resolution = data_api_resolution_event(view, payload)
        if resolution is not None:
            resolution["ts"] = engine.state.now_ms
            engine.handle_event(resolution)


def _hit_cap(opened: int, max_markets: int) -> bool:
    return max_markets > 0 and opened >= max_markets


def _session_busy(engine) -> bool:
    for market in engine.state.markets.values():
        if market.status not in {"redeemed", "expired_unresolved"}:
            return True
    return False


def _should_bust(config, engine) -> bool:
    if engine.state.killed:
        return True
    return not can_fund_session(config, engine.state.cash)


def _progress(engine) -> dict:
    redeemed = [
        market
        for market in engine.state.markets.values()
        if market.status == "redeemed"
    ]
    wins = losses = flats = 0
    pnl = ZERO
    for market in redeemed:
        flow = market.net_cash_flow
        pnl += flow
        if flow > 0:
            wins += 1
        elif flow < 0:
            losses += 1
        else:
            flats += 1
    return {
        "equity": money(worst_case_equity(engine.state)),
        "cash": money(engine.state.cash),
        "sessions": len(engine.state.markets),
        "redeemed": len(redeemed),
        "wins": wins,
        "losses": losses,
        "flats": flats,
        "cumulative_pnl": money(pnl),
    }


_SESSION_COLUMNS = (
    "slug",
    "market_id",
    "status",
    "winner",
    "shares_up",
    "shares_down",
    "pair_cost",
    "pnl",
    "pnl_matched",
    "pnl_excess",
    "filters_adverse",
    "filters_loser_cutoff",
    "filters_imbalance",
    "filters_late_loser",
    "fill_fair_minus_price_avg",
)


def write_sessions_csv(directory: Path, summary: dict) -> Path:
    path = Path(directory) / "sessions.csv"
    rounds = summary.get("rounds") or []
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=_SESSION_COLUMNS)
        writer.writeheader()
        for row in rounds:
            filters = row.get("filters") or {}
            writer.writerow(
                {
                    "slug": row.get("slug") or "",
                    "market_id": row.get("market_id") or "",
                    "status": row.get("status") or "",
                    "winner": row.get("winner") or "",
                    "shares_up": row.get("shares_up") or "",
                    "shares_down": row.get("shares_down") or "",
                    "pair_cost": row.get("pair_cost") or "",
                    "pnl": row.get("pnl") or "",
                    "pnl_matched": row.get("pnl_matched") or "",
                    "pnl_excess": row.get("pnl_excess") or "",
                    "filters_adverse": filters.get("adverse", 0),
                    "filters_loser_cutoff": filters.get("loser_cutoff", 0),
                    "filters_imbalance": filters.get("imbalance", 0),
                    "filters_late_loser": filters.get("late_loser", 0),
                    "fill_fair_minus_price_avg": row.get("fill_fair_minus_price_avg") or "",
                }
            )
    return path


def _write_run_files(
    out_dir,
    engine,
    notes,
    public_ok,
    duration_seconds,
    max_markets,
    polls,
    opened,
    stats,
    stop_reason,
) -> dict:
    summary = engine.summary(notes=notes)
    summary["public_data_ok"] = public_ok
    summary["duration_seconds"] = duration_seconds
    summary["max_markets"] = max_markets
    summary["polls"] = polls
    summary["opened"] = opened
    summary["discovery"] = stats
    summary["stop_reason"] = stop_reason
    summary["progress"] = _progress(engine)
    write_summary(out_dir, summary)
    write_sessions_csv(out_dir, summary)
    return summary


def _start_price_feeds(config, engine, note) -> list:
    """Public prices for the paper filter. The default source is Binance only."""
    if not getattr(config, "small_pair_binance", False):
        return []
    if str(getattr(config, "strategy", "") or "") != "small_pair":
        return []
    source = getattr(config, "price_source", "binance") or "binance"
    started = []
    if source in {"binance", "combined"}:
        from paper_split_maker.binance_btc import BinanceBtcFeed

        feed = BinanceBtcFeed(lookback_s=int(config.small_pair_vol_lookback_seconds))
        engine.binance_feed = feed
        feed.start()
        note("binance btc feed started (public trade stream, no orders)")
        started.append(feed)
    if source in {"chainlink", "combined"}:
        from paper_split_maker.chainlink_btc import ChainlinkBtcFeed

        link = ChainlinkBtcFeed(poll_s=int(config.chainlink_poll_seconds))
        engine.chainlink_feed = link
        link.start()
        note("chainlink btc/usd feed started (public bnb aggregator, no orders)")
        started.append(link)
    return started


def sessions_due(series: str, now_s: int, lead: int, *, include_current: bool) -> list[str]:
    """Current window, when requested, plus later windows that open inside the lead."""
    window = window_seconds(series)
    current = (int(now_s) // window) * window
    slugs = [f"{series}-{current}"] if include_current else []
    slugs.extend(_due_slugs(series, window, now_s, lead, max_markets=8))
    return slugs


def _discover_due(engine, reader, config, stamp: int, max_markets: int, note, opened_slugs: set[str]) -> int:
    """Load the next session during its pre-open lead.

    Split mode skips a window that has already opened. ``dual_bid`` also loads
    the current window while it is still inside the quote stop. ``small_pair``
    loads each series in ``small_pair_series`` (5m and 15m by default).
    """
    strategy = str(getattr(config, "strategy", "") or "")
    if strategy == "small_pair":
        series_list = [
            str(item)
            for item in (
                getattr(config, "small_pair_series", None)
                or ("btc-updown-5m", "btc-updown-15m")
            )
        ]
    else:
        series_list = [str(getattr(config, "series_slug", "") or "btc-updown-5m")]
    discoveries = []
    for series in series_list:
        if _hit_cap(len(opened_slugs), max_markets):
            break
        _discover_series(
            engine, reader, config, stamp, max_markets, note, opened_slugs, series
        )
        discoveries.append(dict(getattr(reader, "last_discovery", {}) or {}))
        if strategy != "small_pair":
            break
    if len(discoveries) > 1 and hasattr(reader, "last_discovery"):
        reader.last_discovery = _merge_discovery(discoveries)
    return len(opened_slugs)


def _discover_series(
    engine, reader, config, stamp, max_markets, note, opened_slugs, series: str
) -> None:
    strategy = str(getattr(config, "strategy", "") or "")
    window = window_seconds(series)
    now_s = stamp // 1000
    current = (now_s // window) * window
    lead = int(getattr(config, "split_lead_seconds", 0) or 0)
    current_slug = f"{series}-{current}"
    age = now_s - current
    want_current = False
    if strategy == "small_pair":
        if 0 <= age < window:
            want_current = True
            if getattr(config, "small_pair_fast", False):
                start = int(getattr(config, "small_pair_fast_start_seconds", 1) or 0)
            elif getattr(config, "small_pair_binance", False):
                start = int(getattr(config, "small_pair_start_seconds", 10) or 10)
            else:
                start = 2
            note(f"{current_slug} is open; small_pair quotes from +{start}s until close")
    elif strategy == "dual_bid":
        stop = int(getattr(config, "dual_bid_stop_seconds", 275) or 275)
        start = int(getattr(config, "dual_bid_start_seconds", 8) or 8)
        if 0 <= age <= stop:
            want_current = True
            note(
                f"{current_slug} is open; dual_bid quotes from +{start}s until +{stop}s"
            )
    if not want_current:
        note(f"{series}-{current} already started; waiting for the next session")
    lookahead = 8 if max_markets <= 0 else min(int(max_markets), 8)
    due = _due_slugs(series, window, now_s, lead, lookahead)
    explicit = [str(item) for item in (config.market_slugs or []) if str(item)]
    if strategy == "small_pair":
        explicit = []
    slugs = explicit or (([current_slug] if want_current else []) + due)
    if not slugs:
        if lead <= 0:
            note("split_lead_seconds is 0; no upcoming session is loaded")
        else:
            nxt = current + window
            note(f"next session {series}-{nxt} opens in {nxt - now_s}s; lead is {lead}s")
        return
    try:
        payloads = reader.discover_btc_updown(now_s, max(len(slugs), 1), slugs, series=series)
    except PublicDataError as exc:
        note(f"discovery failed: {exc}")
        return
    stats = getattr(reader, "last_discovery", {}) or {}
    if stats.get("error"):
        note(f"discovery failed: {stats['error']}")
    elif int(stats.get("rounds") or 0) == 0:
        note(f"discovery returned no rounds for {series} on {reader.network}")
    by_slug = {str(item.get("slug") or ""): item for item in payloads if isinstance(item, dict)}
    for slug in slugs:
        if _hit_cap(len(opened_slugs), max_markets):
            break
        if _already_open(engine, slug):
            opened_slugs.add(slug)
            note(f"opened {slug}")
            continue
        payload = by_slug.get(slug)
        if payload is None:
            starts = stats.get("starts") or []
            note(f"wanted {slug} not in {stats.get('rounds', 0)} rounds; saw starts {starts}")
            continue
        event = gamma_market_event(payload, market_id=slug, synthetic=False)
        if event is None:
            note(f"{slug} is not a usable Up/Down market")
            continue
        event["event_id"] = f"open:{slug}:{stamp}"
        event["ts"] = stamp
        engine.handle_event(event)
        if _already_open(engine, slug):
            opened_slugs.add(slug)
            note(f"opened {slug}")
        else:
            note(f"{slug} was not loaded")


def _merge_discovery(rows: list[dict]) -> dict:
    errors = [str(row.get("error") or "") for row in rows if row.get("error")]
    starts = []
    for row in rows:
        starts.extend(row.get("starts") or [])
    return {
        "network": rows[-1].get("network") if rows else "",
        "series": ",".join(str(row.get("series") or "") for row in rows),
        "http_status": rows[-1].get("http_status") if rows else 0,
        "rounds": sum(int(row.get("rounds") or 0) for row in rows),
        "parsed": sum(int(row.get("parsed") or 0) for row in rows),
        "matched": sum(int(row.get("matched") or 0) for row in rows),
        "unparsed": sum(int(row.get("unparsed") or 0) for row in rows),
        "starts": starts[:8],
        "error": "; ".join(errors),
        "by_series": rows,
    }


def _due_slugs(series: str, window: int, now_s: int, lead: int, max_markets: int) -> list[str]:
    current = (int(now_s) // window) * window
    due = []
    for step in range(1, max(max_markets, 1) + 2):
        open_s = current + step * window
        wait = open_s - int(now_s)
        if wait <= 0:
            continue
        if wait > lead:
            break
        due.append(f"{series}-{open_s}")
        if len(due) >= max_markets:
            break
    return due


def _stamp_missing(value) -> bool:
    if value in (None, "", 0, "0"):
        return True
    try:
        return int(value) <= 0
    except (TypeError, ValueError):
        return False


def _poll_trades(reader, market, note) -> dict:
    """Count the matches request. A trade error does not take the book feed down."""
    try:
        payload = reader.fetch_trades(market.condition_id)
    except PublicDataError as exc:
        market.trade_fetches_failed += 1
        market.trade_fetch_status = int(getattr(exc, "status", 0) or 0)
        note(f"trades {market.slug} http {market.trade_fetch_status}")
        return {"data": []}
    info = getattr(reader, "last_trade_fetch", None)
    if not isinstance(info, dict):
        market.trade_fetches_ok += 1
        market.trade_fetch_status = market.trade_fetch_status or 200
        return payload if isinstance(payload, dict) else {"data": []}
    market.trade_fetch_status = int(info.get("status") or 0)
    market.trades_returned += int(info.get("rows") or 0)
    if info.get("ok"):
        market.trade_fetches_ok += 1
    else:
        market.trade_fetches_failed += 1
        note(
            f"trades {market.slug} http {market.trade_fetch_status} "
            f"rows={info.get('rows', 0)}"
        )
    return payload if isinstance(payload, dict) else {"data": []}


def _dual_status_line(market) -> str:
    return _status_line("dual_bid", market)


def _status_line(label: str, market) -> str:
    skipped = market.skip_counts or {}
    reasons = " ".join(f"{key}={skipped[key]}" for key in sorted(skipped))
    return (
        f"{label} {market.slug} books_ok={market.book_fetches_ok} "
        f"books_failed={market.book_fetches_failed} bids_posted={market.bids_posted} "
        f"trades_ok={market.trade_fetches_ok} trades_failed={market.trade_fetches_failed} "
        f"trades_http={market.trade_fetch_status} trades_seen={market.trades_seen} "
        f"skipped {reasons or 'none'}"
    )


def _already_open(engine, slug: str) -> bool:
    if slug in engine.state.markets:
        return True
    return any(getattr(market, "slug", "") == slug for market in engine.state.markets.values())


def _discovery_line(stats: dict) -> str:
    if not stats:
        return ""
    return (
        "discovery "
        f"network={stats.get('network')} series={stats.get('series')} "
        f"http={stats.get('http_status')} rounds={stats.get('rounds')} "
        f"parsed={stats.get('parsed')} matched={stats.get('matched')} "
        f"unparsed={stats.get('unparsed')}"
    )


def _stderr(text: str) -> None:
    try:
        print(text, file=sys.stderr)
    except UnicodeEncodeError:
        stream = getattr(sys.stderr, "buffer", None)
        if stream is not None:
            stream.write((text + "\n").encode("utf-8", "replace"))
            stream.flush()
