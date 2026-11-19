"""Replay combined small_pair inventory with and without the pair guards.

This is not a venue matcher. Each decision posts the planner's bids. A
one-sided tape fills the underdog immediately and fills the other side only
when its bid has been lifted to the ask. A two-sided tape fills both. The
same 30 fairs are run twice so the pair gate, the catch-up bid, and the
late first-leg stop can be compared with the old ladder.
"""

from __future__ import annotations

import csv
from decimal import Decimal, ROUND_DOWN
from pathlib import Path

from paper_split_maker.small_pair import plan_small_pair


TICK = Decimal("0.01")
# Thirteen tapes where only the underdog is sold, then seventeen where both are.
FAIRS = (
    [Decimal("0.32"), Decimal("0.35"), Decimal("0.38"), Decimal("0.40"), Decimal("0.42")]
    * 2
    + [Decimal("0.36"), Decimal("0.44"), Decimal("0.33")]
    + [Decimal("0.55"), Decimal("0.58"), Decimal("0.62"), Decimal("0.48"), Decimal("0.52")]
    * 3
    + [Decimal("0.60"), Decimal("0.46")]
)


def replay_combined_tape(controls: bool) -> dict:
    """Thirty rounds. ``controls`` turns on the combined pair guards."""
    matched_pnl = Decimal("0")
    excess_pnl = Decimal("0")
    total_pnl = Decimal("0")
    pair_cost_sum = Decimal("0")
    pair_cost_n = 0
    one_sided = 0
    two_sided = 0
    max_unpaired = Decimal("0")
    adverse_excess = Decimal("0")
    adverse_unpaired = Decimal("0")
    for index, fair in enumerate(FAIRS):
        adverse = index < 13
        row = _one_round(
            fair,
            both_sides=not adverse,
            controls=controls,
            adverse=adverse,
        )
        matched_pnl += row["pnl_matched"]
        excess_pnl += row["pnl_excess"]
        total_pnl += row["pnl"]
        if adverse:
            adverse_excess += row["pnl_excess"]
            if row["unpaired"] > adverse_unpaired:
                adverse_unpaired = row["unpaired"]
        if row["pair_cost"] is not None:
            pair_cost_sum += row["pair_cost"]
            pair_cost_n += 1
            two_sided += 1
        else:
            one_sided += 1
        if row["unpaired"] > max_unpaired:
            max_unpaired = row["unpaired"]
    return {
        "rounds": len(FAIRS),
        "two_sided": two_sided,
        "one_sided": one_sided,
        "pnl": total_pnl,
        "pnl_matched": matched_pnl,
        "pnl_excess": excess_pnl,
        "avg_pair_cost": (pair_cost_sum / pair_cost_n) if pair_cost_n else None,
        "max_unpaired": max_unpaired,
        "adverse_excess": adverse_excess,
        "adverse_unpaired": adverse_unpaired,
    }


def replay_sessions_csv(path: Path, lot_cap: Decimal = Decimal("8")) -> dict:
    """Cap each row's unpaired shares at one lot and reprice that leftover.

    Matched pairs are left as they were. The leftover above one lot is removed
    at the heavy side's average price. This is the inventory cap on a finished
    sessions file, not a new set of fills.
    """
    before_m = Decimal("0")
    before_x = Decimal("0")
    after_x = Decimal("0")
    one_sided_before = 0
    one_sided_after = 0
    with Path(path).open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            up = _dec(row.get("shares_up"))
            down = _dec(row.get("shares_down"))
            matched = up if up < down else down
            if matched > 0 and row.get("pnl_matched"):
                before_m += _dec(row.get("pnl_matched"))
            excess = _dec(row.get("pnl_excess"))
            before_x += excess
            heavy = up - down
            side_shares = up if heavy >= 0 else down
            if side_shares > 0 and matched == 0:
                one_sided_before += 1
            unpaired = abs(heavy)
            if unpaired <= 0 or side_shares <= 0:
                after_x += excess
                continue
            lot = _dec(row.get("lot")) if row.get("lot") else Decimal("3")
            if lot < 1:
                lot = Decimal("1")
            if lot > lot_cap:
                lot = lot_cap
            kept = unpaired if unpaired < lot else lot
            if kept < unpaired and unpaired > 0:
                after_x += excess * (kept / unpaired)
            else:
                after_x += excess
            if kept > 0 and matched == 0:
                one_sided_after += 1
    return {
        "pnl_matched": before_m,
        "pnl_excess_before": before_x,
        "pnl_excess_after": after_x,
        "one_sided_before": one_sided_before,
        "one_sided_after": one_sided_after,
    }


