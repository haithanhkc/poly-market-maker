"""Bounded demo, replay, and public-data paper sessions."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, Optional

from paper_split_maker.config import Config, ConfigError
from paper_split_maker.engine import Engine
from paper_split_maker.logs import write_summary
from paper_split_maker.normalize import (
    clob_book_event,
    data_api_resolution_event,
    data_api_trade_events,
    gamma_market_event,
)
from paper_split_maker.public_client import PublicDataError, PublicReadClient


def load_scenario(path: Path) -> tuple[dict, list[dict]]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("events"), list):
        raise ConfigError("scenario must be an object with an events list")
    label = str(data.get("label") or "")
    if "synthetic" not in label.casefold() and "fixture" not in label.casefold():
        raise ConfigError("scenario label must say synthetic or fixture; unlabeled files are refused")
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
) -> dict:
    import time

    if duration_seconds <= 0:
        raise ConfigError("duration_seconds must be positive")
    if max_markets <= 0:
        raise ConfigError("max_markets must be positive")
    clock = now_s or time.time
    sleeper = sleep or time.sleep
    reader = client or PublicReadClient()
    engine = open_engine(config, out_dir)
    engine.mode = "paper"
    started = clock()
    deadline = started + duration_seconds
    notes = [
        "Public read-only paper session. No orders were sent.",
        "Bounded by duration_seconds and max_markets.",
    ]
    public_ok = True
    try:
        payloads = reader.discover_btc_updown(
            int(started),
            max_markets,
            list(config.market_slugs) or None,
        )
    except PublicDataError as exc:
        public_ok = False
        notes.append(f"discovery failed: {exc}")
        payloads = []
    opened = 0
    for index, payload in enumerate(payloads):
        if opened >= max_markets:
            break
        event = gamma_market_event(payload, synthetic=False)
        if event is None:
            notes.append(f"skipped undiscoverable or non Up/Down payload index {index}")
            continue
        event["event_id"] = f"open:{event['condition_id']}"
        event["ts"] = int(clock() * 1000)
        engine.handle_event(event)
        if event["market_id"] in engine.state.markets:
            opened += 1
    polls = 0
    while clock() < deadline:
        polls += 1
        stamp = int(clock() * 1000)
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
                for outcome, token in (("up", market.up_token_id), ("down", market.down_token_id)):
                    book = reader.fetch_book(token)
                    event = clob_book_event(
                        market.market_id,
                        outcome,
                        book,
                        event_id=f"book:{market.market_id}:{outcome}:{book.get('hash')}:{book.get('timestamp')}",
                    )
                    if event is None:
                        failed = True
                        continue
                    # Exchange timestamps can lead the local clock. Keep them on the
                    # book, and drive the ledger clock with the session clock.
                    event["book_ts"] = event["ts"]
                    event["ts"] = stamp
                    engine.handle_event(event)
                for trade in data_api_trade_events(view, reader.fetch_trades(market.condition_id)):
                    trade["trade_ts"] = trade["ts"]
                    trade["ts"] = stamp
                    engine.handle_event(trade)
                resolution = data_api_resolution_event(view, reader.fetch_resolution(market.condition_id))
                if resolution is not None:
                    engine.handle_event(resolution)
            except PublicDataError as exc:
                failed = True
                notes.append(f"poll failed: {exc}")
                break
        if failed:
            public_ok = False
            engine.handle_event({"type": "feed", "status": "down", "ts": stamp, "event_id": f"feed-down-{polls}"})
        elif not engine.state.feed_up:
            engine.handle_event({"type": "feed", "status": "up", "ts": stamp, "event_id": f"feed-up-{polls}"})
        engine.handle_event({"type": "heartbeat", "ts": stamp, "event_id": f"hb-{polls}-{stamp}"})
        remaining = deadline - clock()
        if remaining <= 0:
            break
        sleeper(min(config.poll_interval_seconds, remaining))
    notes.append(f"polls={polls} opened={opened}")
    summary = engine.summary(notes=notes)
    summary["public_data_ok"] = public_ok
    summary["duration_seconds"] = duration_seconds
    summary["max_markets"] = max_markets
    summary["polls"] = polls
    write_summary(out_dir, summary)
    return summary
