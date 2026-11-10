"""Two-sided maker-sell quotes from each outcome's own book.

Down is not priced as one minus Up. A missing edge, a wide book, or a quote
that would cross the bid produces no order. Skew shrinks and reprices the
sell that would add to an existing imbalance; it does not authorize a sale at
an arbitrary price.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from paper_split_maker.config import Config
from paper_split_maker.decimal_utils import ZERO, ceil_tick, qdown
from paper_split_maker.models import WORKING_ORDER_STATUSES, Market, State
from paper_split_maker.risk import reserved


@dataclass(frozen=True)
class Proposal:
    outcome: str
    price: Decimal
    size: Decimal
    fair: Decimal
    required_edge: Decimal
    repair: bool
    reason: str = "ok"


def pair_edge_floor(market: Market, outcome: str, config: Config):
    """Minimum price for shares that pair with the other side already sold.

    Each pair pays 1 at resolution, so selling this side below
    ``1 + pair_min_edge - avg_opposite`` locks in a loss on those shares.
    """
    edge = config.pair_min_edge
    if outcome == "up":
        opposite, proceeds, own = (
            market.sold_down_shares,
            market.sold_down_proceeds,
            market.sold_up_shares,
        )
    else:
        opposite, proceeds, own = (
            market.sold_up_shares,
            market.sold_up_proceeds,
            market.sold_down_shares,
        )
    if opposite <= 0 or opposite <= own:
        return None
    average = proceeds / opposite
    floor = Decimal("1") + edge - average
    if floor <= 0:
        return None
    return floor


def _other(outcome: str) -> str:
    return "down" if outcome == "up" else "up"


def matched_to_sold(market: Market, outcome: str) -> Decimal:
    """Shares of ``outcome`` that pair with the other side already sold."""
    if outcome == "up":
        gap = market.sold_down_shares - market.sold_up_shares
    else:
        gap = market.sold_up_shares - market.sold_down_shares
    if gap < 0:
        return ZERO
    return gap


def maker_sell_price(market: Market, outcome: str, config: Config) -> Decimal | None:
    """Price ``propose`` would post before any pair floor. None if the book is unquotable."""
    book = market.book(outcome)
    bid = book.best_bid()
    ask = book.best_ask()
    if bid is None or ask is None or ask <= bid or ask - bid > config.max_spread:
        return None
    self_inv = market.inventory(outcome)
    other_inv = market.down if outcome == "up" else market.up
    adverse = other_inv - self_inv
    if adverse < 0:
        adverse = ZERO
    required = config.min_edge + config.skew_edge_per_share * adverse
    fair = (bid + ask) / 2
    raw = ask if ask >= fair + required else fair + required
    price = ceil_tick(raw, market.tick_size)
    if price <= bid or price >= 1 or price < config.min_price or price > config.max_price:
        return None
    return price


def planned_opposite_quote(
    state: State, market: Market, outcome: str, config: Config
) -> Decimal:
    """Price we are quoting, or would quote, on the other side.

    That is the best of our posted ask, one tick above that side's bid, and
    the maker price we would post there before the pair floor.
    """
    other = _other(outcome)
    book = market.book(other)
    bid = book.best_bid()
    ask = book.best_ask()
    tick = market.tick_size if market.tick_size > 0 else Decimal("0.01")
    quotable = (
        bid is not None
        and ask is not None
        and ask > bid
        and ask - bid <= config.max_spread
    )
    planned = (bid + tick) if quotable else ZERO
    for order in state.orders.values():
        if (
            order.market_id == market.market_id
            and order.outcome == other
            and order.status in WORKING_ORDER_STATUSES
            and order.price > planned
        ):
            planned = order.price
    natural = maker_sell_price(market, other, config)
    if natural is not None and natural > planned:
        planned = natural
    filled = pair_edge_floor(market, other, config)
    if filled is not None:
        raised = ceil_tick(filled, tick)
        if raised > planned:
            planned = raised
    return planned


def pair_plan_covers(
    state: State, market: Market, outcome: str, price: Decimal, config: Config
) -> bool:
    """True when this price plus the planned opposite quote clears the pair edge."""
    if price <= 0:
        return False
    planned = planned_opposite_quote(state, market, outcome, config)
    return price + planned >= Decimal("1") + config.pair_min_edge


def binding_sell_floor(
    state: State,
    market: Market,
    outcome: str,
    config: Config,
    size: Decimal,
    price: Decimal | None = None,
) -> Decimal | None:
    """Lowest price a working sell of ``size`` may use.

    Shares matched to the other side's fills use ``1 + edge - avg_sold``.
    Extra size uses ``1 + edge - planned_opposite`` only when this price
    already belongs to a covered pair, so a quote the loss budget allowed
    on its own is not pulled up to 1.01.
    """
    floors: list[Decimal] = []
    edge = pair_edge_floor(market, outcome, config)
    if edge is not None and edge > 0:
        floors.append(edge)
    if size > matched_to_sold(market, outcome):
        planned = planned_opposite_quote(state, market, outcome, config)
        probe = price if price is not None else maker_sell_price(market, outcome, config)
        if (
            probe is not None
            and probe + planned >= Decimal("1") + config.pair_min_edge
        ):
            uncovered = Decimal("1") + config.pair_min_edge - planned
            if uncovered > 0:
                floors.append(uncovered)
    if not floors:
        return None
    return max(floors)


def unpaired_sell_room(
    market: Market, outcome: str, price: Decimal, config: Config
):
    """How many shares of ``outcome`` may still be sold.

    Fresh one-sided size is capped by ``max_unpaired_fraction`` of the split
    and by ``initial_capital * max_loss_fraction``. A planned pair does not
    waive that budget: one filled leg can be the winner. Shares that catch up
    to the other side's fills complete that leg and stay allowed.
    """
    split = market.gross_split
    if split <= 0:
        return None
    if outcome == "up":
        sold, other, proceeds = (
            market.sold_up_shares,
            market.sold_down_shares,
            market.sold_up_proceeds,
        )
    else:
        sold, other, proceeds = (
            market.sold_down_shares,
            market.sold_up_shares,
            market.sold_down_proceeds,
        )
    minimum = (
        market.min_order_size
        if market.min_order_size > config.min_order_size
        else config.min_order_size
    )
    cap = config.max_unpaired_fraction * split
    # A 5-share split cannot post half a minimum order. The fraction still
    # caps a full-size split (15 of 30) and the loss budget caps a cheap sell.
    if ZERO < cap < minimum <= split:
        cap = minimum
    ahead = sold - other
    if ahead < 0:
        ahead = ZERO
    fraction_room = cap - ahead
    if fraction_room < 0:
        fraction_room = ZERO
    budget = config.initial_capital * config.max_loss_fraction
    existing = ZERO
    if ahead > 0 and sold > 0:
        existing = ahead * (Decimal("1") - proceeds / sold)
        if existing < 0:
            existing = ZERO
    remain = budget - existing
    loss_per = Decimal("1") - price
    if loss_per <= 0:
        loss_room = fraction_room
    elif remain <= 0:
        loss_room = ZERO
    else:
        loss_room = remain / loss_per
    fresh = fraction_room if fraction_room < loss_room else loss_room
    catch_up = other - sold
    if catch_up < 0:
        catch_up = ZERO
    return catch_up + fresh


def _no(reason: str, outcome: str) -> Proposal:
    return Proposal(
        outcome=outcome,
        price=ZERO,
        size=ZERO,
        fair=ZERO,
        required_edge=ZERO,
        repair=False,
        reason=reason,
    )


def propose(state: State, market: Market, outcome: str, config: Config) -> Proposal:
    book = market.book(outcome)
    bid = book.best_bid()
    ask = book.best_ask()
    if bid is None or ask is None:
        return _no("uncertain_book", outcome)
    if ask <= bid:
        return _no("uncertain_book", outcome)
    if ask - bid > config.max_spread:
        return _no("uncertain_book", outcome)
    self_inv = market.inventory(outcome)
    other_inv = market.down if outcome == "up" else market.up
    adverse = other_inv - self_inv
    if adverse < 0:
        adverse = ZERO
    required_edge = config.min_edge + config.skew_edge_per_share * adverse
    fair = (bid + ask) / 2
    raw = ask if ask >= fair + required_edge else fair + required_edge
    price = ceil_tick(raw, market.tick_size)
    edge_floor = pair_edge_floor(market, outcome, config)
    floors = [edge_floor] if edge_floor is not None else []
    floor = max(floors) if floors else None
    if floor is not None and price < floor:
        price = ceil_tick(floor, market.tick_size)
    held = floor is not None
    if price < config.min_price or price > config.max_price or price >= 1:
        return _no("pair_floor" if held else "no_edge", outcome)
    if price <= bid:
        return _no("pair_floor" if held else "would_cross", outcome)
    repair = self_inv > other_inv
    if adverse > 0:
        size = config.clip_size - config.skew_size_per_share * adverse
    else:
        size = config.clip_size
    free = self_inv - reserved(state, market, outcome)
    if size > free:
        size = free
    room = unpaired_sell_room(market, outcome, price, config)
    if room is not None and size > room:
        size = room
    size = qdown(size, config.order_size_quantum)
    minimum = (
        market.min_order_size
        if market.min_order_size > config.min_order_size
        else config.min_order_size
    )
    if size < minimum:
        blocked = room is not None and room < minimum
        return Proposal(
            outcome=outcome,
            price=price,
            size=ZERO,
            fair=fair,
            required_edge=required_edge,
            repair=repair,
            reason=(
                "pair_floor"
                if blocked
                else "below_min_size"
                if free > 0
                else "insufficient_inventory"
            ),
        )
    return Proposal(
        outcome=outcome,
        price=price,
        size=size,
        fair=fair,
        required_edge=required_edge,
        repair=repair,
    )


def _mid(market: Market, outcome: str) -> Decimal | None:
    book = market.book(outcome)
    bid, ask = book.best_bid(), book.best_ask()
    if bid is None or ask is None or ask <= bid:
        return None
    return (bid + ask) / 2


def _rung_prices(
    market: Market,
    config: Config,
    bid: Decimal,
    ask: Decimal,
    mid: Decimal,
    other_mid: Decimal | None,
    late: bool,
) -> list[Decimal] | None:
    """Post-only prices. None means the book is too wide to quote."""
    tick = market.tick_size if market.tick_size > 0 else Decimal("0.01")
    spacing = config.ladder_spacing if config.ladder_spacing > tick else tick
    touch = ceil_tick(bid + tick, tick)
    if touch >= 1 or touch > config.max_price:
        return []
    winner_px = config.winner_min_price
    losing = other_mid is not None and mid + config.skew_mid_gap < other_mid
    winning = other_mid is not None and other_mid + config.skew_mid_gap < mid
    if late and mid >= winner_px:
        price = touch if touch > winner_px else ceil_tick(winner_px, tick)
        if price <= bid or price >= 1 or price > config.max_price:
            return []
        return [price]
    if late and (mid <= Decimal("1") - winner_px or losing):
        prices = [touch]
        nxt = ceil_tick(touch + spacing, tick)
        if config.ladder_levels > 1 and nxt < 1 and nxt <= config.max_price:
            prices.append(nxt)
        return prices
    if ask - bid > config.max_spread:
        return None
    inside = ceil_tick(mid if mid > touch else touch, tick)
    levels = config.ladder_levels
    if losing:
        inside = touch
    elif winning:
        inside = ceil_tick(inside + spacing, tick)
        levels = 1 if levels < 2 else levels - 1
    prices = []
    price = inside
    guard = 0
    while len(prices) < levels and price < 1 and guard < 12:
        if price > bid and price <= config.max_price:
            prices.append(price)
        price = ceil_tick(price + spacing, tick)
        guard += 1
    return prices


def _clip_rungs(
    state: State,
    market: Market,
    outcome: str,
    config: Config,
    prices: list[Decimal],
    fair: Decimal,
    repair: bool,
) -> list[Proposal]:
    """Give each rung one clip, inside first, until inventory or the budget ends."""
    free = market.inventory(outcome) - reserved(state, market, outcome)
    minimum = (
        market.min_order_size
        if market.min_order_size > config.min_order_size
        else config.min_order_size
    )
    if free < minimum:
        return []
    usable = [
        price
        for price in prices
        if _room(market, outcome, price, config, free) >= minimum
    ]
    if not usable:
        return []
    left = free
    posted: list[Proposal] = []
    for price in usable:
        room = _room(market, outcome, price, config, left)
        if room < minimum or left < minimum:
            break
        size = config.clip_size if config.clip_size < room else room
        if size > left:
            size = left
        size = qdown(size, config.order_size_quantum)
        if size < minimum:
            break
        posted.append(
            Proposal(
                outcome=outcome,
                price=price,
                size=size,
                fair=fair,
                required_edge=config.min_edge,
                repair=repair,
            )
        )
        left -= size
    return posted


def _room(
    market: Market,
    outcome: str,
    price: Decimal,
    config: Config,
    free: Decimal,
) -> Decimal:
    cap = free
    allowed = unpaired_sell_room(market, outcome, price, config)
    if allowed is not None and allowed < cap:
        cap = allowed
    if cap < 0:
        return ZERO
    return cap


def ladder_plan(
    state: State, market: Market, outcome: str, config: Config
) -> list[Proposal]:
    """Maker rungs for ``strategy=ladder``. One non-ok row when nothing posts.

    The cheap side joins the touch. The rich side steps back one spacing and
    drops a level. In the last minute a side at or above ``winner_min_price``
    is offered only there, and a side at or below ``1 - winner_min_price``
    joins the bid. Every rung stays strictly above the bid.
    """
    book = market.book(outcome)
    bid, ask = book.best_bid(), book.best_ask()
    if bid is None or ask is None or ask <= bid:
        return [_no("uncertain_book", outcome)]
    mid = (bid + ask) / 2
    other = _mid(market, _other(outcome))
    left_ms = market.end_ts_ms - state.now_ms
    late = (
        config.late_window_seconds > 0
        and 0 <= left_ms <= config.late_window_seconds * 1000
    )
    prices = _rung_prices(market, config, bid, ask, mid, other, late)
    if prices is None:
        return [_no("uncertain_book", outcome)]
    if not prices:
        return [_no("no_edge", outcome)]
    losing = other is not None and mid + config.skew_mid_gap < other
    posted = _clip_rungs(state, market, outcome, config, prices, mid, losing)
    if posted:
        return posted
    return [_no("pair_floor", outcome)]
