"""Equal gross-USD sell engine.

A fill increases that outcome's cumulative gross proceeds (price * shares).
The engine then targets only the remaining net difference:

    deficit = gross_sold[ahead] - gross_sold[behind]

One opposite SELL, sized from the uncovered deficit, may be posted. A later
fill on the behind side reduces that deficit and does not start a reverse
hedge chain. Hedge orders are cancelled when they would overshoot the deficit.
Child hedges are not created from a hedge fill.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from poly_market_maker.sell_equal.config import SellConfig
from poly_market_maker.sell_equal.quant import (
    floor_to_step,
    gross,
    price_in_bounds,
    shares_for_gross,
)
from poly_market_maker.sell_equal.state import (
    ACTIVE_STATUSES,
    BotState,
    TrackedOrder,
    load_state,
    save_state,
)


@dataclass(frozen=True)
class Fill:
    fill_id: str
    exchange_order_id: str
    shares: Decimal
    price: Decimal


@dataclass(frozen=True)
class SubmitResult:
    status: str
    exchange_id: str | None = None


@dataclass(frozen=True)
class CancelResult:
    status: str
    canceled_ids: frozenset[str] = frozenset()
    not_canceled: dict[str, str] | None = None


@dataclass
class MarketView:
    ts: Decimal
    accepting: bool
    ended: bool
    tick_size: Decimal
    min_order_size: Decimal


class Broker:
    """Minimal broker surface. Implementations must sell only."""

    def submit_sell(self, token_id: str, price: Decimal, size: Decimal) -> SubmitResult:
        raise NotImplementedError

    def cancel(self, order_ids: list[str]) -> CancelResult:
        raise NotImplementedError


class SellEqualEngine:
    def __init__(
        self,
        config: SellConfig,
        broker: Broker,
        now,
        state: BotState | None = None,
    ):
        self.config = config
        self.broker = broker
        self.now = now
        self.market: MarketView | None = None
        if state is None:
            state = BotState(
                on_hand={
                    "up": config.inventory_up,
                    "down": config.inventory_down,
                },
                start_on_hand={
                    "up": config.inventory_up,
                    "down": config.inventory_down,
                },
            )
        self.state = state
        self._seen = set(state.seen_fill_ids)
        self._persist()

    @classmethod
    def resume(cls, config: SellConfig, broker: Broker, now, path: str):
        return cls(config, broker, now, load_state(path))

    def observe_market(self, view: MarketView) -> None:
        self.market = view
        self.state.last_market_ts = view.ts
        self._refresh_feed_halt()
        self._persist()

    def observe_user(self, ts: Decimal) -> None:
        self.state.last_user_ts = ts
        self._refresh_feed_halt()
        self._persist()

    def tick(self) -> None:
        self._refresh_feed_halt()
        if self.state.halt_reason == "market_ended":
            self._cancel_active(self._active_orders(), "market_ended")
            return
        if self.state.halt_reason:
            return
        if not self._feeds_ok():
            return
        if not self.state.seeds_placed:
            self._place_seeds()
            self.state.seeds_placed = True
            self._persist()
        self._rebalance()

    def note_failed_trade(self, fill_id: str) -> None:
        if fill_id not in self._seen:
            self._seen.add(fill_id)
            self.state.seen_fill_ids.append(fill_id)
        self._halt("fill_failed")
        self._persist()

    def consume_user_batch(self, batch) -> None:
        for fill_id in batch.failed_ids:
            self.note_failed_trade(fill_id)
        if self.state.halt_reason == "fill_failed":
            return
        self.apply_fills(list(batch.fills))
        for order_id, size_matched in batch.size_matched:
            if self.state.halt_reason:
                return
            self.note_size_matched(order_id, size_matched)

    def apply_fills(self, fills: list[Fill]) -> None:
        for item in fills:
            self._apply_fill(item)
        self._check_session_loss()
        if self.state.halt_reason in (None, "session_loss"):
            if self.state.halt_reason == "session_loss":
                self._cancel_active(self._active_orders(), "session_loss")
            elif self._feeds_ok():
                self._rebalance()
        self._persist()

    def note_size_matched(self, exchange_order_id: str, size_matched: Decimal) -> None:
        order = self._by_exchange_id(exchange_order_id)
        if order is None:
            self._halt("unknown_order_fill")
            self._persist()
            return
        self._apply_gap(order, size_matched)
        self._check_session_loss()
        if self.state.halt_reason is None and self._feeds_ok():
            self._rebalance()
        self._persist()

    def reconcile(
        self,
        open_ids: set[str],
        size_matched: dict[str, Decimal],
        read_ok: bool,
        ts: Decimal,
    ) -> None:
        if not read_ok:
            self._halt("stale_user")
            self._persist()
            return
        self.state.last_user_ts = ts
        for order in list(self.state.orders):
            if not order.exchange_id:
                continue
            matched = size_matched.get(order.exchange_id)
            if matched is not None:
                self._apply_gap(order, matched)
            self._resolve_presence(order, open_ids)
        self._clear_resolved_unknown()
        self._refresh_feed_halt()
        if self.state.halt_reason is None and self._feeds_ok():
            self._rebalance()
        self._persist()

    def shutdown(self) -> None:
        """Cancel only orders this engine submitted. Never an account-wide cancel."""

        self._cancel_active(self._active_orders(), "shutdown")
        self._persist()

    def available(self, outcome: str) -> Decimal:
        reserved = sum(
            (
                order.reserved_size
                for order in self.state.orders
                if order.outcome == outcome and order.status in ACTIVE_STATUSES
            ),
            Decimal("0"),
        )
        return self.state.on_hand[outcome] - reserved

    def open_orders(self) -> list[TrackedOrder]:
        return [order for order in self.state.orders if order.status == "open"]

    def hedges(self, outcome: str | None = None) -> list[TrackedOrder]:
        rows = [
            order
            for order in self.state.orders
            if order.role == "hedge" and order.status in ACTIVE_STATUSES
        ]
        if outcome is None:
            return rows
        return [order for order in rows if order.outcome == outcome]

    def session_loss(self) -> Decimal:
        start = self.state.start_on_hand["up"] * self.config.mark(
            "up"
        ) + self.state.start_on_hand["down"] * self.config.mark("down")
        held = self.state.on_hand["up"] * self.config.mark("up") + self.state.on_hand[
            "down"
        ] * self.config.mark("down")
        cash = self.state.gross_sold["up"] + self.state.gross_sold["down"]
        return start - (held + cash)

    def outstanding_notional(self) -> Decimal:
        total = Decimal("0")
        for order in self.state.orders:
            if order.status in ACTIVE_STATUSES:
                total += order.remaining * order.price
        return total

    def _persist(self) -> None:
        if self.config.state_path:
            save_state(self.state, self.config.state_path)

    def _halt(self, reason: str) -> None:
        if self.state.halt_reason is None:
            self.state.halt_reason = reason

    def _feeds_ok(self) -> bool:
        return (
            self.state.halt_reason is None
            and self._market_status() == "ok"
            and (not self.config.require_user_feed or self._user_fresh())
        )

    def _refresh_feed_halt(self) -> None:
        status = self._market_status()
        if status == "ended":
            self._halt("market_ended")
            return
        if status != "ok":
            self._halt(status)
            return
        if self.config.require_user_feed and not self._user_fresh():
            self._halt("stale_user")
            return
        if self.state.halt_reason in (
            "stale_market",
            "stale_user",
            "market_not_accepting",
        ):
            if not self._has_unresolved_unknown():
                self.state.halt_reason = None

    def _market_status(self) -> str:
        view = self.market
        if view is None or self.state.last_market_ts is None:
            return "stale_market"
        age = self.now() - self.state.last_market_ts
        if age > self.config.stale_after_seconds or age < 0:
            return "stale_market"
        if view.ended or self.now() >= self.config.market_end_ts:
            return "ended"
        if not view.accepting:
            return "market_not_accepting"
        if view.tick_size != self.config.tick_size:
            return "tick_mismatch"
        if view.min_order_size != self.config.min_order_size:
            return "min_size_mismatch"
        return "ok"

    def _user_fresh(self) -> bool:
        if self.state.last_user_ts is None:
            return False
        age = self.now() - self.state.last_user_ts
        return Decimal("0") <= age <= self.config.stale_after_seconds

    def _has_unresolved_unknown(self) -> bool:
        return any(
            order.status in ("unknown_submit", "unknown_cancel", "intent")
            or (
                order.status == "cancel_requested"
                and order.exchange_id not in self.state.confirmed_cancel_ids
            )
            for order in self.state.orders
        )

    def _place_seeds(self) -> None:
        for outcome in ("up", "down"):
            for price in self._seed_prices():
                self._try_submit(
                    outcome=outcome,
                    role="seed",
                    price=price,
                    size=self.config.seed_shares,
                    gross_cap=None,
                )

    def _seed_prices(self) -> list[Decimal]:
        prices = []
        price = self.config.seed_price_min
        while price <= self.config.seed_price_max:
            prices.append(price)
            price += self.config.seed_price_step
        return prices

    def _rebalance(self) -> None:
        if self.state.halt_reason or not self._feeds_ok():
            return
        ahead = self._deficit()
        if ahead is None:
            self._cancel_active(self.hedges(), "deficit_flat")
            return
        outcome, deficit = ahead
        wrong = [order for order in self.hedges() if order.outcome != outcome]
        if wrong:
            self._cancel_active(wrong, "wrong_side_hedge")
            return
        pending = self._pending_hedge_gross(outcome)
        uncovered = deficit - pending
        if uncovered < -self.config.value_dust:
            excess = self._hedges_to_shrink(outcome, deficit)
            if excess:
                self._cancel_active(excess, "excess_hedge")
            return
        if uncovered <= self.config.value_dust:
            return
        price = self._hedge_limit(outcome)
        if price is None:
            return
        size = shares_for_gross(
            uncovered,
            price,
            self.config.size_quantum,
            self.config.min_order_size,
            self.available(outcome),
        )
        if size is None:
            return
        self._try_submit(
            outcome=outcome,
            role="hedge",
            price=price,
            size=size,
            gross_cap=uncovered,
        )

    def _deficit(self) -> tuple[str, Decimal] | None:
        gap = self.state.gross_sold["up"] - self.state.gross_sold["down"]
        if gap > self.config.value_dust:
            return "down", gap
        if gap < -self.config.value_dust:
            return "up", -gap
        return None

    def _signed_gross_gap(self) -> Decimal:
        return self.state.gross_sold["up"] - self.state.gross_sold["down"]

    def _pending_hedge_gross(self, outcome: str) -> Decimal:
        total = Decimal("0")
        for order in self.hedges(outcome):
            total += order.remaining * order.price
        return total

    def _hedges_to_shrink(self, outcome: str, deficit: Decimal) -> list[TrackedOrder]:
        rows = sorted(
            self.hedges(outcome),
            key=lambda order: order.remaining * order.price,
            reverse=True,
        )
        pending = self._pending_hedge_gross(outcome)
        chosen = []
        for order in rows:
            if pending <= deficit + self.config.value_dust:
                break
            chosen.append(order)
            pending -= order.remaining * order.price
        return chosen

    def _hedge_limit(self, outcome: str) -> Decimal | None:
        price = self.config.hedge_price(outcome)
        tick = self.config.tick_size
        moved = floor_to_step(price, tick)
        if abs(moved - price) > tick * self.config.max_reprice_ticks:
            return None
        if not price_in_bounds(
            moved, tick, self.config.price_floor, self.config.price_ceiling
        ):
            return None
        return moved

    def _try_submit(
        self,
        outcome: str,
        role: str,
        price: Decimal,
        size: Decimal,
        gross_cap: Decimal | None,
    ) -> None:
        if self.state.halt_reason:
            return
        if not price_in_bounds(
            price,
            self.config.tick_size,
            self.config.price_floor,
            self.config.price_ceiling,
        ):
            return
        size = floor_to_step(size, self.config.size_quantum)
        if size < self.config.min_order_size:
            return
        if size > self.available(outcome):
            return
        if gross_cap is not None and size * price > gross_cap:
            return
        added = size * price
        if self.outstanding_notional() + added > self.config.max_outstanding_notional:
            return
        if (
            role == "seed"
            and abs(self._signed_gross_gap()) > self.config.max_gross_skew
        ):
            return
        order = TrackedOrder(
            local_id="local-%04d" % self.state.next_local,
            exchange_id=None,
            outcome=outcome,
            role=role,
            price=price,
            original_size=size,
            reserved_size=size,
            status="intent",
        )
        self.state.next_local += 1
        self.state.orders.append(order)
        self._persist()
        try:
            result = self.broker.submit_sell(self.config.token_id(outcome), price, size)
        except Exception:
            order.status = "unknown_submit"
            self._halt("unknown_submit")
            self._persist()
            return
        self._apply_submit(order, result)

    def _apply_submit(self, order: TrackedOrder, result: SubmitResult) -> None:
        if result.status == "accepted" and result.exchange_id:
            order.exchange_id = result.exchange_id
            order.status = "open"
        elif result.status == "rejected":
            order.status = "rejected"
            order.reserved_size = Decimal("0")
        else:
            order.exchange_id = result.exchange_id or order.exchange_id
            order.status = "unknown_submit"
            self._halt("unknown_submit")
        self._persist()

    def _apply_fill(self, fill: Fill) -> None:
        if fill.fill_id in self._seen:
            return
        self._seen.add(fill.fill_id)
        self.state.seen_fill_ids.append(fill.fill_id)
        order = self._by_exchange_id(fill.exchange_order_id)
        if order is None:
            self._halt("unknown_order_fill")
            return
        if fill.shares <= 0:
            return
        projected = order.trade_accounted + fill.shares
        if projected <= order.filled_size:
            order.trade_accounted = projected
            return
        delta = projected - order.filled_size
        if order.filled_size + delta > order.original_size:
            self._halt("fill_exceeds_order")
            return
        self._credit(order, delta, fill.price)
        order.trade_accounted = projected

    def _apply_gap(self, order: TrackedOrder, size_matched: Decimal) -> None:
        if size_matched < order.filled_size:
            self._halt("matched_shrunk")
            return
        if size_matched == order.filled_size:
            return
        if size_matched > order.original_size:
            self._halt("fill_exceeds_order")
            return
        fill_id = "gap:%s:%s" % (order.exchange_id, size_matched)
        if fill_id in self._seen:
            return
        delta = size_matched - order.filled_size
        self._seen.add(fill_id)
        self.state.seen_fill_ids.append(fill_id)
        self._credit(order, delta, order.price)

    def _credit(self, order: TrackedOrder, delta: Decimal, price: Decimal) -> None:
        if delta > order.reserved_size or delta > self.state.on_hand[order.outcome]:
            self._halt("oversell")
            return
        order.filled_size += delta
        order.reserved_size -= delta
        self.state.on_hand[order.outcome] -= delta
        self.state.gross_sold[order.outcome] += gross(delta, price)
        if order.filled_size == order.original_size:
            order.status = "filled"
            order.reserved_size = Decimal("0")

    def _check_session_loss(self) -> None:
        if self.session_loss() >= self.config.max_session_loss:
            self._halt("session_loss")

    def _cancel_active(self, orders: list[TrackedOrder], reason: str) -> None:
        del reason
        targets = [
            order
            for order in orders
            if order.status in ("open", "unknown_cancel", "cancel_requested")
            and order.exchange_id
        ]
        if not targets:
            return
        for order in targets:
            order.status = "cancel_requested"
        self._persist()
        ids = [order.exchange_id for order in targets if order.exchange_id]
        try:
            result = self.broker.cancel(ids)
        except Exception:
            result = CancelResult(status="unknown")
        self._apply_cancel(targets, result)

    def _apply_cancel(self, targets: list[TrackedOrder], result: CancelResult) -> None:
        if result.status != "known":
            for order in targets:
                order.status = "unknown_cancel"
            self._halt("unknown_cancel")
            self._persist()
            return
        canceled = result.canceled_ids
        failed = result.not_canceled or {}
        for order in targets:
            exchange_id = order.exchange_id or ""
            if exchange_id in canceled:
                self._release_cancelled(order)
            elif exchange_id in failed:
                order.status = "open"
            else:
                order.status = "unknown_cancel"
                self._halt("unknown_cancel")
        self._persist()

    def _release_cancelled(self, order: TrackedOrder) -> None:
        order.reserved_size = Decimal("0")
        order.status = (
            "filled" if order.filled_size >= order.original_size else "cancelled"
        )
        if (
            order.exchange_id
            and order.exchange_id not in self.state.confirmed_cancel_ids
        ):
            self.state.confirmed_cancel_ids.append(order.exchange_id)

    def _resolve_presence(self, order: TrackedOrder, open_ids: set[str]) -> None:
        if order.status not in ACTIVE_STATUSES or not order.exchange_id:
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
            self._release_cancelled(order)
            return
        self._halt("unexplained_absence")

    def _clear_resolved_unknown(self) -> None:
        if self.state.halt_reason == "unknown_submit" and not any(
            order.status == "unknown_submit" for order in self.state.orders
        ):
            self.state.halt_reason = None
        if self.state.halt_reason == "unknown_cancel" and not any(
            order.status == "unknown_cancel" for order in self.state.orders
        ):
            self.state.halt_reason = None

    def _active_orders(self) -> list[TrackedOrder]:
        return [order for order in self.state.orders if order.status in ACTIVE_STATUSES]

    def _by_exchange_id(self, exchange_id: str) -> TrackedOrder | None:
        for order in self.state.orders:
            if order.exchange_id == exchange_id:
                return order
        return None
