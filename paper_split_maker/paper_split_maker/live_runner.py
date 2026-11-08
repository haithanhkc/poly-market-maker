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
from paper_split_maker.engine import is_post_only_cross
from paper_split_maker.live_clob import http_status
from paper_split_maker.feeds import describe_user_subscribe
from paper_split_maker.logs import write_summary
from paper_split_maker.session import open_engine


def quoting_allowed(confirmed: bool) -> bool:
    return confirmed


def _ensure_live_state(engine) -> None:
    if getattr(engine, "_live_ready", False):
        return
    engine._live_ready = True
    engine.call_failures = {}
    engine.call_ready_at = {}
    engine.heartbeat_ok = False
    engine.orders_ok = False
    engine.heartbeat_id = None
    engine.next_heartbeat_at = 0.0
    engine.open_orders_cache = None
    engine.post_failures = {}
    engine.sell_accepted = set()
    engine.early_merged = set()
    engine.skipped_unready = set()
    engine.sessions_finished = set()
    engine.exchange_ready = False
    engine.require_exchange_ready = False
    engine.clock_now = 0.0


def _backoff_delay(failures: int) -> float:
    return min(30.0, 0.5 * (2 ** min(max(failures - 1, 0), 6)))


def _call_is_waiting(engine, label: str, now: float) -> bool:
    ready = getattr(engine, "call_ready_at", None)
    if not isinstance(ready, dict):
        return False
    return now < ready.get(label, 0.0)


def _record_call_ok(engine, label: str) -> None:
    failures = getattr(engine, "call_failures", None)
    if isinstance(failures, dict):
        failures[label] = 0
    ready = getattr(engine, "call_ready_at", None)
    if isinstance(ready, dict):
        ready.pop(label, None)


def _record_call_failure(engine, label: str, now: float, notes: list[str], text: str) -> None:
    """Log one failed exchange call and wait before the next attempt."""
    _ensure_live_state(engine)
    fails = int(engine.call_failures.get(label, 0)) + 1
    engine.call_failures[label] = fails
    engine.call_ready_at[label] = now + _backoff_delay(fails)
    notes.append(text)
    _log_exchange_error(engine, label.split(":", 1)[0][:80], text)


def _log_exchange_error(engine, label: str, message: str) -> None:
    """Append one log row without letting a logging failure escape."""
    try:
        pending = list(engine._batch)
        engine._batch = []
        engine._emit(
            kind="exchange_error",
            reason=str(label)[:80],
            detail=str(message)[:300],
        )
        engine._finish()
        engine._batch = pending
    except Exception:
        return


def _safe_call(engine, creds, notes: list[str], label: str, fn, *args, now: float):
    """Call ``fn``. On any exception, log it and wait until a later tick.

    The second value is True on success, False on failure, and None when the
    call is skipped because the previous failure is still backing off.
    """
    _ensure_live_state(engine)
    ready_at = engine.call_ready_at.get(label, 0.0)
    if now < ready_at:
        return None, None
    try:
        value = fn(*args)
    except Exception as exc:
        fails = int(engine.call_failures.get(label, 0)) + 1
        engine.call_failures[label] = fails
        engine.call_ready_at[label] = now + _backoff_delay(fails)
        text = creds.redact(f"{label} failed: {exc}")
        notes.append(text)
        print(text, flush=True)
        _log_exchange_error(engine, label, text)
        return None, False
    engine.call_failures[label] = 0
    engine.call_ready_at.pop(label, None)
    return value, True


def _refresh_readiness(engine, client, creds, notes: list[str], now: float) -> bool:
    """Heartbeat and the authenticated open-orders call must both succeed."""
    from paper_split_maker.live_clob import heartbeat

    _ensure_live_state(engine)
    if client is None:
        engine.exchange_ready = False
        return False
    if now >= engine.next_heartbeat_at or not engine.heartbeat_ok:
        def _beat():
            new_id, err = heartbeat(client, engine.heartbeat_id)
            if err is not None:
                raise err
            engine.heartbeat_id = new_id
            return new_id

        _value, status = _safe_call(engine, creds, notes, "heartbeat", _beat, now=now)
        if status is True:
            engine.heartbeat_ok = True
            engine.next_heartbeat_at = now + 5
        elif status is False:
            engine.heartbeat_ok = False
    _orders, status = _safe_call(
        engine, creds, notes, "open_orders", client.get_open_orders, now=now
    )
    if status is True:
        engine.orders_ok = isinstance(_orders, list)
        engine.open_orders_cache = _orders if engine.orders_ok else None
        if not engine.orders_ok:
            notes.append("open orders payload was not a list")
            engine.orders_ok = False
    elif status is False:
        engine.orders_ok = False
        engine.open_orders_cache = None
    engine.exchange_ready = bool(engine.heartbeat_ok and engine.orders_ok)
    return engine.exchange_ready


