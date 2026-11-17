"""Paper dual-bid: buy Up and Down during the open round, then hold.

This mode does not split, sell, or merge. It posts post-only bids on both
outcomes after the round opens and keeps the tokens until resolution.

Paper fills are not the venue matcher. The session polls
``GET /v1/markets/{id}/orderbook`` and ``GET /v1/orders/matches?marketId=``
(with ``x-api-key`` on mainnet). ``/v1/markets/{id}/matches`` is not a
route. Those prints drive the model below.
The websocket at ``wss://ws.predict.fun/ws`` is not used for paper fills.

Assumptions, all of which can disagree with the real book:

- A bid is last at its price. ``queue_ahead`` is the size already displayed
  at that price when the bid was posted. Later joins are not added.
- A SELL print at our price consumes that queue, then fills us. A BUY print
  does not. A SELL print strictly below our price means the level was swept:
  the queue is cleared without spending this print's size, then the print
  size can fill us. One small through-print can therefore fill a bid that
  had a large queue.
- A cancel we have requested no longer fills, even during cancel latency.
- Maker fee is charged from ``maker_fee_rate`` (0 on these markets). There
  is no maker rebate in the PnL.
- Resting bids are invisible to the public book, so we do not see ourselves.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING

from paper_split_maker.decimal_utils import ZERO, floor_cash, money
from paper_split_maker.models import Market


@dataclass(frozen=True)
class Bid:
    outcome: str
    price: Decimal
    size: Decimal
    queue_ahead: Decimal


def round_open_ms(slug: str, end_ts_ms: int, window_s: int) -> int:
    """Open time from ``btc-updown-5m-{unix}``, else one window before the end."""
    tail = str(slug or "").rsplit("-", 1)[-1]
    if tail.isdigit():
        return int(tail) * 1000
    return int(end_ts_ms) - int(window_s) * 1000


def quote_phase(now_ms: int, open_ms: int, start_s: int, stop_s: int) -> str:
    """``before`` the start, ``open`` while new bids are allowed, else ``after``."""
    age = int(now_ms) - int(open_ms)
    if age < int(start_s) * 1000:
        return "before"
    if age >= int(stop_s) * 1000:
        return "after"
    return "open"


def clip_for(price: Decimal, clip: Decimal, max_clip: Decimal) -> Decimal | None:
    """Share size in ``[clip, max_clip]`` whose notional is at least 1 USDT."""
    if price <= 0 or clip <= 0 or max_clip < clip:
        return None
    need = (Decimal("1") / price).to_integral_value(rounding=ROUND_CEILING)
    size = clip if clip >= need else need
    if size > max_clip:
        return None
    if floor_cash(size * price) < Decimal("1"):
        return None
    return size


def bid_prices(
    best_bid: Decimal | None,
    best_ask: Decimal | None,
    tick: Decimal,
    levels: int,
    min_price: Decimal,
) -> list[Decimal]:
    """Post-only rungs at the best bid and below it. Nothing under ``min_price``."""
    if (
        best_bid is None
        or best_ask is None
        or tick <= 0
        or levels < 1
        or best_ask <= best_bid
    ):
        return []
    price = best_bid if best_bid < best_ask else best_ask - tick
    out: list[Decimal] = []
    while len(out) < levels and price >= min_price:
        if price < best_ask:
            out.append(price)
        price -= tick
    return out


def _size_at(levels: list[tuple[Decimal, Decimal]], price: Decimal) -> Decimal:
    total = ZERO
    for level, size in levels:
        if level == price:
            total += size
    return total


def _avg(shares: Decimal, cost: Decimal) -> Decimal | None:
    if shares <= 0:
        return None
    return cost / shares


def pair_cost(up_shares: Decimal, up_cost: Decimal, down_shares: Decimal, down_cost: Decimal) -> Decimal | None:
    """Average Up price plus average Down price, once both sides have shares."""
    up_avg = _avg(up_shares, up_cost)
    down_avg = _avg(down_shares, down_cost)
    if up_avg is None or down_avg is None:
        return None
    return up_avg + down_avg


def share_balance(up_shares: Decimal, down_shares: Decimal) -> Decimal | None:
    hi = up_shares if up_shares > down_shares else down_shares
    if hi <= 0:
        return None
    lo = down_shares if up_shares > down_shares else up_shares
    return lo / hi


def heavier(up_shares: Decimal, down_shares: Decimal) -> str | None:
    if up_shares > down_shares:
        return "up"
    if down_shares > up_shares:
        return "down"
    return None


def allow_bid(
    outcome: str,
    price: Decimal,
    *,
    up_shares: Decimal,
    up_cost: Decimal,
    down_shares: Decimal,
    down_cost: Decimal,
    target_pair_cost: Decimal,
    pair_cost_stop: Decimal,
    min_share_balance: Decimal,
    min_price: Decimal,
) -> str:
    """``ok`` or the reason this side must not be bid."""
    if price < min_price:
        return "below_min_price"
    other = down_cost / down_shares if outcome == "up" and down_shares > 0 else None
    if outcome == "down" and up_shares > 0:
        other = up_cost / up_shares
    if other is not None and other + price > target_pair_cost:
        return "pair_cost"
    matched = pair_cost(up_shares, up_cost, down_shares, down_cost)
    heavy = heavier(up_shares, down_shares)
    if matched is not None and matched >= pair_cost_stop and heavy == outcome:
        return "pair_cost_stop"
    balance = share_balance(up_shares, down_shares)
    if balance is not None and balance < min_share_balance and heavy == outcome:
        return "imbalance"
    return "ok"


def plan_bids(
    *,
    up_bids: list[tuple[Decimal, Decimal]],
    up_ask: Decimal | None,
    down_bids: list[tuple[Decimal, Decimal]],
    down_ask: Decimal | None,
    tick: Decimal,
    levels: int,
    clip: Decimal,
    max_clip: Decimal,
    min_price: Decimal,
    up_shares: Decimal,
    up_cost: Decimal,
    down_shares: Decimal,
    down_cost: Decimal,
    target_pair_cost: Decimal,
    pair_cost_stop: Decimal,
    min_share_balance: Decimal,
    budget_left: Decimal,
) -> list[Bid]:
    """Interleaved post-only ladders that still fit the round budget."""
    sides = {
        "up": bid_prices(
            up_bids[0][0] if up_bids else None, up_ask, tick, levels, min_price
        ),
        "down": bid_prices(
            down_bids[0][0] if down_bids else None, down_ask, tick, levels, min_price
        ),
    }
    books = {"up": up_bids, "down": down_bids}
    planned: list[Bid] = []
    left = budget_left
    for rung in range(levels):
        for outcome in ("up", "down"):
            prices = sides[outcome]
            if rung >= len(prices):
                continue
            price = prices[rung]
            reason = allow_bid(
                outcome,
                price,
                up_shares=up_shares,
                up_cost=up_cost,
                down_shares=down_shares,
                down_cost=down_cost,
                target_pair_cost=target_pair_cost,
                pair_cost_stop=pair_cost_stop,
                min_share_balance=min_share_balance,
                min_price=min_price,
            )
            if reason != "ok":
                continue
            size = clip_for(price, clip, max_clip)
            if size is None:
                continue
            notional = floor_cash(size * price)
            if notional > left:
                continue
            left -= notional
            planned.append(
                Bid(
                    outcome=outcome,
                    price=price,
                    size=size,
                    queue_ahead=_size_at(books[outcome], price),
                )
            )
    return planned


def round_report(market: Market) -> dict:
    """One round's fills, pair cost, and PnL. PnL is null until redeem."""
    up_shares = market.bought_up_shares
    down_shares = market.bought_down_shares
    up_avg = _avg(up_shares, market.bought_up_cost)
    down_avg = _avg(down_shares, market.bought_down_cost)
    matched = up_shares if up_shares < down_shares else down_shares
    excess = abs(up_shares - down_shares)
    heavy = heavier(up_shares, down_shares)
    cost = pair_cost(up_shares, market.bought_up_cost, down_shares, market.bought_down_cost)
    balance = share_balance(up_shares, down_shares)
    redeemed = market.status == "redeemed"
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
        "pair_cost": money(cost) if cost is not None else None,
        "matched_shares": money(matched),
        "balance": money(balance) if balance is not None else None,
        "excess_shares": money(excess),
        "excess_side": heavy or "",
        "pnl": money(market.net_cash_flow) if redeemed else None,
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


