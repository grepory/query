"""Review admission and attempt records, independent of providers and storage."""

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, model_validator

from query.investigation import Contract
from query.review_clients import CallUsage

Count = Annotated[int, Field(ge=0, strict=True)]


class Prices(Contract):
    input_usd_per_million: Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]
    output_usd_per_million: Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]
    assumption: str = Field(min_length=1)

    def cost(self, usage: CallUsage):
        return (
            usage.input_tokens * self.input_usd_per_million
            + usage.output_tokens * self.output_usd_per_million
        ) / Decimal(1000000)


class ReviewPolicy(Contract):
    closeout_remaining_tokens: Count | None = None
    retry_delay_seconds: Annotated[int, Field(gt=0, strict=True)] = 60
    max_retry_delay_seconds: Annotated[int, Field(gt=0, strict=True)] = 3600
    prices: dict[Literal["author", "reviewer", "adjudicator"], Prices] = Field(default_factory=dict)

    @model_validator(mode="after")
    def bounded_delay(self):
        if self.max_retry_delay_seconds < self.retry_delay_seconds:
            raise ValueError("Maximum retry delay must cover the initial delay")
        return self


class ReviewAttempt(Contract):
    id: UUID
    key: str
    number: Annotated[int, Field(gt=0, strict=True)]
    state: Literal["started", "succeeded", "failed", "unknown"]
    started_at: datetime
    finished_at: datetime | None = None
    usage: CallUsage | None = None
    charged_tokens: Count = 0
    charge_basis: Literal["pending", "reported", "reservation", "not_called"] = "pending"
    error_kind: str | None = None
    retryable: bool | None = None
    retry_at: datetime | None = None
    prices: Prices | None = None
    estimated_usd: Decimal | None = None


def review_attempts(investigation):
    records = {}
    for event in investigation.events:
        payload = event.details.get("review_attempt")
        if payload is not None:
            attempt = ReviewAttempt.model_validate(payload)
            records[attempt.id] = attempt
    return tuple(records.values())


def saved_policy(investigation, snapshot_id):
    return next(
        (
            ReviewPolicy.model_validate(e.details["policy"])
            for e in investigation.events
            if e.kind == "review_policy" and e.details["snapshot_id"] == str(snapshot_id)
        ),
        None,
    )


def in_closeout(investigation, snapshot_id):
    return any(
        e.kind == "review_closeout" and e.details["snapshot_id"] == str(snapshot_id)
        for e in investigation.events
    )


def admission_reserve(operation, bound, last_paid_revision=0):
    """Protect a binding revision and full review before each adjudication."""
    if operation.action == "carry_forward":
        return bound
    if operation.action in {"write", "revise"}:
        return 2 * bound
    if operation.action == "adjudicate":
        return 2 * bound + max(bound, last_paid_revision)
    return bound
