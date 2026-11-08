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
from paper_split_maker.decimal_utils import ONE, ZERO, ceil_tick, money, on_grid, qdown
from paper_split_maker.logs import LogWriter
from paper_split_maker.models import (
    WORKING_ORDER_STATUSES,
    LevelBook,
    Market,
    Order,
    State,
)
from paper_split_maker.quotes import (
    Proposal,
    binding_sell_floor,
    ladder_prices,
    propose,
)
from paper_split_maker.risk import (
    assert_ledger_invariants,
    best_case_equity,
    conservative_fee,
    net_maker_proceeds,
    reserved,
    session_loss,
    worst_case_equity,
)
from paper_split_maker.store import load_state, save_state

SIZE_REASONS = frozenset({"ok", "below_min_size", "insufficient_inventory"})



RECONCILE_GRACE_MS = 15000
CANCEL_CONFIRM_MS = 3000
REJECT_PAUSE_MS = 5000


def is_post_only_cross(message: str) -> bool:
    text = str(message or "").lower()
    return (
        "post-only" in text
        or "post only" in text
        or         "would cross" in text
        or "crosses book" in text
        or "crosses the book" in text
    )

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
        # The open-orders endpoint lags a just-posted order by a few seconds.
        # Remember when each disagreement was first seen so only a persistent
        # one stops the bot.
        self._reconcile_first_seen: dict[str, int] = {}
        self._ignored_remote: set[str] = set()
        self.stray_remote_cancels: list[str] = []
        # After a post-only cross, the next sell on that outcome is one tick
        # less aggressive until the book no longer requires it.
        self.sell_floors: dict[tuple[str, str], Decimal] = {}
        # Recent mids per outcome, used as a short-window volatility estimate.
        self.mid_samples: dict[tuple[str, str], list[tuple[int, Decimal]]] = {}
        self._skip_once: set[tuple[str, str, str]] = set()
        self.mode = "library"
        self.chain_outbox: list[dict] = []
        self.buy_outbox: list[dict] = []
        self.marketable_ids: set[str] = set()
        self.leg_since: dict[str, int] = {}
        self.leg_cross: set[tuple[str, str]] = set()
        self._leg_log_key: dict[str, tuple] = {}
        self.buy_fail_outcome: str = ""
        # Long replays flush the ledger once at the end. Live paper mode keeps
        # the default and writes state after every event.
        self.persist_each_event = True

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
        # A restart may tighten these caps. It may not remove or raise them.
        if state.session_max_loss > 0 and (
            config.session_max_loss == 0
            or config.session_max_loss > state.session_max_loss
        ):
            pass
        else:
            state.session_max_loss = config.session_max_loss
        if config.max_pair_loss < state.max_pair_loss:
            state.max_pair_loss = config.max_pair_loss
        config.max_pair_loss = state.max_pair_loss
        config.session_max_loss = state.session_max_loss
        engine = cls(state, config, state_path, log_dir, resumed=True)
        engine.assert_invariants()
        return engine

    def assert_invariants(self) -> None:
        assert_ledger_invariants(self.state)

    def at_cap(self) -> bool:
        return (
            worst_case_equity(self.state)
            <= self.state.loss_floor + self.config.freeze_band
        )

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
                self._emit(
                    kind="duplicate_event", reason="duplicate_event", detail=event_id
                )
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
                self._emit(
                    kind="out_of_order_clock",
                    reason="out_of_order_clock",
                    detail=str(stamp),
                )
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
            "reference": self._on_reference,
            "user_fill": self._on_user_fill,
            "reconcile": self._on_reconcile,
            "kill": self._on_kill,
            "split_pending": self._on_split_pending,
            "split_confirmed": self._on_split_confirmed,
            "split_abandoned": self._on_split_abandoned,
            "taker_repair": self._on_taker_repair,
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

        return build_summary(
            self.state, mode=self.mode, resumed=self.resumed, notes=notes
        )

    def _finish(self) -> None:
        if self.writer is not None:
            self.writer.write(self._batch)
        if self.state_path is not None and self.persist_each_event:
            save_state(self.state_path, self.state)

    def flush(self) -> None:
        if self.state_path is not None:
            save_state(self.state_path, self.state)

    def _emit(
        self,
        kind: str,
        market: Optional[Market] = None,
        order: Optional[Order] = None,
        **fields,
    ) -> None:
        price = fields.pop("price", None)
        size = fields.pop("size", None)
        outcome = fields.pop("outcome", None)
        if order is not None:
            price = order.price if price is None else price
            outcome = order.outcome if outcome is None else outcome
        row = {
            "ts": self.state.now_ms,
            "kind": kind,
            "market_id": market.market_id
            if market is not None
            else fields.pop("market_id", ""),
            "order_id": order.order_id
            if order is not None
            else fields.pop("order_id", ""),
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
                    {
                        "ts": self.state.now_ms,
                        "reason": row["reason"],
                        "market_id": row["market_id"],
                    }
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
            if (
                order.status == "queued"
                and self.state.now_ms >= order.submit_ts + self.state.ack_latency_ms
            ):
                order.status = "live"
                order.live_ts = order.submit_ts + self.state.ack_latency_ms
                market = self.state.markets[order.market_id]
                self._emit(
                    kind="order_live", market=market, order=order, size=order.remaining
                )
            if (
                order.status == "cancel_requested"
                and not self.config.live
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
        self._check_realized_loss()
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
                    order.cancel_ready_ts = (
                        self.state.now_ms + self.state.cancel_latency_ms
                    )
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
            self._emit(
                kind="reject", market=existing, reason="market_identity_conflict"
            )
            return
        # A finished flat session, including one whose merge is still on chain,
        # does not hold a slot. An open window we have not traded yet does.
        open_count = sum(
            1
            for item in self.state.markets.values()
            if self._holds_slot(item)
        )
        if open_count >= self.config.max_open_markets:
            self._emit(
                kind="reject", reason="max_open_markets", detail=market.market_id
            )
            return
        for item in self.state.markets.values():
            finished = item.status in ("expired_unresolved", "resolved")
            if finished or self._idle_flat(item):
                # A finished or flat session must not keep a reserve that blocks the next one.
                item.reserve_active = False
        market.reserve_active = True
        market.operational_reserve_amount = self.state.operational_reserve
        self.state.markets[market.market_id] = market
        # Ended markets with no working orders no longer need a reserve.
        if worst_case_equity(self.state) < self.state.loss_floor:
            del self.state.markets[market.market_id]
            self._emit(
                kind="quote_reject", reason="risk_floor", detail=market.market_id
            )
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
            self._emit(
                kind="book", market=market, outcome=outcome, reason="uncertain_book"
            )
            return
        book = market.book(outcome)
        digest = str(event.get("hash") or "")
        if book.ts_ms >= 0 and stamp < book.ts_ms:
            self._emit(
                kind="book", market=market, outcome=outcome, reason="out_of_order_book"
            )
            return
        if book.ts_ms == stamp and digest and book.digest == digest:
            # A repeated snapshot is still a message. Quiet books stay fresh.
            book.received_ms = self.state.now_ms
            self._emit(
                kind="book", market=market, outcome=outcome, reason="duplicate_book"
            )
            return
        if book.ts_ms == stamp and digest and book.digest and book.digest != digest:
            book.uncertain = True
            self._emit(
                kind="book", market=market, outcome=outcome, reason="uncertain_book"
            )
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
            bids = _aggregate_levels(
                event.get("bids") or [], market.tick_size, descending=True
            )
            asks = _aggregate_levels(
                event.get("asks") or [], market.tick_size, descending=False
            )
        except (ValueError, ArithmeticError):
            book.uncertain = True
            self._emit(
                kind="book", market=market, outcome=outcome, reason="uncertain_book"
            )
            return
        asset = event.get("asset_id")
        expected = market.up_token_id if outcome == "up" else market.down_token_id
        if asset is not None and str(asset) != expected:
            book.uncertain = True
            self._emit(
                kind="book", market=market, outcome=outcome, reason="token_mismatch"
            )
            return
        book.bids = bids
        book.asks = asks
        book.ts_ms = stamp
        book.digest = digest
        book.uncertain = False
        book.received_ms = self.state.now_ms
        self._record_mid(market, outcome)
        self._note_volatility(market)
        self._emit(kind="book", market=market, outcome=outcome, reason="ok")
        # Reprice when the leftover leg's own book moves. The other side's
        # book does not change this bid.
        naked = self._naked_leg(market)
        if naked is not None and naked[0] == outcome:
            self._complete_leg(market)

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
            self._emit(
                kind="duplicate_trade",
                market=market,
                reason="duplicate_trade",
                detail=trade_id,
            )
            return
        self.state.seen_trade_ids.add(trade_id)
        if self.mode == "live":
            # Live fills come only from the exchange (user_fill).
            self._emit(
                kind="trade_ignored",
                market=market,
                reason="live_mode_no_model_fill",
                detail=trade_id,
            )
            return
        if self.state.fill_mode != "queue":
            self._emit(
                kind="trade_ignored",
                market=market,
                reason="fill_mode_none",
                detail=trade_id,
            )
            return
        outcome = str(event.get("outcome") or "")
        if outcome not in {"up", "down"}:
            self._emit(kind="trade_ignored", market=market, reason="bad_outcome")
            return
        if str(event.get("taker_side") or "").upper() != "BUY":
            self._emit(
                kind="trade_ignored",
                market=market,
                outcome=outcome,
                reason="not_lifting_ask",
            )
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
            self._emit(
                kind="reject",
                market=market,
                order=order,
                reason="adverse_fill_on_live_market",
            )
            return
        try:
            size = qdown(Decimal(str(event["size"])), Decimal("0.000001"))
        except (KeyError, ValueError, ArithmeticError):
            self._emit(kind="reject", market=market, order=order, reason="bad_size")
            return
        if size <= 0 or size > order.remaining:
            self._emit(
                kind="reject", market=market, order=order, size=size, reason="no_short"
            )
            return
        self._apply_fill(market, order, size, synthetic=True)

    def _apply_fill(
        self,
        market: Market,
        order: Order,
        size: Decimal,
        *,
        synthetic: bool,
        source: str = "model",
    ) -> None:
        proceeds = net_maker_proceeds(size, order.price, self.state.maker_fee_rate)
        self.state.cash += proceeds
        market.net_cash_flow += proceeds
        if order.outcome == "up":
            market.up -= size
            market.sold_up_shares += size
            market.sold_up_proceeds += proceeds
        else:
            market.down -= size
            market.sold_down_shares += size
            market.sold_down_proceeds += proceeds
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
        self._note_leg_gap(market)
        self._emit(
            kind="fill",
            market=market,
            order=order,
            size=size,
            price=order.price,
            reason=(
                "synthetic_adverse_fill"
                if synthetic
                else ("exchange_fill" if source == "exchange" else "queue_model_fill")
            ),
        )

    def _on_resolution(self, event: dict) -> None:
        market = self._market(event)
        if market is None:
            return
        winner = _winner_from_resolution(market, event)
        if winner is None:
            self._emit(
                kind="resolution_rejected",
                market=market,
                reason=str(event.get("_reject_reason") or "unresolved_outcome"),
            )
            return
        if market.status == "redeemed":
            reason = (
                "idempotent"
                if market.winner == winner
                else "resolution_conflict_after_redeem"
            )
            self._emit(
                kind="resolution", market=market, reason=reason, detail=winner or ""
            )
            return
        if market.resolution_conflict:
            self._emit(
                kind="resolution_rejected", market=market, reason="resolution_conflict"
            )
            return
        if market.winner and market.winner != winner:
            market.resolution_conflict = True
            market.winner = None
            if market.status == "resolved":
                market.status = "expired_unresolved"
            self._cancel_market(market, "resolution_conflict")
            self._emit(
                kind="resolution_rejected", market=market, reason="resolution_conflict"
            )
            return
        if market.status == "redeemed":
            self._emit(
                kind="resolution", market=market, reason="idempotent", detail=winner
            )
            return
        market.winner = winner
        market.status = "resolved"
        market.resolution_source = str(event.get("source"))
        market.resolution_ts_ms = self.state.now_ms
        self._emit(
            kind="resolution",
            market=market,
            reason=str(event.get("source")),
            detail=winner,
        )
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
        self._cancel_market(
            market, "manual_cancel", outcome=str(outcome) if outcome else None
        )

    def _decide(self, market: Market) -> None:
        if market.status == "redeemed":
            return
        self._check_kill_switch()
        self._check_realized_loss()
        if self.state.killed:
            # Still sell a naked leg. Then merge whatever pair is left.
            if not self._complete_leg(market):
                self._cancel_market(market, "killed")
                self._merge_free(market, "stop")
                self._skip(market, "killed")
            return
        # A reconcile disagreement is handled on that market inside
        # ``_on_reconcile``. It must not flatten the account or end the run.
        if self.state.mismatch_stop:
            self.state.mismatch_stop = False
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
        if market.session_stopped or self._session_capped(market):
            self._cancel_market(market, "session_max_loss")
            self._merge_free(market, "session_max_loss")
            market.session_stopped = True
            self._skip(market, "session_max_loss")
            return
        # The leftover leg is managed through the close window. A stop-window
        # cancel must not pull it before it can be sold at breakeven.
        if self._complete_leg(market):
            return
        if self._in_stop_window(market):
            self._cancel_market(market, "near_expiry")
            self._merge_free(market, "near_expiry")
            self._skip(market, "near_expiry")
            return
        if self.state.now_ms < market.pause_until_ms:
            self._cancel_market(market, "vol_pause")
            self._skip(market, "vol_pause")
            return
        if not market.accepting_orders:
            self._cancel_market(market, "not_accepting")
            self._skip(market, "not_accepting")
            return
        if self.config.entry_mode == "buy_pair_arb":
            if not self.at_cap() and not self.state.killed:
                self._try_buy_pair(market)
            return
        if self._in_cooldown(market):
            self._skip(market, "cooldown")
            return
        if self._quotes_possible(market) and not self.at_cap():
            self._auto_split(market)
        proposals = [
            self._ease_for_close(market, propose(self.state, market, "up", self.config)),
            self._ease_for_close(
                market, propose(self.state, market, "down", self.config)
            ),
        ]
        proposals.sort(key=lambda item: (0 if item.repair else 1, item.outcome))
        for item in proposals:
            if item.reason == "pair_floor":
                self._note_pair_block(market, item.outcome, item.price)
        self._sync_working(market, proposals)
        if (
            self._in_close_improve(market)
            and any(item.reason == "pair_floor" for item in proposals)
            and not any(item.reason == "ok" for item in proposals)
            and not self._working(market)
        ):
            # The floor cannot be met. Merge the pair and hold the remainder
            # for resolution instead of cutting the ask down to the bid.
            self._merge_free(market, "pair_floor")
        if any(item.reason == "ok" for item in proposals):
            self._place_proposals(market, proposals)
        elif self.config.merge_when_idle and not self._working(market):
            if self._wait_for_sell(market):
                # The pair stays until a side is expensive enough to sell
                # under the loss floor, or the session reaches its stop window.
                self._skip(market, "waiting_for_price")
            else:
                self._merge_free(market, "idle_no_edge")
        for item in proposals:
            if item.reason != "ok":
                self._skip(market, item.reason, item.outcome)

    def _wait_for_sell(self, market: Market) -> bool:
        """True when a live split should be held for a later, floor-legal sell."""
        if self.mode != "live":
            return False
        if market.gross_split <= 0 or market.status != "open":
            return False
        if self._in_stop_window(market):
            return False
        return market.up > 0 or market.down > 0

    def retire_finished(self) -> list[str]:
        """Remove markets that are finished, flat, and have no working orders.

        Leaving them tracked blocks discovery of the next session and keeps
        their dead books in the poll. Cash and realized PnL stay on the ledger.
        Terminal orders leave with the market so every remaining order still
        points at a market.
        """
        removed: list[str] = []
        for market in list(self.state.markets.values()):
            if market.status == "open" or market.pending_split > 0:
                continue
            if self._working(market) or market.up > 0 or market.down > 0:
                continue
            for order_id, order in list(self.state.orders.items()):
                if order.market_id == market.market_id:
                    del self.state.orders[order_id]
            del self.state.markets[market.market_id]
            removed.append(market.market_id)
        if removed:
            self.assert_invariants()
        return removed

    def note_order_posted(
        self,
        market: Market,
        *,
        exchange_id: str,
        outcome: str,
        price: Decimal,
        size: Decimal,
        internal_id: str,
    ) -> None:
        """Persist an exchange ack. Callable outside ``handle_event``."""
        pending = list(self._batch)
        self._batch = []
        self._emit(
            kind="order_posted",
            market=market,
            order_id=str(exchange_id),
            outcome=outcome,
            price=price,
            size=size,
            reason="posted",
            detail=str(internal_id),
        )
        self._finish()
        self._batch = pending

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
        minimum = (
            market.min_order_size
            if market.min_order_size > self.config.min_order_size
            else self.config.min_order_size
        )
        if amount < minimum:
            reason = "per_market_budget" if room < minimum else "insufficient_cash"
            self._skip(market, reason)
            return
        self._split(market, amount, "auto_split")

    def _sync_working(self, market: Market, proposals: list[Proposal]) -> None:
        desired: dict[str, list[Decimal]] = {}
        sized: dict[str, Decimal] = {}
        for proposal in proposals:
            sized[proposal.outcome] = proposal.size
            if proposal.price <= 0 or proposal.reason != "ok":
                desired[proposal.outcome] = []
                continue
            bid = market.book(proposal.outcome).best_bid()
            if self.config.slices_per_side <= 1:
                desired[proposal.outcome] = [proposal.price]
            else:
                desired[proposal.outcome] = ladder_prices(
                    proposal.price,
                    market.tick_size,
                    self.config.slices_per_side,
                    bid,
                    inside=self.config.slice_inside_tick,
                )
        rest_ms = int(self.config.min_order_rest_seconds) * 1000
        closing = self._in_close_improve(market)
        for order in list(self.state.orders.values()):
            if (
                order.market_id != market.market_id
                or order.status not in WORKING_ORDER_STATUSES
            ):
                continue
            floor = binding_sell_floor(
                self.state,
                market,
                order.outcome,
                self.config,
                order.remaining,
                order.price,
            )
            if floor is not None and order.price < floor:
                self._request_cancel(market, order, "pair_floor")
                continue
            start = order.live_ts if order.live_ts is not None else order.submit_ts
            if self.state.now_ms - start < rest_ms:
                # Leave a resting sell alone until it has had time to fill.
                continue
            prices = desired.get(order.outcome, [])
            if prices:
                diff = min(abs(order.price - item) for item in prices)
                band = self._requote_band(market, order.outcome)
                price_move = diff > band
                # The close window intentionally walks the ask down by a tick.
                if closing and diff >= market.tick_size:
                    price_move = True
                wanted = sized.get(order.outcome, order.remaining)
                size_move = (
                    abs(order.remaining - wanted) > self.config.requote_size_threshold
                )
                if not price_move and not size_move:
                    continue
                if not self._requote_allowed(market):
                    continue
            self._request_cancel(market, order, "requote")
            market.last_requote_ts = self.state.now_ms

    def _requote_allowed(self, market: Market) -> bool:
        return (
            self.state.now_ms
            >= market.last_requote_ts + self.config.requote_min_interval_ms
        )

    def _rested_ms(self, order: Order) -> int:
        start = order.live_ts if order.live_ts is not None else order.submit_ts
        return max(0, self.state.now_ms - int(start))

    def _record_mid(self, market: Market, outcome: str) -> None:
        book = market.book(outcome)
        bid, ask = book.best_bid(), book.best_ask()
        if bid is None or ask is None or ask <= bid:
            return
        window = int(self.config.requote_vol_window_ms) or 30_000
        samples = self.mid_samples.setdefault((market.market_id, outcome), [])
        samples.append((self.state.now_ms, (bid + ask) / 2))
        cutoff = self.state.now_ms - window
        drop = 0
        for ts, _mid in samples:
            if ts < cutoff:
                drop += 1
            else:
                break
        if drop:
            del samples[:drop]
        if len(samples) > 240:
            del samples[: len(samples) - 240]

    def _mid_stdev(self, market: Market, outcome: str) -> Decimal:
        window = int(self.config.requote_vol_window_ms)
        if window <= 0:
            return ZERO
        cutoff = self.state.now_ms - window
        vals = [
            px
            for ts, px in self.mid_samples.get((market.market_id, outcome), [])
            if ts >= cutoff
        ]
        count = len(vals)
        if count < 2:
            return ZERO
        mean = sum(vals, ZERO) / Decimal(count)
        var = sum((item - mean) ** 2 for item in vals) / Decimal(count)
        if var <= 0:
            return ZERO
        return var.sqrt()

    def _requote_band(self, market: Market, outcome: str) -> Decimal:
        """Replace only when the target moved more than this far."""
        band = market.tick_size * 2
        if self.config.requote_vol_k > 0:
            widened = self.config.requote_vol_k * self._mid_stdev(market, outcome)
            if widened > band:
                band = widened
        return band

    def _preclose_ms(self, market: Market) -> int:
        """When this session's quotes are pulled for the close or the next split."""
        stop = market.end_ts_ms - int(self.config.stop_quoting_before_expiry_ms)
        lead = int(self.config.pre_split_lead_ms)
        if lead > 0:
            lead_stop = market.end_ts_ms - lead
            if lead_stop < stop:
                stop = lead_stop
        return stop

    def _in_close_improve(self, market: Market) -> bool:
        seconds = int(self.config.close_improve_seconds)
        if seconds <= 0:
            return False
        deadline = self._preclose_ms(market)
        start = deadline - seconds * 1000
        return start <= self.state.now_ms < deadline

    def _min_sell_price(
        self, market: Market, outcome: str, size: Decimal, bid: Decimal
    ) -> Optional[Decimal]:
        """Lowest tick that still clears the loss floor for ``size``."""
        tick = market.tick_size
        if tick <= 0 or size <= 0:
            return None
        low = ceil_tick(max(self.config.min_price, bid + tick), tick)
        floor = binding_sell_floor(
            self.state, market, outcome, self.config, size
        )
        if floor is not None:
            paired = ceil_tick(floor, tick)
            if paired > low:
                low = paired
        high = qdown(self.config.max_price, tick)
        if high < low or not self._size_ok(market, outcome, high, size, False):
            return None
        steps = int((high - low) / tick)
        left = 0
        right = steps
        best = steps
        while left <= right:
            mid = (left + right) // 2
            px = low + tick * mid
            if self._size_ok(market, outcome, px, size, False):
                best = mid
                right = mid - 1
            else:
                left = mid + 1
        return low + tick * best

    def _ease_for_close(self, market: Market, proposal: Proposal) -> Proposal:
        """Walk a sell toward the bid near the close, never through the loss floor."""
        if (
            proposal.reason != "ok"
            or proposal.price <= 0
            or not self._in_close_improve(market)
        ):
            return proposal
        bid = market.book(proposal.outcome).best_bid()
        if bid is None:
            return proposal
        tick = market.tick_size
        aggressive = bid + tick
        edge = binding_sell_floor(
            self.state,
            market,
            proposal.outcome,
            self.config,
            proposal.size,
            proposal.price,
        )
        if edge is not None:
            edged = ceil_tick(edge, tick)
            if edged > aggressive:
                aggressive = edged
        if proposal.price < aggressive:
            return Proposal(
                outcome=proposal.outcome,
                price=ZERO,
                size=ZERO,
                fair=proposal.fair,
                required_edge=proposal.required_edge,
                repair=proposal.repair,
                reason="pair_floor",
            )
        if aggressive >= proposal.price:
            return proposal
        deadline = self._preclose_ms(market)
        window_ms = int(self.config.close_improve_seconds) * 1000
        if window_ms <= 0:
            return proposal
        elapsed = self.state.now_ms - (deadline - window_ms)
        if elapsed < 0:
            return proposal
        if elapsed > window_ms:
            elapsed = window_ms
        progress = Decimal(elapsed) / Decimal(window_ms)
        stepped = ceil_tick(
            proposal.price - (proposal.price - aggressive) * progress, tick
        )
        if stepped < aggressive:
            stepped = aggressive
        floor_px = self._min_sell_price(
            market, proposal.outcome, proposal.size, bid
        )
        if floor_px is not None and stepped < floor_px:
            stepped = floor_px
        if stepped <= bid or stepped >= 1 or stepped >= proposal.price:
            return proposal
        return Proposal(
            outcome=proposal.outcome,
            price=stepped,
            size=proposal.size,
            fair=proposal.fair,
            required_edge=proposal.required_edge,
            repair=proposal.repair,
        )

    def _place_proposals(self, market: Market, proposals: list[Proposal]) -> None:
        for proposal in proposals:
            if proposal.reason != "ok":
                continue
            require_repair = self.at_cap()
            if self.config.slices_per_side <= 1:
                if self._working_outcome(market, proposal.outcome):
                    continue
                self._place_one(market, proposal, proposal.price, require_repair)
                continue
            bid = market.book(proposal.outcome).best_bid()
            for price in ladder_prices(
                proposal.price,
                market.tick_size,
                self.config.slices_per_side,
                bid,
                inside=self.config.slice_inside_tick,
            ):
                if self._working_at(market, proposal.outcome, price):
                    continue
                sized = Proposal(
                    outcome=proposal.outcome,
                    price=price,
                    size=proposal.size,
                    fair=proposal.fair,
                    required_edge=proposal.required_edge,
                    repair=proposal.repair,
                )
                self._place_one(market, sized, price, require_repair)

    def _place_one(
        self,
        market: Market,
        proposal: Proposal,
        price: Decimal,
        require_repair: bool,
        *,
        marketable: bool = False,
        ignore_floor: bool = False,
    ) -> None:
        bid = market.book(proposal.outcome).best_bid()
        forced = self.sell_floors.get((market.market_id, proposal.outcome))
        if forced is not None and bid is not None and bid + market.tick_size < forced:
            self.sell_floors.pop((market.market_id, proposal.outcome), None)
            forced = None
        if forced is not None and price < forced:
            price = forced
            if price >= 1 or price > self.config.max_price:
                self._skip(market, "post_only_backoff", proposal.outcome)
                return
            proposal = Proposal(
                outcome=proposal.outcome,
                price=price,
                size=proposal.size,
                fair=proposal.fair,
                required_edge=proposal.required_edge,
                repair=proposal.repair,
            )
        if not marketable and bid is not None and price <= bid:
            # Post-only SELL must rest above the best bid; never cross.
            price = bid + market.tick_size
            if price >= 1 or self._working_at(market, proposal.outcome, price):
                self._skip(market, "would_cross", proposal.outcome)
                return
            proposal = Proposal(
                outcome=proposal.outcome,
                price=price,
                size=proposal.size,
                fair=proposal.fair,
                required_edge=proposal.required_edge,
                repair=proposal.repair,
            )
        size = self._largest_size(
            market,
            proposal,
            require_repair,
            ignore_floor=ignore_floor,
            allow_small=ignore_floor,
        )
        if size is None:
            free = market.inventory(proposal.outcome) - reserved(
                self.state, market, proposal.outcome
            )
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
            return
        order = self._commit_order(
            market, proposal, size, risk_reducing=require_repair or ignore_floor
        )
        if marketable and order is not None:
            self.marketable_ids.add(order.order_id)

    def _largest_size(
        self,
        market: Market,
        proposal: Proposal,
        require_repair: bool,
        *,
        ignore_floor: bool = False,
        allow_small: bool = False,
    ) -> Optional[Decimal]:
        quantum = self.config.order_size_quantum
        minimum = (
            market.min_order_size
            if market.min_order_size > self.config.min_order_size
            else self.config.min_order_size
        )
        desired = qdown(proposal.size, quantum)
        if desired <= 0:
            return None
        if desired < minimum and not allow_small:
            return None
        if self._size_ok(
            market,
            proposal.outcome,
            proposal.price,
            desired,
            require_repair,
            ignore_floor=ignore_floor,
        ):
            return desired
        if desired < minimum:
            return None
        if not self._size_ok(
            market,
            proposal.outcome,
            proposal.price,
            minimum,
            require_repair,
            ignore_floor=ignore_floor,
        ):
            return None
        span = int((desired - minimum) / quantum)
        lo = 0
        hi = span
        best = minimum
        while lo <= hi:
            mid = (lo + hi) // 2
            size = minimum + quantum * mid
            if self._size_ok(
                market,
                proposal.outcome,
                proposal.price,
                size,
                require_repair,
                ignore_floor=ignore_floor,
            ):
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
        *,
        ignore_floor: bool = False,
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
        if projected < self.state.loss_floor and not ignore_floor:
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

    def _commit_order(
        self, market: Market, proposal: Proposal, size: Decimal, *, risk_reducing: bool
    ) -> Order:
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
        return order

    def _idle_flat(self, market: Market) -> bool:
        return (
            market.up == 0
            and market.down == 0
            and market.pending_split <= 0
            and not self._working(market)
        )

    def _holds_slot(self, market: Market) -> bool:
        if market.status == "redeemed" or market.status in (
            "expired_unresolved",
            "resolved",
        ):
            return False
        if market.end_ts_ms and self.state.now_ms >= market.end_ts_ms:
            return False
        # Traded and flat: a chain merge can still be pending. That must not
        # keep the next session out. An untraded open window still holds one.
        if self._idle_flat(market) and market.gross_split > 0:
            return False
        return True

    def _note_leg_gap(self, market: Market) -> None:
        gap = market.sold_down_shares - market.sold_up_shares
        if gap == 0:
            self.leg_since.pop(market.market_id, None)
            return
        self.leg_since.setdefault(market.market_id, self.state.now_ms)

    def _naked_leg(self, market: Market) -> Optional[tuple[str, Decimal]]:
        gap = market.sold_down_shares - market.sold_up_shares
        if gap > 0 and market.up > 0:
            return "up", gap if gap < market.up else market.up
        if gap < 0 and market.down > 0:
            size = -gap
            return "down", size if size < market.down else market.down
        if gap == 0:
            self.leg_since.pop(market.market_id, None)
        return None

    def _leg_fill_price(self, market: Market, outcome: str) -> Optional[Decimal]:
        """Average price already sold on the other side. ``outcome`` is still held."""
        if outcome == "up":
            sold, proceeds = market.sold_down_shares, market.sold_down_proceeds
        else:
            sold, proceeds = market.sold_up_shares, market.sold_up_proceeds
        if sold <= 0:
            return None
        return proceeds / sold

    def _leg_floor(self, market: Market, outcome: str, size: Decimal) -> Decimal:
        """Breakeven for the unsold leg: ``1 - P``, lifted by the taker fee when asked."""
        tick = market.tick_size if market.tick_size > 0 else Decimal("0.01")
        avg = self._leg_fill_price(market, outcome)
        if avg is None:
            return ceil_tick(self.config.min_price, tick)
        raw = ONE - avg
        if raw < self.config.min_price:
            raw = self.config.min_price
        price = ceil_tick(raw, tick)
        if (
            self.config.leg_fee_buffer
            and self.state.risk_fee_rate > 0
            and size > 0
            and price < 1
        ):
            need = size * (ONE - avg)
            for _ in range(50):
                net = size * price - conservative_fee(
                    size, price, self.state.risk_fee_rate
                )
                if net >= need or price + tick >= 1:
                    break
                price += tick
        if price >= 1:
            price = ONE - tick
        if price < self.config.min_price:
            price = ceil_tick(self.config.min_price, tick)
        return price

    def _log_leg(
        self,
        market: Market,
        outcome: str,
        reason: str,
        *,
        price: Optional[Decimal],
        bid: Optional[Decimal],
        floor: Decimal,
        size: Decimal,
    ) -> None:
        key = (reason, money(price) if price is not None else "", money(size))
        if self._leg_log_key.get(market.market_id) == key:
            return
        self._leg_log_key[market.market_id] = key
        bid_txt = money(bid) if bid is not None else "none"
        price_txt = money(price) if price is not None else "none"
        detail = f"bid={bid_txt} floor={money(floor)} price={price_txt} size={money(size)}"
        self._emit(
            kind="leg_unwind",
            market=market,
            outcome=outcome,
            price=price,
            size=size,
            reason=reason,
            detail=detail,
        )
        print(
            f"leg_unwind {market.slug or market.market_id} {outcome} {reason} {detail}",
            flush=True,
        )

    def _complete_leg(self, market: Market) -> bool:
        """Sell the unsold leg of a split at breakeven or better.

        Floor is ``1 - P`` for the filled price P. When the best bid is at
        least that floor, sell the filled size as a taker at the bid. Otherwise
        rest a post-only sell at ``max(floor, best_ask - tick)``. After
        ``leg_unwind_seconds`` the same bid check is repeated. Below the floor,
        a taker is used only near expiry when ``allow_loss_unwind`` permits it.
        A partial fill sells the same gap, not the whole inventory.
        """
        naked = self._naked_leg(market)
        if naked is None:
            self.leg_cross.discard((market.market_id, "up"))
            self.leg_cross.discard((market.market_id, "down"))
            self._leg_log_key.pop(market.market_id, None)
            return False
        outcome, gap = naked
        self.leg_since.setdefault(market.market_id, self.state.now_ms)
        book = market.book(outcome)
        bid = book.best_bid()
        ask = book.best_ask()
        tick = market.tick_size if market.tick_size > 0 else Decimal("0.01")
        floor = self._leg_floor(market, outcome, gap)
        if bid is None:
            self._log_leg(
                market, outcome, "no_bid", price=None, bid=None, floor=floor, size=gap
            )
            return True
        started = self.leg_since.get(market.market_id, self.state.now_ms)
        elapsed = self.state.now_ms - started
        timed_out = elapsed >= int(self.config.leg_unwind_seconds) * 1000
        left_ms = market.end_ts_ms - self.state.now_ms
        near_expiry = 0 <= left_ms <= int(
            self.config.unwind_loss_before_expiry_seconds
        ) * 1000
        take_px = qdown(bid, tick)
        if take_px <= 0:
            take_px = bid
        shortfall = (floor - take_px) * gap if take_px < floor else ZERO
        loss_ok = (
            self.config.allow_loss_unwind
            and near_expiry
            and shortfall > 0
            and shortfall <= self.config.max_unwind_loss
        )
        crossed = (market.market_id, outcome) in self.leg_cross
        taker = False
        reason = "rest_floor"
        if take_px >= floor:
            taker = True
            price = take_px
            reason = "taker_timeout" if timed_out else "taker_breakeven"
        elif loss_ok:
            taker = True
            price = take_px
            reason = "taker_loss_cap"
        elif crossed:
            price = bid + tick
            price = ceil_tick(price, tick)
            reason = "cross_retry_maker"
        else:
            price = floor
            if ask is not None and ask - tick > price:
                price = ask - tick
            price = ceil_tick(price, tick)
            if price <= bid:
                price = ceil_tick(bid + tick, tick)
                reason = "rest_above_bid"
        if price >= 1:
            price = ONE - tick
            if not taker and price <= bid:
                taker = take_px >= floor
                price = take_px if taker else price
        if price <= 0:
            self._log_leg(
                market, outcome, "bad_price", price=price, bid=bid, floor=floor, size=gap
            )
            return True
        if crossed and taker:
            reason = "cross_retry_taker"
        self.leg_cross.discard((market.market_id, outcome))
        self.sell_floors.pop((market.market_id, outcome), None)
        want_marketable = taker
        for order in list(self._working(market, outcome)):
            same = order.price == price and (
                (order.order_id in self.marketable_ids) == want_marketable
            )
            if same:
                continue
            # Never posted, so the exchange is not holding the shares.
            if order.status == "queued" and not order.exchange_id:
                order.status = "cancelled"
                self._emit(
                    kind="order_cancelled",
                    market=market,
                    order=order,
                    size=order.remaining,
                    reason="leg_unwind",
                    detail="unsent",
                )
                continue
            self._request_cancel(market, order, "leg_unwind")
        if not self.config.live:
            self._promote()
        free = market.inventory(outcome) - reserved(self.state, market, outcome)
        size = gap if gap < free else free
        size = qdown(size, self.config.order_size_quantum)
        minimum = (
            market.min_order_size
            if market.min_order_size > self.config.min_order_size
            else self.config.min_order_size
        )
        if size < minimum:
            self._log_leg(
                market,
                outcome,
                "below_min",
                price=price,
                bid=bid,
                floor=floor,
                size=size,
            )
            return True
        resting = [
            order
            for order in self._working(market, outcome)
            if order.price == price
            and (order.order_id in self.marketable_ids) == want_marketable
        ]
        if not resting:
            self._log_leg(
                market, outcome, reason, price=price, bid=bid, floor=floor, size=size
            )
            self._place_one(
                market,
                Proposal(
                    outcome=outcome,
                    price=price,
                    size=size,
                    fair=price,
                    required_edge=ZERO,
                    repair=True,
                ),
                price,
                False,
                marketable=want_marketable,
                ignore_floor=True,
            )
            resting = [
                order
                for order in self._working(market, outcome)
                if order.price == price
                and (order.order_id in self.marketable_ids) == want_marketable
            ]
        else:
            self._log_leg(
                market,
                outcome,
                reason + "_keep",
                price=price,
                bid=bid,
                floor=floor,
                size=size,
            )
        if want_marketable and not self.config.live:
            for order in resting:
                if order.remaining <= 0 or take_px < order.price:
                    continue
                fill = order.remaining if order.remaining < gap else gap
                fill = qdown(fill, Decimal("0.000001"))
                if fill > 0:
                    self._apply_fill(
                        market, order, fill, synthetic=True, source="leg_unwind"
                    )
                    gap -= fill
        # Matched shares merge. The other side's resting sell keeps its own
        # reservation, so a partial fill does not pull the remainder.
        self._merge_free(market, "leg_unwind")
        return True

    def _buy_plan(self, market: Market) -> Optional[dict]:
        """Both asks, or nothing. One tick of edge after taker fees."""
        if market.status != "open" or market.up > 0 or market.down > 0:
            return None
        if market.pending_split > 0 or market.gross_split > 0:
            return None
        up = market.book("up")
        down = market.book("down")
        ask_up, ask_down = up.best_ask(), down.best_ask()
        bid_up, bid_down = up.best_bid(), down.best_bid()
        if (
            ask_up is None
            or ask_down is None
            or bid_up is None
            or bid_down is None
            or ask_up <= bid_up
            or ask_down <= bid_down
        ):
            return None
        fee = conservative_fee(ONE, ask_up, self.state.risk_fee_rate) + conservative_fee(
            ONE, ask_down, self.state.risk_fee_rate
        )
        if ask_up + ask_down + fee > ONE - self.config.pair_min_edge:
            return None
        slip = ask_up - bid_up
        other = ask_down - bid_down
        if other > slip:
            slip = other
        if slip <= 0:
            slip = market.tick_size if market.tick_size > 0 else Decimal("0.01")
        budget = self.config.initial_capital * self.config.max_loss_fraction
        if self.state.realized_pnl < 0:
            budget += self.state.realized_pnl
        if budget <= 0:
            return None
        size = budget / slip
        cost = ask_up + ask_down + fee
        if cost <= 0:
            return None
        affordable = self.state.cash / cost
        if affordable < size:
            size = affordable
        if self.config.clip_size < size:
            size = self.config.clip_size
        have_up = up.ask_size_at(ask_up)
        have_down = down.ask_size_at(ask_down)
        if have_up < size:
            size = have_up
        if have_down < size:
            size = have_down
        size = qdown(size, self.config.order_size_quantum)
        minimum = (
            market.min_order_size
            if market.min_order_size > self.config.min_order_size
            else self.config.min_order_size
        )
        if size < minimum:
            return None
        return {
            "market_id": market.market_id,
            "up_price": ask_up,
            "down_price": ask_down,
            "size": size,
            "up_token": market.up_token_id,
            "down_token": market.down_token_id,
            "tick_size": market.tick_size,
        }

    def _try_buy_pair(self, market: Market) -> None:
        plan = self._buy_plan(market)
        if plan is None:
            return
        if self.mode == "live":
            self.buy_outbox.append(plan)
            return
        fail = self.buy_fail_outcome
        up_size = ZERO if fail == "up" else plan["size"]
        down_size = ZERO if fail == "down" else plan["size"]
        self.apply_buy_fills(
            market,
            up_price=plan["up_price"],
            down_price=plan["down_price"],
            up_size=up_size,
            down_size=down_size,
        )

    def apply_buy_fills(
        self,
        market: Market,
        *,
        up_price: Decimal,
        down_price: Decimal,
        up_size: Decimal,
        down_size: Decimal,
    ) -> None:
        """Take the filled buy legs, merge a pair, and sell a lone leg now."""
        paid = ZERO
        if up_size > 0:
            paid += up_size * up_price + conservative_fee(
                up_size, up_price, self.state.risk_fee_rate
            )
            market.up += up_size
        if down_size > 0:
            paid += down_size * down_price + conservative_fee(
                down_size, down_price, self.state.risk_fee_rate
            )
            market.down += down_size
        if paid > self.state.cash:
            market.up -= up_size
            market.down -= down_size
            self._emit(kind="quote_reject", market=market, reason="insufficient_cash")
            return
        self.state.cash -= paid
        market.net_cash_flow -= paid
        self._emit(
            kind="buy_pair",
            market=market,
            size=up_size if up_size > down_size else down_size,
            price=up_price + down_price,
            reason="buy_pair_arb",
        )
        paired = up_size if up_size < down_size else down_size
        if paired > 0:
            self._merge(market, paired, "buy_pair")
        lone = "up" if market.up > market.down else "down"
        if market.up == market.down:
            return
        book = market.book(lone)
        bid = book.best_bid()
        held = market.inventory(lone)
        if bid is None or held <= 0:
            return
        self._place_one(
            market,
            Proposal(
                outcome=lone,
                price=bid,
                size=held,
                fair=bid,
                required_edge=ZERO,
                repair=True,
            ),
            bid,
            False,
            marketable=True,
            ignore_floor=True,
        )
        if self.config.live:
            return
        for order in list(self._working(market, lone)):
            if order.price == bid and order.remaining > 0:
                self._apply_fill(
                    market,
                    order,
                    order.remaining,
                    synthetic=True,
                    source="leg_unwind",
                )

    def _check_realized_loss(self) -> None:
        budget = self.state.initial_capital * self.state.max_loss_fraction
        if budget <= 0 or self.state.realized_pnl > -budget:
            return
        if self.state.killed:
            return
        self.state.killed = True
        self._emit(kind="kill", reason="realized_loss")

    def _split(self, market: Market, amount: Decimal, reason: str) -> bool:
        if (
            market.status != "open"
            or self._in_stop_window(market)
            or market.status == "expired_unresolved"
        ):
            self._emit(kind="reject", market=market, size=amount, reason="near_expiry")
            return False
        if self.state.killed or self.at_cap():
            self._emit(
                kind="quote_reject",
                market=market,
                size=amount,
                reason="exposure_frozen",
            )
            return False
        if amount <= 0:
            self._emit(kind="reject", market=market, reason="bad_size")
            return False
        if amount > self.state.cash:
            self._emit(
                kind="reject", market=market, size=amount, reason="insufficient_cash"
            )
            return False
        if market.gross_split + amount > self.state.per_market_budget:
            self._emit(
                kind="quote_reject",
                market=market,
                size=amount,
                reason="per_market_budget",
            )
            return False
        market.up += amount
        market.down += amount
        projected = worst_case_equity(self.state, cash_delta=-amount)
        market.up -= amount
        market.down -= amount
        if projected < self.state.loss_floor:
            self._emit(
                kind="quote_reject", market=market, size=amount, reason="risk_floor"
            )
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
            self._emit(
                kind="reject", market=market, size=amount, reason="reserved_inventory"
            )
            return False
        market.up -= amount
        market.down -= amount
        self.state.cash += amount
        market.net_cash_flow += amount
        self._emit(kind="merge", market=market, size=amount, reason=reason)
        if self.mode == "live":
            self.chain_outbox.append(
                {
                    "action": "merge",
                    "market_id": market.market_id,
                    "condition_id": market.condition_id,
                    "amount": format(amount, "f"),
                }
            )
        return True

    def _merge_free(self, market: Market, reason: str) -> None:
        free_up = market.up - reserved(self.state, market, "up")
        free_down = market.down - reserved(self.state, market, "down")
        amount = free_up if free_up < free_down else free_down
        amount = qdown(amount, Decimal("0.000001"))
        if amount > 0:
            self._merge(market, amount, reason)

    def _try_redeem(self, market: Market) -> bool:
        if (
            market.status != "resolved"
            or market.winner not in {"up", "down"}
            or market.resolution_conflict
        ):
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
            self._check_realized_loss()
        self._emit(
            kind="redeem", market=market, size=payout, reason=market.winner or ""
        )
        if self.mode == "live" and payout > 0:
            self.chain_outbox.append(
                {
                    "action": "redeem",
                    "market_id": market.market_id,
                    "condition_id": market.condition_id,
                    "amount": format(payout, "f"),
                }
            )
        return True

    def _refresh_expiry(self, market: Market) -> None:
        if market.status == "open" and self.state.now_ms >= market.end_ts_ms:
            market.status = "expired_unresolved"
            self._emit(kind="expiry", market=market, reason="expired_unresolved")

    def _cancel_market(
        self, market: Market, reason: str, outcome: Optional[str] = None
    ) -> None:
        for order in list(self.state.orders.values()):
            if (
                order.market_id != market.market_id
                or order.status not in WORKING_ORDER_STATUSES
            ):
                continue
            if outcome and order.outcome != outcome:
                continue
            if (
                self.mode == "live"
                and reason == "stale_feed"
                and not self._price_moved_past(market, order)
                and self._rested_ms(order)
                < int(self.config.min_order_rest_seconds) * 1000
            ):
                continue
            self._request_cancel(market, order, reason)

    def _price_moved_past(self, market: Market, order: Order) -> bool:
        """A resting sell is through when the bid has traded up to its price."""
        bid = market.book(order.outcome).best_bid()
        return bid is not None and bid >= order.price

    def _note_pair_block(self, market: Market, outcome: str, price: Decimal) -> None:
        seen = getattr(self, "pair_blocked", None)
        if seen is None:
            seen = set()
            self.pair_blocked = seen
        seen.add(market.market_id)
        key = (market.market_id, outcome)
        once = getattr(self, "_pair_block_once", None)
        if once is None:
            once = set()
            self._pair_block_once = once
        if key in once:
            return
        once.add(key)
        self._emit(
            kind="pair_floor_block",
            market=market,
            outcome=outcome,
            price=price,
            reason="pair_floor",
        )

    def _request_cancel(self, market: Market, order: Order, reason: str) -> None:
        if (
            order.status not in WORKING_ORDER_STATUSES
            or order.status == "cancel_requested"
        ):
            return
        order.status = "cancel_requested"
        order.cancel_request_ts = self.state.now_ms
        if self.state.feed_up:
            order.cancel_ready_ts = self.state.now_ms + self.state.cancel_latency_ms
        else:
            order.cancel_ready_ts = None
        rested = self._rested_ms(order)
        self._emit(
            kind="cancel_request",
            market=market,
            order=order,
            size=order.remaining,
            reason=reason,
        )
        self._emit(
            kind="order_cancelled",
            market=market,
            order=order,
            size=order.remaining,
            reason=reason,
            detail=f"rested_ms={rested}",
        )

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
        return (
            self.state.feed_up
            and self._book_fresh(market, "up")
            and self._book_fresh(market, "down")
        )

    def _book_fresh(self, market: Market, outcome: str) -> bool:
        book = market.book(outcome)
        if book.uncertain:
            return False
        if self.mode == "live":
            # Freshness is the last time we received a book, not the exchange
            # timestamp. A quiet book does not go stale just because nothing
            # inside it changed. ``stale_after_ms`` stays the paper threshold.
            if book.received_ms < 0:
                return False
            age = self.state.now_ms - book.received_ms
            limit = int(self.config.stale_feed_seconds) * 1000
            if age < 0:
                return -age <= self.config.clock_skew_ms
            return age <= limit
        if book.ts_ms < 0:
            return False
        age = self.state.now_ms - book.ts_ms
        if age < 0:
            return -age <= self.config.clock_skew_ms
        return age <= self.config.stale_after_ms

    def _in_stop_window(self, market: Market) -> bool:
        return (
            self.state.now_ms
            >= market.end_ts_ms - self.config.stop_quoting_before_expiry_ms
        )

    def _in_cooldown(self, market: Market) -> bool:
        return self.state.now_ms < market.last_action_ts + self.config.quote_cooldown_ms

    def _session_capped(self, market: Market) -> bool:
        cap = self.state.session_max_loss
        if cap <= 0:
            return False
        return session_loss(self.state, market) >= cap

    def _working_at(self, market: Market, outcome: str, price: Decimal) -> bool:
        return any(
            order.outcome == outcome and order.price == price
            for order in self._working(market, outcome)
        )

    def _check_kill_switch(self) -> None:
        path = self.config.kill_switch_file
        if not path or self.state.killed:
            return
        if Path(path).exists():
            self.state.killed = True
            self._emit(kind="kill", reason="kill_switch_file")

    def _note_volatility(self, market: Market) -> None:
        book = market.book("up")
        bid, ask = book.best_bid(), book.best_ask()
        if bid is None or ask is None:
            return
        mid = (bid + ask) / 2
        move = self.config.vol_pause_move
        window = self.config.vol_pause_window_ms
        if (
            move > 0
            and window > 0
            and market.last_mid is not None
            and self.state.now_ms - market.last_mid_ts <= window
            and abs(mid - market.last_mid) > move
        ):
            market.pause_until_ms = self.state.now_ms + max(
                self.config.vol_pause_hold_ms, window
            )
            self._emit(
                kind="vol_pause",
                market=market,
                reason="outcome_jump",
                detail=money(mid),
            )
        market.last_mid = mid
        market.last_mid_ts = self.state.now_ms

    def _on_reference(self, event: dict) -> None:
        try:
            price = Decimal(str(event["price"]))
        except (KeyError, ValueError, ArithmeticError):
            self._emit(kind="reject", reason="bad_reference")
            return
        previous = self.state.btc_price
        self.state.btc_price = price
        self.state.btc_price_ts = self.state.now_ms
        threshold = self.config.btc_pause_move
        if previous is not None and threshold > 0 and abs(price - previous) > threshold:
            hold = self.config.vol_pause_hold_ms or self.config.vol_pause_window_ms
            for market in self.state.markets.values():
                market.pause_until_ms = max(
                    market.pause_until_ms, self.state.now_ms + hold
                )
            self._emit(kind="vol_pause", reason="btc_jump", detail=money(price))
        else:
            self._emit(kind="reference", reason="ok", detail=money(price))

    def _on_user_fill(self, event: dict) -> None:
        trade_id = str(event.get("trade_id") or "")
        if not trade_id:
            self._emit(kind="reject", reason="missing_trade_id")
            return
        if trade_id in self.state.seen_trade_ids:
            self._emit(
                kind="duplicate_trade", reason="duplicate_trade", detail=trade_id
            )
            return
        order = self.state.orders.get(str(event.get("order_id") or ""))
        # A late fill on an order we already consider cancelled is still real.
        if order is None or (
            order.status not in WORKING_ORDER_STATUSES and order.status != "cancelled"
        ):
            self.state.seen_trade_ids.add(trade_id)
            self._emit(kind="trade_ignored", reason="unknown_order", detail=trade_id)
            return
        market = self.state.markets[order.market_id]
        try:
            size = qdown(Decimal(str(event["size"])), Decimal("0.000001"))
        except (KeyError, ValueError, ArithmeticError):
            self._emit(kind="reject", market=market, reason="bad_size")
            return
        if size <= 0 or size > order.remaining:
            self.state.seen_trade_ids.add(trade_id)
            self._emit(
                kind="reject", market=market, order=order, size=size, reason="no_short"
            )
            return
        self.state.seen_trade_ids.add(trade_id)
        self._apply_fill(market, order, size, synthetic=False, source="exchange")

    def note_post_reject(self, order, reason: str) -> None:
        """Drop a refused order. A post-only cross is retried one tick higher."""
        text = str(reason or "no order id")
        if is_post_only_cross(text):
            market = self.state.markets.get(order.market_id)
            naked = self._naked_leg(market) if market is not None else None
            if market is not None and naked is not None and naked[0] == order.outcome:
                # The book moved through the post-only price. Retry as a taker
                # when the bid still clears breakeven, otherwise one tick up.
                self.leg_cross.add((order.market_id, order.outcome))
                self.reject_live_order(order, text, pause=False)
                self._complete_leg(market)
                return
            if market is not None:
                bumped = order.price + market.tick_size
                if bumped < 1:
                    key = (order.market_id, order.outcome)
                    prev = self.sell_floors.get(key)
                    if prev is None or bumped > prev:
                        self.sell_floors[key] = bumped
            # Do not pause the market: the next tick posts the less aggressive price.
            self.reject_live_order(order, text, pause=False)
            return
        self.reject_live_order(order, text, pause=True)

    def reject_live_order(self, order, reason: str, *, pause: bool = True) -> None:
        """The exchange refused an order: drop it without touching inventory."""
        text = str(reason or "exchange_rejected")
        order.status = "rejected"
        order.remaining = ZERO
        market = self.state.markets.get(order.market_id)
        if pause and market is not None:
            # Pause briefly so a refused order is not re-posted in a tight loop.
            market.pause_until_ms = self.state.now_ms + REJECT_PAUSE_MS
        self._emit(
            kind="order_rejected",
            market=market,
            order=order,
            reason="post_only_cross" if is_post_only_cross(text) else "exchange_rejected",
            detail=f"rested_ms={self._rested_ms(order)}; {text[:160]}",
        )

    def _on_reconcile(self, event: dict) -> None:
        remote = event.get("open_order_ids")
        if not isinstance(remote, list):
            self._emit(kind="reject", reason="bad_reconcile")
            return
        # Queued orders are not required on the exchange. A live order missing
        # remotely, or a remote id we never recorded, stops quoting.
        remote_set = {str(item) for item in remote}
        if self.config.live:
            # A post that never received an id is not an open order. Drop it
            # before the diff, or a 4xx looks like a local/remote mismatch.
            for order in list(self.state.orders.values()):
                if order.status == "live" and not order.exchange_id:
                    self.reject_live_order(
                        order, "no exchange order id", pause=False
                    )
        known = set()
        local = set()
        owner = {}
        for order in self.state.orders.values():
            identity = order.exchange_id or order.order_id
            known.add(identity)
            owner[identity] = order
            if order.status == "live":
                local.add(identity)
        known |= self._ignored_remote
        ts = int(event.get("ts") or 0)
        # Live: a cancel is only confirmed once the exchange no longer lists the
        # order, and a few seconds have passed for any late fill to arrive.
        # Until then it still counts as fully fillable exposure.
        for order in list(self.state.orders.values()):
            if (
                order.status == "cancel_requested"
                and self.config.live
                and order.exchange_id
                and order.exchange_id not in remote_set
                and ts - (order.cancel_request_ts or 0) >= CANCEL_CONFIRM_MS
            ):
                order.status = "cancelled"
                self._emit(
                    kind="cancel_confirm",
                    market=self.state.markets.get(order.market_id),
                    order=order,
                    size=order.remaining,
                    reason="exchange_confirmed",
                )
        diffs = {f"remote:{item}" for item in remote_set - known} | {
            f"local:{item}" for item in local - remote_set
        }
        for key in list(self._reconcile_first_seen):
            if key not in diffs:
                del self._reconcile_first_seen[key]
        for key in diffs:
            self._reconcile_first_seen.setdefault(key, ts)
        grace = RECONCILE_GRACE_MS
        persistent = [
            key for key in diffs if ts - self._reconcile_first_seen[key] >= grace
        ]
        if diffs and not persistent:
            self._emit(kind="reconcile", reason="pending", detail=",".join(sorted(diffs))[:200])
            return
        if persistent:
            # Contain the damage to the affected market(s): cancel, stop quoting
            # there, merge, and keep the run going for later sessions.
            hit = set()
            for key in persistent:
                kind, ident = key.split(":", 1)
                self._reconcile_first_seen.pop(key, None)
                if kind == "local" and ident in owner:
                    hit.add(owner[ident].market_id)
                else:
                    # An unknown remote id is cancelled and forgotten. It does
                    # not stop quoting on every open market.
                    self._ignored_remote.add(ident)
                    self.stray_remote_cancels.append(ident)
            for market_id in hit:
                market = self.state.markets.get(market_id)
                if market is None:
                    continue
                market.session_stopped = True
                self._cancel_market(market, "reconcile_mismatch")
                for order in self.state.orders.values():
                    ident = order.exchange_id or order.order_id
                    if order.market_id == market_id and f"local:{ident}" in persistent:
                        order.status = "cancelled"
            self._emit(kind="reconcile", reason="mismatch", detail=",".join(sorted(persistent))[:200])
            return
        self._emit(kind="reconcile", reason="ok")

    def _on_kill(self, event: dict) -> None:
        self.state.killed = True
        for market in list(self.state.markets.values()):
            self._cancel_market(market, "kill")
            self._merge_free(market, "kill")
        self._emit(kind="kill", reason=str(event.get("reason") or "kill"))

    def _on_split_pending(self, event: dict) -> None:
        market = self._market(event)
        if market is None:
            return
        try:
            amount = qdown(Decimal(str(event["amount"])), Decimal("0.000001"))
        except (KeyError, ValueError, ArithmeticError):
            self._emit(kind="reject", market=market, reason="bad_size")
            return
        reserved_cash = sum(
            (item.pending_split for item in self.state.markets.values()), Decimal("0")
        )
        if amount + reserved_cash > self.state.cash:
            self._emit(
                kind="reject", market=market, size=amount, reason="insufficient_cash"
            )
            return
        if (
            market.gross_split + market.pending_split + amount
            > self.state.per_market_budget
        ):
            self._emit(
                kind="quote_reject",
                market=market,
                size=amount,
                reason="per_market_budget",
            )
            return
        market.pending_split += amount
        self._emit(
            kind="split_pending", market=market, size=amount, reason="unconfirmed"
        )

    def _on_split_abandoned(self, event: dict) -> None:
        market = self._market(event)
        if market is None:
            return
        try:
            amount = qdown(Decimal(str(event["amount"])), Decimal("0.000001"))
        except (KeyError, ValueError, ArithmeticError):
            self._emit(kind="reject", market=market, reason="bad_size")
            return
        if amount > market.pending_split:
            self._emit(
                kind="reject", market=market, size=amount, reason="unconfirmed_split"
            )
            return
        market.pending_split -= amount
        self._emit(kind="split_abandoned", market=market, size=amount, reason="timeout")

    def _on_split_confirmed(self, event: dict) -> None:
        market = self._market(event)
        if market is None:
            return
        try:
            amount = qdown(Decimal(str(event["amount"])), Decimal("0.000001"))
        except (KeyError, ValueError, ArithmeticError):
            self._emit(kind="reject", market=market, reason="bad_size")
            return
        if amount > market.pending_split:
            self._emit(
                kind="reject", market=market, size=amount, reason="unconfirmed_split"
            )
            return
        market.pending_split -= amount
        self._split(market, amount, "split_confirmed")

    def _on_taker_repair(self, event: dict) -> None:
        if not self.config.taker_repair:
            self._emit(kind="reject", reason="taker_disabled")
            return
        market = self._market(event)
        if market is None:
            return
        try:
            size = qdown(Decimal(str(event["size"])), Decimal("0.000001"))
            price = Decimal(str(event["price"]))
        except (KeyError, ValueError, ArithmeticError):
            self._emit(kind="reject", market=market, reason="bad_size")
            return
        if size * price > self.config.taker_repair_max_usdc:
            self._emit(kind="reject", market=market, size=size, reason="taker_cap")
            return
        outcome = str(event.get("outcome") or "")
        from paper_split_maker.quotes import pair_min_price

        floor = pair_min_price(market, outcome, self.config)
        if floor is not None and price < floor:
            self._emit(kind="reject", market=market, price=price, reason="pair_floor")
            return
        self._emit(
            kind="taker_repair",
            market=market,
            outcome=outcome,
            size=size,
            price=price,
            reason="capped",
        )


def _jsonable(value):
    if isinstance(value, Decimal):
        return money(value)
    return value


def _aggregate_levels(
    raw, tick: Decimal, *, descending: bool
) -> list[tuple[Decimal, Decimal]]:
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
        if (
            asset == market.up_token_id
            and label.casefold() == market.up_label.casefold()
        ):
            return "up"
        if (
            asset == market.down_token_id
            and label.casefold() == market.down_label.casefold()
        ):
            return "down"
        event["_reject_reason"] = "outcome_mapping_conflict"
        return None
    event["_reject_reason"] = "untrusted_resolution_source"
    return None