def _sessions_are_done(engine, max_markets: int) -> bool:
    done = getattr(engine, "sessions_finished", set())
    return max_markets > 0 and len(done) >= max_markets


def _note_session_done(engine, market) -> None:
    joined = getattr(engine, "preopen_joined", set())
    if market.market_id not in joined and market.slug not in joined:
        return
    if market.pending_split > 0 or market.up > 0 or market.down > 0:
        return
    if engine._working(market):
        return
    _ensure_live_state(engine)
    engine.sessions_finished.add(market.market_id)


def run_confirmed_live(
    config: Config,
    creds: Credentials,
    out_dir: Path,
    *,
    duration_seconds: int,
    max_markets: int,
    client=None,
    reader=None,
    now_s=None,
    sleep=None,
    poster=None,
) -> dict:
    from paper_split_maker.live_clob import (
        build_client,
        cancel_all,
        post_only_sells,
    )

    post_fn = poster or post_only_sells

    if duration_seconds <= 0:
        raise RuntimeError("duration_seconds must be positive")
    clock = now_s or time.time
    sleeper = sleep or time.sleep
    engine = open_engine(config, out_dir)
    engine.mode = "live"
    _ensure_live_state(engine)
    engine.require_exchange_ready = True
    if reader is None:
        from paper_split_maker.public_client import PublicReadClient

        reader = PublicReadClient()
    ws_queue: queue.Queue = queue.Queue()
    ws_stop = threading.Event()
    ws_assets: list[str] = []
    ws_thread = None if client is not None else _start_market_ws(ws_assets, ws_queue, ws_stop)
    notes = [
        "Live session on the configured funder. Orders are post-only sells.",
        describe_user_subscribe(creds.funder, max_markets),
        "Unconfirmed splits are not quoted.",
        "Shutdown sends cancel-all and merges held pairs.",
        "A single exchange error is retried on a later tick.",
        "Market websocket is on."
        if ws_thread is not None
        else "Market websocket package is missing; books come from REST.",
    ]
    print(describe_user_subscribe(creds.funder, max_markets))
    deadline = clock() + duration_seconds
    try:
        while clock() < deadline:
            if config.kill_switch_file and Path(config.kill_switch_file).exists():
                notes.append("kill switch file seen")
                break
            if engine.state.killed:
                notes.append("stopped on kill")
                break
            if _sessions_are_done(engine, max_markets):
                notes.append(f"finished {max_markets} sessions")
                break
            now = clock()
            engine.clock_now = now
            try:
                if client is None:
                    client, started = _safe_call(
                        engine,
                        creds,
                        notes,
                        "api_key",
                        build_client,
                        creds,
                        now=now,
                    )
                    if started is not True:
                        _pause(sleeper, deadline, clock, 1.0)
                        continue
                _live_tick(
                    engine,
                    client,
                    reader,
                    config,
                    creds,
                    notes,
                    now,
                    max_markets,
                    ws_assets,
                    ws_queue,
                    post_fn,
                )
            except Exception as exc:
                text = creds.redact(f"tick failed: {exc}")
                notes.append(text)
                print(text, flush=True)
                _log_exchange_error(engine, "tick", text)
            if engine.state.killed:
                notes.append("stopped on kill")
                break
            if _sessions_are_done(engine, max_markets):
                notes.append(f"finished {max_markets} sessions")
                break
            _pause(sleeper, deadline, clock, 0.2)
    finally:
        ws_stop.set()
        _shutdown_live(engine, client, creds, notes, cancel_all)
    summary = engine.summary(notes=notes)
    summary["mode"] = "live"
    summary["funder"] = creds.funder
    summary["signature_type"] = creds.signature_type
    write_summary(out_dir, summary)
    return summary


def _pause(sleeper, deadline: float, clock, delay: float) -> None:
    remaining = deadline - clock()
    if remaining <= 0:
        return
    sleeper(min(delay, remaining))


