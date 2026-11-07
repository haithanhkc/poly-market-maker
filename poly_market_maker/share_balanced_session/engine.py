"""Share-balanced maker sells for one allocated session.

Neutrality is a target after fills, not a promise. One-sided execution of
every outstanding sell is stressed before a new order. Missing orders are not
fills. Cancelling does not close exposure.
"""

from __future__ import annotations

from decimal import Decimal

from poly_market_maker.sell_equal.quant import floor_to_step
from poly_market_maker.share_balanced_session.book import (
    Book,
    bids_high_to_low,
    maker_sell_price,
)
from poly_market_maker.share_balanced_session.config import SessionConfig
from poly_market_maker.share_balanced_session.pnl import (
    bid_walk_cash,
    curve_fee,
    midpoint_mark,
    settlement_lower_bound,
    unpaid_rebate_estimate,
)
from poly_market_maker.share_balanced_session.state import (
    ACTIVE,
    SessionState,
    TrackedOrder,
    load_state,
    save_state,
)
from poly_market_maker.share_balanced_session.submit import submit_maker_sell


class Fill:
    def __init__(
        self,
        fill_id: str,
        exchange_order_id: str,
        shares: Decimal,
        price: Decimal,
        liquidity: str = "maker",
    ):
        self.fill_id = fill_id
        self.exchange_order_id = exchange_order_id
        self.shares = shares
        self.price = price
        self.liquidity = liquidity


