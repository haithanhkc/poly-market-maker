"""Bounded demo, replay, and public-data paper sessions."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Callable, Optional

from paper_split_maker.chain_data import window_seconds
from paper_split_maker.config import Config, ConfigError
from paper_split_maker.engine import Engine
from paper_split_maker.logs import write_summary
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
) -> dict:
    import time

    if duration_seconds <= 0:
        raise ConfigError("duration_seconds must be positive")
    if max_markets <= 0:
        raise ConfigError("max_markets must be positive")
    clock = now_s or time.time
    sleeper = sleep or time.sleep
    reader = client or client_from_env(env)
    engine = open_engine(config, out_dir)
    engine.mode = "paper"
    started = clock()
    deadline = started + duration_seconds
    notes = [
        "Public read-only paper session. No orders were sent.",
        "Bounded by duration_seconds and max_markets.",
        f"discovery network={reader.network} series={getattr(config, 'series_slug', '') or 'btc-updown-5m'}",
    ]
    seen: set[str] = set()
    opened_slugs: set[str] = set()

    def note(text: str) -> None:
        if not text or text in seen:
            return
        seen.add(text)
        notes.append(text)
        _stderr(text)

    public_ok = True
    polls = 0
    while clock() < deadline:
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
        engine.handle_event(
            {"type": "heartbeat", "ts": stamp, "event_id": f"hb-{polls}-{stamp}"}
        )
        if getattr(config, "strategy", "") in {"dual_bid", "small_pair"}:
            label = str(config.strategy)
            for market in engine.state.markets.values():
                note(_status_line(label, market))
        remaining = deadline - clock()
        if remaining <= 0:
            break
        sleeper(min(config.poll_interval_seconds, remaining))
    opened = len(opened_slugs)
    stats = dict(getattr(reader, "last_discovery", {}) or {})
    note(_discovery_line(stats))
    note(f"polls={polls} opened={opened}")
    summary = engine.summary(notes=notes)
    summary["public_data_ok"] = public_ok
    summary["duration_seconds"] = duration_seconds
    summary["max_markets"] = max_markets
    summary["polls"] = polls
    summary["opened"] = opened
    summary["discovery"] = stats
    write_summary(out_dir, summary)
    return summary


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
        if len(opened_slugs) >= max_markets:
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
            note(f"{current_slug} is open; small_pair quotes from +2s until close")
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
    due = _due_slugs(series, window, now_s, lead, max_markets)
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
        if len(opened_slugs) >= max_markets:
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
