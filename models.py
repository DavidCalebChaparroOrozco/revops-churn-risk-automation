"""
Core data models for the application.

This module is the single source of truth for the shape of the data
flowing through the system. It contains no business logic and no I/O:
only structures and basic type/format validation.

We use Pydantic (instead of plain dataclasses) because the source data
comes from an external CSV -> it's untyped text with no guarantees.
Pydantic validates and coerces automatically (e.g. "12.5" -> float) and
fails fast with a clear message if a CSV row is malformed, instead of
propagating a confusing error later in risk.py or llm.py.
"""

from __future__ import annotations

from datetime import date
from enum import Enum

from pydantic import BaseModel, Field, field_validator


class SubscriptionStatus(str, Enum):
    """
    Closed enum instead of a free-form string.

    Why: subscription_status is a field with a known, finite set of
    business values. Using an Enum does two things:
    1. Fails explicitly if the CSV brings an unexpected value
       (e.g. a typo "acive" instead of "active"), instead of risk.py
       silently not recognizing the value and treating it as "healthy".
    2. Acts as executable documentation of which states exist.
    """

    ACTIVE = "active"
    PAST_DUE = "past_due"
    CANCELED = "canceled"
    TRIALING = "trialing"


class Account(BaseModel):
    """Represents a single validated and typed row from the input CSV."""

    account_id: str
    account_name: str
    mrr: float = Field(ge=0, description="Monthly Recurring Revenue in USD")
    plan_name: str
    subscription_status: SubscriptionStatus
    failed_payment_count_last_30d: int = Field(ge=0)
    days_since_last_login: int = Field(ge=0)
    open_support_tickets: int = Field(ge=0)
    contract_end_date: date

    @field_validator("account_id", "account_name", "plan_name")
    @classmethod
    def not_blank(cls, value: str) -> str:
        """
        Prevents a critical text field from arriving blank due to a
        malformed row, and strips control characters (newlines, carriage
        returns, tabs) that could otherwise be used for log injection --
        e.g. an account_name containing a fake newline-delimited log
        line, forging a bogus log entry when this value is later
        interpolated into a log message (see observability.py,
        app.py's logger.warning calls) or into the LLM prompt itself.
        This is a real concern specifically because this field comes
        from an external CSV we don't control the origin of.
        """
        if not value or not value.strip():
            raise ValueError("field must not be blank")
        cleaned = "".join(ch for ch in value if ch.isprintable() or ch == " ")
        return cleaned.strip()

    @field_validator("subscription_status", mode="before")
    @classmethod
    def normalize_subscription_status(cls, value):
        """
        Normalizes common formatting variance before enum matching.

        Real-world CSVs exported from a CRM rarely use the exact
        snake_case values our Enum defines internally (e.g. a CRM might
        export "Past Due" or "PAST_DUE" instead of "past_due"). This
        normalizes casing and whitespace/hyphens to underscores so those
        known variations are accepted, while genuinely unknown values
        (e.g. a typo, or a new status our Enum doesn't model yet) still
        fail loudly with a clear Pydantic error -- we normalize *format*,
        we don't silently accept *unknown business values*.
        """
        if isinstance(value, str):
            return value.strip().lower().replace(" ", "_").replace("-", "_")
        return value


class RiskLevel(str, Enum):
    """
    Discrete, orderable risk level.

    We don't use a simple "is_at_risk" boolean because RevOps needs to
    prioritize: a "medium" account is not the same as a "high" one.
    That nuance is exactly what separates this approach from a simple
    if/else, and it's what makes the LLM-generated summary more useful.
    """

    NONE = "none"
    MEDIUM = "medium"
    HIGH = "high"


class RiskSignal(BaseModel):
    """
    A single signal that contributed to the risk score.

    We store this (instead of just the final score) because it's what
    gives the system traceability: when someone in RevOps asks "why was
    this account flagged?", the answer shouldn't be "the LLM said so"
    nor "a magic number" -- it should be this list.
    This is also what we pass to the LLM as context, so it doesn't have
    to "guess" why the account is at risk.
    """

    name: str
    detail: str
    points: int


class RiskAssessment(BaseModel):
    """Full result of evaluating an account's risk."""

    account: Account
    score: int
    level: RiskLevel
    signals: list[RiskSignal]

    @property
    def is_at_risk(self) -> bool:
        return self.level != RiskLevel.NONE


class AccountRiskReport(BaseModel):
    """
    Final per-account result, ready to be sent to Slack.

    We separate this from RiskAssessment because they are different
    responsibilities: RiskAssessment is the "what" (business rules, no
    LLM involved). AccountRiskReport is "what we tell a human" (includes
    the generated summary, which can fail and use a fallback -- see llm.py).
    """

    assessment: RiskAssessment
    summary: str
    summary_source: str = "fallback"  # "openai" | "gemini" | "fallback" -- see llm_providers.py
    summary_generation_failed: bool = False