def _one_round(fair: Decimal, both_sides: bool, controls: bool, adverse: bool) -> dict:
    up_bid, up_ask, down_bid, down_ask = _books(fair)
    up_shares = Decimal("0")
    down_shares = Decimal("0")
    up_cost = Decimal("0")
    down_cost = Decimal("0")
    resting = {"up": Decimal("0"), "down": Decimal("0")}
    spent = Decimal("0")
    for age in range(10, 300, 10):
        if spent >= Decimal("25"):
            break
        plan = plan_small_pair(
            up_bid=up_bid,
            up_ask=up_ask,
            up_levels=[(up_bid, Decimal("10"))],
            down_bid=down_bid,
            down_ask=down_ask,
            down_levels=[(down_bid, Decimal("10"))],
            tick=TICK,
            up_shares=up_shares,
            down_shares=down_shares,
            budget_left=Decimal("25") - spent,
            age_s=age,
            window_s=300,
            fair_up=fair,
            binance_filter=True,
            binance_age_s=0,
            fair_quote=True,
            pair_gate=controls,
            catch_up=controls,
            catch_up_sum=Decimal("0.97"),
            up_cost=up_cost,
            down_cost=down_cost,
            first_leg_s=200 if controls else 0,
            resting_up=resting["up"],
            resting_down=resting["down"],
            start_s=10,
        )
        groups = {"up": [], "down": []}
        for bid in plan.bids:
            groups[bid.outcome].append(bid)
        underdog = "up" if fair < Decimal("0.5") else "down"
        favorite = "down" if underdog == "up" else "up"
        # One-sided tapes hit the favorite. That is the side the old 12-share
        # cap left on, and it is the side that then loses.
        swept = favorite if adverse else underdog
        asks = {"up": up_ask, "down": down_ask}
        for side in ("up", "down"):
            bids = groups[side]
            if not bids:
                resting[side] = Decimal("0")
                continue
            if both_sides or side == swept:
                hitting = bids
            else:
                hitting = [bid for bid in bids if bid.price >= asks[side] - TICK]
            for bid in hitting:
                notional = (bid.size * bid.price).quantize(Decimal("0.0001"), rounding=ROUND_DOWN)
                if notional <= 0 or spent + notional > Decimal("25"):
                    continue
                if side == "up":
                    up_shares += bid.size
                    up_cost += notional
                else:
                    down_shares += bid.size
                    down_cost += notional
                spent += notional
            resting[side] = sum(
                (bid.size for bid in bids if bid not in hitting),
                Decimal("0"),
            )
    return _settle(fair, up_shares, down_shares, up_cost, down_cost, adverse)


def _books(fair: Decimal):
    up_bid = _tick(fair - Decimal("0.02"))
    up_ask = _tick(fair + Decimal("0.02"))
    if up_bid < TICK:
        up_bid = TICK
    if up_ask <= up_bid:
        up_ask = up_bid + TICK
    down_bid = _tick(Decimal("1") - up_ask)
    down_ask = _tick(Decimal("1") - up_bid)
    if down_bid < TICK:
        down_bid = TICK
    if down_ask <= down_bid:
        down_ask = down_bid + TICK
    return up_bid, up_ask, down_bid, down_ask


def _settle(fair, up_shares, down_shares, up_cost, down_cost, adverse: bool) -> dict:
    matched = up_shares if up_shares < down_shares else down_shares
    up_avg = (up_cost / up_shares) if up_shares > 0 else None
    down_avg = (down_cost / down_shares) if down_shares > 0 else None
    pair = None
    matched_pnl = Decimal("0")
    if up_avg is not None and down_avg is not None:
        pair = up_avg + down_avg
        matched_pnl = matched * (Decimal("1") - pair)
    # Adverse tapes fill the side that then loses. Two-sided tapes follow fair.
    if adverse and up_shares != down_shares:
        winner_up = down_shares > up_shares
    else:
        winner_up = fair >= Decimal("0.5")
    redeem = up_shares if winner_up else down_shares
    total = redeem - up_cost - down_cost
    return {
        "pnl": total,
        "pnl_matched": matched_pnl,
        "pnl_excess": total - matched_pnl,
        "pair_cost": pair,
        "unpaired": abs(up_shares - down_shares),
    }


def _tick(price: Decimal) -> Decimal:
    units = (price / TICK).to_integral_value(rounding=ROUND_DOWN)
    return units * TICK


def _dec(value) -> Decimal:
    if value is None or value == "":
        return Decimal("0")
    return Decimal(str(value))