def _live_tick(
    engine,
    client,
    reader,
    config,
    creds,
    notes,
    now: float,
    max_markets: int,
    ws_assets,
    ws_queue,
    post_only_sells,
) -> None:
    stamp = int(now * 1000)
    _remember_assets(engine, ws_assets)
    drain_market_ws(engine, ws_queue, stamp, creds, notes)
    _refresh_readiness(engine, client, creds, notes, now)
    _poll_public(engine, reader, config, stamp, max_markets, notes)
    confirmed = _confirm_splits(engine, creds, stamp, notes)
    if confirmed:
        # Quote and post in this same tick. Do not wait for the next pass.
        engine.handle_event(
            {
                "type": "heartbeat",
                "ts": stamp,
                "event_id": f"hb-after-split-{stamp}",
            }
        )
        _send_new_quotes(engine, client, post_only_sells, creds, notes)
    _consider_pre_split(engine, reader, config, creds, stamp, notes)
    _ingest_matched(engine, client, creds, notes)
    _send_cancels(engine, client, creds, notes, now)
    if engine.orders_ok and isinstance(engine.open_orders_cache, list):
        remote_ids = [
            str(item.get("id"))
            for item in engine.open_orders_cache
            if isinstance(item, dict)
        ]
        engine.handle_event(
            {
                "type": "reconcile",
                "open_order_ids": remote_ids,
                "ts": stamp,
                "event_id": f"reconcile-{stamp}",
            }
        )
    else:
        notes.append("reconcile skipped; open orders were not readable")
    if engine.state.mismatch_stop:
        notes.append("reconcile mismatch contained; continuing")
        engine.state.mismatch_stop = False
    _send_new_quotes(engine, client, post_only_sells, creds, notes)
    _unwind_failed_posts(engine, stamp, notes)
    _flush_chain(engine, creds, notes)
    for market in list(engine.state.markets.values()):
        _note_session_done(engine, market)


def _shutdown_live(engine, client, creds, notes: list[str], cancel_all) -> None:
    """Best-effort cancel and merge. Used on duration, kill, and crashes."""
    cancel_ok = False
    if client is not None:
        try:
            cancel_all(client)
            notes.append("cancel-all sent")
            cancel_ok = True
        except Exception as exc:
            text = creds.redact(f"cancel-all failed: {exc}")
            notes.append(text)
            _log_exchange_error(engine, "cancel_all", text)
    stamp = int(getattr(engine, "clock_now", 0) * 1000) or engine.state.now_ms or 0
    for market in list(engine.state.markets.values()):
        try:
            _release_orders(engine, market, cancel_ok=cancel_ok)
            _merge_flat(engine, market, stamp, "shutdown")
        except Exception as exc:
            notes.append(creds.redact(f"shutdown merge failed: {exc}"))
    try:
        _flush_chain(engine, creds, notes)
    except Exception as exc:
        notes.append(creds.redact(f"shutdown chain failed: {exc}"))


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
        message = creds.redact(str(exc))
        notes.append(f"post failed: {message}")
        status = http_status(exc)
        refused = (status is not None and 400 <= status < 500) or is_post_only_cross(
            str(exc)
        )
        for item in pending:
            _mark_post_failure(engine, item["order"])
        if refused:
            for item in pending:
                _reject_quote(engine, item["order"], str(exc), creds, notes)
        else:
            notes.append("post error was not a 4xx; retrying without stopping")
        return
    if not isinstance(results, list):
        results = [results]
    ack_ms = int((time.time() - started) * 1000)
    for item, result in zip(pending, results):
        ident = ""
        error = ""
        status = None
        if isinstance(result, dict):
            ident = str(result.get("orderID") or result.get("id") or "")
            error = str(result.get("errorMsg") or result.get("error") or "")
            status = result.get("status") or result.get("status_code")
            if result.get("success") is False and not error:
                error = "order not accepted"
        elif result is None:
            error = "empty post result"
        item["order"].sent = True
        client_error = isinstance(status, int) and 400 <= status < 500
        if error or not ident or client_error:
            # The exchange did not take this order. It must not count as resting.
            _mark_post_failure(engine, item["order"])
            _reject_quote(engine, item["order"], error or "no order id", creds, notes)
            continue
        item["order"].exchange_id = ident
        _mark_sell_accepted(engine, item["order"])
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


def _reject_quote(engine, order, reason: str, creds, notes: list[str]) -> None:
    # Persist the rejection immediately. The next engine event clears _batch.
    pending = list(engine._batch)
    engine._batch = []
    engine.note_post_reject(order, reason)
    engine._finish()
    engine._batch = pending
    notes.append(creds.redact(f"order rejected: {reason}"))


