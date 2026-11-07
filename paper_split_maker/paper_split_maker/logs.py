"""Append-only JSONL and CSV ledgers plus the end-of-run summary."""

from __future__ import annotations

import csv
import json
from pathlib import Path

from paper_split_maker.decimal_utils import money
from paper_split_maker.models import State
from paper_split_maker.risk import (
    best_case_equity,
    market_contribution,
    worst_case_equity,
)

CSV_FIELDS = [
    "ts",
    "kind",
    "market_id",
    "order_id",
    "outcome",
    "price",
    "size",
    "cash",
    "worst_case_equity",
    "best_case_equity",
    "realized_pnl",
    "rebate_estimate",
    "reason",
    "detail",
]

DISCLAIMER_EN = (
    "Paper model only. This floor limits modeled terminal loss inside the ledger. "
    "It does not limit mark-to-market drawdown and it does not cover smart-contract, "
    "oracle, or gas risk. Simulator tests are not evidence that live losses are capped at 3%."
)
DISCLAIMER_VI = (
    "Chỉ là mô hình giấy. Sàn này giới hạn lỗ cuối kỳ trong sổ mô phỏng, "
    "không giới hạn lỗ mark-to-market, và không bao gồm rủi ro hợp đồng thông minh, "
    "oracle hay gas. Bài kiểm thử mô phỏng không phải bằng chứng rằng lỗ live bị chặn ở 3%."
)


class LogWriter:
    def __init__(self, directory: Path):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.jsonl_path = self.directory / "events.jsonl"
        self.csv_path = self.directory / "events.csv"
        if not self.csv_path.exists():
            with self.csv_path.open("w", encoding="utf-8", newline="") as handle:
                csv.DictWriter(handle, fieldnames=CSV_FIELDS).writeheader()

    def write(self, rows: list[dict]) -> None:
        if not rows:
            return
        with self.jsonl_path.open("a", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        with self.csv_path.open("a", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(
                handle, fieldnames=CSV_FIELDS, extrasaction="ignore"
            )
            for row in rows:
                writer.writerow({key: row.get(key, "") for key in CSV_FIELDS})


def build_summary(
    state: State, *, mode: str, resumed: bool, notes: list[str] | None = None
) -> dict:
    worst = worst_case_equity(state)
    best = best_case_equity(state)
    markets = []
    for market_id in sorted(state.markets):
        market = state.markets[market_id]
        orders = [
            order.to_dict()
            for order in state.orders.values()
            if order.market_id == market_id
        ]
        markets.append(
            {
                "market_id": market.market_id,
                "condition_id": market.condition_id,
                "slug": market.slug,
                "synthetic": market.synthetic,
                "status": market.status,
                "winner": market.winner,
                "resolution_source": market.resolution_source,
                "resolution_conflict": market.resolution_conflict,
                "up_inventory": money(market.up),
                "down_inventory": money(market.down),
                "gross_split_usdc": money(market.gross_split),
                "net_cash_flow": money(market.net_cash_flow),
                "worst_contribution": money(market_contribution(state, market)),
                "orders": orders,
            }
        )
    return {
        "mode": mode,
        "resumed": resumed,
        "live_orders": False,
        "paper_only": True,
        "synthetic_data_present": any(
            market.synthetic for market in state.markets.values()
        ),
        "initial_capital": money(state.initial_capital),
        "loss_floor": money(state.loss_floor),
        "loss_budget_usdc": money(state.initial_capital - state.loss_floor),
        "max_loss_fraction_locked": money(state.max_loss_fraction),
        "cash": money(state.cash),
        "realized_pnl": money(state.realized_pnl),
        "worst_terminal_equity": money(worst),
        "best_terminal_equity": money(best),
        "worst_terminal_pnl": money(worst - state.initial_capital),
        "best_terminal_pnl": money(best - state.initial_capital),
        "rebate_estimate_unguaranteed": money(state.rebate_estimate),
        "rebate_included_in_pnl": False,
        "rebate_included_in_risk_floor": False,
        "at_risk_cap": worst <= state.loss_floor,
        "limits_terminal_modeled_loss_not_mtm": True,
        "risk_fee_rate_locked": money(state.risk_fee_rate),
        "maker_fee_rate_locked": money(state.maker_fee_rate),
        "operational_reserve_locked": money(state.operational_reserve),
        "global_rounding_reserve_locked": money(state.global_rounding_reserve),
        "per_market_budget_locked": money(state.per_market_budget),
        "fill_mode_locked": state.fill_mode,
        "cancel_latency_ms_locked": state.cancel_latency_ms,
        "ack_latency_ms_locked": state.ack_latency_ms,
        "risk_rejections": state.risk_rejections[-50:],
        "markets": markets,
        "notes": notes or [],
        "disclaimer_en": DISCLAIMER_EN,
        "disclaimer_vi": DISCLAIMER_VI,
    }


def write_summary(directory: Path, summary: dict) -> Path:
    path = Path(directory) / "summary.json"
    path.write_text(
        json.dumps(summary, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path
