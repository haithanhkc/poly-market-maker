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
        engine._emit(
            kind="latency",
            market=engine.state.markets[item["order"].market_id],
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
    from paper_split_maker.normalize import clob_book_event, gamma_market_event
    from paper_split_maker.public_client import PublicDataError

    if not engine.state.markets:
        try:
            payloads = reader.discover_btc_updown(
                stamp // 1000, max_markets, list(config.market_slugs) or None
            )
        except PublicDataError as exc:
            notes.append(f"discovery failed: {exc}")
            engine.handle_event(
                {
                    "type": "feed",
                    "status": "down",
                    "ts": stamp,
                    "event_id": f"feed-down-{stamp}",
                }
            )
            return
        for payload in payloads:
            event = gamma_market_event(payload, synthetic=False)
            if event is None:
                continue
            event["event_id"] = f"open:{event['condition_id']}"
            event["ts"] = stamp
            engine.handle_event(event)
            _remember_route(engine, event["market_id"], payload, event["condition_id"])
    failed = False
    for market in list(engine.state.markets.values()):
        if market.status == "redeemed":
            continue
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
                    failed = True
                    continue
                event["book_ts"] = event["ts"]
                event["ts"] = stamp
                engine.handle_event(event)
        except PublicDataError as exc:
            failed = True
            notes.append(f"book poll failed: {exc}")
            break
    engine.handle_event(
        {
            "type": "feed",
            "status": "down" if failed else "up",
            "ts": stamp,
            "event_id": f"feed-{'down' if failed else 'up'}-{stamp}",
        }
    )
    engine.handle_event({"type": "heartbeat", "ts": stamp, "event_id": f"hb-{stamp}"})


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
        market = engine.state.markets.get(slug)
        if market is None or market.pending_split <= 0:
            txs.pop(slug, None)
            continue
        settled = slug in getattr(engine, "split_settled", set())
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
        window = (
            int(slug.rsplit("-", 1)[-1]) if slug.rsplit("-", 1)[-1].isdigit() else 0
        )
        late = window and stamp > window * 1000
        engine.handle_event(
            {
                "type": "split_confirmed",
                "market_id": slug,
                "amount": str(amount),
                "ts": stamp,
                "event_id": f"confirmed:{slug}:{ident}",
            }
        )
        if late:
            market.session_stopped = True
            notes.append(
                f"{slug} split confirmed after the open; inventory is not quoted"
            )
        txs.pop(slug, None)


def _consider_pre_split(
    engine, reader, config, creds, stamp: int, notes: list[str]
) -> None:
    """Split the next 5-minute market before it opens. Quote only after confirmation."""
    from paper_split_maker.live_session import should_pre_split
    from paper_split_maker.normalize import gamma_market_event
    from paper_split_maker.public_client import PublicDataError

    window = (stamp // 1000 // 300) * 300
    next_open = window + 300
    slug = f"btc-updown-5m-{next_open}"
    if slug in engine.state.markets and engine.state.markets[slug].gross_split > 0:
        return
    pending = (
        slug in engine.state.markets and engine.state.markets[slug].pending_split > 0
    )
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
                    "market_id": slug,
                    "amount": str(engine.state.markets[slug].pending_split),
                    "ts": stamp,
                    "event_id": f"abandon:{slug}",
                }
            )
            notes.append(
                f"{slug} split was not confirmed before the open; not quoting it"
            )
        return
    try:
        found = reader.discover_btc_updown(next_open, 1, [slug])
    except PublicDataError as exc:
        notes.append(f"next market lookup failed: {exc}")
        return
    if not found:
        return
    event = gamma_market_event(found[0], market_id=slug, synthetic=False)
    if event is None:
        return
    event["ts"] = stamp
    event["event_id"] = f"open:{slug}"
    engine.handle_event(event)
    if slug not in engine.state.markets:
        # The engine refused the market: never split on-chain for it.
        notes.append(f"{slug} not accepted by engine; no split")
        return
    amount = config.split_clip
    engine.handle_event(
        {
            "type": "split_pending",
            "market_id": slug,
            "amount": str(amount),
            "ts": stamp,
            "event_id": f"pending:{slug}",
        }
    )
    version = str(found[0].get("version") or "v1")
    _remember_route(engine, slug, found[0], event["condition_id"])
    try:
        from paper_split_maker.chain_send import submit_position

        ident, settled = submit_position(
            creds,
            "split",
            event["condition_id"],
            str(amount),
            version,
            bool(found[0].get("negRisk")),
            config.gas_limit,
            str(config.max_fee_gwei),
            str(config.priority_fee_gwei),
        )
        if settled:
            ready = getattr(engine, "split_settled", set())
            ready.add(slug)
            engine.split_settled = ready
    except Exception as exc:
        notes.append(creds.redact(f"split submit failed: {exc}"))
        engine.handle_event(
            {
                "type": "split_abandoned",
                "market_id": slug,
                "amount": str(amount),
                "ts": stamp,
                "event_id": f"abandon-fail:{slug}",
            }
        )
        return
    sent = getattr(engine, "split_sent", set())
    sent.add(slug)
    engine.split_sent = sent
    engine.split_tx = getattr(engine, "split_tx", {})
    engine.split_tx[slug] = ident
    notes.append(f"split submitted for {slug}")


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
