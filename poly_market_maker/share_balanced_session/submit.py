"""The only sell submission path. post_only is not a caller choice."""

from __future__ import annotations

from decimal import Decimal


class MakerOnlyViolation(RuntimeError):
    pass


class SubmitResult:
    def __init__(self, status: str, exchange_id: str | None = None, reason: str = ""):
        self.status = status
        self.exchange_id = exchange_id
        self.reason = reason


class CancelResult:
    def __init__(
        self,
        status: str,
        canceled_ids: frozenset[str] | None = None,
        not_canceled: dict | None = None,
    ):
        self.status = status
        self.canceled_ids = canceled_ids or frozenset()
        self.not_canceled = not_canceled or {}


def submit_maker_sell(
    broker, token_id: str, price: Decimal, size: Decimal
) -> SubmitResult:
    """Submit one resting sell. There is no non-post-only retry."""

    result = broker.submit_sell(token_id, price, size, post_only=True)
    if getattr(result, "retried_without_post_only", False):
        raise MakerOnlyViolation("post-only reject was retried as a crossing order")
    return result
