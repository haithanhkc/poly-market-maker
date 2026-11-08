"""Bounded live session. Paper mode never imports this.

The clock stops at duration_seconds. Shutdown calls cancel-all. A kill-switch
file does the same. Inventory is quoted only after a split confirmation.
"""

from __future__ import annotations

import json
import queue
import threading
import time
from pathlib import Path

from paper_split_maker.chain_data import (
    encode_split,
    position_target,
    shares_to_base_units,
)
from paper_split_maker.config import Config
from paper_split_maker.credentials import Credentials
from paper_split_maker.feeds import describe_user_subscribe
from paper_split_maker.logs import write_summary
from paper_split_maker.session import open_engine


def quoting_allowed(confirmed: bool) -> bool:
    return confirmed


def run_confirmed_live(
    config: Config,
    creds: Credentials,
    out_dir: Path,
    *,
    duration_seconds: int,
    max_markets: int,
) -> dict:
    from paper_split_maker.live_clob import (
        build_client,
        cancel_all,
        heartbeat,
        post_only_sells,
    )

    if duration_seconds <= 0:
        raise RuntimeError("duration_seconds must be positive")
    client = build_client(creds)
    engine = open_engine(config, out_dir)
    engine.mode = "live"
    from paper_split_maker.public_client import PublicDataError, PublicReadClient

    reader = PublicReadClient()
    ws_queue: queue.Queue = queue.Queue()
    ws_stop = threading.Event()
    ws_assets: list[str] = []
    ws_thread = _start_market_ws(ws_assets, ws_queue, ws_stop)
    notes = [
        "Live session on the configured funder. Orders are post-only sells.",
        describe_user_subscribe(creds.funder, max_markets),
        "Unconfirmed splits are not quoted.",
        "Shutdown sends cancel-all.",
        "Market websocket is on."
        if ws_thread is not None
        else "Market websocket package is missing; books come from REST.",
    ]
    print(describe_user_subscribe(creds.funder, max_markets))
    heartbeat_id = None
    next_heartbeat = 0.0
    deadline = time.time() + duration_seconds
    try:
        while time.time() < deadline:
            if config.kill_switch_file and Path(config.kill_switch_file).exists():
                notes.append("kill switch file seen")
                break
            now = time.time()
            _remember_assets(engine, ws_assets)
            drain_market_ws(engine, ws_queue, int(now * 1000))
            _poll_public(engine, reader, config, int(now * 1000), max_markets, notes)
            _confirm_splits(engine, creds, int(now * 1000), notes)
            _consider_pre_split(engine, reader, config, creds, int(now * 1000), notes)
            _ingest_matched(engine, client, creds, notes)
            if now >= next_heartbeat:
                heartbeat_id = heartbeat(client, heartbeat_id)
                next_heartbeat = now + 5
            _send_cancels(engine, client, creds, notes)
            remote = client.get_open_orders()
            remote_ids = [
                str(item.get("id")) for item in remote if isinstance(item, dict)
            ]
            engine.handle_event(
                {
                    "type": "reconcile",
                    "open_order_ids": remote_ids,
                    "ts": int(now * 1000),
                    "event_id": f"reconcile-{int(now * 1000)}",
                }
            )
            if engine.state.mismatch_stop or engine.state.killed:
                notes.append("stopped on reconcile or kill")
                break
            _send_new_quotes(engine, client, post_only_sells, creds, notes)
            _flush_chain(engine, creds, notes)
            time.sleep(min(0.2, max(0.0, deadline - time.time())))
    finally:
        ws_stop.set()
        try:
            cancel_all(client)
            notes.append("cancel-all sent")
        except Exception as exc:
            notes.append(creds.redact(f"cancel-all failed: {exc}"))
    summary = engine.summary(notes=notes)
    summary["mode"] = "live"
    summary["funder"] = creds.funder
    summary["signature_type"] = creds.signature_type
    write_summary(out_dir, summary)
    return summary


