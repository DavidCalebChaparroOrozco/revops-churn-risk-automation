"""
Churn risk scoring logic.

This module contains ONLY business rules. It has no knowledge of the
LLM, Slack, or the CSV file -- it takes an Account and returns a
RiskAssessment. This makes it trivial to unit test with plain asserts
and no mocks, and easy to explain to a non-technical RevOps audience
as a flat list of rules.

Design choice: weighted scoring instead of a single boolean rule.
A lone weak signal (e.g. 20 days without login) is noise on its own,
but several moderate signals together are a strong indicator. Scoring
also gives RevOps a natural priority order (high vs medium) instead of
a flat, unordered list of "at risk" accounts.
"""

from __future__ import annotations

from datetime import date

from models import Account, RiskAssessment, RiskLevel, RiskSignal, SubscriptionStatus

MEDIUM_THRESHOLD = 3
HIGH_THRESHOLD = 5
CONTRACT_RENEWAL_WINDOW_DAYS = 30


def _evaluate_signals(account: Account, reference_date: date) -> list[RiskSignal]:
    signals: list[RiskSignal] = []

    if account.failed_payment_count_last_30d >= 2:
        signals.append(
            RiskSignal(
                name="failed_payments",
                detail=f"{account.failed_payment_count_last_30d} failed payments in the last 30 days",
                points=3,
            )
        )
    elif account.failed_payment_count_last_30d == 1:
        signals.append(
            RiskSignal(
                name="failed_payments",
                detail="1 failed payment in the last 30 days",
                points=1,
            )
        )

    if account.days_since_last_login > 30:
        signals.append(
            RiskSignal(
                name="inactivity",
                detail=f"No login in {account.days_since_last_login} days",
                points=3,
            )
        )
    elif account.days_since_last_login > 14:
        signals.append(
            RiskSignal(
                name="inactivity",
                detail=f"No login in {account.days_since_last_login} days",
                points=1,
            )
        )

    if account.open_support_tickets >= 3:
        signals.append(
            RiskSignal(
                name="support_load",
                detail=f"{account.open_support_tickets} open support tickets",
                points=2,
            )
        )

    if account.subscription_status in (
        SubscriptionStatus.PAST_DUE,
        SubscriptionStatus.CANCELED,
    ):
        signals.append(
            RiskSignal(
                name="subscription_status",
                detail=f"Subscription status is '{account.subscription_status.value}'",
                points=4,
            )
        )

    days_until_contract_end = (account.contract_end_date - reference_date).days
    if 0 <= days_until_contract_end <= CONTRACT_RENEWAL_WINDOW_DAYS:
        signals.append(
            RiskSignal(
                name="contract_ending_soon",
                detail=f"Contract ends in {days_until_contract_end} days",
                points=2,
            )
        )

    return signals


def _level_from_score(score: int, medium_threshold: int, high_threshold: int) -> RiskLevel:
    if score >= high_threshold:
        return RiskLevel.HIGH
    if score >= medium_threshold:
        return RiskLevel.MEDIUM
    return RiskLevel.NONE


def evaluate_account_risk(
    account: Account,
    reference_date: date | None = None,
    medium_threshold: int = MEDIUM_THRESHOLD,
    high_threshold: int = HIGH_THRESHOLD,
) -> RiskAssessment:
    reference_date = reference_date or date.today()
    signals = _evaluate_signals(account, reference_date)
    score = sum(signal.points for signal in signals)
    level = _level_from_score(score, medium_threshold, high_threshold)

    return RiskAssessment(
        account=account,
        score=score,
        level=level,
        signals=signals,
    )


def evaluate_accounts(
    accounts: list[Account],
    reference_date: date | None = None,
    medium_threshold: int = MEDIUM_THRESHOLD,
    high_threshold: int = HIGH_THRESHOLD,
) -> list[RiskAssessment]:
    assessments = [
        evaluate_account_risk(a, reference_date, medium_threshold, high_threshold)
        for a in accounts
    ]
    at_risk = [a for a in assessments if a.is_at_risk]
    return sorted(at_risk, key=lambda a: a.score, reverse=True)