class ShareSession:
    def __init__(
        self, config: SessionConfig, broker, now, state: SessionState | None = None
    ):
        self.config = config
        self.broker = broker
        self.now = now
        self.books: dict[str, Book] = {}
        self.skip_reasons: list[str] = []
        self.notices: list[str] = []
        if state is None:
            state = SessionState(
                cost_basis_usd=config.cost_basis_usd,
                capital_usd=config.capital_usd,
                condition_id=config.condition_id,
                on_hand={"up": config.allocated_up, "down": config.allocated_down},
            )
        self._require_basis(state)
        self.state = state
        self._seen = set(state.seen_fill_ids)
        self._persist()

    @classmethod
    def resume(cls, config: SessionConfig, broker, now, path: str):
        state = load_state(path)
        if state.condition_id != config.condition_id:
            raise ValueError("saved session is for a different condition")
        if state.cost_basis_usd != config.cost_basis_usd:
            raise ValueError("saved cost basis does not match the snapshot")
        return cls(config, broker, now, state)

    def observe_book(self, outcome: str, book: Book) -> None:
        self.books[outcome] = book
        self.broker.set_book(self.config.token_id(outcome), book)

    def observe_user(self, ts: Decimal) -> None:
        self.state.last_user_ts = ts
        self._persist()

    def tick(self) -> None:
        self.skip_reasons = []
        if self.state.phase == "refused":
            return
        self._phase_from_clock()
        if self.state.phase in ("refused", "ended"):
            self._cancel_own(self.state.phase)
            self._persist()
            return
        if self._blocked():
            self._cancel_own("blocked")
            return
        if self._stale():
            self.state.phase = "stale"
            self.skip_reasons.append("stale_book_or_user")
            self._cancel_own("stale")
            self._persist()
            return
        self._phase_from_pnl()
        if self.state.phase in (
            "ending",
            "ended",
            "take_profit",
            "take_profit_unverified",
        ):
            self._cancel_own(self.state.phase)
            self._persist()
            return
        if self.state.phase == "risk_halt":
            self._cancel_own("risk_halt")
            self._risk_reducing_quote()
            self._persist()
            return
        self._quote()
        self._persist()

    def apply_fills(self, fills: list[Fill]) -> None:
        for fill in fills:
            self._apply_fill(fill)
        self._phase_from_pnl()
        if (
            self.state.halt_reason is None
            and self.state.phase == "quoting"
            and not self._stale()
        ):
            self._quote()
        self._persist()

    def reconcile(self, open_ids: set[str], read_ok: bool, ts: Decimal) -> None:
        if not read_ok:
            self.state.phase = "stale"
            self.skip_reasons.append("user_read_failed")
            self._persist()
            return
        self.state.last_user_ts = ts
        for order in self.state.orders:
            self._resolve_presence(order, open_ids)
        self._clear_unknown_if_resolved()
        self._persist()

    def shutdown(self) -> None:
        self._cancel_own("shutdown")
        self._persist()

    def current_pnl(self) -> Decimal:
        return self._pnl(
            self.state.on_hand["up"],
            self.state.on_hand["down"],
            self.state.gross_proceeds,
            self._fees(),
        )

    def worst_pnl(self) -> Decimal:
        return min(self._scenario_pnls())

    def share_gap(self) -> Decimal:
        return self.state.on_hand["up"] - self.state.on_hand["down"]

    def reserved(self, outcome: str) -> Decimal:
        total = Decimal("0")
        for order in self.state.orders:
            if order.outcome == outcome and order.status in ACTIVE:
                total += order.reserved_size
        return total

    def available(self, outcome: str) -> Decimal:
        return self.state.on_hand[outcome] - self.reserved(outcome)

    def open_orders(self) -> list[TrackedOrder]:
        return [order for order in self.state.orders if order.status == "open"]

    def liquidation_estimate(self) -> Decimal | None:
        cash = self.state.gross_proceeds
        fees = self._fees()
        for outcome in ("up", "down"):
            book = self.books.get(outcome)
            if book is None:
                return None
            gained, fee = bid_walk_cash(
                bids_high_to_low(book),
                self.state.on_hand[outcome],
                self.config.taker_fee_rate,
            )
            cash += gained
            fees += fee
        return cash - self.config.cost_basis_usd - fees

    def midpoint_pnl(self) -> Decimal | None:
        mids = []
        for outcome in ("up", "down"):
            book = self.books.get(outcome)
            if book is None or book.best_bid is None or book.best_ask is None:
                return None
            mids.append((book.best_bid + book.best_ask) / 2)
        return midpoint_mark(
            self.state.gross_proceeds,
            self.state.on_hand["up"],
            self.state.on_hand["down"],
            mids[0],
            mids[1],
            self.config.cost_basis_usd,
            self._fees(),
        )

    def _require_basis(self, state: SessionState) -> None:
        if (
            state.cost_basis_source != "explicit_allocation"
            or state.cost_basis_usd <= 0
        ):
            self.state = state
            state.phase = "refused"
            raise ValueError(
                "refusing to start without an explicit allocation cost basis"
            )

    def _remember_skips(self) -> None:
        for reason in self.skip_reasons:
            if reason not in self.notices:
                self.notices.append(reason)

    def _persist(self) -> None:
        self._remember_skips()
        self.state.skip_reasons = list(self.skip_reasons)
        save_state(self.state, self.config.state_path)

    def _fees(self) -> Decimal:
        return self.state.fees_paid + self.state.fees_accrued

    def _pnl(
        self, rem_up: Decimal, rem_down: Decimal, gross: Decimal, fees: Decimal
    ) -> Decimal:
        return settlement_lower_bound(
            gross, rem_up, rem_down, self.config.cost_basis_usd, fees
        )

    def _scenario_pnls(self) -> list[Decimal]:
        current = self.current_pnl()
        return [
            current,
            self._pnl_if(self._outstanding("up")),
            self._pnl_if(self._outstanding("down")),
        ]

    def _pnl_if(self, orders: list[TrackedOrder]) -> Decimal:
        rem_up = self.state.on_hand["up"]
        rem_down = self.state.on_hand["down"]
        gross = self.state.gross_proceeds
        fees = self._fees()
        for order in orders:
            size = order.remaining
            if order.outcome == "up":
                rem_up -= size
            else:
                rem_down -= size
            gross += size * order.price
            fees += curve_fee(size, order.price, self.config.maker_fee_rate)
        return self._pnl(rem_up, rem_down, gross, fees)

    def _outstanding(self, outcome: str) -> list[TrackedOrder]:
        return [
            order
            for order in self.state.orders
            if order.outcome == outcome
            and order.status in ACTIVE
            and order.remaining > 0
        ]

    def _blocked(self) -> bool:
        return self.state.halt_reason in (
            "unknown_submit",
            "unknown_cancel",
            "unexplained_absence",
            "unexpected_taker_fill",
            "fill_exceeds_order",
        )

    def _stale(self) -> bool:
        now = self.now()
        limit = self.config.stale_after_seconds
        for outcome in ("up", "down"):
            book = self.books.get(outcome)
            if book is None or now - book.ts > limit or book.ts > now:
                return True
        if self.state.last_user_ts is None:
            return True
        age = now - self.state.last_user_ts
        return age > limit or age < 0

    def _phase_from_clock(self) -> None:
        cutoff = self.config.session_end_ts - self.config.cutoff_seconds
        if self.now() >= self.config.session_end_ts:
            self.state.phase = "ended"
        elif self.now() >= cutoff or self.now() < self.config.session_start_ts:
            if self.now() < self.config.session_start_ts:
                self.state.phase = "refused"
                self.skip_reasons.append("before_session")
            else:
                self.state.phase = "ending"

    def _phase_from_pnl(self) -> None:
        if self.state.phase in ("ending", "ended", "refused", "stale"):
            return
        if self.current_pnl() >= self.config.take_profit_usd:
            self.state.phase = "take_profit"
            return
        if self.worst_pnl() <= -self.config.risk_budget_usd:
            self.state.phase = "risk_halt"

    def _quote(self) -> None:
        gap = self._unreserved_gap()
        dust = self.config.size_quantum
        if gap > dust:
            self._ensure("up", gap, "balance_shares")
            return
        if gap < -dust:
            self._ensure("down", -gap, "balance_shares")
            return
        self._symmetric_seed()

    def _unreserved_gap(self) -> Decimal:
        up = self.state.on_hand["up"] - self.reserved("up")
        down = self.state.on_hand["down"] - self.reserved("down")
        return up - down

    def _symmetric_seed(self) -> None:
        price_up, reason_up = self._price("up")
        price_down, reason_down = self._price("down")
        if price_up is None or price_down is None:
            self.skip_reasons.append(reason_up if price_up is None else reason_down)
            return
        size = min(
            self.config.seed_shares, self.available("up"), self.available("down")
        )
        size = self._safe_pair_size(size, price_up, price_down)
        if size is None:
            self.skip_reasons.append("min_size_exceeds_risk_budget")
            return
        self._ensure("up", size, "symmetric_seed")
        self._ensure("down", size, "symmetric_seed")

    def _safe_pair_size(
        self, desired: Decimal, price_up: Decimal, price_down: Decimal
    ) -> Decimal | None:
        size = floor_to_step(desired, self.config.size_quantum)
        while size >= self.config.min_order_size:
            if self._pair_ok(size, price_up, price_down):
                return size
            size -= self.config.size_quantum
        return None

    def _pair_ok(self, size: Decimal, price_up: Decimal, price_down: Decimal) -> bool:
        up = self._hypothetical("up", size, price_up)
        down = self._hypothetical("down", size, price_down)
        if min(up, down) <= -self.config.risk_budget_usd:
            return False
        return self._skew_ok("up", size) and self._skew_ok("down", size)

    def _ensure(self, outcome: str, desired: Decimal, purpose: str) -> None:
        if self.state.halt_reason or self.state.phase != "quoting":
            return
        price, reason = self._price(outcome)
        if price is None:
            self.skip_reasons.append(reason)
            return
        size = self._safe_single_size(outcome, desired, price)
        if size is None:
            self.skip_reasons.append("min_size_exceeds_risk_budget")
            return
        existing = self._live(outcome)
        if existing and self._keep(existing, price):
            return
        if existing:
            if not self._replace_allowed(outcome):
                self.skip_reasons.append("replace_rate_limit")
                return
            self._cancel_orders(existing, "replace")
            if any(order.status != "cancelled" for order in existing):
                return
        self._submit(outcome, price, size, purpose)

    def _safe_single_size(
        self, outcome: str, desired: Decimal, price: Decimal
    ) -> Decimal | None:
        size = floor_to_step(
            min(desired, self.available(outcome)), self.config.size_quantum
        )
        while size >= self.config.min_order_size:
            pnl = self._hypothetical(outcome, size, price)
            if pnl > -self.config.risk_budget_usd and self._skew_ok(outcome, size):
                return size
            size -= self.config.size_quantum
        return None

    def _hypothetical(self, outcome: str, size: Decimal, price: Decimal) -> Decimal:
        orders = list(self._outstanding(outcome))
        extra = TrackedOrder(
            local_id="hypothetical",
            exchange_id=None,
            outcome=outcome,
            price=price,
            original_size=size,
            reserved_size=size,
            status="open",
            post_only=True,
        )
        orders.append(extra)
        rem_up = self.state.on_hand["up"]
        rem_down = self.state.on_hand["down"]
        gross = self.state.gross_proceeds
        fees = self._fees()
        for order in orders:
            if order.outcome == "up":
                rem_up -= order.remaining
            else:
                rem_down -= order.remaining
            gross += order.remaining * order.price
            fees += curve_fee(order.remaining, order.price, self.config.maker_fee_rate)
        return self._pnl(rem_up, rem_down, gross, fees)

    def _skew_ok(self, outcome: str, size: Decimal) -> bool:
        up = self.state.on_hand["up"] - self.reserved("up")
        down = self.state.on_hand["down"] - self.reserved("down")
        if outcome == "up":
            up -= size
        else:
            down -= size
        return abs(up - down) <= self.config.max_share_skew

    def _price(self, outcome: str):
        book = self.books.get(outcome)
        if book is None:
            return None, "missing_book"
        if (
            book.tick_size != self.config.tick_size
            or book.min_order_size != self.config.min_order_size
        ):
            return None, "book_constraints_mismatch"
        return maker_sell_price(
            book,
            self.config.improve_ticks,
            self.config.price_floor,
            self.config.price_ceiling,
        )

    def _live(self, outcome: str) -> list[TrackedOrder]:
        return [order for order in self._outstanding(outcome) if order.status == "open"]

    def _keep(self, orders: list[TrackedOrder], price: Decimal) -> bool:
        band = self.config.tick_size * self.config.hysteresis_ticks
        return all(abs(order.price - price) <= band for order in orders)

    def _replace_allowed(self, outcome: str) -> bool:
        previous = self.state.last_replace_ts.get(outcome)
        if previous is None:
            return True
        return self.now() - previous >= self.config.min_replace_seconds

    def _risk_reducing_quote(self) -> None:
        gap = self._unreserved_gap()
        if gap == 0:
            self.skip_reasons.append("residual_unmatched")
            return
        outcome = "up" if gap > 0 else "down"
        price, reason = self._price(outcome)
        if price is None:
            self.skip_reasons.append(reason)
            self.skip_reasons.append("residual_unmatched")
            return
        size = floor_to_step(
            min(abs(gap), self.available(outcome)), self.config.size_quantum
        )
        if size < self.config.min_order_size:
            self.skip_reasons.append("residual_below_min_size")
            return
        if self._hypothetical(outcome, size, price) < self.current_pnl():
            self.skip_reasons.append("maker_quote_not_risk_reducing")
            self.skip_reasons.append("residual_unmatched")
            return
        self._submit(outcome, price, size, "risk_reducing")

    def _submit(
        self, outcome: str, price: Decimal, size: Decimal, purpose: str
    ) -> None:
        del purpose
        if size > self.available(outcome) or size < self.config.min_order_size:
            self.skip_reasons.append("min_size_exceeds_risk_budget")
            return
        order = TrackedOrder(
            local_id="local-%04d" % self.state.next_local,
            exchange_id=None,
            outcome=outcome,
            price=price,
            original_size=size,
            reserved_size=size,
            status="intent",
            post_only=True,
        )
        self.state.next_local += 1
        self.state.orders.append(order)
        self._persist()
        try:
            result = submit_maker_sell(
                self.broker, self.config.token_id(outcome), price, size
            )
        except Exception:
            order.status = "unknown_submit"
            self.state.halt_reason = self.state.halt_reason or "unknown_submit"
            return
        self._apply_submit(order, result)

    def _apply_submit(self, order: TrackedOrder, result) -> None:
        if result.status == "accepted" and result.exchange_id:
            order.exchange_id = result.exchange_id
            order.status = "open"
            return
        if result.status == "rejected":
            order.status = "rejected"
            order.reserved_size = Decimal("0")
            self.skip_reasons.append(result.reason or "rejected")
            if result.reason == "post_only_would_cross":
                self.skip_reasons.append("post_only_rejected_not_retried")
            return
        order.exchange_id = result.exchange_id or order.exchange_id
        order.status = "unknown_submit"
        self.state.halt_reason = self.state.halt_reason or "unknown_submit"

    def _apply_fill(self, fill: Fill) -> None:
        if fill.fill_id in self._seen:
            return
        self._seen.add(fill.fill_id)
        self.state.seen_fill_ids.append(fill.fill_id)
        order = self._by_exchange(fill.exchange_order_id)
        if order is None:
            self.state.halt_reason = self.state.halt_reason or "unknown_order_fill"
            return
        if fill.liquidity != "maker" or fill.price < order.price:
            shares = fill.shares if fill.shares <= order.remaining else order.remaining
            self._credit(order, shares, fill.price, taker=True)
            self.state.halt_reason = self.state.halt_reason or "unexpected_taker_fill"
            return
        if fill.shares > order.remaining:
            self.state.halt_reason = self.state.halt_reason or "fill_exceeds_order"
            return
        self._credit(order, fill.shares, fill.price, taker=False)

    def _credit(
        self, order: TrackedOrder, shares: Decimal, price: Decimal, taker: bool
    ) -> None:
        if (
            shares <= 0
            or shares > order.reserved_size
            or shares > self.state.on_hand[order.outcome]
        ):
            self.state.halt_reason = self.state.halt_reason or "oversell"
            return
        order.filled_size += shares
        order.reserved_size -= shares
        self.state.on_hand[order.outcome] -= shares
        self.state.gross_proceeds += shares * price
        rate = self.config.taker_fee_rate if taker else self.config.maker_fee_rate
        self.state.fees_paid += curve_fee(shares, price, rate)
        if not taker:
            self.state.rebate_unpaid_estimate += unpaid_rebate_estimate(
                shares, price, self.config.taker_fee_rate, self.config.rebate_share
            )
        if order.filled_size == order.original_size:
            order.status = "filled"
            order.reserved_size = Decimal("0")

    def _cancel_own(self, reason: str) -> None:
        rows = [
            order
            for order in self.state.orders
            if order.status == "open" and order.exchange_id
        ]
        self._cancel_orders(rows, reason)

    def _cancel_orders(self, orders: list[TrackedOrder], reason: str) -> None:
        del reason
        if not orders:
            return
        for order in orders:
            order.status = "cancel_requested"
            if order.outcome in ("up", "down"):
                self.state.last_replace_ts[order.outcome] = self.now()
        self._persist()
        ids = [order.exchange_id for order in orders if order.exchange_id]
        try:
            result = self.broker.cancel(ids)
        except Exception:
            result = None
        if result is None or result.status != "known":
            for order in orders:
                order.status = "unknown_cancel"
            self.state.halt_reason = self.state.halt_reason or "unknown_cancel"
            return
        failed = result.not_canceled
        for order in orders:
            exchange_id = order.exchange_id or ""
            if exchange_id in result.canceled_ids:
                order.reserved_size = Decimal("0")
                order.status = (
                    "filled"
                    if order.filled_size >= order.original_size
                    else "cancelled"
                )
                if exchange_id not in self.state.confirmed_cancel_ids:
                    self.state.confirmed_cancel_ids.append(exchange_id)
            elif exchange_id in failed:
                order.status = "open"
            else:
                order.status = "unknown_cancel"
                self.state.halt_reason = self.state.halt_reason or "unknown_cancel"

    def _resolve_presence(self, order: TrackedOrder, open_ids: set[str]) -> None:
        if order.status not in ACTIVE or not order.exchange_id:
            return
        if order.exchange_id in open_ids:
            if order.status in ("unknown_submit", "unknown_cancel", "cancel_requested"):
                order.status = "open"
            return
        if order.remaining == 0:
            order.status = "filled"
            order.reserved_size = Decimal("0")
            return
        if order.exchange_id in self.state.confirmed_cancel_ids:
            order.reserved_size = Decimal("0")
            order.status = "cancelled"
            return
        self.state.halt_reason = self.state.halt_reason or "unexplained_absence"

    def _clear_unknown_if_resolved(self) -> None:
        if self.state.halt_reason == "unknown_submit" and not any(
            order.status == "unknown_submit" for order in self.state.orders
        ):
            self.state.halt_reason = None
        if self.state.halt_reason == "unknown_cancel" and not any(
            order.status == "unknown_cancel" for order in self.state.orders
        ):
            self.state.halt_reason = None

    def _by_exchange(self, exchange_id: str) -> TrackedOrder | None:
        for order in self.state.orders:
            if order.exchange_id == exchange_id:
                return order
        return None