def _send_new_quotes(engine, client, post_only_sells, creds, notes: list[str]) -> None:
    pending = []
    for order in engine.state.orders.values():
        if order.status != "queued" or getattr(order, "sent", False):
            continue
        market = engine.state.markets[order.market_id]
        token = market.up_token_id if order.outcome == "up" else market.down_token_id
        pending.append(
            {
                "token_id": token,
                "price": str(order.price),
                "size": str(order.remaining),
                "tick_size": str(market.tick_size),
                "order": order,
            }
        )
    if not pending:
        return
    started = time.time()
    try:
        results = post_only_sells(
            client,
            [
                {
                    "token_id": item["token_id"],
                    "price": item["price"],
                    "size": item["size"],
                    "tick_size": item["tick_size"],
                }
                for item in pending
            ],
        )
    except Exception as exc:
        notes.append(creds.redact(f"post failed: {exc}"))
        engine.state.mismatch_stop = True
        return
    if not isinstance(results, list):
        results = [results]
    ack_ms = int((time.time() - started) * 1000)
    for item, result in zip(pending, results):
        ident = ""
        error = ""
        if isinstance(result, dict):
            ident = str(result.get("orderID") or result.get("id") or "")
            error = str(result.get("errorMsg") or result.get("error") or "")
            if result.get("success") is False and not error:
                error = "order not accepted"
        item["order"].sent = True
        if error or not ident:
            # The exchange did not take this order. It must not count as resting.
            engine.reject_live_order(item["order"], error or "no order id")
            notes.append(creds.redact(f"order rejected: {error or 'no order id'}"))
            continue
        item["order"].exchange_id = ident
        market = engine.state.markets[item["order"].market_id]
        engine.note_order_posted(
            market,
            exchange_id=ident,
            outcome=item["order"].outcome,
            price=item["order"].price,
            size=item["order"].remaining,
            internal_id=item["order"].order_id,
        )
        posted = (
            f"order_posted side={item['order'].outcome} "
            f"price={item['order'].price} size={item['order'].remaining} "
            f"order_id={ident}"
        )
        print(posted, flush=True)
        notes.append(posted)
        engine._emit(
            kind="latency",
            market=market,
            order=item["order"],
            reason="order_ack",
            detail=str(ack_ms),
        )


def _send_cancels(engine, client, creds, notes: list[str]) -> None:
    ids = []
    for order in engine.state.orders.values():
        if (
            order.status == "cancel_requested"
            and order.exchange_id
            and not getattr(order, "cancel_sent", False)
        ):
            ids.append(order.exchange_id)
            order.cancel_sent = True
    stray = getattr(engine, "stray_remote_cancels", None)
    if stray:
        ids.extend(stray)
        stray.clear()
    if not ids:
        return
    started = time.time()
    try:
        client.cancel_orders(ids)
    except Exception as exc:
        notes.append(creds.redact(f"cancel failed: {exc}"))
        engine.state.mismatch_stop = True
        return
    ack_ms = int((time.time() - started) * 1000)
    engine._emit(kind="latency", reason="cancel_ack", detail=str(ack_ms))


