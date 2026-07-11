"""
LLM integration: generates a natural-language risk summary for each
at-risk account using the OpenAI API.

Design choices worth calling out:

1. The OpenAI client is injected as a parameter (not instantiated
   inside every function). This lets us unit-test the prompt-building
   and error-handling logic with a fake client, with zero network
   calls and zero API cost.

2. Generation failures are modeled as a custom exception
   (LLMGenerationError), not a silent None return. app.py is expected
   to catch it explicitly and fall back to build_fallback_summary().

3. Fallback generation lives in this module, not in app.py. The
   reasoning: "what do we say when the LLM fails" is still a decision
   about how we describe risk to a human, which is this module's job.
"""

from __future__ import annotations

import logging

from models import RiskAssessment

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 15.0
MIN_SUMMARY_LENGTH = 20

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
    """Raised when the LLM fails to produce a usable summary for an account."""


def _build_user_prompt(assessment: RiskAssessment) -> str:
    signals_block = "\n".join(f"- {signal.detail}" for signal in assessment.signals)
    return USER_PROMPT_TEMPLATE.format(
        account_name=assessment.account.account_name,
        plan_name=assessment.account.plan_name,
        mrr=assessment.account.mrr,
        risk_level=assessment.level.value,
        score=assessment.score,
        signals_block=signals_block,
    )


def _looks_valid(text: str) -> bool:
    return bool(text) and len(text.strip()) >= MIN_SUMMARY_LENGTH


def generate_risk_summary(
    assessment: RiskAssessment,
    client,
    model: str,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> str:
    prompt = _build_user_prompt(assessment)

    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            temperature=0.3,
            timeout=timeout,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "LLM call failed for account_id=%s: %s",
            assessment.account.account_id,
            exc,
        )
        raise LLMGenerationError(
            f"OpenAI call failed for account {assessment.account.account_id}"
        ) from exc

    text = (response.choices[0].message.content or "").strip()

    if not _looks_valid(text):
        logger.warning(
            "LLM returned an empty/invalid summary for account_id=%s: %r",
            assessment.account.account_id,
            text,
        )
        raise LLMGenerationError(
            f"OpenAI returned an empty or invalid summary for account "
            f"{assessment.account.account_id}"
        )

    return text


def build_fallback_summary(assessment: RiskAssessment) -> str:
    top_signals = ", ".join(signal.detail.lower() for signal in assessment.signals[:3])
    return (
        f"{assessment.account.account_name} is flagged as {assessment.level.value} "
        f"risk (score {assessment.score}) based on: {top_signals}. "
        f"Automated summary generation was unavailable; manual review is recommended."
    )
