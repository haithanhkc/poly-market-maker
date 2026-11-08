"""Ledger objects. Risk-lock fields live here so a restart cannot loosen them."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

from paper_split_maker.config import Config
from paper_split_maker.decimal_utils import D, ZERO, money

SCHEMA_VERSION = 1
TERMINAL_ORDER_STATUSES = frozenset({"cancelled", "filled"})
WORKING_ORDER_STATUSES = frozenset({"queued", "live", "cancel_requested"})


def _d(value: object) -> Decimal:
    return D(value)


@dataclass
class LevelBook:
    ts_ms: int = -1
    bids: list[tuple[Decimal, Decimal]] = field(default_factory=list)
    asks: list[tuple[Decimal, Decimal]] = field(default_factory=list)
    digest: str = ""
    uncertain: bool = True

    def best_bid(self) -> Optional[Decimal]:
        return self.bids[0][0] if self.bids else None

    def best_ask(self) -> Optional[Decimal]:
        return self.asks[0][0] if self.asks else None

    def ask_size_at(self, price: Decimal) -> Decimal:
        for level, size in self.asks:
            if level == price:
                return size
        return ZERO

    def to_dict(self) -> dict:
        return {
            "ts_ms": self.ts_ms,
            "bids": [[money(p), money(s)] for p, s in self.bids],
            "asks": [[money(p), money(s)] for p, s in self.asks],
            "digest": self.digest,
            "uncertain": self.uncertain,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "LevelBook":
        return cls(
            ts_ms=int(data["ts_ms"]),
            bids=[(_d(p), _d(s)) for p, s in data["bids"]],
            asks=[(_d(p), _d(s)) for p, s in data["asks"]],
            digest=str(data.get("digest", "")),
            uncertain=bool(data["uncertain"]),
        )


@dataclass
class Market:
    market_id: str
    condition_id: str
    slug: str
    question: str
    up_token_id: str
    down_token_id: str
    up_label: str
    down_label: str
    up_index: int
    down_index: int
    end_ts_ms: int
    tick_size: Decimal
    min_order_size: Decimal
    synthetic: bool
    accepting_orders: bool = True
    up: Decimal = ZERO
    down: Decimal = ZERO
    gross_split: Decimal = ZERO
    net_cash_flow: Decimal = ZERO
    status: str = "open"
    winner: Optional[str] = None
    resolution_source: Optional[str] = None
    resolution_ts_ms: Optional[int] = None
    resolution_conflict: bool = False
    reserve_active: bool = False
    operational_reserve_amount: Decimal = ZERO
    realized_recorded: bool = False
    up_book: LevelBook = field(default_factory=LevelBook)
    down_book: LevelBook = field(default_factory=LevelBook)
    last_action_ts: int = -(10**18)
    last_skip_reason: Optional[str] = None
    tape_seeded: bool = False
    sold_up_shares: Decimal = ZERO
    sold_up_proceeds: Decimal = ZERO
    sold_down_shares: Decimal = ZERO
    sold_down_proceeds: Decimal = ZERO
    session_stopped: bool = False
    pause_until_ms: int = 0
    last_mid: Optional[Decimal] = None
    last_mid_ts: int = 0
    last_requote_ts: int = -(10**18)
    pending_split: Decimal = ZERO

    def inventory(self, outcome: str) -> Decimal:
        return self.up if outcome == "up" else self.down

    def book(self, outcome: str) -> LevelBook:
        return self.up_book if outcome == "up" else self.down_book

    def to_dict(self) -> dict:
        return {
            "market_id": self.market_id,
            "condition_id": self.condition_id,
            "slug": self.slug,
            "question": self.question,
            "up_token_id": self.up_token_id,
            "down_token_id": self.down_token_id,
            "up_label": self.up_label,
            "down_label": self.down_label,
            "up_index": self.up_index,
            "down_index": self.down_index,
            "end_ts_ms": self.end_ts_ms,
            "tick_size": money(self.tick_size),
            "min_order_size": money(self.min_order_size),
            "synthetic": self.synthetic,
            "accepting_orders": self.accepting_orders,
            "up": money(self.up),
            "down": money(self.down),
            "gross_split": money(self.gross_split),
            "net_cash_flow": money(self.net_cash_flow),
            "status": self.status,
            "winner": self.winner,
            "resolution_source": self.resolution_source,
            "resolution_ts_ms": self.resolution_ts_ms,
            "resolution_conflict": self.resolution_conflict,
            "reserve_active": self.reserve_active,
            "operational_reserve_amount": money(self.operational_reserve_amount),
            "realized_recorded": self.realized_recorded,
            "up_book": self.up_book.to_dict(),
            "down_book": self.down_book.to_dict(),
            "last_action_ts": self.last_action_ts,
            "last_skip_reason": self.last_skip_reason,
            "tape_seeded": self.tape_seeded,
            "sold_up_shares": money(self.sold_up_shares),
            "sold_up_proceeds": money(self.sold_up_proceeds),
            "sold_down_shares": money(self.sold_down_shares),
            "sold_down_proceeds": money(self.sold_down_proceeds),
            "session_stopped": self.session_stopped,
            "pause_until_ms": self.pause_until_ms,
            "last_mid": None if self.last_mid is None else money(self.last_mid),
            "last_mid_ts": self.last_mid_ts,
            "last_requote_ts": self.last_requote_ts,
            "pending_split": money(self.pending_split),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Market":
        return cls(
            market_id=data["market_id"],
            condition_id=data["condition_id"],
            slug=data["slug"],
            question=data["question"],
            up_token_id=str(data["up_token_id"]),
            down_token_id=str(data["down_token_id"]),
            up_label=data["up_label"],
            down_label=data["down_label"],
            up_index=int(data["up_index"]),
            down_index=int(data["down_index"]),
            end_ts_ms=int(data["end_ts_ms"]),
            tick_size=_d(data["tick_size"]),
            min_order_size=_d(data["min_order_size"]),
            synthetic=bool(data["synthetic"]),
            accepting_orders=bool(data["accepting_orders"]),
            up=_d(data["up"]),
            down=_d(data["down"]),
            gross_split=_d(data["gross_split"]),
            net_cash_flow=_d(data["net_cash_flow"]),
            status=data["status"],
            winner=data.get("winner"),
            resolution_source=data.get("resolution_source"),
            resolution_ts_ms=data.get("resolution_ts_ms"),
            resolution_conflict=bool(data.get("resolution_conflict", False)),
            reserve_active=bool(data["reserve_active"]),
            operational_reserve_amount=_d(data["operational_reserve_amount"]),
            realized_recorded=bool(data["realized_recorded"]),
            up_book=LevelBook.from_dict(data["up_book"]),
            down_book=LevelBook.from_dict(data["down_book"]),
            last_action_ts=int(data["last_action_ts"]),
            last_skip_reason=data.get("last_skip_reason"),
            tape_seeded=bool(data.get("tape_seeded", False)),
            sold_up_shares=_d(data.get("sold_up_shares", "0")),
            sold_up_proceeds=_d(data.get("sold_up_proceeds", "0")),
            sold_down_shares=_d(data.get("sold_down_shares", "0")),
            sold_down_proceeds=_d(data.get("sold_down_proceeds", "0")),
            session_stopped=bool(data.get("session_stopped", False)),
            pause_until_ms=int(data.get("pause_until_ms", 0)),
            last_mid=None
            if data.get("last_mid") in (None, "")
            else _d(data.get("last_mid")),
            last_mid_ts=int(data.get("last_mid_ts", 0)),
            last_requote_ts=int(data.get("last_requote_ts", -(10**18))),
            pending_split=_d(data.get("pending_split", "0")),
        )


@dataclass
class Order:
    order_id: str
    market_id: str
    outcome: str
    price: Decimal
    size: Decimal
    remaining: Decimal
    queue_ahead: Decimal
    status: str
    submit_ts: int
    live_ts: Optional[int] = None
    cancel_request_ts: Optional[int] = None
    cancel_ready_ts: Optional[int] = None
    risk_reducing: bool = False
    exchange_id: str = ""

    def to_dict(self) -> dict:
        return {
            "order_id": self.order_id,
            "market_id": self.market_id,
            "outcome": self.outcome,
            "price": money(self.price),
            "size": money(self.size),
            "remaining": money(self.remaining),
            "queue_ahead": money(self.queue_ahead),
            "status": self.status,
            "submit_ts": self.submit_ts,
            "live_ts": self.live_ts,
            "cancel_request_ts": self.cancel_request_ts,
            "cancel_ready_ts": self.cancel_ready_ts,
            "risk_reducing": self.risk_reducing,
            "exchange_id": self.exchange_id,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Order":
        return cls(
            order_id=data["order_id"],
            market_id=data["market_id"],
            outcome=data["outcome"],
            price=_d(data["price"]),
            size=_d(data["size"]),
            remaining=_d(data["remaining"]),
            queue_ahead=_d(data["queue_ahead"]),
            status=data["status"],
            submit_ts=int(data["submit_ts"]),
            live_ts=data.get("live_ts"),
            cancel_request_ts=data.get("cancel_request_ts"),
            cancel_ready_ts=data.get("cancel_ready_ts"),
            risk_reducing=bool(data.get("risk_reducing", False)),
            exchange_id=str(data.get("exchange_id") or ""),
        )


@dataclass
class State:
    initial_capital: Decimal
    loss_floor: Decimal
    max_loss_fraction: Decimal
    cash: Decimal
    realized_pnl: Decimal
    rebate_estimate: Decimal
    now_ms: int
    feed_up: bool
    order_seq: int
    risk_fee_rate: Decimal
    maker_fee_rate: Decimal
    operational_reserve: Decimal
    global_rounding_reserve: Decimal
    per_market_budget: Decimal
    ack_latency_ms: int
    cancel_latency_ms: int
    fill_mode: str
    trade_through_fills: bool
    published_taker_fee_rate: Decimal
    rebate_share: Decimal
    markets: dict[str, Market] = field(default_factory=dict)
    orders: dict[str, Order] = field(default_factory=dict)
    applied_event_ids: set[str] = field(default_factory=set)
    seen_trade_ids: set[str] = field(default_factory=set)
    risk_rejections: list[dict] = field(default_factory=list)
    was_at_cap: bool = False
    schema_version: int = SCHEMA_VERSION
    session_max_loss: Decimal = ZERO
    max_pair_loss: Decimal = Decimal("1")
    killed: bool = False
    btc_price: Optional[Decimal] = None
    btc_price_ts: int = 0
    mismatch_stop: bool = False

    @classmethod
    def fresh(cls, config: Config) -> "State":
        config.validate()
        return cls(
            initial_capital=config.initial_capital,
            loss_floor=config.loss_floor(),
            max_loss_fraction=config.max_loss_fraction,
            cash=config.initial_capital,
            realized_pnl=ZERO,
            rebate_estimate=ZERO,
            now_ms=0,
            feed_up=True,
            order_seq=0,
            risk_fee_rate=config.risk_fee_rate,
            maker_fee_rate=config.maker_fee_rate,
            operational_reserve=config.operational_reserve,
            global_rounding_reserve=config.global_rounding_reserve,
            per_market_budget=config.per_market_budget,
            ack_latency_ms=config.ack_latency_ms,
            cancel_latency_ms=config.cancel_latency_ms,
            fill_mode=config.fill_mode,
            trade_through_fills=config.trade_through_fills,
            published_taker_fee_rate=config.published_taker_fee_rate,
            rebate_share=config.rebate_share,
            session_max_loss=config.session_max_loss,
            max_pair_loss=config.max_pair_loss,
        )

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "initial_capital": money(self.initial_capital),
            "loss_floor": money(self.loss_floor),
            "max_loss_fraction": money(self.max_loss_fraction),
            "cash": money(self.cash),
            "realized_pnl": money(self.realized_pnl),
            "rebate_estimate": money(self.rebate_estimate),
            "now_ms": self.now_ms,
            "feed_up": self.feed_up,
            "order_seq": self.order_seq,
            "risk_fee_rate": money(self.risk_fee_rate),
            "maker_fee_rate": money(self.maker_fee_rate),
            "operational_reserve": money(self.operational_reserve),
            "global_rounding_reserve": money(self.global_rounding_reserve),
            "per_market_budget": money(self.per_market_budget),
            "ack_latency_ms": self.ack_latency_ms,
            "cancel_latency_ms": self.cancel_latency_ms,
            "fill_mode": self.fill_mode,
            "trade_through_fills": self.trade_through_fills,
            "published_taker_fee_rate": money(self.published_taker_fee_rate),
            "rebate_share": money(self.rebate_share),
            "markets": {
                key: market.to_dict() for key, market in sorted(self.markets.items())
            },
            "orders": {
                key: order.to_dict() for key, order in sorted(self.orders.items())
            },
            "applied_event_ids": sorted(self.applied_event_ids),
            "seen_trade_ids": sorted(self.seen_trade_ids),
            "risk_rejections": list(self.risk_rejections),
            "was_at_cap": self.was_at_cap,
            "session_max_loss": money(self.session_max_loss),
            "max_pair_loss": money(self.max_pair_loss),
            "killed": self.killed,
            "btc_price": None if self.btc_price is None else money(self.btc_price),
            "btc_price_ts": self.btc_price_ts,
            "mismatch_stop": self.mismatch_stop,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "State":
        version = int(data["schema_version"])
        if version != SCHEMA_VERSION:
            raise ValueError(f"unsupported state schema {version}")
        state = cls(
            initial_capital=_d(data["initial_capital"]),
            loss_floor=_d(data["loss_floor"]),
            max_loss_fraction=_d(data["max_loss_fraction"]),
            cash=_d(data["cash"]),
            realized_pnl=_d(data["realized_pnl"]),
            rebate_estimate=_d(data["rebate_estimate"]),
            now_ms=int(data["now_ms"]),
            feed_up=bool(data["feed_up"]),
            order_seq=int(data["order_seq"]),
            risk_fee_rate=_d(data["risk_fee_rate"]),
            maker_fee_rate=_d(data["maker_fee_rate"]),
            operational_reserve=_d(data["operational_reserve"]),
            global_rounding_reserve=_d(data["global_rounding_reserve"]),
            per_market_budget=_d(data["per_market_budget"]),
            ack_latency_ms=int(data["ack_latency_ms"]),
            cancel_latency_ms=int(data["cancel_latency_ms"]),
            fill_mode=str(data["fill_mode"]),
            trade_through_fills=bool(data["trade_through_fills"]),
            published_taker_fee_rate=_d(data["published_taker_fee_rate"]),
            rebate_share=_d(data["rebate_share"]),
            was_at_cap=bool(data.get("was_at_cap", False)),
            schema_version=version,
            session_max_loss=_d(data.get("session_max_loss", "0")),
            max_pair_loss=_d(data.get("max_pair_loss", "1")),
            killed=bool(data.get("killed", False)),
            btc_price=None
            if data.get("btc_price") in (None, "")
            else _d(data.get("btc_price")),
            btc_price_ts=int(data.get("btc_price_ts", 0)),
            mismatch_stop=bool(data.get("mismatch_stop", False)),
        )
        state.markets = {
            key: Market.from_dict(value) for key, value in data["markets"].items()
        }
        state.orders = {
            key: Order.from_dict(value) for key, value in data["orders"].items()
        }
        state.applied_event_ids = set(data.get("applied_event_ids", []))
        state.seen_trade_ids = set(data.get("seen_trade_ids", []))
        state.risk_rejections = list(data.get("risk_rejections", []))
        return state
