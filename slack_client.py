"""
Slack delivery: formats the weekly churn-risk report and sends it to a
Slack channel via an Incoming Webhook.

Design choices worth calling out:

1. Formatting (build_slack_message) is fully separated from delivery
   (send_to_slack): formatting is pure and testable with plain asserts,
   delivery is the only part that touches the network.

2. `send_to_slack` accepts the HTTP POST function as a parameter
   (defaulting to `requests.post`), the same dependency-injection
   pattern used for the OpenAI client in llm.py.

3. We send ONE message per run (not one per account).
"""

from __future__ import annotations

import logging
from datetime import date

import requests

from models import AccountRiskReport, RiskLevel

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 10.0
SLACK_MAX_BLOCKS = 50

_LEVEL_EMOJI = {
    RiskLevel.HIGH: "🔴",
    RiskLevel.MEDIUM: "🟡",
}


class SlackDeliveryError(Exception):
    """Raised when the report could not be delivered to Slack."""


def _account_block(report: AccountRiskReport) -> dict:
    account = report.assessment.account
    emoji = _LEVEL_EMOJI.get(report.assessment.level, "⚪")
    level_label = report.assessment.level.value.upper()

    header_line = (
        f"{emoji} *{account.account_name}* — {level_label} risk "
        f"(score {report.assessment.score}) · ${account.mrr:,.0f} MRR · {account.plan_name}"
    )

    summary_text = report.summary
    if report.summary_generation_failed:
        summary_text += "\n_⚠️ AI summary unavailable, showing rule-based fallback._"

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
    report_date = report_date or date.today()

    blocks: list[dict] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "📊 Weekly Churn Risk Report"},
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
        blocks.pop()

    fallback_text = f"Weekly Churn Risk Report: {len(reports)} account(s) flagged"
    return {"text": fallback_text, "blocks": blocks}


def send_to_slack(
    payload: dict,
    webhook_url: str,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    http_post=requests.post,
) -> None:
    try:
        response = http_post(webhook_url, json=payload, timeout=timeout)
    except requests.RequestException as exc:
        logger.error("Failed to reach Slack webhook: %s", exc)
        raise SlackDeliveryError("Could not reach the Slack webhook endpoint") from exc

    if response.status_code != 200:
        logger.error(
            "Slack webhook returned status %s: %s", response.status_code, response.text
        )
        raise SlackDeliveryError(
            f"Slack webhook responded with status {response.status_code}: {response.text}"
        )

    logger.info("Weekly churn risk report delivered to Slack successfully.")
