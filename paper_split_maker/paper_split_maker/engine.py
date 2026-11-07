"""Risk-controlled two-sided sell paper engine.

The engine never signs, never submits, and never reads a credential. Inventory
comes from modeled SPLIT/MERGE/REDEEM entries. Working sells reserve tokens
immediately. A cancel removes that reservation only after the configured
latency and only while the feed is up.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Optional

from paper_split_maker.config import Config
from paper_split_maker.decimal_utils import ONE, ZERO, money, on_grid, qdown
from paper_split_maker.logs import LogWriter
from paper_split_maker.models import (
    WORKING_ORDER_STATUSES,
    LevelBook,
    Market,
    Order,
    State,
)
from paper_split_maker.quotes import Proposal, propose
from paper_split_maker.risk import (
    assert_ledger_invariants,
    best_case_equity,
    net_maker_proceeds,
    reserved,
    worst_case_equity,
)
from paper_split_maker.store import load_state, save_state

SIZE_REASONS = frozenset({"ok", "below_min_size", "insufficient_inventory"})


class Engine:
    def __init__(
        self,
        state: State,
        config: Config,
        state_path: Optional[Path] = None,
        log_dir: Optional[Path] = None,
        *,
        resumed: bool = False,
    ):
        self.state = state
        self.config = config
        self.state_path = Path(state_path) if state_path else None
        self.log_dir = Path(log_dir) if log_dir else None
        self.resumed = resumed
        self.writer = LogWriter(self.log_dir) if self.log_dir else None
        self._batch: list[dict] = []
        self._skip_once: set[tuple[str, str, str]] = set()
        self.mode = "library"

    @classmethod
    def create(
        cls,
        config: Config,
        state_path: Optional[Path] = None,
        log_dir: Optional[Path] = None,
    ) -> "Engine":
        config.validate()
        engine = cls(State.fresh(config), config, state_path, log_dir, resumed=False)
        engine.assert_invariants()
        if engine.state_path:
            save_state(engine.state_path, engine.state)
        return engine

    @classmethod
    def load(
        cls,
        config: Config,
        state_path: Path,
        log_dir: Optional[Path] = None,
    ) -> "Engine":
        """Resume a ledger. Config cannot loosen the locked floor or risk parameters."""
        config.validate()
        state = load_state(state_path)
        engine = cls(state, config, state_path, log_dir, resumed=True)
        engine.assert_invariants()
        return engine

    def assert_invariants(self) -> None:
        assert_ledger_invariants(self.state)

    def at_cap(self) -> bool:
        return worst_case_equity(self.state) <= self.state.loss_floor + self.config.freeze_band

    def handle_event(self, event: dict) -> list[dict]:
        self._batch = []
        if not isinstance(event, dict) or "type" not in event:
            self._emit(kind="reject", reason="bad_event")
            self._finish()
            return list(self._batch)
        event_id = event.get("event_id")
        if event_id is not None:
            event_id = str(event_id)
            if event_id in self.state.applied_event_ids:
                self._emit(kind="duplicate_event", reason="duplicate_event", detail=event_id)
                self._finish()
                return list(self._batch)
            self.state.applied_event_ids.add(event_id)
        kind = str(event["type"])
        if "ts" in event and event["ts"] is not None:
            try:
                stamp = int(event["ts"])
            except (TypeError, ValueError):
                self._emit(kind="reject", reason="bad_timestamp")
                self._finish()
                return list(self._batch)
            if stamp < self.state.now_ms:
                self._emit(kind="out_of_order_clock", reason="out_of_order_clock", detail=str(stamp))
                if kind in {"heartbeat", "clock", "split", "merge"}:
                    self._finish()
                    return list(self._batch)
            else:
                self.state.now_ms = stamp
                self._promote()
        else:
            self._promote()
        handler = {
            "heartbeat": self._on_heartbeat,
            "clock": self._on_clock,
            "market_open": self._on_market_open,
            "book": self._on_book,
            "trade": self._on_trade,
            "resolution": self._on_resolution,
            "feed": self._on_feed,
            "adverse_fill": self._on_adverse_fill,
            "split": self._on_split,
            "merge": self._on_merge,
            "cancel": self._on_cancel,
        }.get(kind)
        if handler is None:
            self._emit(kind="reject", reason="unknown_event")
        else:
            handler(event)
        self._note_cap()
        self.assert_invariants()
        self._finish()
        return list(self._batch)

    def summary(self, notes: Optional[list[str]] = None) -> dict:
        from paper_split_maker.logs import build_summary

        return build_summary(self.state, mode=self.mode, resumed=self.resumed, notes=notes)

    def _finish(self) -> None:
        if self.writer is not None:
            self.writer.write(self._batch)
        if self.state_path is not None:
            save_state(self.state_path, self.state)

    def _emit(self, kind: str, market: Optional[Market] = None, order: Optional[Order] = None, **fields) -> None:
        price = fields.pop("price", None)
        size = fields.pop("size", None)
        outcome = fields.pop("outcome", None)
        if order is not None:
            price = order.price if price is None else price
            outcome = order.outcome if outcome is None else outcome
        row = {
            "ts": self.state.now_ms,
            "kind": kind,
            "market_id": market.market_id if market is not None else fields.pop("market_id", ""),
            "order_id": order.order_id if order is not None else fields.pop("order_id", ""),
            "outcome": outcome or "",
            "price": money(price) if isinstance(price, Decimal) else (price or ""),
            "size": money(size) if isinstance(size, Decimal) else (size or ""),
            "cash": money(self.state.cash),
            "worst_case_equity": money(worst_case_equity(self.state)),
            "best_case_equity": money(best_case_equity(self.state)),
            "realized_pnl": money(self.state.realized_pnl),
            "rebate_estimate": money(self.state.rebate_estimate),
            "reason": fields.pop("reason", "") or "",
            "detail": fields.pop("detail", "") or "",
        }
        if fields:
            row["extra"] = {key: _jsonable(value) for key, value in fields.items()}
        self._batch.append(row)
        if kind in {"quote_reject", "risk_freeze"} and row["reason"] in {
            "risk_floor",
            "exposure_frozen",
            "loss_floor",
            "not_risk_reducing",
        }:
            if len(self.state.risk_rejections) < 200:
                self.state.risk_rejections.append(
                    {"ts": self.state.now_ms, "reason": row["reason"], "market_id": row["market_id"]}
                )

    def _note_cap(self) -> None:
        at_cap = self.at_cap()
        if at_cap and not self.state.was_at_cap:
            self._emit(kind="risk_freeze", reason="loss_floor")
        self.state.was_at_cap = at_cap

    def _skip(self, market: Market, reason: str, outcome: str = "") -> None:
        key = (market.market_id, outcome, reason)
        if key in self._skip_once:
            return
        self._skip_once.add(key)
        self._emit(kind="quote_reject", market=market, outcome=outcome, reason=reason)

    def _market(self, event: dict) -> Optional[Market]:
        market_id = event.get("market_id")
        if market_id is None:
            self._emit(kind="reject", reason="missing_market")
            return None
        market = self.state.markets.get(str(market_id))
        if market is None:
            self._emit(kind="reject", reason="unknown_market", detail=str(market_id))
            return None
        return market

    def _promote(self) -> None:
        for order in list(self.state.orders.values()):
            if order.status == "queued" and self.state.now_ms >= order.submit_ts + self.state.ack_latency_ms:
                order.status = "live"
                order.live_ts = order.submit_ts + self.state.ack_latency_ms
                market = self.state.markets[order.market_id]
                self._emit(kind="order_live", market=market, order=order, size=order.remaining)
            if (
                order.status == "cancel_requested"
                and order.cancel_ready_ts is not None
                and self.state.feed_up
                and self.state.now_ms >= order.cancel_ready_ts
            ):
                order.status = "cancelled"
                market = self.state.markets[order.market_id]
                self._emit(
                    kind="cancel_confirm",
                    market=market,
                    order=order,
                    size=order.remaining,
                    reason="cancel_latency_elapsed",
                )

    def _on_clock(self, event: dict) -> None:
        self._emit(kind="clock", detail=str(self.state.now_ms))

    def _on_heartbeat(self, event: dict) -> None:
        market_id = event.get("market_id")
        markets = (
            [self.state.markets[str(market_id)]]
            if market_id
            else [self.state.markets[key] for key in sorted(self.state.markets)]
        )
        if market_id and str(market_id) not in self.state.markets:
            self._emit(kind="reject", reason="unknown_market", detail=str(market_id))
            return
        for market in markets:
            self._decide(market)

    def _on_feed(self, event: dict) -> None:
        status = str(event.get("status") or "")
        if status not in {"up", "down"}:
            self._emit(kind="reject", reason="bad_feed_status")
            return
        feed_up = status == "up"
        if feed_up == self.state.feed_up:
            self._emit(kind="feed", reason="unchanged", detail=status)
            return
        self.state.feed_up = feed_up
        self._skip_once.clear()
        if not feed_up:
            for order in self.state.orders.values():
                if order.status == "cancel_requested":
                    order.cancel_ready_ts = None
            self._emit(kind="feed", reason="disconnected")
            for market in self.state.markets.values():
                self._cancel_market(market, "disconnected")
        else:
            for order in self.state.orders.values():
                if order.status == "cancel_requested":
                    order.cancel_ready_ts = self.state.now_ms + self.state.cancel_latency_ms
            self._emit(kind="feed", reason="reconnected")

    def _on_market_open(self, event: dict) -> None:
        try:
            market = _market_from_event(event, self.config)
        except (KeyError, ValueError, ArithmeticError) as exc:
            self._emit(kind="reject", reason="bad_market", detail=str(exc))
            return
        existing = self.state.markets.get(market.market_id)
        if existing is not None:
            same = (
                existing.up_token_id == market.up_token_id
                and existing.down_token_id == market.down_token_id
                and existing.condition_id == market.condition_id
            )
            if same:
                existing.accepting_orders = market.accepting_orders
                existing.end_ts_ms = market.end_ts_ms
                self._emit(kind="market_open", market=existing, reason="idempotent")
                return
            self._emit(kind="reject", market=existing, reason="market_identity_conflict")
            return
        open_count = sum(1 for item in self.state.markets.values() if item.status != "redeemed")
        if open_count >= self.config.max_open_markets:
            self._emit(kind="reject", reason="max_open_markets", detail=market.market_id)
            return
        market.reserve_active = True
        market.operational_reserve_amount = self.state.operational_reserve
        self.state.markets[market.market_id] = market
        if worst_case_equity(self.state) < self.state.loss_floor:
            del self.state.markets[market.market_id]
            self._emit(kind="quote_reject", reason="risk_floor", detail=market.market_id)
            return
        self._emit(
            kind="market_open",
            market=market,
            reason="synthetic" if market.synthetic else "public",
            detail=market.slug,
        )

    def _on_book(self, event: dict) -> None:
        market = self._market(event)
        if market is None:
            return
        outcome = str(event.get("outcome") or "")
        if outcome not in {"up", "down"}:
            self._emit(kind="reject", market=market, reason="bad_outcome")
            return
        try:
            stamp = int(event.get("book_ts", event["ts"]))
        except (KeyError, TypeError, ValueError):
            market.book(outcome).uncertain = True
            self._emit(kind="book", market=market, outcome=outcome, reason="uncertain_book")
            return
        book = market.book(outcome)
        digest = str(event.get("hash") or "")
        if book.ts_ms >= 0 and stamp < book.ts_ms:
            self._emit(kind="book", market=market, outcome=outcome, reason="out_of_order_book")
            return
        if book.ts_ms == stamp and digest and book.digest == digest:
            self._emit(kind="book", market=market, outcome=outcome, reason="duplicate_book")
            return
        if book.ts_ms == stamp and digest and book.digest and book.digest != digest:
            book.uncertain = True
            self._emit(kind="book", market=market, outcome=outcome, reason="uncertain_book")
            return
        try:
            if "tick_size" in event and event["tick_size"] not in (None, ""):
                tick = Decimal(str(event["tick_size"]))
                if tick > market.tick_size:
                    market.tick_size = tick
            if "min_order_size" in event and event["min_order_size"] not in (None, ""):
                minimum = Decimal(str(event["min_order_size"]))
                if minimum > market.min_order_size:
                    market.min_order_size = minimum
            bids = _aggregate_levels(event.get("bids") or [], market.tick_size, descending=True)
            asks = _aggregate_levels(event.get("asks") or [], market.tick_size, descending=False)
        except (ValueError, ArithmeticError):
            book.uncertain = True
            self._emit(kind="book", market=market, outcome=outcome, reason="uncertain_book")
            return
        asset = event.get("asset_id")
        expected = market.up_token_id if outcome == "up" else market.down_token_id
        if asset is not None and str(asset) != expected:
            book.uncertain = True
            self._emit(kind="book", market=market, outcome=outcome, reason="token_mismatch")
            return
        book.bids = bids
        book.asks = asks
        book.ts_ms = stamp
        book.digest = digest
        book.uncertain = False
        self._emit(kind="book", market=market, outcome=outcome, reason="ok")

    def _on_trade(self, event: dict) -> None:
        market = self._market(event)
        if market is None:
            return
        trade_id = event.get("trade_id")
        if not trade_id:
            self._emit(kind="trade_ignored", market=market, reason="missing_trade_id")
            return
        trade_id = str(trade_id)
        if trade_id in self.state.seen_trade_ids:
            self._emit(kind="duplicate_trade", market=market, reason="duplicate_trade", detail=trade_id)
            return
        self.state.seen_trade_ids.add(trade_id)
        if self.state.fill_mode != "queue":
            self._emit(kind="trade_ignored", market=market, reason="fill_mode_none", detail=trade_id)
            return
        outcome = str(event.get("outcome") or "")
        if outcome not in {"up", "down"}:
            self._emit(kind="trade_ignored", market=market, reason="bad_outcome")
            return
        if str(event.get("taker_side") or "").upper() != "BUY":
            self._emit(kind="trade_ignored", market=market, outcome=outcome, reason="not_lifting_ask")
            return
        try:
            price = Decimal(str(event["price"]))
            volume = qdown(Decimal(str(event["size"])), Decimal("0.000001"))
            trade_ts = int(event.get("trade_ts", event["ts"]))
        except (KeyError, ValueError, ArithmeticError):
            self._emit(kind="trade_ignored", market=market, reason="bad_trade")
            return
        if volume <= 0:
            self._emit(kind="trade_ignored", market=market, reason="dust")
            return
        resting = []
        for order in self.state.orders.values():
            if order.market_id != market.market_id or order.outcome != outcome:
                continue
            if order.status not in {"live", "cancel_requested"}:
                continue
            if order.live_ts is None or trade_ts < order.live_ts:
                continue
            if price < order.price:
                continue
            if price > order.price and not self.state.trade_through_fills:
                continue
            resting.append(order)
        resting.sort(key=lambda order: (order.submit_ts, order.order_id))
        if not resting:
            self._emit(
                kind="trade_ignored",
                market=market,
                outcome=outcome,
                price=price,
                size=volume,
                reason="no_resting_order",
            )
            return
        for order in resting:
            if volume <= 0:
                break
            if order.queue_ahead > 0:
                used = order.queue_ahead if order.queue_ahead < volume else volume
                order.queue_ahead -= used
                volume -= used
                self._emit(
                    kind="queue_consume",
                    market=market,
                    order=order,
                    size=used,
                    reason="queue_ahead",
                )
            if volume <= 0 or order.remaining <= 0:
                continue
            fill = order.remaining if order.remaining < volume else volume
            fill = qdown(fill, Decimal("0.000001"))
            if fill <= 0:
                continue
            self._apply_fill(market, order, fill, synthetic=False)
            volume -= fill

    def _on_adverse_fill(self, event: dict) -> None:
        if event.get("label") != "synthetic":
            self._emit(kind="reject", reason="adverse_fill_not_labeled")
            return
        order = self.state.orders.get(str(event.get("order_id")))
        if order is None or order.status not in WORKING_ORDER_STATUSES:
            self._emit(kind="reject", reason="unknown_order")
            return
        market = self.state.markets[order.market_id]
        if not market.synthetic:
            self._emit(kind="reject", market=market, order=order, reason="adverse_fill_on_live_market")
            return
        try:
            size = qdown(Decimal(str(event["size"])), Decimal("0.000001"))
        except (KeyError, ValueError, ArithmeticError):
            self._emit(kind="reject", market=market, order=order, reason="bad_size")
            return
        if size <= 0 or size > order.remaining:
            self._emit(kind="reject", market=market, order=order, size=size, reason="no_short")
            return
        self._apply_fill(market, order, size, synthetic=True)

    def _apply_fill(self, market: Market, order: Order, size: Decimal, *, synthetic: bool) -> None:
        proceeds = net_maker_proceeds(size, order.price, self.state.maker_fee_rate)
        self.state.cash += proceeds
        market.net_cash_flow += proceeds
        if order.outcome == "up":
            market.up -= size
        else:
            market.down -= size
        order.remaining -= size
        if order.remaining <= 0:
            order.remaining = ZERO
            order.status = "filled"
        if ZERO < order.price < ONE:
            raw = (
                size
                * self.state.published_taker_fee_rate
                * order.price
                * (ONE - order.price)
                * self.state.rebate_share
            )
            self.state.rebate_estimate += qdown(raw, Decimal("0.00000001"))
        market.last_action_ts = self.state.now_ms
        self._emit(
            kind="fill",
            market=market,
            order=order,
            size=size,
            price=order.price,
            reason="synthetic_adverse_fill" if synthetic else "queue_model_fill",
        )

    def _on_resolution(self, event: dict) -> None:
        market = self._market(event)
        if market is None:
            return
        winner = _winner_from_resolution(market, event)
        if winner is None:
            self._emit(kind="resolution_rejected", market=market, reason=str(event.get("_reject_reason") or "unresolved_outcome"))
            return
        if market.status == "redeemed":
            reason = "idempotent" if market.winner == winner else "resolution_conflict_after_redeem"
            self._emit(kind="resolution", market=market, reason=reason, detail=winner or "")
            return
        if market.resolution_conflict:
            self._emit(kind="resolution_rejected", market=market, reason="resolution_conflict")
            return
        if market.winner and market.winner != winner:
            market.resolution_conflict = True
            market.winner = None
            if market.status == "resolved":
                market.status = "expired_unresolved"
            self._cancel_market(market, "resolution_conflict")
            self._emit(kind="resolution_rejected", market=market, reason="resolution_conflict")
            return
        if market.status == "redeemed":
            self._emit(kind="resolution", market=market, reason="idempotent", detail=winner)
            return
        market.winner = winner
        market.status = "resolved"
        market.resolution_source = str(event.get("source"))
        market.resolution_ts_ms = self.state.now_ms
        self._emit(kind="resolution", market=market, reason=str(event.get("source")), detail=winner)
        self._cancel_market(market, "resolved")
        self._try_redeem(market)

    def _on_split(self, event: dict) -> None:
        market = self._market(event)
        if market is None:
            return
        try:
            amount = qdown(Decimal(str(event["amount"])), Decimal("0.000001"))
        except (KeyError, ValueError, ArithmeticError):
            self._emit(kind="reject", market=market, reason="bad_size")
            return
        self._split(market, amount, "manual_split")

    def _on_merge(self, event: dict) -> None:
        market = self._market(event)
        if market is None:
            return
        try:
            amount = qdown(Decimal(str(event["amount"])), Decimal("0.000001"))
        except (KeyError, ValueError, ArithmeticError):
            self._emit(kind="reject", market=market, reason="bad_size")
            return
        self._merge(market, amount, "manual_merge")

    def _on_cancel(self, event: dict) -> None:
        order_id = event.get("order_id")
        if order_id:
            order = self.state.orders.get(str(order_id))
            if order is None:
                self._emit(kind="reject", reason="unknown_order")
                return
            market = self.state.markets[order.market_id]
            self._request_cancel(market, order, "manual_cancel")
            return
        market = self._market(event)
        if market is None:
            return
        outcome = event.get("outcome")
        self._cancel_market(market, "manual_cancel", outcome=str(outcome) if outcome else None)

    def _decide(self, market: Market) -> None:
        if market.status == "redeemed":
            return
        self._refresh_expiry(market)
        if market.status == "resolved":
            self._cancel_market(market, "resolved")
            self._try_redeem(market)
            return
        if market.status == "expired_unresolved":
            self._cancel_market(market, "expired_unresolved")
            self._merge_free(market, "expired_unresolved")
            self._skip(market, "expired_unresolved")
            return
        if not self._feeds_ok(market):
            reason = "disconnected" if not self.state.feed_up else "stale_feed"
            self._cancel_market(market, reason)
            self._skip(market, reason)
            return
        if self._in_stop_window(market):
            self._cancel_market(market, "near_expiry")
            self._merge_free(market, "near_expiry")
            self._skip(market, "near_expiry")
            return
        if not market.accepting_orders:
            self._cancel_market(market, "not_accepting")
            self._skip(market, "not_accepting")
            return
        if self._in_cooldown(market):
            self._skip(market, "cooldown")
            return
        if self._quotes_possible(market) and not self.at_cap():
            self._auto_split(market)
        proposals = [propose(self.state, market, "up", self.config), propose(self.state, market, "down", self.config)]
        proposals.sort(key=lambda item: (0 if item.repair else 1, item.outcome))
        self._sync_working(market, proposals)
        if any(item.reason == "ok" for item in proposals):
            self._place_proposals(market, proposals)
        elif self.config.merge_when_idle and not self._working(market):
            self._merge_free(market, "idle_no_edge")
        for item in proposals:
            if item.reason != "ok":
                self._skip(market, item.reason, item.outcome)

    def _quotes_possible(self, market: Market) -> bool:
        for outcome in ("up", "down"):
            item = propose(self.state, market, outcome, self.config)
            if item.price > 0 and item.reason in SIZE_REASONS:
                return True
        return False

    def _auto_split(self, market: Market) -> None:
        if not self.config.auto_split or self.at_cap():
            return
        free_up = market.up - reserved(self.state, market, "up")
        free_down = market.down - reserved(self.state, market, "down")
        paired = free_up if free_up < free_down else free_down
        if paired >= self.config.min_pair_inventory:
            return
        room = self.state.per_market_budget - market.gross_split
        amount = self.config.split_clip
        if room < amount:
            amount = room
        if self.state.cash < amount:
            amount = self.state.cash
        amount = qdown(amount, self.config.order_size_quantum)
        minimum = market.min_order_size if market.min_order_size > self.config.min_order_size else self.config.min_order_size
        if amount < minimum:
            reason = "per_market_budget" if room < minimum else "insufficient_cash"
            self._skip(market, reason)
            return
        self._split(market, amount, "auto_split")

    def _sync_working(self, market: Market, proposals: list[Proposal]) -> None:
        by_outcome = {item.outcome: item for item in proposals}
        for order in list(self.state.orders.values()):
            if order.market_id != market.market_id or order.status not in WORKING_ORDER_STATUSES:
                continue
            proposal = by_outcome.get(order.outcome)
            if proposal is None or proposal.price <= 0 or proposal.price != order.price:
                self._request_cancel(market, order, "requote")

    def _place_proposals(self, market: Market, proposals: list[Proposal]) -> None:
        for proposal in proposals:
            if proposal.reason != "ok":
                continue
            if self._working_outcome(market, proposal.outcome):
                continue
            require_repair = self.at_cap()
            size = self._largest_size(market, proposal, require_repair)
            if size is None:
                free = market.inventory(proposal.outcome) - reserved(self.state, market, proposal.outcome)
                minimum = (
                    market.min_order_size
                    if market.min_order_size > self.config.min_order_size
                    else self.config.min_order_size
                )
                if free < minimum:
                    reason = "insufficient_inventory"
                elif require_repair:
                    reason = "not_risk_reducing"
                else:
                    reason = "risk_floor"
                self._skip(market, reason, proposal.outcome)
                continue
            self._commit_order(market, proposal, size, risk_reducing=require_repair)

    def _largest_size(self, market: Market, proposal: Proposal, require_repair: bool) -> Optional[Decimal]:
        quantum = self.config.order_size_quantum
        minimum = market.min_order_size if market.min_order_size > self.config.min_order_size else self.config.min_order_size
        desired = qdown(proposal.size, quantum)
        if desired < minimum:
            return None
        if self._size_ok(market, proposal.outcome, proposal.price, desired, require_repair):
            return desired
        if not self._size_ok(market, proposal.outcome, proposal.price, minimum, require_repair):
            return None
        span = int((desired - minimum) / quantum)
        lo = 0
        hi = span
        best = minimum
        while lo <= hi:
            mid = (lo + hi) // 2
            size = minimum + quantum * mid
            if self._size_ok(market, proposal.outcome, proposal.price, size, require_repair):
                best = size
                lo = mid + 1
            else:
                hi = mid - 1
        return best

    def _size_ok(
        self,
        market: Market,
        outcome: str,
        price: Decimal,
        size: Decimal,
        require_repair: bool,
    ) -> bool:
        free = market.inventory(outcome) - reserved(self.state, market, outcome)
        if size <= 0 or size > free:
            return False
        virtual = Order(
            order_id="virtual",
            market_id=market.market_id,
            outcome=outcome,
            price=price,
            size=size,
            remaining=size,
            queue_ahead=ZERO,
            status="queued",
            submit_ts=self.state.now_ms,
        )
        projected = worst_case_equity(self.state, extra=[virtual])
        if projected < self.state.loss_floor:
            return False
        if require_repair:
            current = worst_case_equity(self.state)
            if projected < current:
                return False
            self_inv = market.inventory(outcome)
            other = market.down if outcome == "up" else market.up
            old_gap = abs(self_inv - other)
            new_gap = abs(self_inv - size - other)
            if new_gap >= old_gap:
                return False
        return True

    def _commit_order(self, market: Market, proposal: Proposal, size: Decimal, *, risk_reducing: bool) -> None:
        book = market.book(proposal.outcome)
        best_ask = book.best_ask()
        if best_ask is not None and proposal.price < best_ask:
            queue = self.config.improve_queue_shares + self.config.extra_queue_shares
        elif best_ask is not None and proposal.price == best_ask:
            queue = book.ask_size_at(proposal.price) + self.config.extra_queue_shares
        else:
            queue = self.config.extra_queue_shares
        if queue < 0:
            queue = ZERO
        self.state.order_seq += 1
        order = Order(
            order_id=f"{market.market_id}-{proposal.outcome}-{self.state.order_seq}",
            market_id=market.market_id,
            outcome=proposal.outcome,
            price=proposal.price,
            size=size,
            remaining=size,
            queue_ahead=queue,
            status="queued",
            submit_ts=self.state.now_ms,
            risk_reducing=risk_reducing,
        )
        self.state.orders[order.order_id] = order
        market.last_action_ts = self.state.now_ms
        self._skip_once.clear()
        self._emit(
            kind="quote_submit",
            market=market,
            order=order,
            size=size,
            price=proposal.price,
            reason="repair" if proposal.repair else "paired",
            detail=f"fair={money(proposal.fair)} edge={money(proposal.required_edge)}",
        )

    def _split(self, market: Market, amount: Decimal, reason: str) -> bool:
        if market.status != "open" or self._in_stop_window(market) or market.status == "expired_unresolved":
            self._emit(kind="reject", market=market, size=amount, reason="near_expiry")
            return False
        if self.at_cap():
            self._emit(kind="quote_reject", market=market, size=amount, reason="exposure_frozen")
            return False
        if amount <= 0:
            self._emit(kind="reject", market=market, reason="bad_size")
            return False
        if amount > self.state.cash:
            self._emit(kind="reject", market=market, size=amount, reason="insufficient_cash")
            return False
        if market.gross_split + amount > self.state.per_market_budget:
            self._emit(kind="quote_reject", market=market, size=amount, reason="per_market_budget")
            return False
        market.up += amount
        market.down += amount
        projected = worst_case_equity(self.state, cash_delta=-amount)
        market.up -= amount
        market.down -= amount
        if projected < self.state.loss_floor:
            self._emit(kind="quote_reject", market=market, size=amount, reason="risk_floor")
            return False
        self.state.cash -= amount
        market.up += amount
        market.down += amount
        market.gross_split += amount
        market.net_cash_flow -= amount
        self._emit(kind="split", market=market, size=amount, reason=reason)
        return True

    def _merge(self, market: Market, amount: Decimal, reason: str) -> bool:
        if amount <= 0:
            self._emit(kind="reject", market=market, reason="bad_size")
            return False
        free_up = market.up - reserved(self.state, market, "up")
        free_down = market.down - reserved(self.state, market, "down")
        if amount > free_up or amount > free_down:
            self._emit(kind="reject", market=market, size=amount, reason="reserved_inventory")
            return False
        market.up -= amount
        market.down -= amount
        projected = worst_case_equity(self.state, cash_delta=amount)
        market.up += amount
        market.down += amount
        if projected < self.state.loss_floor:
            self._emit(kind="quote_reject", market=market, size=amount, reason="risk_floor")
            return False
        market.up -= amount
        market.down -= amount
        self.state.cash += amount
        market.net_cash_flow += amount
        self._emit(kind="merge", market=market, size=amount, reason=reason)
        return True

    def _merge_free(self, market: Market, reason: str) -> None:
        free_up = market.up - reserved(self.state, market, "up")
        free_down = market.down - reserved(self.state, market, "down")
        amount = free_up if free_up < free_down else free_down
        amount = qdown(amount, Decimal("0.000001"))
        if amount > 0:
            self._merge(market, amount, reason)

    def _try_redeem(self, market: Market) -> bool:
        if market.status != "resolved" or market.winner not in {"up", "down"} or market.resolution_conflict:
            return False
        if self._working(market):
            return False
        payout = market.up if market.winner == "up" else market.down
        payout = qdown(payout, Decimal("0.000001"))
        self.state.cash += payout
        market.net_cash_flow += payout
        market.up = ZERO
        market.down = ZERO
        market.status = "redeemed"
        market.reserve_active = False
        if not market.realized_recorded:
            self.state.realized_pnl += market.net_cash_flow
            market.realized_recorded = True
        self._emit(kind="redeem", market=market, size=payout, reason=market.winner or "")
        return True

    def _refresh_expiry(self, market: Market) -> None:
        if market.status == "open" and self.state.now_ms >= market.end_ts_ms:
            market.status = "expired_unresolved"
            self._emit(kind="expiry", market=market, reason="expired_unresolved")

    def _cancel_market(self, market: Market, reason: str, outcome: Optional[str] = None) -> None:
        for order in list(self.state.orders.values()):
            if order.market_id != market.market_id or order.status not in WORKING_ORDER_STATUSES:
                continue
            if outcome and order.outcome != outcome:
                continue
            self._request_cancel(market, order, reason)

    def _request_cancel(self, market: Market, order: Order, reason: str) -> None:
        if order.status not in WORKING_ORDER_STATUSES or order.status == "cancel_requested":
            return
        order.status = "cancel_requested"
        order.cancel_request_ts = self.state.now_ms
        if self.state.feed_up:
            order.cancel_ready_ts = self.state.now_ms + self.state.cancel_latency_ms
        else:
            order.cancel_ready_ts = None
        self._emit(kind="cancel_request", market=market, order=order, size=order.remaining, reason=reason)

    def _working(self, market: Market, outcome: Optional[str] = None) -> list[Order]:
        return [
            order
            for order in self.state.orders.values()
            if order.market_id == market.market_id
            and order.status in WORKING_ORDER_STATUSES
            and (outcome is None or order.outcome == outcome)
        ]

    def _working_outcome(self, market: Market, outcome: str) -> bool:
        return bool(self._working(market, outcome))

    def _feeds_ok(self, market: Market) -> bool:
        return self.state.feed_up and self._book_fresh(market, "up") and self._book_fresh(market, "down")

    def _book_fresh(self, market: Market, outcome: str) -> bool:
        book = market.book(outcome)
        if book.uncertain or book.ts_ms < 0:
            return False
        age = self.state.now_ms - book.ts_ms
        if age < 0:
            return -age <= self.config.clock_skew_ms
        return age <= self.config.stale_after_ms

    def _in_stop_window(self, market: Market) -> bool:
        return self.state.now_ms >= market.end_ts_ms - self.config.stop_quoting_before_expiry_ms

    def _in_cooldown(self, market: Market) -> bool:
        return self.state.now_ms < market.last_action_ts + self.config.quote_cooldown_ms


def _jsonable(value):
    if isinstance(value, Decimal):
        return money(value)
    return value


def _aggregate_levels(raw, tick: Decimal, *, descending: bool) -> list[tuple[Decimal, Decimal]]:
    grouped: dict[Decimal, Decimal] = {}
    if not isinstance(raw, list):
        raise ValueError("levels")
    for item in raw:
        if isinstance(item, dict):
            price = Decimal(str(item["price"]))
            size = Decimal(str(item["size"]))
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            price = Decimal(str(item[0]))
            size = Decimal(str(item[1]))
        else:
            raise ValueError("level")
        size = qdown(size, Decimal("0.000001"))
        if size <= 0 or price <= 0 or price >= 1:
            continue
        if not on_grid(price, tick):
            continue
        grouped[price] = grouped.get(price, ZERO) + size
    ordered = sorted(grouped.items(), key=lambda pair: pair[0], reverse=descending)
    return ordered


def _market_from_event(event: dict, config: Config) -> Market:
    tick = Decimal(str(event.get("tick_size") or config.default_tick))
    minimum = Decimal(str(event.get("min_order_size") or config.min_order_size))
    if minimum < config.min_order_size:
        minimum = config.min_order_size
    market = Market(
        market_id=str(event["market_id"]),
        condition_id=str(event["condition_id"]),
        slug=str(event.get("slug") or ""),
        question=str(event.get("question") or ""),
        up_token_id=str(event["up_token_id"]),
        down_token_id=str(event["down_token_id"]),
        up_label=str(event.get("up_label") or "Up"),
        down_label=str(event.get("down_label") or "Down"),
        up_index=int(event.get("up_index", 0)),
        down_index=int(event.get("down_index", 1)),
        end_ts_ms=int(event["end_ts"]),
        tick_size=tick,
        min_order_size=minimum,
        synthetic=bool(event.get("synthetic", False)),
        accepting_orders=bool(event.get("accepting_orders", True)),
        up_book=LevelBook(),
        down_book=LevelBook(),
    )
    if market.up_token_id == market.down_token_id:
        raise ValueError("duplicate token ids")
    if {market.up_label.casefold(), market.down_label.casefold()} != {"up", "down"}:
        raise ValueError("outcomes must be Up and Down")
    if market.up_index == market.down_index:
        raise ValueError("outcome indexes collide")
    return market


def _winner_from_resolution(market: Market, event: dict) -> Optional[str]:
    source = str(event.get("source") or "")
    event["_reject_reason"] = "unresolved_outcome"
    if source == "fixture":
        if not market.synthetic or event.get("label") != "synthetic-fixture":
            event["_reject_reason"] = "fixture_not_allowed"
            return None
        winner = str(event.get("winner") or "")
        if winner not in {"up", "down"}:
            return None
        return winner
    if source == "data-api-v2":
        if str(event.get("status") or "").lower() != "resolved":
            return None
        market_type = event.get("market_type")
        if market_type and str(market_type).upper() != "BINARY":
            event["_reject_reason"] = "not_binary"
            return None
        payouts = event.get("payouts")
        if payouts == [1_000_000, 0]:
            index = 0
        elif payouts == [0, 1_000_000]:
            index = 1
        else:
            return None
        if index == market.up_index:
            return "up"
        if index == market.down_index:
            return "down"
        event["_reject_reason"] = "outcome_index_mismatch"
        return None
    if source == "clob-market-resolved":
        asset = str(event.get("winning_asset_id") or "")
        label = str(event.get("winning_outcome") or "")
        if asset == market.up_token_id and label.casefold() == market.up_label.casefold():
            return "up"
        if asset == market.down_token_id and label.casefold() == market.down_label.casefold():
            return "down"
        event["_reject_reason"] = "outcome_mapping_conflict"
        return None
    event["_reject_reason"] = "untrusted_resolution_source"
    return None