def _mark_post_failure(engine, order) -> None:
    _ensure_live_state(engine)
    market_id = order.market_id
    engine.post_failures[market_id] = int(engine.post_failures.get(market_id, 0)) + 1


def _mark_sell_accepted(engine, order) -> None:
    _ensure_live_state(engine)
    engine.sell_accepted.add(order.market_id)


def _send_cancels(engine, client, creds, notes: list[str], now: float = 0.0) -> None:
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

    def _cancel():
        client.cancel_orders(ids)

    _value, status = _safe_call(
        engine, creds, notes, "cancel", _cancel, now=now or time.time()
    )
    if status is not True:
        for order in engine.state.orders.values():
            if order.exchange_id in ids:
                order.cancel_sent = False
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

    now_s = stamp // 1000
    window = (now_s // 300) * 300
    next_open = window + 300
    current_slug = f"btc-updown-5m-{window}"
    if current_slug not in engine.state.markets and _market_by_slug(engine, current_slug) is None:
        _note_skipped_start(engine, current_slug, notes)
    _forget_missed_sessions(engine, stamp, notes)
    if stamp >= next_open * 1000 - config.pre_split_lead_ms:
        # Free the ending session before discovery, or the new market is
        # refused by the loss floor while the old sell is still working.
        _flatten_for_rollover(
            engine, next_open, stamp, lead_ms=config.pre_split_lead_ms
        )
    slugs = _candidate_slugs(config, now_s, max_markets)
    poll_now = stamp / 1000
    if slugs and _call_is_waiting(engine, "discovery", poll_now):
        payloads = []
    elif slugs:
        try:
            payloads = reader.discover_btc_updown(now_s, len(slugs), slugs)
        except Exception as exc:
            payloads = []
            _record_call_failure(
                engine, "discovery", poll_now, notes, f"discovery failed: {exc}"
            )
        else:
            _record_call_ok(engine, "discovery")
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
        book_label = f"book:{market.market_id}"
        if _call_is_waiting(engine, book_label, poll_now):
            # Keep the last book. A retry wait is not a dead feed.
            continue
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
        except Exception as exc:
            market_ok = False
            _record_call_failure(
                engine,
                book_label,
                poll_now,
                notes,
                f"book poll failed for {market.market_id}: {exc}",
            )
        else:
            _record_call_ok(engine, book_label)
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
    now = float(getattr(engine, "clock_now", 0) or time.time())
    if _call_is_waiting(engine, "order_read", now):
        return
    for order in list(engine.state.orders.values()):
        if not order.exchange_id or order.status not in {"live", "cancel_requested"}:
            continue
        try:
            payload = client.get_order(order.exchange_id)
        except Exception as exc:
            now = float(getattr(engine, "clock_now", 0) or time.time())
            _record_call_failure(
                engine,
                "order_read",
                now,
                notes,
                creds.redact(f"order read failed: {exc}"),
            )
            return
        _record_call_ok(engine, "order_read")
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


def _confirm_splits(engine, creds, stamp: int, notes: list[str]) -> list[str]:
    txs = getattr(engine, "split_tx", {})
    confirmed: list[str] = []
    if not txs:
        return confirmed
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
        confirmed.append(market.market_id)
    return confirmed


def _consider_pre_split(
    engine, reader, config, creds, stamp: int, notes: list[str]
) -> None:
    """Split the next 5-minute market before it opens. Quote only after confirmation.

    The split is sent only after the engine has loaded that market and will
    still quote it. The session that is about to end is flattened first so a
    bankroll equal to one split can fund the next one.
    """
    from paper_split_maker.live_session import should_pre_split

    window = (stamp // 1000 // 300) * 300
    next_open = window + 300
    slug = f"btc-updown-5m-{next_open}"
    if stamp >= next_open * 1000:
        # This session has started. Never split it on the way in; the
        # following window is handled once it is the upcoming one.
        return
    if stamp >= next_open * 1000 - config.pre_split_lead_ms:
        _flatten_for_rollover(engine, next_open, stamp, lead_ms=config.pre_split_lead_ms)
    existing = _market_by_slug(engine, slug)
    if existing is not None and existing.gross_split > 0:
        return
    pending = existing is not None and existing.pending_split > 0
    skipped = getattr(engine, "skipped_unready", set())
    if slug in skipped:
        return
    in_lead = next_open * 1000 - config.pre_split_lead_ms <= stamp < next_open * 1000
    if (
        in_lead
        and not pending
        and getattr(engine, "require_exchange_ready", False)
        and not getattr(engine, "exchange_ready", False)
    ):
        # Heartbeat or the order endpoint is down. Do not split this window.
        skipped.add(slug)
        engine.skipped_unready = skipped
        notes.append(f"{slug} skipped; exchange not ready to sell")
        return
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
    lookup_now = float(getattr(engine, "clock_now", 0) or (stamp / 1000))
    if _call_is_waiting(engine, "discovery", lookup_now):
        return
    try:
        found = reader.discover_btc_updown(next_open, 1, [slug])
    except Exception as exc:
        _record_call_failure(
            engine, "discovery", lookup_now, notes, f"next market lookup failed: {exc}"
        )
        return
    _record_call_ok(engine, "discovery")
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
    split_now = float(getattr(engine, "clock_now", 0) or (stamp / 1000))
    if _call_is_waiting(engine, "split", split_now):
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
        _record_call_failure(
            engine,
            "split",
            split_now,
            notes,
            creds.redact(f"split submit failed: {exc}"),
        )
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
    _record_call_ok(engine, "split")
    sent = getattr(engine, "split_sent", set())
    sent.add(slug)
    sent.add(market.market_id)
    engine.split_sent = sent
    if stamp < next_open * 1000:
        joined = getattr(engine, "preopen_joined", set())
        joined.add(slug)
        joined.add(market.market_id)
        engine.preopen_joined = joined
    if settled:
        ready = getattr(engine, "split_settled", set())
        ready.add(slug)
        ready.add(market.market_id)
        engine.split_settled = ready
    engine.split_tx = getattr(engine, "split_tx", {})
    engine.split_tx[market.market_id] = ident
    notes.append(f"split submitted for {slug}")


def _note_skipped_start(engine, slug: str, notes: list[str]) -> None:
    seen = getattr(engine, "skipped_started", set())
    if slug in seen:
        return
    seen.add(slug)
    engine.skipped_started = seen
    notes.append(f"{slug} already started; waiting for the next session")


def _forget_missed_sessions(engine, stamp: int, notes: list[str]) -> None:
    """Drop a market whose open passed without a pre-open split.

    A session loaded during its lead is kept. One that is already open when
    first seen, or that we failed to split before the open, is not quoted.
    """
    joined = getattr(engine, "preopen_joined", set())
    for market in list(engine.state.markets.values()):
        open_s = _slug_open_s(market.slug)
        if open_s is None:
            open_s = _slug_open_s(market.market_id)
        if open_s is None or stamp < open_s * 1000:
            continue
        if market.market_id in joined or (market.slug and market.slug in joined):
            continue
        if (
            market.gross_split > 0
            or market.pending_split > 0
            or engine._working(market)
        ):
            continue
        if market.status != "open":
            continue
        market.status = "expired_unresolved"
        market.reserve_active = False
        _note_skipped_start(engine, market.slug or market.market_id, notes)
    for market_id in engine.retire_finished():
        notes.append(f"skipped started session {market_id}")


def _candidate_slugs(config, now_s: int, max_markets: int) -> list[str]:
    """Only the next window, which has not started yet.

    The window containing ``now_s`` is already open. Startup and every
    rollover wait for the following session and split it during its pre-open
    lead. A placeholder slug is ignored. Explicit ``btc-updown-5m-<unix>``
    slugs are honored only while they are still in the future; ``_load_market``
    drops one that has already started.
    """
    configured = []
    now_ms = int(now_s) * 1000
    for slug in config.market_slugs:
        text = str(slug)
        tail = text.rsplit("-", 1)[-1]
        if text.startswith("btc-updown-5m-") and tail.isdigit():
            if int(tail) * 1000 > now_ms:
                configured.append(text)
    if configured:
        return configured[: max(max_markets, 1)]
    start = (int(now_s) // 300) * 300
    return [f"btc-updown-5m-{start + 300}"]


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
    if market.session_stopped or engine.state.killed:
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
            # Started when first seen: do not split or quote it. The next
            # unstarted window is the one this run may enter.
            _note_skipped_start(engine, slug, notes)
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


def _release_orders(engine, market, *, cancel_ok: bool) -> None:
    """Drop local reservations for orders the exchange does not have."""
    from paper_split_maker.models import WORKING_ORDER_STATUSES

    for order in list(engine.state.orders.values()):
        if order.market_id != market.market_id:
            continue
        if order.status not in WORKING_ORDER_STATUSES:
            continue
        if order.exchange_id and not cancel_ok:
            continue
        order.status = "cancelled"


def _merge_flat(engine, market, stamp: int, reason: str) -> bool:
    from paper_split_maker.decimal_utils import qdown
    from paper_split_maker.risk import reserved

    if engine._working(market) or market.pending_split > 0:
        return False
    free_up = market.up - reserved(engine.state, market, "up")
    free_down = market.down - reserved(engine.state, market, "down")
    amount = free_up if free_up < free_down else free_down
    amount = qdown(amount, engine.config.order_size_quantum)
    if amount <= 0:
        market.reserve_active = market.up > 0 or market.down > 0
        return market.up == 0 and market.down == 0
    seq = getattr(engine, "_rollover_seq", 0) + 1
    engine._rollover_seq = seq
    engine.handle_event(
        {
            "type": "merge",
            "market_id": market.market_id,
            "amount": format(amount, "f"),
            "ts": stamp,
            "event_id": f"{reason}-merge:{market.market_id}:{seq}:{format(amount, 'f')}",
        }
    )
    if market.up == 0 and market.down == 0:
        market.reserve_active = False
        market.session_stopped = True
        return True
    return False


def _unwind_failed_posts(engine, stamp: int, notes: list[str]) -> None:
    """Merge a pair whose sells keep failing after the open.

    If the loss floor is what blocks the quote, there is no post error and
    the pair stays until the normal close.
    """
    timeout_s = int(getattr(engine.config, "sell_ready_timeout_seconds", 30) or 0)
    if timeout_s <= 0:
        return
    _ensure_live_state(engine)
    for market in list(engine.state.markets.values()):
        if market.gross_split <= 0 or market.status != "open":
            continue
        open_s = _slug_open_s(market.slug) or _slug_open_s(market.market_id)
        if open_s is None or stamp < open_s * 1000 + timeout_s * 1000:
            continue
        if market.market_id in engine.sell_accepted:
            continue
        if market.market_id in engine.early_merged:
            continue
        if int(engine.post_failures.get(market.market_id, 0)) < 1:
            continue
        _release_orders(engine, market, cancel_ok=False)
        merged = _merge_flat(engine, market, stamp, "post_failures")
        if merged:
            engine.early_merged.add(market.market_id)
            notes.append(
                f"early merge {market.slug or market.market_id}: "
                f"posts failed for {timeout_s}s"
            )
            _note_session_done(engine, market)


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

    fetch = getattr(reader, "fetch_resolution", None)
    if fetch is None:
        return
    for market in list(engine.state.markets.values()):
        if market.status in {"open", "redeemed"}:
            continue
        label = f"resolution:{market.market_id}"
        poll_now = stamp / 1000
        if _call_is_waiting(engine, label, poll_now):
            continue
        try:
            payload = fetch(market.condition_id)
        except Exception as exc:
            _record_call_failure(
                engine, label, poll_now, notes, f"resolution poll failed: {exc}"
            )
            continue
        _record_call_ok(engine, label)
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
    _ensure_live_state(engine)
    now = float(getattr(engine, "clock_now", 0) or time.time())
    if now < engine.call_ready_at.get("chain", 0):
        return
    items = list(engine.chain_outbox)
    engine.chain_outbox.clear()
    if not items:
        return
    from paper_split_maker.chain_send import submit_position

    routes = getattr(engine, "market_routes", {})
    for index, item in enumerate(items):
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
            text = creds.redact(f"{item['action']} failed: {exc}")
            notes.append(text)
            _log_exchange_error(engine, "chain", text)
            fails = int(engine.call_failures.get("chain", 0)) + 1
            engine.call_failures["chain"] = fails
            engine.call_ready_at["chain"] = now + _backoff_delay(fails)
            engine.chain_outbox.extend(items[index:])
            return


def drain_market_ws(engine, ws_queue, stamp: int, creds=None, notes: list[str] | None = None) -> int:
    """Apply queued public book frames, then let the quote logic run."""
    applied = 0
    seq = 0
    while True:
        try:
            raw = ws_queue.get_nowait()
        except queue.Empty:
            break
        seq += 1
        if isinstance(raw, dict) and raw.get("__ws_error__"):
            text = f"market websocket failed: {raw.get('__ws_error__')}"
            if notes is not None:
                notes.append(text)
            _log_exchange_error(engine, "websocket", text)
            continue
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
                except Exception as exc:
                    if stop.is_set():
                        return
                    try:
                        out_queue.put({"__ws_error__": str(exc)})
                    except Exception:
                        pass
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
