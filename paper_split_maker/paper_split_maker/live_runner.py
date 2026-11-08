"""Bounded live session. Paper mode never imports this.

The clock stops at duration_seconds. Shutdown calls cancel-all. A kill-switch
file does the same. Inventory is quoted only after a split confirmation.
"""

from __future__ import annotations

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
    notes = [
        "Live session on the configured funder. Orders are post-only sells.",
        describe_user_subscribe(creds.funder, max_markets),
        "Unconfirmed splits are not quoted.",
        "Shutdown sends cancel-all.",
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
            time.sleep(min(0.2, max(0.0, deadline - time.time())))
    finally:
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
        if isinstance(result, dict):
            ident = str(result.get("orderID") or result.get("id") or "")
        item["order"].exchange_id = ident
        item["order"].sent = True
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
    from paper_split_maker.chain_send import eoa_confirmed

    for slug, ident in list(txs.items()):
        market = engine.state.markets.get(slug)
        if market is None or market.pending_split <= 0:
            txs.pop(slug, None)
            continue
        try:
            ok = eoa_confirmed(ident) if creds.signature_type == 0 else False
        except Exception as exc:
            notes.append(creds.redact(f"split receipt failed: {exc}"))
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
    plan = split_plan(
        version, event["condition_id"], str(amount), bool(found[0].get("negRisk"))
    )
    try:
        from paper_split_maker.chain_send import send_eoa

        if creds.signature_type != 0:
            raise RuntimeError(
                "POLY_PROXY, GNOSIS_SAFE, and POLY_1271 splits must be signed "
                "for the official relayer. This process will not submit an unsigned request."
            )
        ident = send_eoa(
            plan,
            creds,
            config.gas_limit,
            str(config.max_fee_gwei),
            str(config.priority_fee_gwei),
        )
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
