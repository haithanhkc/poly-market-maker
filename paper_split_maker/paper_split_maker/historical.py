"""Historical paper replay from public prints.

Polymarket's public CLOB ``/book`` endpoint is the live book. It is not a
historical snapshot, and a closed 5-minute token returns 404. This module
fetches Gamma identity, Data API trades, Data API one-minute price history,
and Data API resolution payouts, then builds a labeled book reconstruction.

The reconstruction is a counterfactual. It is not a claim that these orders
would have filled, and it is not evidence of live profit.
"""

from __future__ import annotations

import csv
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Optional

from paper_split_maker.config import Config, ConfigError
from paper_split_maker.decimal_utils import D, money, qfloor
from paper_split_maker.engine import Engine
from paper_split_maker.logs import write_summary
from paper_split_maker.models import WORKING_ORDER_STATUSES
from paper_split_maker.normalize import data_api_resolution_event, gamma_market_event
from paper_split_maker.public_client import (
    GAMMA,
    PublicDataError,
    PublicReadClient,
    UrllibTransport,
)
from paper_split_maker.risk import conservative_fee, worst_case_equity
from paper_split_maker.session import open_engine

BKK = timezone(timedelta(hours=7))
UTC = timezone.utc
WINDOW_START = "2026-11-06T17:00:00Z"
WINDOW_END = "2026-11-07T17:00:00Z"
MARKET_COUNT = 10
LABEL = "reconstructed-from-public-prints"
QUEUE_SHARES = Decimal("5")
MIN_JOIN_SPREAD = Decimal("0.02")

ASSUMPTIONS = [
    "No public historical order-book endpoint was used. CLOB GET /book is the live book and returned 404 for a closed token from this day.",
    "Books are reconstructed. best ask is the last BUY print ceiled to the tick. best bid is the last SELL print floored to the tick.",
    "When that observed spread is positive and below 0.02, the bid is lowered so the spread is 0.02 and the engine joins the ask. The configured minimum edge is 0.01, so a tighter reconstructed book would post behind the ask with an empty queue. Widening avoids that optimistic empty queue.",
    "When the observed spread is above 0.10, the observed bid is kept and the engine's own max_spread rule declines the quote.",
    "Displayed size on both sides is 5 shares, the same prior as improve_queue_shares. Resting depth is not in the public tape. A join therefore sits behind 5 shares.",
    "A one-minute Data API price that covers the decision time and falls outside the observed bid/ask makes the book uncertain. No new quote is placed on an uncertain book.",
    "Only a later taker BUY at exactly our limit can fill, and only after those 5 shares of queue. Sells do not fill our sells. A higher print does not fill us (trade_through_fills is false).",
    "Gamma currently reports these markets closed. The replay turns accepting_orders on only for the historical 5-minute window. The snapshot keeps the fetched closed flag.",
    "Resolution is the Data API payout row only. outcomePrices and a 0.99 print are not a winner.",
    "Maker rebates are estimated and are not in cash, realized PnL, or the loss floor.",
]


def parse_utc(value: str) -> int:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp())


