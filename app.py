"""
Orchestrator for the weekly churn-risk automation.

Pipeline: CSV -> validated Accounts -> risk assessment -> LLM summaries
(with fallback on failure) -> Slack report.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import date

import pandas as pd
import requests
from dotenv import load_dotenv
from openai import OpenAI

from llm import LLMGenerationError, build_fallback_summary, generate_risk_summary
from models import Account, AccountRiskReport
from risk import MEDIUM_THRESHOLD, evaluate_accounts
from slack_client import SlackDeliveryError, build_slack_message, send_to_slack

logger = logging.getLogger(__name__)


class ConfigError(Exception):
    """Raised when required configuration is missing or invalid."""


def load_accounts_from_csv(csv_path: str) -> tuple[list[Account], list[tuple[str, str]]]:
    df = pd.read_csv(csv_path)

    accounts: list[Account] = []
    errors: list[tuple[str, str]] = []

    for row in df.to_dict(orient="records"):
        row_id = str(row.get("account_id", "<unknown>"))
        try:
            accounts.append(Account(**row))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Skipping invalid row account_id=%s: %s", row_id, exc)
            errors.append((row_id, str(exc)))

    return accounts, errors


def build_reports(
    assessments: list,
    openai_client,
    openai_model: str,
) -> list[AccountRiskReport]:
    reports: list[AccountRiskReport] = []

    for assessment in assessments:
        try:
            summary = generate_risk_summary(assessment, openai_client, openai_model)
            failed = False
        except LLMGenerationError as exc:
            logger.warning(
                "Falling back to rule-based summary for account_id=%s: %s",
                assessment.account.account_id,
                exc,
            )
            summary = build_fallback_summary(assessment)
            failed = True

        reports.append(
            AccountRiskReport(
                assessment=assessment,
                summary=summary,
                summary_generation_failed=failed,
            )
        )

    return reports


def run_pipeline(
    csv_path: str,
    openai_client,
    openai_model: str,
    slack_webhook_url: str | None,
    medium_threshold: int = MEDIUM_THRESHOLD,
    dry_run: bool = False,
    reference_date: date | None = None,
    http_post=requests.post,
) -> dict:
    accounts, load_errors = load_accounts_from_csv(csv_path)
    logger.info("Loaded %d valid account(s), %d row error(s)", len(accounts), len(load_errors))

    assessments = evaluate_accounts(
        accounts, reference_date=reference_date, medium_threshold=medium_threshold
    )
    logger.info("%d account(s) flagged at risk", len(assessments))

    reports = build_reports(assessments, openai_client, openai_model)
    llm_failures = sum(1 for r in reports if r.summary_generation_failed)

    payload = build_slack_message(reports, report_date=reference_date)

    delivered = False
    if dry_run:
        logger.info("Dry run enabled -- Slack payload was NOT sent. Preview:")
        print(json.dumps(payload, indent=2))
    else:
        if not slack_webhook_url:
            raise ConfigError("SLACK_WEBHOOK_URL is not set. Cannot deliver the report.")
        send_to_slack(payload, slack_webhook_url, http_post=http_post)
        delivered = True

    return {
        "accounts_loaded": len(accounts),
        "row_errors": len(load_errors),
        "accounts_at_risk": len(assessments),
        "llm_fallbacks_used": llm_failures,
        "delivered_to_slack": delivered,
    }


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Weekly RevOps churn-risk automation: CSV -> risk scoring -> LLM summaries -> Slack."
    )
    parser.add_argument(
        "--csv-path",
        default="sample_accounts.csv",
        help="Path to the input accounts CSV (default: sample_accounts.csv)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Build the Slack report but do not send it; print it to stdout instead.",
    )
    return parser


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    load_dotenv()

    args = _build_arg_parser().parse_args()

    openai_api_key = os.environ.get("OPENAI_API_KEY")
    openai_model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
    slack_webhook_url = os.environ.get("SLACK_WEBHOOK_URL")
    medium_threshold = int(os.environ.get("RISK_SCORE_THRESHOLD", MEDIUM_THRESHOLD))

    if not openai_api_key:
        logger.error("OPENAI_API_KEY is not set. Check your .env file.")
        sys.exit(1)

    openai_client = OpenAI(api_key=openai_api_key)

    try:
        summary = run_pipeline(
            csv_path=args.csv_path,
            openai_client=openai_client,
            openai_model=openai_model,
            slack_webhook_url=slack_webhook_url,
            medium_threshold=medium_threshold,
            dry_run=args.dry_run,
        )
    except (ConfigError, SlackDeliveryError) as exc:
        logger.error("Pipeline failed: %s", exc)
        sys.exit(1)

    logger.info("Run summary: %s", summary)


if __name__ == "__main__":
    main()
