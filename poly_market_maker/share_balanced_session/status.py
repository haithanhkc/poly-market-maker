"""Plain terminal status. Separate numbers are not interchangeable."""

from __future__ import annotations

from poly_market_maker.share_balanced_session.engine import ShareSession


def format_status(session: ShareSession) -> str:
    worst = session.worst_pnl()
    current = session.current_pnl()
    liquidation = session.liquidation_estimate()
    midpoint = session.midpoint_pnl()
    pending = [
        order
        for order in session.state.orders
        if order.status
        in ("open", "intent", "unknown_submit", "cancel_requested", "unknown_cancel")
    ]
    lines = [
        "mode=share_balanced_session",
        "maker_only=true",
        "post_only=true",
        "taker_route=none",
        "cap_guarantee=false",
        "two_leg_fill_guarantee=false",
        "phase=" + session.state.phase,
        "halt=" + (session.state.halt_reason or "none"),
        "shares_up=" + _num(session.state.on_hand["up"]),
        "shares_down=" + _num(session.state.on_hand["down"]),
        "shares_gap=" + _num(session.share_gap()),
        "reserved_up=" + _num(session.reserved("up")),
        "reserved_down=" + _num(session.reserved("down")),
        "pending_orders=" + str(len(pending)),
        "risk_budget_usd=" + _num(session.config.risk_budget_usd),
        "remaining_risk_room_usd=" + _num(session.config.risk_budget_usd + worst),
        "worst_case_pnl=" + _num(worst),
        "settlement_floor_pnl=" + _num(current),
        "liquidation_estimate_pnl="
        + ("na" if liquidation is None else _num(liquidation)),
        "midpoint_mark_pnl=" + ("na" if midpoint is None else _num(midpoint)),
        "rebate_unpaid_estimate=" + _num(session.state.rebate_unpaid_estimate),
        "fees_paid=" + _num(session.state.fees_paid),
        "gross_proceeds=" + _num(session.state.gross_proceeds),
        "skip=" + (",".join(session.skip_reasons) if session.skip_reasons else "none"),
        "notices=" + (",".join(session.notices) if session.notices else "none"),
        "residual_exposure=" + ("yes" if _residual(session) else "no"),
        "note=maker_only_can_leave_inventory_into_resolution",
        "note=worst_case_pnl_is_not_a_stop_fill_and_liquidation_estimate_is_not_an_order",
    ]
    return "\n".join(lines)


def _residual(session: ShareSession) -> bool:
    if session.state.on_hand["up"] > 0 or session.state.on_hand["down"] > 0:
        return True
    return any(
        order.status
        in ("open", "intent", "unknown_submit", "cancel_requested", "unknown_cancel")
        for order in session.state.orders
    )


def _num(value) -> str:
    return format(value, "f")