def window_starts(start_s: int, end_s: int, count: int = MARKET_COUNT) -> list[int]:
    """Ten 5-minute window starts spread across [start_s, end_s)."""
    if end_s - start_s < 300 * count:
        raise ConfigError("window is shorter than 10 five-minute markets")
    span = end_s - start_s
    starts = []
    for index in range(count):
        slice_start = start_s + index * (span // count)
        slice_end = (
            end_s if index == count - 1 else start_s + (index + 1) * (span // count)
        )
        window = (slice_start // 300) * 300
        if window < slice_start:
            window += 300
        if window + 300 > slice_end:
            raise ConfigError("could not place a 5-minute window in every slice")
        starts.append(window)
    if len(set(starts)) != count:
        raise ConfigError("window starts are not unique")
    return starts


def load_historical_snapshot(path: Path) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ConfigError("snapshot must be an object")
    label = str(data.get("label") or "")
    if "reconstructed" not in label.casefold():
        raise ConfigError(
            "snapshot label must say reconstructed; unlabeled history is refused"
        )
    if data.get("synthetic_order_books") is not True:
        raise ConfigError("snapshot must mark synthetic_order_books true")
    markets = data.get("markets")
    if not isinstance(markets, list) or not markets:
        raise ConfigError("snapshot has no markets")
    return data


def fetch_snapshot(
    start_s: int,
    end_s: int,
    client: Optional[PublicReadClient] = None,
) -> dict:
    reader = client or PublicReadClient(UrllibTransport(timeout=30))
    markets = []
    for window in window_starts(start_s, end_s):
        print(f"fetching btc-updown-5m-{window}", flush=True)
        markets.append(_fetch_one(reader, window, start_s, end_s))
        print(
            f"  {markets[-1]['slug']} prints={markets[-1]['print_count']}",
            flush=True,
        )
    return {
        "label": LABEL,
        "synthetic_order_books": True,
        "official_resolution": True,
        "not_live_results": True,
        "description": (
            "Public read-only snapshot for a paper replay. Order books in the "
            "replay are reconstructed from these prints. This is not a live "
            "trading result."
        ),
        "assumptions": list(ASSUMPTIONS),
        "sources": {
            "gamma": f"{GAMMA}/markets/slug/{{slug}}",
            "trades": "https://data-api.polymarket.com/v2/trades",
            "prices": "https://data-api.polymarket.com/v2/prices-history",
            "resolution": "https://data-api.polymarket.com/v2/resolutions",
            "order_book": "not available historically; live GET /book returned 404 after close",
        },
        "window": {
            "start": datetime.fromtimestamp(start_s, UTC).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
            "end": datetime.fromtimestamp(end_s, UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "timezone": "Asia/Bangkok",
            "local_date": "2026-11-07",
        },
        "markets": markets,
    }


def save_snapshot(path: Path, snapshot: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def build_replay_events(snapshot: dict) -> list[dict]:
    """Turn one labeled snapshot into engine events, in time order."""
    load_historical_snapshot_dict(snapshot)
    events: list[dict] = [
        {
            "type": "feed",
            "status": "up",
            "ts": _ms(snapshot["markets"][0]["window_start"]),
            "event_id": "feed-up",
        }
    ]
    for market in snapshot["markets"]:
        events.extend(_market_events(market))
    events.sort(key=lambda item: int(item["ts"]))
    return events


def load_historical_snapshot_dict(snapshot: dict) -> None:
    if str(snapshot.get("label") or "").casefold().find("reconstructed") < 0:
        raise ConfigError("snapshot label must say reconstructed")
    if snapshot.get("synthetic_order_books") is not True:
        raise ConfigError("snapshot must mark synthetic_order_books true")
    if not isinstance(snapshot.get("markets"), list) or not snapshot["markets"]:
        raise ConfigError("snapshot has no markets")


def run_historical(config: Config, snapshot: dict, out_dir: Path) -> dict:
    events = build_replay_events(snapshot)
    engine = open_engine(config, out_dir)
    engine.mode = "historical-replay"
    engine.persist_each_event = False
    minimum = worst_case_equity(engine.state)
    peak_fees: dict[str, Decimal] = {}
    risk_hits: set[str] = set()
    for event in events:
        rows = engine.handle_event(event)
        worst = worst_case_equity(engine.state)
        if worst < minimum:
            minimum = worst
        _track_fees(engine, peak_fees)
        for row in rows:
            if row.get("kind") == "risk_freeze" or row.get("reason") in {
                "risk_floor",
                "exposure_frozen",
                "not_risk_reducing",
                "loss_floor",
            }:
                risk_hits.add(str(row.get("market_id") or ""))
            row_worst = D(row["worst_case_equity"])
            if row_worst < minimum:
                minimum = row_worst
    engine.flush()
    report = _report(engine, snapshot, minimum, peak_fees, risk_hits)
    notes = [
        "Historical replay on reconstructed books. Not a live result and not evidence of profitability.",
        "Order books were not observed. See snapshot assumptions.",
        f"Worst-case equity minimum during the run: {money(minimum)}",
    ]
    summary = engine.summary(notes=notes)
    summary["mode"] = "historical-replay"
    summary["historical"] = {
        "net_pnl_usdc": report["net_pnl_usdc"],
        "net_pnl_pct_of_initial": report["net_pnl_pct_of_initial"],
        "worst_case_equity_minimum": report["worst_case_equity_minimum"],
        "markets_declined": report["markets_declined"],
        "markets": report["market_count"],
    }
    write_summary(out_dir, summary)
    _write_report(out_dir, report)
    summary["results_json"] = str(out_dir / "historical_results.json")
    summary["results_csv"] = str(out_dir / "historical_results.csv")
    return summary


def reconstruct_top(
    last_buy: Optional[tuple[Decimal, Decimal]],
    last_sell: Optional[tuple[Decimal, Decimal]],
    tick: Decimal,
    history_price: Optional[Decimal],
) -> Optional[dict]:
    """Return a reconstructed top of book, or None when it is not usable."""
    if last_buy is None or last_sell is None:
        return None
    ask = _ceil_tick(last_buy[0], tick)
    observed_bid = _floor_tick(last_sell[0], tick)
    if ask <= 0 or ask >= 1 or observed_bid <= 0 or observed_bid >= 1:
        return None
    if observed_bid >= ask:
        return None
    if history_price is not None and (
        history_price < observed_bid or history_price > ask
    ):
        return None
    widened = False
    bid = observed_bid
    if ask - observed_bid < MIN_JOIN_SPREAD:
        bid = ask - MIN_JOIN_SPREAD
        widened = True
        if bid <= 0 or ask - bid > Decimal("0.10"):
            return None
    return {
        "bid": bid,
        "ask": ask,
        "bid_size": QUEUE_SHARES,
        "ask_size": QUEUE_SHARES,
        "widened": widened,
        "observed_bid": observed_bid,
    }


def _fetch_one(
    reader: PublicReadClient, first_window: int, day_start: int, day_end: int
) -> dict:
    slice_end = min(first_window + (day_end - day_start) // MARKET_COUNT, day_end)
    window = first_window
    last_error: Optional[Exception] = None
    while window + 300 <= slice_end:
        slug = f"btc-updown-5m-{window}"
        try:
            payload = reader.discover_btc_updown(window, 1, [slug])
        except PublicDataError as exc:
            last_error = exc
            window += 300
            continue
        if not payload:
            window += 300
            continue
        gamma = _trim_gamma(payload[0])
        condition = str(gamma["conditionId"])
        tokens = _token_pair(gamma)
        if tokens is None:
            window += 300
            continue
        start = window
        end = window + 300
        prints, seeds = _collect_prints(reader, condition, tokens, start, end)
        history = {
            "up": _collect_history(reader, tokens[0], start, end),
            "down": _collect_history(reader, tokens[1], start, end),
        }
        resolution = _trim_resolution(reader.fetch_resolution(condition))
        opened = gamma_market_event(gamma, market_id=slug, synthetic=False)
        if (
            opened is None
            or data_api_resolution_event(
                {
                    "market_id": slug,
                    "condition_id": opened["condition_id"],
                    "up_index": opened["up_index"],
                    "down_index": opened["down_index"],
                },
                resolution,
            )
            is None
        ):
            window += 300
            continue
        return {
            "slug": slug,
            "window_start": start,
            "window_end": end,
            "window_start_bkk": _bkk(start),
            "window_end_bkk": _bkk(end),
            "gamma": gamma,
            "prints": prints,
            "seed_prints": seeds,
            "price_history": history,
            "resolution": resolution,
            "print_count": len(prints),
            "seed_count": len(seeds),
        }
    raise PublicDataError(
        f"no BTC 5m market found from {first_window}: {last_error or 'not found'}"
    )


def _trim_gamma(payload: dict) -> dict:
    keep = (
        "id",
        "slug",
        "question",
        "conditionId",
        "outcomes",
        "clobTokenIds",
        "version",
        "endDate",
        "eventStartTime",
        "closed",
        "acceptingOrders",
        "orderPriceMinTickSize",
        "orderMinSize",
        "umaResolutionStatus",
    )
    return {key: payload.get(key) for key in keep}


def _token_pair(gamma: dict) -> Optional[tuple[str, str]]:
    event = gamma_market_event(gamma, synthetic=False)
    if event is None:
        return None
    return event["up_token_id"], event["down_token_id"]


def _collect_prints(
    reader: PublicReadClient,
    condition: str,
    tokens: tuple[str, str],
    start: int,
    end: int,
) -> tuple[list[dict], list[dict]]:
    up, down = tokens
    cursor = None
    pages = 0
    raw_rows: list[dict] = []
    while pages < 40:
        page = reader.fetch_trades_page(condition, limit=500, cursor=cursor)
        rows = page.get("data") if isinstance(page, dict) else None
        if not isinstance(rows, list):
            raise PublicDataError("trade page has no data list")
        raw_rows.extend(row for row in rows if isinstance(row, dict))
        pagination = page.get("pagination") if isinstance(page, dict) else {}
        if not isinstance(pagination, dict) or not pagination.get("has_more"):
            break
        cursor = pagination.get("next_cursor")
        if not cursor:
            break
        pages += 1
    prints = []
    seeds: dict[tuple[str, str], dict] = {}
    for index, row in enumerate(raw_rows):
        token = str(row.get("token_id") or "")
        if token == up:
            outcome = "up"
        elif token == down:
            outcome = "down"
        else:
            continue
        side = str(row.get("side") or "").upper()
        if side not in {"BUY", "SELL"}:
            continue
        try:
            ts = int(row.get("timestamp"))
            price = format(D(row.get("price")), "f")
            size = format(D(row.get("size")), "f")
        except (TypeError, ValueError, ArithmeticError):
            continue
        item = {
            "ts": ts,
            "side": side,
            "outcome": outcome,
            "price": price,
            "size": size,
            "tx": str(row.get("transaction_hash") or ""),
            "id": "|".join(
                [
                    str(row.get("transaction_hash") or ""),
                    outcome,
                    side,
                    price,
                    size,
                    str(ts),
                    str(index),
                ]
            ),
        }
        if start <= ts < end + 180:
            prints.append(item)
        elif ts < start:
            key = (outcome, side)
            current = seeds.get(key)
            if current is None or item["ts"] >= current["ts"]:
                seeds[key] = item
    prints.sort(key=lambda item: (item["ts"], item["id"]))
    return prints, list(seeds.values())


def _collect_history(
    reader: PublicReadClient, token: str, start: int, end: int
) -> list[dict]:
    cursor = None
    points = []
    pages = 0
    while pages < 10:
        page = reader.fetch_price_history(
            token, start - 120, end + 180, bucket_seconds=60, cursor=cursor
        )
        rows = page.get("data") if isinstance(page, dict) else None
        if not isinstance(rows, list):
            break
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                ts = int(row.get("timestamp"))
                price = format(D(row.get("price")), "f")
                width = int(row.get("resolution_seconds") or 60)
            except (TypeError, ValueError, ArithmeticError):
                continue
            points.append(
                {"timestamp": ts, "price": price, "resolution_seconds": width}
            )
        pagination = page.get("pagination") if isinstance(page, dict) else {}
        if not isinstance(pagination, dict) or not pagination.get("has_more"):
            break
        cursor = pagination.get("next_cursor")
        if not cursor:
            break
        pages += 1
    points.sort(key=lambda item: item["timestamp"])
    return points


def _trim_resolution(payload: dict) -> dict:
    rows = payload.get("data") if isinstance(payload, dict) else None
    kept = []
    if isinstance(rows, list):
        for row in rows:
            if not isinstance(row, dict):
                continue
            kept.append(
                {
                    "condition_id": row.get("condition_id"),
                    "status": row.get("status"),
                    "payouts": row.get("payouts"),
                    "resolved_at": row.get("resolved_at"),
                    "market_type": row.get("market_type"),
                }
            )
    return {"data": kept}


def _market_events(market: dict) -> list[dict]:
    slug = str(market["slug"])
    gamma = dict(market["gamma"])
    gamma["closed"] = False
    gamma["acceptingOrders"] = True
    opened = gamma_market_event(gamma, market_id=slug, synthetic=False)
    if opened is None:
        raise ConfigError(f"{slug} is not an Up/Down market in the snapshot")
    start_ms = int(market["window_start"]) * 1000
    end_ms = int(opened["end_ts"])
    opened["ts"] = start_ms
    opened["event_id"] = f"open:{slug}"
    events = [opened]
    tick = D(opened["tick_size"])
    state = {
        "up": _SideBook(market.get("price_history", {}).get("up") or []),
        "down": _SideBook(market.get("price_history", {}).get("down") or []),
    }
    for seed in market.get("seed_prints") or []:
        state[seed["outcome"]].apply(seed)
    prints = list(market.get("prints") or [])
    cursor = start_ms
    grouped: dict[int, list[dict]] = {}
    for item in prints:
        grouped.setdefault(int(item["ts"]) * 1000, []).append(item)
    stamps = sorted(set(grouped) | set(range(start_ms, end_ms + 1, 2000)))
    sequence = 0
    for stamp in stamps:
        if stamp < cursor:
            continue
        batch = grouped.get(stamp) or []
        for item in sorted(batch, key=lambda row: row["id"]):
            state[item["outcome"]].apply(item)
            events.append(
                {
                    "type": "trade",
                    "market_id": slug,
                    "outcome": item["outcome"],
                    "ts": stamp,
                    "trade_ts": stamp,
                    "price": item["price"],
                    "size": item["size"],
                    "taker_side": item["side"],
                    "trade_id": item["id"],
                    "event_id": f"trade:{item['id']}",
                }
            )
        sequence += 1
        events.extend(_books(slug, opened, state, tick, stamp, f"t{sequence}"))
        events.append(
            {
                "type": "heartbeat",
                "ts": stamp,
                "market_id": slug,
                "event_id": f"hb:{slug}:{stamp}",
            }
        )
        if batch:
            live_at = stamp + 250
            events.append(
                {
                    "type": "heartbeat",
                    "ts": live_at,
                    "market_id": slug,
                    "event_id": f"hb:{slug}:live:{stamp}",
                }
            )
        cursor = stamp
    # Let the stop-window cancels become confirmed before settlement.
    for extra in (500, 1000, 1500):
        events.append(
            {
                "type": "heartbeat",
                "ts": end_ms + extra,
                "market_id": slug,
                "event_id": f"hb:{slug}:after:{extra}",
            }
        )
    view = {
        "market_id": slug,
        "condition_id": opened["condition_id"],
        "up_index": opened["up_index"],
        "down_index": opened["down_index"],
    }
    resolution = data_api_resolution_event(view, market.get("resolution") or {})
    if resolution is None:
        raise ConfigError(f"{slug} has no official binary resolution in the snapshot")
    resolution["ts"] = end_ms + 2000
    events.append(resolution)
    events.append(
        {
            "type": "heartbeat",
            "ts": end_ms + 2500,
            "market_id": slug,
            "event_id": f"hb:{slug}:redeem",
        }
    )
    return events


class _SideBook:
    def __init__(self, history: list[dict]):
        self.last_buy: Optional[tuple[Decimal, Decimal, int]] = None
        self.last_sell: Optional[tuple[Decimal, Decimal, int]] = None
        self.history = list(history)

    def apply(self, print_row: dict) -> None:
        price = D(print_row["price"])
        size = D(print_row["size"])
        ts = int(print_row["ts"])
        if print_row["side"] == "BUY":
            self.last_buy = (price, size, ts)
        else:
            self.last_sell = (price, size, ts)

    def history_at(self, ts_ms: int) -> Optional[Decimal]:
        now_s = ts_ms // 1000
        chosen = None
        for point in self.history:
            if int(point["timestamp"]) <= now_s:
                chosen = point
            else:
                break
        if chosen is None:
            return None
        width = int(chosen.get("resolution_seconds") or 60)
        if int(chosen["timestamp"]) + width < now_s:
            return None
        return D(chosen["price"])


def _books(slug, opened, state, tick, ts_ms: int, tag: str) -> list[dict]:
    events = []
    for outcome in ("up", "down"):
        side: _SideBook = state[outcome]
        top = reconstruct_top(
            None if side.last_buy is None else (side.last_buy[0], side.last_buy[1]),
            None if side.last_sell is None else (side.last_sell[0], side.last_sell[1]),
            tick,
            side.history_at(ts_ms),
        )
        token = opened["up_token_id"] if outcome == "up" else opened["down_token_id"]
        if top is None:
            bids: list = []
            asks: list = []
        else:
            bids = [(format(top["bid"], "f"), format(top["bid_size"], "f"))]
            asks = [(format(top["ask"], "f"), format(top["ask_size"], "f"))]
        events.append(
            {
                "type": "book",
                "market_id": slug,
                "outcome": outcome,
                "ts": ts_ms,
                "book_ts": ts_ms,
                "bids": bids,
                "asks": asks,
                "hash": f"reconstructed:{slug}:{outcome}:{tag}",
                "asset_id": token,
                "tick_size": opened["tick_size"],
                "event_id": f"book:{slug}:{outcome}:{tag}",
            }
        )
    return events


def _track_fees(engine: Engine, peak: dict[str, Decimal]) -> None:
    totals: dict[str, Decimal] = {}
    for order in engine.state.orders.values():
        if order.status not in WORKING_ORDER_STATUSES:
            continue
        fee = conservative_fee(order.remaining, order.price, engine.state.risk_fee_rate)
        totals[order.market_id] = totals.get(order.market_id, Decimal("0")) + fee
    for market_id, fee in totals.items():
        if fee > peak.get(market_id, Decimal("0")):
            peak[market_id] = fee


def _report(
    engine: Engine, snapshot: dict, minimum: Decimal, peak_fees, risk_hits
) -> dict:
    rows = []
    declined = 0
    for market_snapshot in snapshot["markets"]:
        slug = market_snapshot["slug"]
        market = engine.state.markets.get(slug)
        stats = _market_stats(engine, slug)
        if stats["splits"] == "0":
            declined += 1
            stats["declined_to_trade"] = True
        else:
            stats["declined_to_trade"] = False
        stats["fees_reserved_peak"] = money(peak_fees.get(slug, Decimal("0")))
        stats["risk_cap_triggered"] = slug in risk_hits
        stats["slug"] = slug
        stats["window_start_utc"] = datetime.fromtimestamp(
            int(market_snapshot["window_start"]), UTC
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        stats["window_end_utc"] = datetime.fromtimestamp(
            int(market_snapshot["window_end"]), UTC
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        stats["window_start_bkk"] = market_snapshot.get("window_start_bkk")
        stats["window_end_bkk"] = market_snapshot.get("window_end_bkk")
        stats["question"] = (
            market.question if market else market_snapshot["gamma"].get("question")
        )
        stats["status"] = market.status if market else "not_opened"
        stats["winner"] = market.winner if market else None
        stats["print_count"] = market_snapshot.get("print_count")
        rows.append(stats)
    initial = engine.state.initial_capital
    net = engine.state.realized_pnl
    pct = (net / initial) * Decimal("100") if initial else Decimal("0")
    return {
        "label": LABEL,
        "counterfactual": True,
        "not_evidence_of_live_profitability": True,
        "disclaimer": (
            "Simulated counterfactual on reconstructed books from public prints. "
            "Not evidence that these orders would have filled, and not evidence "
            "of live profitability. The 3% figure is the model's terminal-loss "
            "budget, not a live cap."
        ),
        "initial_capital": money(initial),
        "loss_floor": money(engine.state.loss_floor),
        "loss_budget_usdc": money(initial - engine.state.loss_floor),
        "per_market_budget": money(engine.state.per_market_budget),
        "net_pnl_usdc": money(net),
        "net_pnl_pct_of_initial": format(qfloor(pct, Decimal("0.0001")), "f"),
        "cash": money(engine.state.cash),
        "realized_pnl": money(net),
        "rebate_estimate_unguaranteed": money(engine.state.rebate_estimate),
        "rebate_included_in_pnl": False,
        "rebate_included_in_floor": False,
        "worst_case_equity_minimum": money(minimum),
        "worst_terminal_equity": money(worst_case_equity(engine.state)),
        "markets_declined": declined,
        "market_count": len(rows),
        "risk_cap_triggered": bool(risk_hits),
        "scaled_limits": {
            "initial_capital": "200",
            "max_loss_fraction": "0.03",
            "loss_budget_usdc": "6",
            "loss_floor": "194",
            "per_market_budget": "8",
            "split_clip": "5",
            "min_pair_inventory": "5",
            "unchanged": [
                "clip_size",
                "min_order_size",
                "min_edge",
                "fill_mode",
                "latencies",
                "fee rates",
                "reserves",
                "max_open_markets",
                "max_spread",
            ],
        },
        "markets": rows,
    }


def _market_stats(engine: Engine, slug: str) -> dict:
    market = engine.state.markets.get(slug)
    splits = Decimal("0")
    merges = Decimal("0")
    fills = {"up": Decimal("0"), "down": Decimal("0")}
    redeem = Decimal("0")
    quotes = 0
    # Recover activity from the in-memory orders plus the market ledger fields.
    # Fills are the difference between original size and remaining for this market.
    if market is not None:
        splits = market.gross_split
    for order in engine.state.orders.values():
        if order.market_id != slug:
            continue
        if order.status == "filled" or order.remaining < order.size:
            filled = order.size - order.remaining
            fills[order.outcome] += filled
        if order.submit_ts:
            quotes += 1
    realized = (
        market.net_cash_flow
        if market is not None and market.realized_recorded
        else Decimal("0")
    )
    rebate = Decimal("0")
    # Rebate is global. Attribute the estimate from this market's fills only.
    for order in engine.state.orders.values():
        if order.market_id != slug:
            continue
        filled = order.size - order.remaining
        if filled <= 0 or not (Decimal("0") < order.price < Decimal("1")):
            continue
        raw = (
            filled
            * engine.state.published_taker_fee_rate
            * order.price
            * (Decimal("1") - order.price)
            * engine.state.rebate_share
        )
        from paper_split_maker.decimal_utils import qdown

        rebate += qdown(raw, Decimal("0.00000001"))
    merge_amount = Decimal("0")
    if market is not None:
        # Cash returned by merges is not stored separately. Inventory left after
        # sells was merged or redeemed. Report merge as paired tokens removed
        # before redeem: gross split minus the larger of the two sold amounts,
        # when the market is settled and both sides were not fully sold.
        sold_up = fills["up"]
        sold_down = fills["down"]
        if market.status == "redeemed":
            leftover_up = splits - sold_up
            leftover_down = splits - sold_down
            merge_amount = leftover_up if leftover_up < leftover_down else leftover_down
            if merge_amount < 0:
                merge_amount = Decimal("0")
            winner_left = leftover_up if market.winner == "up" else leftover_down
            redeem = winner_left - merge_amount
            if redeem < 0:
                redeem = Decimal("0")
            merges = merge_amount
    return {
        "splits": money(splits),
        "fills_up": money(fills["up"]),
        "fills_down": money(fills["down"]),
        "merges": money(merges),
        "redeem": money(redeem),
        "realized_pnl": money(realized),
        "quotes_submitted": quotes,
        "rebate_estimate_unguaranteed": money(rebate),
    }


def _write_report(out_dir: Path, report: dict) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "historical_results.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    fields = [
        "slug",
        "window_start_utc",
        "window_end_utc",
        "window_start_bkk",
        "window_end_bkk",
        "question",
        "status",
        "winner",
        "splits",
        "fills_up",
        "fills_down",
        "merges",
        "redeem",
        "realized_pnl",
        "fees_reserved_peak",
        "rebate_estimate_unguaranteed",
        "risk_cap_triggered",
        "declined_to_trade",
        "quotes_submitted",
        "print_count",
    ]
    with (out_dir / "historical_results.csv").open(
        "w", encoding="utf-8", newline=""
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in report["markets"]:
            writer.writerow(row)
        writer.writerow(
            {
                "slug": "TOTAL",
                "realized_pnl": report["net_pnl_usdc"],
                "rebate_estimate_unguaranteed": report["rebate_estimate_unguaranteed"],
                "declined_to_trade": report["markets_declined"],
                "risk_cap_triggered": report["risk_cap_triggered"],
                "fees_reserved_peak": "",
                "question": report["disclaimer"],
            }
        )


def _ms(start_s: int) -> int:
    return int(start_s) * 1000


def _bkk(epoch_s: int) -> str:
    return datetime.fromtimestamp(epoch_s, BKK).strftime("%Y-%m-%d %H:%M %Z")


def _ceil_tick(price: Decimal, tick: Decimal) -> Decimal:
    from paper_split_maker.decimal_utils import ceil_tick

    return ceil_tick(price, tick)


def _floor_tick(price: Decimal, tick: Decimal) -> Decimal:
    return qfloor(price, tick)


def fetch_window(reader: PublicReadClient, window: int) -> Optional[dict]:
    """One exact BTC 5-minute window. Missing or unresolved markets return None."""
    slug = f"btc-updown-5m-{window}"
    try:
        payload = reader.discover_btc_updown(window, 1, [slug])
    except PublicDataError:
        return None
    if not payload:
        return None
    gamma = _trim_gamma(payload[0])
    condition = str(gamma.get("conditionId") or "")
    tokens = _token_pair(gamma)
    if not condition or tokens is None:
        return None
    start = window
    end = window + 300
    try:
        prints, seeds = _collect_prints(reader, condition, tokens, start, end)
        history = {
            "up": _collect_history(reader, tokens[0], start, end),
            "down": _collect_history(reader, tokens[1], start, end),
        }
        resolution = _trim_resolution(reader.fetch_resolution(condition))
    except PublicDataError:
        return None
    opened = gamma_market_event(gamma, market_id=slug, synthetic=False)
    if opened is None:
        return None
    if (
        data_api_resolution_event(
            {
                "market_id": slug,
                "condition_id": opened["condition_id"],
                "up_index": opened["up_index"],
                "down_index": opened["down_index"],
            },
            resolution,
        )
        is None
    ):
        return None
    return {
        "slug": slug,
        "window_start": start,
        "window_end": end,
        "window_start_bkk": _bkk(start),
        "window_end_bkk": _bkk(end),
        "gamma": gamma,
        "prints": prints,
        "seed_prints": seeds,
        "price_history": history,
        "resolution": resolution,
        "print_count": len(prints),
        "seed_count": len(seeds),
    }


def session_distribution(pnls: list[Decimal]) -> dict:
    if not pnls:
        return {
            "sessions": 0,
            "best": "0",
            "worst": "0",
            "median": "0",
            "losing_sessions": 0,
            "max_session_loss": "0",
        }
    ordered = sorted(pnls)
    count = len(ordered)
    if count % 2:
        median = ordered[count // 2]
    else:
        median = (ordered[count // 2 - 1] + ordered[count // 2]) / 2
    worst = ordered[0]
    return {
        "sessions": count,
        "best": money(ordered[-1]),
        "worst": money(worst),
        "median": money(median),
        "losing_sessions": sum(1 for item in pnls if item < 0),
        "max_session_loss": money(-worst if worst < 0 else Decimal("0")),
    }


def run_compare(
    old_config: Config,
    new_config: Config,
    start_s: int,
    end_s: int,
    out_dir: Path,
    client: Optional[PublicReadClient] = None,
) -> dict:
    """Replay every available BTC 5m window through the old and new paper logic.

    Markets are fetched one at a time and not retained. The result is a
    simulation on reconstructed books, not a live result.
    """
    reader = client or PublicReadClient(UrllibTransport(timeout=30))
    out_dir.mkdir(parents=True, exist_ok=True)
    old = Engine.create(
        old_config, out_dir / "old" / "state.json", out_dir / "old" / "logs"
    )
    new = Engine.create(
        new_config, out_dir / "new" / "state.json", out_dir / "new" / "logs"
    )
    old.mode = "historical-replay"
    new.mode = "historical-replay"
    # The day is hundreds of thousands of events. Persisting the ledger on
    # each one does not change the decisions and dominates the runtime.
    for engine in (old, new):
        engine.persist_each_event = False
        engine.writer = None
    rows = []
    windows = list(range((start_s // 300) * 300, end_s, 300))
    for window, market in _iter_windows(
        windows, reader if client is not None else None
    ):
        print(f"compare btc-updown-5m-{window}", flush=True)
        if market is None:
            print("  skipped", flush=True)
            continue
        for event in _market_events(market):
            old.handle_event(event)
            new.handle_event(event)
        old_stats = _market_stats(old, market["slug"])
        new_stats = _market_stats(new, market["slug"])
        rows.append(
            {
                "slug": market["slug"],
                "window_start_bkk": market["window_start_bkk"],
                "print_count": market["print_count"],
                "old_pnl": old_stats["realized_pnl"],
                "new_pnl": new_stats["realized_pnl"],
                "old_splits": old_stats["splits"],
                "new_splits": new_stats["splits"],
                "old_fills_up": old_stats["fills_up"],
                "old_fills_down": old_stats["fills_down"],
                "new_fills_up": new_stats["fills_up"],
                "new_fills_down": new_stats["fills_down"],
                "old_quotes": old_stats["quotes_submitted"],
                "new_quotes": new_stats["quotes_submitted"],
                "old_rebate_estimate": old_stats["rebate_estimate_unguaranteed"],
                "new_rebate_estimate": new_stats["rebate_estimate_unguaranteed"],
                "old_maker_fills": _maker_fills(old, market["slug"]),
                "new_maker_fills": _maker_fills(new, market["slug"]),
            }
        )
        _write_compare(out_dir, _compare_report(old, new, rows))
        _drop_settled_orders(old)
        _drop_settled_orders(new)
        print(
            f"  old {old_stats['realized_pnl']} new {new_stats['realized_pnl']}",
            flush=True,
        )
    report = _compare_report(old, new, rows)
    _write_compare(out_dir, report)
    return report


def _iter_windows(windows: list[int], reader: Optional[PublicReadClient]):
    """Yield windows in order. Live fetches overlap; an injected reader stays serial."""
    from concurrent.futures import Future, ThreadPoolExecutor

    if reader is not None:
        for window in windows:
            yield window, fetch_window(reader, window)
        return

    def job(window: int) -> Optional[dict]:
        local = PublicReadClient(UrllibTransport(timeout=30))
        try:
            return fetch_window(local, window)
        except Exception as exc:
            print(f"  fetch error {window}: {exc}", flush=True)
            return None

    ahead = 4
    with ThreadPoolExecutor(max_workers=ahead) as pool:
        pending: dict[Future, int] = {}
        index = 0
        while index < min(ahead, len(windows)):
            pending[pool.submit(job, windows[index])] = windows[index]
            index += 1
        for window in windows:
            future = next(item for item, stamp in pending.items() if stamp == window)
            market = future.result()
            del pending[future]
            if index < len(windows):
                pending[pool.submit(job, windows[index])] = windows[index]
                index += 1
            yield window, market


def _drop_settled_orders(engine: Engine) -> None:
    settled = {
        market.market_id
        for market in engine.state.markets.values()
        if market.status == "redeemed"
    }
    for order_id, order in list(engine.state.orders.items()):
        if order.market_id in settled and order.status not in WORKING_ORDER_STATUSES:
            del engine.state.orders[order_id]


def _compare_report(old: Engine, new: Engine, rows: list[dict]) -> dict:
    def pnl_of(key: str) -> list[Decimal]:
        return [D(row[key]) for row in rows]

    def fills(prefix: str) -> Decimal:
        total = Decimal("0")
        for row in rows:
            total += D(row[f"{prefix}_fills_up"]) + D(row[f"{prefix}_fills_down"])
        return total

    def splits(prefix: str) -> Decimal:
        return sum((D(row[f"{prefix}_splits"]) for row in rows), Decimal("0"))

    old_fills = fills("old")
    new_fills = fills("new")
    old_splits = splits("old")
    new_splits = splits("new")
    return {
        "label": "reconstructed-from-public-prints",
        "simulation": True,
        "not_evidence_of_live_results": True,
        "disclaimer": (
            "Simulation on reconstructed books from public prints for every "
            "available BTC 5-minute window in the day. Not evidence of live fills, "
            "rebates, or a guaranteed loss bound."
        ),
        "sessions": len(rows),
        "old": {
            "net_pnl_usdc": money(old.state.realized_pnl),
            "rebate_estimate_unguaranteed": money(old.state.rebate_estimate),
            "fill_shares": money(old_fills),
            "split_shares": money(old_splits),
            "fill_rate": _rate(old_fills, old_splits),
            "maker_fill_orders": sum(
                int(row.get("old_maker_fills") or 0) for row in rows
            ),
            "distribution": session_distribution(pnl_of("old_pnl")),
        },
        "new": {
            "net_pnl_usdc": money(new.state.realized_pnl),
            "rebate_estimate_unguaranteed": money(new.state.rebate_estimate),
            "fill_shares": money(new_fills),
            "split_shares": money(new_splits),
            "fill_rate": _rate(new_fills, new_splits),
            "maker_fill_orders": sum(
                int(row.get("new_maker_fills") or 0) for row in rows
            ),
            "logic": "pair floor 0.01, four slices, requote at one tick, session cap 1",
            "distribution": session_distribution(pnl_of("new_pnl")),
        },
        "markets": rows,
    }


def _rate(fills: Decimal, splits: Decimal) -> str:
    if splits <= 0:
        return "0"
    return money(qfloor(fills / (splits * 2), Decimal("0.0001")))


def _maker_fills(engine: Engine, slug: str) -> int:
    return sum(
        1
        for order in engine.state.orders.values()
        if order.market_id == slug and order.remaining < order.size
    )


def _write_compare(out_dir: Path, report: dict) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "compare.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    path = out_dir / "compare.csv"
    fields = [
        "slug",
        "window_start_bkk",
        "print_count",
        "old_pnl",
        "new_pnl",
        "old_splits",
        "new_splits",
        "old_fills_up",
        "old_fills_down",
        "new_fills_up",
        "new_fills_down",
        "old_quotes",
        "new_quotes",
        "old_rebate_estimate",
        "new_rebate_estimate",
        "old_maker_fills",
        "new_maker_fills",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(report["markets"])