def dual_summary(markets) -> dict | None:
    chosen = [
        market
        for market in markets
        if market.quoted_as == "dual_bid"
        or market.buy_up_fills
        or market.buy_down_fills
        or market.bought_up_shares > 0
        or market.bought_down_shares > 0
    ]
    if not chosen:
        return None
    rows = [round_report(market) for market in chosen]
    matched = ZERO
    pnl = ZERO
    redeemed = 0
    fills_up = 0
    fills_down = 0
    books_ok = 0
    books_failed = 0
    bids_posted = 0
    trades_seen = 0
    trades_ok = 0
    trades_failed = 0
    trades_returned = 0
    skipped: dict[str, int] = {}
    for market in chosen:
        matched += (
            market.bought_up_shares
            if market.bought_up_shares < market.bought_down_shares
            else market.bought_down_shares
        )
        fills_up += market.buy_up_fills
        fills_down += market.buy_down_fills
        books_ok += market.book_fetches_ok
        books_failed += market.book_fetches_failed
        bids_posted += market.bids_posted
        trades_seen += market.trades_seen
        trades_ok += market.trade_fetches_ok
        trades_failed += market.trade_fetches_failed
        trades_returned += market.trades_returned
        for key, count in market.skip_counts.items():
            skipped[str(key)] = skipped.get(str(key), 0) + int(count)
        if market.status == "redeemed":
            redeemed += 1
            pnl += market.net_cash_flow
    return {
        "strategy": "dual_bid",
        "rounds": rows,
        "round_totals": {
            "rounds": len(rows),
            "redeemed": redeemed,
            "fills_up": fills_up,
            "fills_down": fills_down,
            "matched_shares": money(matched),
            "pnl": money(pnl) if redeemed else None,
            "books_ok": books_ok,
            "books_failed": books_failed,
            "bids_posted": bids_posted,
            "bids_skipped": skipped,
            "trades_seen": trades_seen,
            "trades_ok": trades_ok,
            "trades_failed": trades_failed,
            "trades_returned": trades_returned,
        },
    }
