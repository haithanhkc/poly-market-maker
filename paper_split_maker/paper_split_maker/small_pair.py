"""Paper small_pair: a 0.96 bid pair on BTC 5m and 15m, then hold.

This mode does not split, sell, merge, or take. It posts a post-only bid
ladder on Up and on Down after the round has been open for 2 seconds, and
keeps the tokens until resolution.

The centre is the Up book's mid. Each top bid sits ``(1 - 0.96) / 2`` under
its mid, so the two top bids sum to 0.96. Four rungs step down by one tick.
A rung is one lot: the smallest whole share count worth at least 1 USDT, and
never more than 8 shares.

When one side is ahead by 4 shares or more, that side's bids are pulled.
From 270s of a 5-minute round, or 840s of a 15-minute round, a flat book
places nothing and a skewed book keeps only the light side. At the close
everything is cancelled. Fills use the same queue model as dual_bid.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_DOWN, ROUND_HALF_UP

from paper_split_maker.decimal_utils import ZERO, floor_cash, money
from paper_split_maker.dual_bid import Bid
from paper_split_maker.models import Market


TARGET = Decimal("0.96")
LEVELS = 4
MAX_SHARES = Decimal("8")
IMBALANCE_CAP = Decimal("4")
START_SECONDS = 2


@dataclass(frozen=True)
class Plan:
    bids: list[Bid]
    reason: str = ""


def quiet_after(window_s: int) -> int:
    """Last moment new two-sided quotes are allowed. 270s on 5m, 840s on 15m."""
    window = int(window_s)
    if window <= 300:
        return window - 30
    return window - 60


def lot_for(price: Decimal, max_shares: Decimal = MAX_SHARES) -> Decimal | None:
    """Smallest whole share count worth at least 1 USDT, capped at 8."""
    if price <= 0 or price >= 1 or max_shares < 1:
        return None
    need = (Decimal("1") / price).to_integral_value(rounding=ROUND_CEILING)
    if need < 1:
        need = Decimal("1")
    if need > max_shares:
        return None
    if floor_cash(need * price) < Decimal("1"):
        return None
    return need


def _on_tick(price: Decimal, tick: Decimal) -> Decimal:
    units = (price / tick).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return units * tick


def _floor_tick(price: Decimal, tick: Decimal) -> Decimal:
    units = (price / tick).to_integral_value(rounding=ROUND_DOWN)
    return units * tick


def _size_at(levels, price: Decimal) -> Decimal:
    total = ZERO
    for level in levels or []:
        if isinstance(level, (tuple, list)) and len(level) >= 2:
            level_price, size = level[0], level[1]
        else:
            continue
        if level_price == price:
            total += size
    return total


def _post_only(price: Decimal, ask: Decimal | None) -> bool:
    return price > 0 and price < 1 and (ask is None or price < ask)


def fit_pair(
    up: Decimal,
    down: Decimal,
    ask_up: Decimal | None,
    ask_down: Decimal | None,
    tick: Decimal,
    target: Decimal,
) -> tuple[Decimal, Decimal] | None:
    """Keep the bids on tick and strictly below the asks.

    The sum stays on ``target`` when that is still post-only. If keeping the
    sum would cross, both touches step down until neither crosses.
    """

    def legal(a: Decimal, b: Decimal) -> bool:
        return _post_only(a, ask_up) and _post_only(b, ask_down)

    cur_up, cur_down = up, down
    seen: set[tuple[Decimal, Decimal]] = set()
    for _ in range(32):
        key = (cur_up, cur_down)
        if key in seen:
            break
        seen.add(key)
        if legal(cur_up, cur_down) and cur_up + cur_down == target:
            return cur_up, cur_down
        if ask_up is not None and cur_up >= ask_up:
            cur_up = _floor_tick(ask_up - tick, tick)
            cur_down = target - cur_up
            continue
        if ask_down is not None and cur_down >= ask_down:
            cur_down = _floor_tick(ask_down - tick, tick)
            cur_up = target - cur_down
            continue
        break
    cur_up, cur_down = up, down
    for _ in range(32):
        if legal(cur_up, cur_down):
            return cur_up, cur_down
        moved = False
        if ask_up is not None and cur_up >= ask_up:
            nxt = _floor_tick(ask_up - tick, tick)
            if nxt >= cur_up:
                nxt = cur_up - tick
            cur_up = nxt
            moved = True
        if ask_down is not None and cur_down >= ask_down:
            nxt = _floor_tick(ask_down - tick, tick)
            if nxt >= cur_down:
                nxt = cur_down - tick
            cur_down = nxt
            moved = True
        if not moved or cur_up <= 0 or cur_down <= 0:
            break
    if legal(cur_up, cur_down):
        return cur_up, cur_down
    return None


def plan_small_pair(
    *,
    up_bid: Decimal | None,
    up_ask: Decimal | None,
    up_levels,
    down_bid: Decimal | None,
    down_ask: Decimal | None,
    down_levels,
    tick: Decimal,
    up_shares: Decimal,
    down_shares: Decimal,
    budget_left: Decimal,
    age_s: int,
    window_s: int,
    target: Decimal = TARGET,
    levels: int = LEVELS,
    max_shares: Decimal = MAX_SHARES,
    imbalance_cap: Decimal = IMBALANCE_CAP,
    start_s: int = START_SECONDS,
) -> Plan:
    """Bids for this heartbeat, plus a short reason when a rule binds."""
    if age_s < int(start_s):
        return Plan([], "before_open")
    if age_s >= int(window_s):
        return Plan([], "round_closed")
    late = age_s >= quiet_after(window_s)
    if late and up_shares == down_shares:
        return Plan([], "late_balanced")
    if (
        up_bid is None
        or up_ask is None
        or tick <= 0
        or up_ask <= up_bid
    ):
        return Plan([], "no_mid")
    mid = (up_bid + up_ask) / 2
    half = (Decimal("1") - target) / 2
    touch = fit_pair(
        _on_tick(mid - half, tick),
        target - _on_tick(mid - half, tick),
        up_ask,
        down_ask,
        tick,
        target,
    )
    if touch is None:
        return Plan([], "cross")
    up_touch, down_touch = touch
    allowed = {"up", "down"}
    reason = ""
    skewed = up_shares - down_shares
    if late:
        allowed = {"down"} if skewed > 0 else {"up"}
        reason = "late_light"
    elif skewed >= imbalance_cap:
        allowed = {"down"}
        reason = "imbalance"
    elif -skewed >= imbalance_cap:
        allowed = {"up"}
        reason = "imbalance"
    remaining = budget_left if budget_left > 0 else ZERO
    bids: list[Bid] = []
    trimmed = False
    books = {"up": up_levels, "down": down_levels}
    touches = {"up": up_touch, "down": down_touch}
    asks = {"up": up_ask, "down": down_ask}
    for step in range(int(levels)):
        for side in ("up", "down"):
            if side not in allowed:
                continue
            price = touches[side] - (tick * step)
            if not _post_only(price, asks[side]):
                continue
            size = lot_for(price, max_shares)
            if size is None:
                continue
            notional = floor_cash(size * price)
            if notional <= 0 or notional > remaining:
                trimmed = True
                continue
            remaining -= notional
            bids.append(
                Bid(
                    outcome=side,
                    price=price,
                    size=size,
                    queue_ahead=_size_at(books[side], price),
                )
            )
    if not bids:
        return Plan([], "budget" if trimmed else (reason or "no_bid"))
    if trimmed and not reason:
        reason = "budget"
    return Plan(bids, reason)


def _avg(shares: Decimal, cost: Decimal) -> Decimal | None:
    if shares <= 0:
        return None
    return cost / shares


def round_report(market: Market) -> dict:
    """One session: fills, pair cost, and matched PnL versus the leftover."""
    up_shares = market.bought_up_shares
    down_shares = market.bought_down_shares
    up_avg = _avg(up_shares, market.bought_up_cost)
    down_avg = _avg(down_shares, market.bought_down_cost)
    matched = up_shares if up_shares < down_shares else down_shares
    if up_shares > down_shares:
        excess = up_shares - down_shares
        excess_side = "up"
        excess_avg = up_avg
    elif down_shares > up_shares:
        excess = down_shares - up_shares
        excess_side = "down"
        excess_avg = down_avg
    else:
        excess = ZERO
        excess_side = ""
        excess_avg = None
    pair = None
    matched_pnl = None
    if up_avg is not None and down_avg is not None:
        pair = up_avg + down_avg
        matched_pnl = matched * (Decimal("1") - pair)
    redeemed = market.status == "redeemed"
    total = market.net_cash_flow if redeemed else None
    excess_pnl = None
    if redeemed:
        if matched_pnl is None:
            matched_pnl = ZERO
            excess_pnl = total
        else:
            excess_pnl = total - matched_pnl
    return {
        "market_id": market.market_id,
        "slug": market.slug,
        "status": market.status,
        "winner": market.winner,
        "fills_up": market.buy_up_fills,
        "fills_down": market.buy_down_fills,
        "shares_up": money(up_shares),
        "shares_down": money(down_shares),
        "avg_price_up": money(up_avg) if up_avg is not None else None,
        "avg_price_down": money(down_avg) if down_avg is not None else None,
        "pair_cost": money(pair) if pair is not None else None,
        "matched_shares": money(matched),
        "excess_shares": money(excess),
        "excess_side": excess_side,
        "excess_price": money(excess_avg) if excess_avg is not None else None,
        "pnl_matched": money(matched_pnl) if matched_pnl is not None else None,
        "pnl_excess": money(excess_pnl) if excess_pnl is not None else None,
        "pnl": money(total) if total is not None else None,
        "books_ok": market.book_fetches_ok,
        "books_failed": market.book_fetches_failed,
        "bids_posted": market.bids_posted,
        "bids_skipped": dict(market.skip_counts),
        "trades_seen": market.trades_seen,
        "trades_ok": market.trade_fetches_ok,
        "trades_failed": market.trade_fetches_failed,
        "trades_http": market.trade_fetch_status,
        "trades_returned": market.trades_returned,
    }


def small_pair_summary(markets) -> dict | None:
    chosen = [market for market in markets if market.quoted_as == "small_pair"]
    if not chosen:
        return None
    rows = [round_report(market) for market in chosen]
    matched = ZERO
    matched_pnl = ZERO
    excess_pnl = ZERO
    pnl = ZERO
    spent = ZERO
    redeemed = 0
    priced = 0
    fills_up = 0
    fills_down = 0
    for market in chosen:
        matched += (
            market.bought_up_shares
            if market.bought_up_shares < market.bought_down_shares
            else market.bought_down_shares
        )
        fills_up += market.buy_up_fills
        fills_down += market.buy_down_fills
        spent += market.bought_up_cost + market.bought_down_cost
        row = rows[priced]
        priced += 1
        if row["pnl_matched"] is not None:
            matched_pnl += Decimal(row["pnl_matched"])
        if market.status == "redeemed":
            redeemed += 1
            pnl += market.net_cash_flow
            if row["pnl_excess"] is not None:
                excess_pnl += Decimal(row["pnl_excess"])
    any_matched = any(row["pnl_matched"] is not None for row in rows)
    return {
        "strategy": "small_pair",
        "rounds": rows,
        "round_totals": {
            "rounds": len(rows),
            "redeemed": redeemed,
            "fills_up": fills_up,
            "fills_down": fills_down,
            "matched_shares": money(matched),
            "spent": money(spent),
            "pnl_matched": money(matched_pnl) if any_matched else None,
            "pnl_excess": money(excess_pnl) if redeemed else None,
            "pnl": money(pnl) if redeemed else None,
        },
    }
