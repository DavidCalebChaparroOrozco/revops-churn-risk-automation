"""
Slack delivery: formats the weekly churn-risk report and sends it to a
Slack channel via an Incoming Webhook.

Design choices worth calling out:

1. Formatting (build_slack_message) is fully separated from delivery
   (send_to_slack). This mirrors the split we used in llm.py between
   prompt-building and the actual API call: formatting is pure and
   testable with plain asserts, delivery is the only part that touches
   the network.

2. `send_to_slack` accepts the HTTP POST function as a parameter
   (defaulting to `requests.post`), the same dependency-injection
   pattern used for the OpenAI client in llm.py. This keeps the
   testing approach consistent across the whole codebase and lets us
   verify delivery/error-handling logic without hitting the real
   Slack API.

3. We send ONE message per run (not one per account). Slack Incoming
   Webhooks are rate-limited and a single account failing to reach
   Slack shouldn't be a partial-failure state to manage -- either the
   whole weekly report goes out, or it doesn't, and we log accordingly.
"""

from __future__ import annotations

import logging
from datetime import date

import requests

from models import AccountRiskReport, RiskLevel

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 10.0

# Slack Block Kit hard limit: max 50 blocks per message, 3000 characters
# per text object. Not enforced here for the prototype (see README's
# "production improvements" section) but documented so it's not a
# silent surprise if the at-risk list grows large.
SLACK_MAX_BLOCKS = 50

_LEVEL_EMOJI = {
    RiskLevel.HIGH: "🔴",
    RiskLevel.MEDIUM: "🟡",
}


class SlackDeliveryError(Exception):
    """Raised when the report could not be delivered to Slack."""


def _account_block(report: AccountRiskReport) -> dict:
    """Builds the Slack block for a single account's risk report."""
    account = report.assessment.account
    emoji = _LEVEL_EMOJI.get(report.assessment.level, "⚪")
    level_label = report.assessment.level.value.upper()

    header_line = (
        f"{emoji} *{account.account_name}* — {level_label} risk "
        f"(score {report.assessment.score}) · ${account.mrr:,.0f} MRR · {account.plan_name}"
    )

    summary_text = report.summary
    if report.summary_generation_failed:
        # Visible marker so a human knows this summary is the
        # deterministic fallback, not an LLM-generated one -- important
        # for trust: nobody should mistake a template for analysis.
        summary_text += "\n_⚠️ AI summary unavailable, showing rule-based fallback._"
    else:
        # Equally important in the other direction: if a failover
        # happened (e.g. OpenAI was out of quota and Gemini produced
        # this summary), that should be visible too -- silently
        # succeeding on a secondary provider without saying so would
        # hide a signal that the primary provider needs attention.
        summary_text += f"\n_Generated via {report.summary_source.title()}._"

    return {
        "type": "section",
        "text": {
            "type": "mrkdwn",
            "text": f"{header_line}\n{summary_text}",
        },
    }


def build_slack_message(
    reports: list[AccountRiskReport],
    report_date: date | None = None,
) -> dict:
    """
    Builds the full Slack Block Kit payload for the weekly report.

    Always produces a valid, well-formed message -- including the
    zero-accounts case. Explicitly reporting "no accounts at risk" is a
    deliberate choice: it confirms the automation actually ran, instead
    of Customer Success wondering whether the report silently failed or
    there genuinely was nothing to flag this week.
    """
    report_date = report_date or date.today()

    blocks: list[dict] = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": "📊 Weekly Churn Risk Report",
            },
        },
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f"{report_date.isoformat()} · {len(reports)} account(s) flagged",
                }
            ],
        },
        {"type": "divider"},
    ]

    if not reports:
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "✅ No accounts crossed the risk threshold this week.",
                },
            }
        )
    else:
        for report in reports:
            blocks.append(_account_block(report))
            blocks.append({"type": "divider"})
        blocks.pop()  # drop the trailing divider after the last account

    # Fallback plain-text summary. Slack requires/recommends a top-level
    # "text" field for notification previews and for clients that don't
    # render blocks (e.g. some notification pop-ups).
    fallback_text = f"Weekly Churn Risk Report: {len(reports)} account(s) flagged"

    return {"text": fallback_text, "blocks": blocks}


def send_to_slack(
    payload: dict,
    webhook_url: str,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    http_post=requests.post,
) -> None:
    """
    Sends the built payload to the Slack Incoming Webhook.

    Raises SlackDeliveryError on any failure (network error, timeout,
    or non-2xx response). Slack's webhook endpoint returns the plain
    text "ok" with a 200 status on success and a descriptive error body
    on failure (e.g. "invalid_payload", "channel_not_found") -- we log
    that body since it's usually specific enough to debug directly.
    """
    try:
        response = http_post(webhook_url, json=payload, timeout=timeout)
    except requests.RequestException as exc:
        logger.error("Failed to reach Slack webhook: %s", exc)
        raise SlackDeliveryError("Could not reach the Slack webhook endpoint") from exc

    if response.status_code != 200:
        logger.error(
            "Slack webhook returned status %s: %s",
            response.status_code,
            response.text,
        )
        raise SlackDeliveryError(
            f"Slack webhook responded with status {response.status_code}: {response.text}"
        )

    logger.info("Weekly churn risk report delivered to Slack successfully.")
