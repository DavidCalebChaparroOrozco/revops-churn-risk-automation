"""
LLM orchestration: generates a natural-language risk summary for each
at-risk account by trying a list of LLMProvider implementations in
order, falling back to the next one if the current one fails.

Design choices worth calling out:

1. This module knows nothing about OpenAI or Gemini specifically --
   that's llm_providers.py's job. Here we only depend on the
   LLMProvider Protocol (name + generate()). This keeps prompt design,
   validation, and failover logic completely decoupled from which
   vendor SDKs are installed.

2. Generation failures are modeled as a custom exception
   (LLMGenerationError), raised only after EVERY provider in the list
   has failed. app.py is expected to catch it and fall back to
   build_fallback_summary(), so one account's failure never stops the
   batch -- and a whole provider being down doesn't either, as long as
   at least one other provider is configured.

3. Fallback generation lives in this module, not in app.py. The
   reasoning: "what do we say when every LLM fails" is still a decision
   about how we describe risk to a human, which is this module's job.
"""

from __future__ import annotations

import logging

from llm_providers import LLMProvider
from models import RiskAssessment

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 15.0
MIN_SUMMARY_LENGTH = 20  # characters; below this, the response is treated as empty/garbage

SYSTEM_PROMPT = """\
You are a senior Revenue Operations analyst writing internal churn-risk \
summaries for the Customer Success team.

Rules you must follow strictly:
- Write exactly 2 to 3 sentences. No more, no less.
- Base your summary ONLY on the facts provided in the user message. \
Never invent, assume, or infer information that is not explicitly given.
- Do not simply restate the raw field values (e.g. do not write \
"failed_payment_count_last_30d is 2"). Translate them into natural, \
professional analyst language.
- If the provided signals are limited or borderline, say so explicitly \
(e.g. "risk signals are moderate and should be monitored") instead of \
overstating confidence.
- Tone: professional, concise, and actionable for a Customer Success \
audience deciding whether to reach out to the account.
- Do not include greetings, headers, bullet points, or any text other \
than the summary itself.
"""

USER_PROMPT_TEMPLATE = """\
Account: {account_name}
Plan: {plan_name}
MRR: ${mrr:,.2f}
Risk level: {risk_level}
Risk score: {score}

Risk signals detected:
{signals_block}

Write the 2-3 sentence risk summary now.
"""


class LLMGenerationError(Exception):
    """Raised when EVERY configured provider fails to produce a usable summary."""


def _build_user_prompt(assessment: RiskAssessment) -> str:
    """
    Builds the user-facing prompt from an already-computed RiskAssessment.

    Important: we pass the *signals* (business-rule output), not the raw
    Account fields directly. This keeps the LLM's job narrow -- turn
    known, already-validated facts into prose -- instead of asking it
    to also reason about what counts as risky, which is risk.py's job
    and should stay deterministic and explainable.
    """
    signals_block = "\n".join(
        f"- {signal.detail}" for signal in assessment.signals
    )
    return USER_PROMPT_TEMPLATE.format(
        account_name=assessment.account.account_name,
        plan_name=assessment.account.plan_name,
        mrr=assessment.account.mrr,
        risk_level=assessment.level.value,
        score=assessment.score,
        signals_block=signals_block,
    )


def _looks_valid(text: str) -> bool:
    """
    Minimal sanity check on the model output.

    We deliberately keep this cheap and heuristic (length + non-empty)
    rather than trying to parse/validate sentence count with regex.
    A stricter check (e.g. exactly 2-3 sentences) would be fragile
    against legitimate variation (abbreviations, decimals like "$1.2k")
    and isn't worth the complexity for a prototype. The prompt already
    does the heavy lifting on format; this check exists only to catch
    the degenerate case of an empty or near-empty response.
    """
    return bool(text) and len(text.strip()) >= MIN_SUMMARY_LENGTH


def generate_risk_summary(
    assessment: RiskAssessment,
    providers: list[LLMProvider],
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> tuple[str, str]:
    """
    Tries each provider in order until one returns a usable summary.

    Returns (summary_text, provider_name) so callers can record which
    provider actually produced the summary -- useful for logging and
    for surfacing in the Slack report ("Generated via Gemini") so
    nobody mistakes a successful failover for the primary provider.

    Raises LLMGenerationError only if EVERY provider in the list fails
    or returns an invalid/empty response. Callers (app.py) are expected
    to catch this and use build_fallback_summary() instead.
    """
    if not providers:
        raise LLMGenerationError("No LLM providers configured")

    last_error: Exception | None = None

    for provider in providers:
        try:
            text = provider.generate(SYSTEM_PROMPT, _build_user_prompt(assessment), timeout)
        except Exception as exc:  # noqa: BLE001 - intentionally broad, see module docstring
            # Every provider's SDK raises different exception types for
            # auth errors, quota errors, timeouts, etc. We don't need to
            # react differently per type here -- any failure just means
            # "try the next provider". See prompt_design.md for the
            # discussion on reacting differently to retryable errors.
            logger.warning(
                "%s failed for account_id=%s: %s",
                provider.name,
                assessment.account.account_id,
                exc,
            )
            last_error = exc
            continue

        if _looks_valid(text):
            return text.strip(), provider.name

        logger.warning(
            "%s returned an empty/invalid summary for account_id=%s: %r",
            provider.name,
            assessment.account.account_id,
            text,
        )
        last_error = ValueError(f"{provider.name} returned an invalid summary")

    raise LLMGenerationError(
        f"All configured providers failed for account {assessment.account.account_id}"
    ) from last_error


def build_fallback_summary(assessment: RiskAssessment) -> str:
    """
    Deterministic, template-based summary used when every provider fails.

    It's built directly from the same RiskSignal list the LLM would
    have used, so it stays factually correct even though it's less
    fluent. This guarantees Customer Success always gets *something*
    actionable, even during a full LLM outage.
    """
    top_signals = ", ".join(signal.detail.lower() for signal in assessment.signals[:3])
    return (
        f"{assessment.account.account_name} is flagged as {assessment.level.value} "
        f"risk (score {assessment.score}) based on: {top_signals}. "
        f"Automated summary generation was unavailable; manual review is recommended."
    )