def _poll_public(
    engine, reader, config, stamp: int, max_markets: int, notes: list[str]
) -> None:
    """Discover the next session even while another market is still tracked.

    A finished market is not polled: a closed token's book is a 404, and one
    failure used to mark the whole feed down so later sessions never quoted.
    """
    from paper_split_maker.normalize import clob_book_event
    from paper_split_maker.public_client import PublicDataError

    now_s = stamp // 1000
    window = (now_s // 300) * 300
    next_open = window + 300
    if stamp >= next_open * 1000 - config.pre_split_lead_ms:
        # Free the ending session before discovery, or the new market is
        # refused by the loss floor while the old sell is still working.
        _flatten_for_rollover(
            engine, next_open, stamp, lead_ms=config.pre_split_lead_ms
        )
    slugs = _candidate_slugs(config, now_s, max_markets)
    if slugs:
        try:
            payloads = reader.discover_btc_updown(now_s, len(slugs), slugs)
        except PublicDataError as exc:
            notes.append(f"discovery failed: {exc}")
            payloads = []
        for payload in payloads:
            _load_market(
                engine,
                payload,
                stamp,
                notes,
                allow_started=False,
                lead_ms=config.pre_split_lead_ms,
            )
    open_markets = [
        market
        for market in engine.state.markets.values()
        if market.status == "open"
    ]
    failed = 0
    for market in open_markets:
        market_ok = True
        try:
            for outcome, token in (
                ("up", market.up_token_id),
                ("down", market.down_token_id),
            ):
                book = reader.fetch_book(token)
                event = clob_book_event(
                    market.market_id,
                    outcome,
                    book,
                    event_id=f"book:{market.market_id}:{outcome}:{book.get('hash')}:{stamp}",
                )
                if event is None:
                    market_ok = False
                    continue
                event["book_ts"] = event["ts"]
                event["ts"] = stamp
                engine.handle_event(event)
        except PublicDataError as exc:
            market_ok = False
            notes.append(f"book poll failed for {market.market_id}: {exc}")
        if not market_ok:
            failed += 1
    _poll_resolutions(engine, reader, stamp, notes)
    feed_down = bool(open_markets) and failed == len(open_markets)
    engine.handle_event(
        {
            "type": "feed",
            "status": "down" if feed_down else "up",
            "ts": stamp,
            "event_id": f"feed-{'down' if feed_down else 'up'}-{stamp}",
        }
    )
    engine.handle_event({"type": "heartbeat", "ts": stamp, "event_id": f"hb-{stamp}"})
    for market_id in engine.retire_finished():
        notes.append(f"retired {market_id}")
        _forget_market(engine, market_id)


def _ingest_matched(engine, client, creds, notes: list[str]) -> None:
    from paper_split_maker.feeds import user_fill_deltas

    matched: dict = {}
    for order in list(engine.state.orders.values()):
        if not order.exchange_id or order.status not in {"live", "cancel_requested"}:
            continue
        try:
            payload = client.get_order(order.exchange_id)
        except Exception as exc:
            notes.append(creds.redact(f"order read failed: {exc}"))
            continue
        if not isinstance(payload, dict):
            continue
        payload = dict(payload)
        payload.setdefault("event_type", "order")
        payload.setdefault("id", order.exchange_id)
        for fill in user_fill_deltas(payload, matched):
            engine.handle_event(
                {
                    "type": "user_fill",
                    "order_id": order.order_id,
                    "trade_id": fill["trade_id"],
                    "size": fill["size"],
                    "ts": engine.state.now_ms,
                    "event_id": f"user:{fill['trade_id']}",
                }
            )


def _confirm_splits(engine, creds, stamp: int, notes: list[str]) -> None:
    txs = getattr(engine, "split_tx", {})
    if not txs:
        return
    from paper_split_maker.chain_send import eoa_confirmed, split_tokens_present

    for slug, ident in list(txs.items()):
        market = engine.state.markets.get(slug) or _market_by_slug(engine, slug)
        if market is None or market.pending_split <= 0:
            txs.pop(slug, None)
            continue
        settled_ids = getattr(engine, "split_settled", set())
        settled = slug in settled_ids or market.market_id in settled_ids
        try:
            ok = settled or (creds.signature_type == 0 and eoa_confirmed(ident))
        except Exception as exc:
            notes.append(creds.redact(f"split receipt failed: {exc}"))
            ok = False
        if not ok:
            continue
        # A settled relayer call or a receipt is not enough: confirm only when
        # the funder really holds both outcome tokens on chain.
        try:
            ok = split_tokens_present(
                creds.funder,
                market.up_token_id,
                market.down_token_id,
                market.pending_split,
            )
        except Exception as exc:
            notes.append(creds.redact(f"split balance check failed: {exc}"))
            ok = False
        if not ok:
            continue
        amount = market.pending_split
        engine.handle_event(
            {
                "type": "split_confirmed",
                "market_id": slug,
                "amount": str(amount),
                "ts": stamp,
                "event_id": f"confirmed:{slug}:{ident}",
            }
        )
        # A confirmation that lands just after the open is still quotable.
        # Only a confirmation inside the stop window is too late to quote.
        stop_at = market.end_ts_ms - engine.config.stop_quoting_before_expiry_ms
        if stamp >= stop_at:
            market.session_stopped = True
            notes.append(
                f"{slug} split confirmed too late to quote; inventory is not quoted"
            )
        txs.pop(slug, None)


def _consider_pre_split(
    engine, reader, config, creds, stamp: int, notes: list[str]
) -> None:
    """Split the next 5-minute market before it opens. Quote only after confirmation.

    The split is sent only after the engine has loaded that market and will
    still quote it. The session that is about to end is flattened first so a
    bankroll equal to one split can fund the next one.
    """
    from paper_split_maker.live_session import should_pre_split
    from paper_split_maker.public_client import PublicDataError

    window = (stamp // 1000 // 300) * 300
    next_open = window + 300
    slug = f"btc-updown-5m-{next_open}"
    if stamp >= next_open * 1000 - config.pre_split_lead_ms:
        _flatten_for_rollover(engine, next_open, stamp, lead_ms=config.pre_split_lead_ms)
    existing = _market_by_slug(engine, slug)
    if existing is not None and existing.gross_split > 0:
        return
    pending = existing is not None and existing.pending_split > 0
    if not should_pre_split(
        stamp,
        next_open * 1000,
        config.pre_split_lead_ms,
        pending or slug in getattr(engine, "split_sent", set()),
        not engine.at_cap(),
    ):
        if pending and stamp >= next_open * 1000 + config.split_confirm_timeout_ms:
            engine.handle_event(
                {
                    "type": "split_abandoned",
                    "market_id": existing.market_id,
                    "amount": str(existing.pending_split),
                    "ts": stamp,
                    "event_id": f"abandon:{slug}",
                }
            )
            notes.append(
                f"{slug} split was not confirmed before quoting ended; not quoting it"
            )
        return
    try:
        found = reader.discover_btc_updown(next_open, 1, [slug])
    except PublicDataError as exc:
        notes.append(f"next market lookup failed: {exc}")
        return
    if not found:
        return
    payload = dict(found[0])
    payload.setdefault("slug", slug)
    if not _load_market(
        engine, payload, stamp, notes, allow_started=False, lead_ms=config.pre_split_lead_ms
    ):
        # The engine refused the market: never split on-chain for it.
        notes.append(f"{slug} not accepted by engine; no split")
        return
    market = _market_by_slug(engine, slug)
    if market is None or not _will_quote(engine, market, stamp):
        notes.append(f"{slug} not quotable; no split")
        return
    amount = _affordable_split(engine, market, config)
    if amount is None:
        notes.append(f"{slug} not split; cash is below the minimum order")
        return
    engine.handle_event(
        {
            "type": "split_pending",
            "market_id": market.market_id,
            "amount": str(amount),
            "ts": stamp,
            "event_id": f"pending:{market.market_id}",
        }
    )
    version = str(found[0].get("version") or "v1")
    _remember_route(engine, market.market_id, found[0], market.condition_id)
    try:
        from paper_split_maker.chain_send import submit_position

        ident, settled = submit_position(
            creds,
            "split",
            market.condition_id,
            str(amount),
            version,
            bool(found[0].get("negRisk")),
            config.gas_limit,
            str(config.max_fee_gwei),
            str(config.priority_fee_gwei),
        )
    except Exception as exc:
        notes.append(creds.redact(f"split submit failed: {exc}"))
        engine.handle_event(
            {
                "type": "split_abandoned",
                "market_id": market.market_id,
                "amount": str(amount),
                "ts": stamp,
                "event_id": f"abandon-fail:{market.market_id}",
            }
        )
        return
    sent = getattr(engine, "split_sent", set())
    sent.add(slug)
    sent.add(market.market_id)
    engine.split_sent = sent
    if settled:
        ready = getattr(engine, "split_settled", set())
        ready.add(slug)
        ready.add(market.market_id)
        engine.split_settled = ready
    engine.split_tx = getattr(engine, "split_tx", {})
    engine.split_tx[market.market_id] = ident
    notes.append(f"split submitted for {slug}")


def _candidate_slugs(config, now_s: int, max_markets: int) -> list[str]:
    """Current window plus the next one.

    A placeholder slug such as ``no-initial-discovery-placeholder`` is ignored
    so a run can skip an already-started session without disabling discovery.
    Explicit ``btc-updown-5m-<unix>`` slugs are honored.
    """
    configured = []
    for slug in config.market_slugs:
        text = str(slug)
        tail = text.rsplit("-", 1)[-1]
        if text.startswith("btc-updown-5m-") and tail.isdigit():
            configured.append(text)
    if configured:
        return configured[: max(max_markets, 1)]
    start = (int(now_s) // 300) * 300
    return [f"btc-updown-5m-{start}", f"btc-updown-5m-{start + 300}"]


def _slug_open_s(slug: str):
    tail = str(slug).rsplit("-", 1)[-1]
    if tail.isdigit():
        return int(tail)
    return None


def _market_by_slug(engine, slug: str, condition_id: str = ""):
    market = engine.state.markets.get(slug)
    if market is not None:
        return market
    for item in engine.state.markets.values():
        if item.slug == slug or (condition_id and item.condition_id == condition_id):
            return item
    return None


def _will_quote(engine, market, stamp: int) -> bool:
    if market is None or market.status != "open":
        return False
    if market.session_stopped or engine.state.killed or engine.state.mismatch_stop:
        return False
    stop_at = market.end_ts_ms - engine.config.stop_quoting_before_expiry_ms
    return stamp < stop_at


def _affordable_split(engine, market, config):
    from paper_split_maker.decimal_utils import qdown

    amount = config.split_clip
    room = engine.state.per_market_budget - market.gross_split - market.pending_split
    if room < amount:
        amount = room
    if engine.state.cash < amount:
        amount = engine.state.cash
    amount = qdown(amount, config.order_size_quantum)
    minimum = (
        market.min_order_size
        if market.min_order_size > config.min_order_size
        else config.min_order_size
    )
    if amount < minimum:
        return None
    return amount


def _load_market(
    engine, payload, stamp: int, notes: list[str], *, allow_started: bool, lead_ms: int = 0
) -> bool:
    """Load a Gamma market under its slug so discovery and pre-split agree.

    A window that has already opened is skipped unless this run loaded it
    before the open (the pre-split). That is what skips a session already
    in progress when the process starts.
    """
    from paper_split_maker.normalize import gamma_market_event

    if not isinstance(payload, dict):
        return False
    slug = str(payload.get("slug") or "")
    condition = str(payload.get("conditionId") or payload.get("condition_id") or "")
    if not slug:
        return False
    existing = _market_by_slug(engine, slug, condition)
    open_s = _slug_open_s(slug)
    if existing is None and open_s is not None:
        open_ms = open_s * 1000
        if not allow_started and stamp >= open_ms:
            return False
        # Do not track a window several minutes early. It is loaded in the
        # pre-split lead, which is also when it can be funded and quoted.
        if stamp < open_ms - max(lead_ms, 0):
            return False
    if existing is not None and existing.accepting_orders and existing.status == "open":
        _remember_route(engine, existing.market_id, payload, existing.condition_id or condition)
        return True
    event = gamma_market_event(
        payload,
        market_id=existing.market_id if existing is not None else slug,
        synthetic=False,
    )
    if event is None:
        notes.append(f"{slug} is not a usable Up/Down market")
        return False
    event["ts"] = stamp
    # A fresh id each attempt: a loss-floor refusal must not burn the open
    # forever, or the next loop cannot load the market after cash is free.
    if existing is None:
        event["event_id"] = f"open:{event['market_id']}:{stamp}"
    else:
        event["event_id"] = f"refresh:{event['market_id']}:{stamp}"
    engine.handle_event(event)
    loaded = engine.state.markets.get(str(event["market_id"]))
    if loaded is None:
        return False
    _remember_route(engine, loaded.market_id, payload, str(event["condition_id"]))
    return True


def _flatten_for_rollover(engine, next_open_s: int, stamp: int, *, lead_ms: int) -> None:
    """Cancel and merge the session that ends as the next one opens.

    Capital equal to one split cannot fund the next split until this returns
    to cash. Two cancel events let a zero-latency cancel promote before the
    merge; live cancels still wait for the exchange confirm on a later pass.
    The current session is left quoted until this lead window.
    """
    from paper_split_maker.decimal_utils import qdown
    from paper_split_maker.risk import reserved

    end_ms = next_open_s * 1000
    rolling = stamp >= end_ms - max(lead_ms, 0)
    for market in list(engine.state.markets.values()):
        if market.status == "redeemed" or market.end_ts_ms > end_ms:
            continue
        ended = market.end_ts_ms <= stamp
        if not ended and not rolling:
            continue
        if (
            market.up == 0
            and market.down == 0
            and market.pending_split <= 0
            and not engine._working(market)
        ):
            market.reserve_active = False
            continue
        if market.pending_split > 0:
            continue
        seq = getattr(engine, "_rollover_seq", 0)
        for _ in range(2):
            seq += 1
            engine.handle_event(
                {
                    "type": "cancel",
                    "market_id": market.market_id,
                    "ts": stamp,
                    "event_id": f"rollover-cancel:{market.market_id}:{seq}",
                }
            )
        engine._rollover_seq = seq
        if engine._working(market):
            continue
        free_up = market.up - reserved(engine.state, market, "up")
        free_down = market.down - reserved(engine.state, market, "down")
        amount = free_up if free_up < free_down else free_down
        amount = qdown(amount, engine.config.order_size_quantum)
        if amount > 0:
            engine.handle_event(
                {
                    "type": "merge",
                    "market_id": market.market_id,
                    "amount": format(amount, "f"),
                    "ts": stamp,
                    "event_id": f"rollover-merge:{market.market_id}:{seq}:{format(amount, 'f')}",
                }
            )
        if market.up == 0 and market.down == 0 and not engine._working(market):
            market.reserve_active = False


def _poll_resolutions(engine, reader, stamp: int, notes: list[str]) -> None:
    from paper_split_maker.normalize import data_api_resolution_event
    from paper_split_maker.public_client import PublicDataError

    fetch = getattr(reader, "fetch_resolution", None)
    if fetch is None:
        return
    for market in list(engine.state.markets.values()):
        if market.status in {"open", "redeemed"}:
            continue
        try:
            payload = fetch(market.condition_id)
        except PublicDataError as exc:
            notes.append(f"resolution poll failed: {exc}")
            continue
        event = data_api_resolution_event(
            {
                "market_id": market.market_id,
                "condition_id": market.condition_id,
                "up_token_id": market.up_token_id,
                "down_token_id": market.down_token_id,
            },
            payload,
        )
        if event is None:
            continue
        event["ts"] = stamp
        engine.handle_event(event)


def _forget_market(engine, market_id: str) -> None:
    for name in ("split_tx",):
        bag = getattr(engine, name, None)
        if isinstance(bag, dict):
            bag.pop(market_id, None)
    for name in ("split_sent", "split_settled"):
        bag = getattr(engine, name, None)
        if isinstance(bag, set):
            bag.discard(market_id)
    routes = getattr(engine, "market_routes", None)
    if isinstance(routes, dict):
        routes.pop(market_id, None)


def _remember_route(engine, market_id: str, payload: dict, condition_id: str) -> None:
    routes = getattr(engine, "market_routes", {})
    routes[market_id] = {
        "version": str(payload.get("version") or "v1"),
        "neg_risk": bool(payload.get("negRisk")),
        "condition_id": condition_id,
    }
    engine.market_routes = routes


def _remember_assets(engine, asset_ids: list[str]) -> None:
    wanted = []
    for market in engine.state.markets.values():
        for token in (market.up_token_id, market.down_token_id):
            if token and token not in wanted:
                wanted.append(token)
    if wanted != list(asset_ids):
        asset_ids[:] = wanted


def _flush_chain(engine, creds, notes: list[str]) -> None:
    items = list(engine.chain_outbox)
    engine.chain_outbox.clear()
    if not items:
        return
    from paper_split_maker.chain_send import submit_position

    routes = getattr(engine, "market_routes", {})
    for item in items:
        route = routes.get(item["market_id"], {"version": "v1", "neg_risk": False})
        try:
            submit_position(
                creds,
                item["action"],
                item["condition_id"],
                item["amount"],
                route["version"],
                bool(route.get("neg_risk")),
                engine.config.gas_limit,
                str(engine.config.max_fee_gwei),
                str(engine.config.priority_fee_gwei),
            )
            notes.append(f"{item['action']} submitted for {item['market_id']}")
        except Exception as exc:
            notes.append(creds.redact(f"{item['action']} failed: {exc}"))
            engine.state.mismatch_stop = True
            return


def drain_market_ws(engine, ws_queue, stamp: int) -> int:
    """Apply queued public book frames, then let the quote logic run."""
    applied = 0
    seq = 0
    while True:
        try:
            raw = ws_queue.get_nowait()
        except queue.Empty:
            break
        seq += 1
        payload = raw
        if isinstance(raw, str):
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                continue
        applied += apply_market_frame(engine, payload, stamp, seq)
    if applied:
        engine.handle_event(
            {
                "type": "heartbeat",
                "ts": stamp,
                "event_id": f"ws-hb-{stamp}-{seq}",
            }
        )
    return applied


def apply_market_frame(engine, frame, stamp: int, seq: int = 0) -> int:
    from paper_split_maker.normalize import clob_book_event

    frames = frame if isinstance(frame, list) else [frame]
    applied = 0
    for item in frames:
        if not isinstance(item, dict):
            continue
        asset = str(item.get("asset_id") or "")
        market = None
        outcome = ""
        for candidate in engine.state.markets.values():
            if asset == candidate.up_token_id:
                market, outcome = candidate, "up"
            elif asset == candidate.down_token_id:
                market, outcome = candidate, "down"
        if market is None:
            continue
        payload = dict(item)
        if "bids" not in payload and item.get("event_type") == "best_bid_ask":
            payload = {
                "bids": [{"price": item.get("best_bid"), "size": "1"}],
                "asks": [{"price": item.get("best_ask"), "size": "1"}],
                "timestamp": item.get("timestamp") or stamp,
                "hash": item.get("hash") or f"bba-{asset}-{stamp}-{seq}",
                "asset_id": asset,
            }
        event = clob_book_event(
            market.market_id,
            outcome,
            payload,
            event_id=f"ws:{market.market_id}:{outcome}:{payload.get('hash')}:{stamp}:{seq}:{applied}",
        )
        if event is None:
            continue
        event["book_ts"] = event["ts"]
        event["ts"] = stamp
        engine.handle_event(event)
        applied += 1
    return applied


def _start_market_ws(asset_ids: list[str], out_queue, stop) -> threading.Thread | None:
    try:
        import websockets
    except ImportError:
        return None

    def _run() -> None:
        import asyncio

        from paper_split_maker.feeds import MARKET_WS, market_subscribe

        async def loop() -> None:
            while not stop.is_set():
                current = list(asset_ids)
                if not current:
                    await asyncio.sleep(0.2)
                    continue
                try:
                    async with websockets.connect(MARKET_WS, ping_interval=None) as ws:
                        await ws.send(json.dumps(market_subscribe(current)))
                        subscribed = list(current)
                        next_ping = time.time() + 10
                        while not stop.is_set() and list(asset_ids) == subscribed:
                            if time.time() >= next_ping:
                                await ws.send("PING")
                                next_ping = time.time() + 10
                            try:
                                raw = await asyncio.wait_for(ws.recv(), timeout=1)
                            except asyncio.TimeoutError:
                                continue
                            if raw in {"PING", "PONG"}:
                                continue
                            out_queue.put(raw)
                except Exception:
                    if stop.is_set():
                        return
                    await asyncio.sleep(1)

        asyncio.run(loop())

    thread = threading.Thread(target=_run, name="polymarket-market-ws", daemon=True)
    thread.start()
    return thread


def split_plan(
    version: str, condition_id: str, shares: str, neg_risk: bool = False
) -> dict:
    base = shares_to_base_units(shares)
    return {
        "action": "split",
        "target": position_target(version, neg_risk),
        "data": encode_split(version, condition_id, base),
        "amount": shares,
        "confirmed": False,
    }
