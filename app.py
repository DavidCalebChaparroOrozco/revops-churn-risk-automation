"""
Orchestrator for the weekly churn-risk automation.

Pipeline: CSV -> validated Accounts -> risk assessment -> LLM summaries
(with fallback on failure) -> Slack report.

Design choice: the actual pipeline logic lives in `run_pipeline()`,
which takes the OpenAI client and the Slack HTTP post function as
parameters. `main()` is a thin CLI wrapper that parses arguments,
loads configuration, builds the *real* client, and calls
`run_pipeline()`.

Why split it this way: it's the same dependency-injection pattern used
throughout the project (llm.py, slack_client.py), applied one level up.
It means the entire pipeline -- including the "one bad account
shouldn't stop the batch" resilience behavior -- can be exercised in a
test with fake clients and zero network calls, instead of only being
verifiable by actually running the script end to end.
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

from llm import LLMGenerationError, build_fallback_summary, generate_risk_summary
from llm_providers import GeminiProvider, LLMProvider, OpenAIProvider
from models import Account, AccountRiskReport
from risk import MEDIUM_THRESHOLD, evaluate_accounts
from slack_client import SlackDeliveryError, build_slack_message, send_to_slack

logger = logging.getLogger(__name__)


class ConfigError(Exception):
    """Raised when required configuration is missing or invalid."""


def load_accounts_from_csv(csv_path: str) -> tuple[list[Account], list[tuple[str, str]]]:
    """
    Loads and validates accounts from the CSV file.

    Returns (valid_accounts, errors). Errors are (row_identifier, message)
    pairs for rows that failed Pydantic validation. We deliberately do
    NOT let one malformed row abort the whole load: a single bad row in
    a 500-row CSV shouldn't prevent reporting on the other 499. This is
    the same "one failure shouldn't stop the batch" principle we apply
    to LLM and Slack calls, applied to the very first step of the
    pipeline.
    """
    df = pd.read_csv(csv_path)

    accounts: list[Account] = []
    errors: list[tuple[str, str]] = []

    for row in df.to_dict(orient="records"):
        row_id = str(row.get("account_id", "<unknown>"))
        try:
            accounts.append(Account(**row))
        except Exception as exc:  # noqa: BLE001 - any validation failure is handled the same way
            logger.warning("Skipping invalid row account_id=%s: %s", row_id, exc)
            errors.append((row_id, str(exc)))

    return accounts, errors


def build_reports(
    assessments: list,
    providers: list[LLMProvider],
) -> list[AccountRiskReport]:
    """
    Generates a risk summary for each assessment, trying providers in
    order (see llm.generate_risk_summary) and falling back to a
    rule-based summary only if ALL providers fail.

    This is where the "no single LLM failure stops the batch" and "no
    single PROVIDER outage stops the batch" requirements are actually
    enforced: the try/except is per-account, inside the loop, so one
    account's failure -- or one provider's outage -- only affects that
    one account's report, and only if every configured provider fails.
    """
    reports: list[AccountRiskReport] = []

    for assessment in assessments:
        try:
            summary, source = generate_risk_summary(assessment, providers)
            failed = False
        except LLMGenerationError as exc:
            logger.warning(
                "Falling back to rule-based summary for account_id=%s: %s",
                assessment.account.account_id,
                exc,
            )
            summary = build_fallback_summary(assessment)
            source = "fallback"
            failed = True

        reports.append(
            AccountRiskReport(
                assessment=assessment,
                summary=summary,
                summary_source=source,
                summary_generation_failed=failed,
            )
        )

    return reports


def run_pipeline(
    csv_path: str,
    providers: list[LLMProvider],
    slack_webhook_url: str | None,
    medium_threshold: int = MEDIUM_THRESHOLD,
    dry_run: bool = False,
    reference_date: date | None = None,
    http_post=requests.post,
) -> dict:
    """
    Runs the full pipeline and returns a summary dict, useful both for
    the CLI's final printout and for tests to assert on.

    In `dry_run` mode, the Slack payload is built but never sent --
    useful for previewing the report locally, e.g. while iterating on
    prompt wording, without spamming the real channel.
    """
    accounts, load_errors = load_accounts_from_csv(csv_path)
    logger.info("Loaded %d valid account(s), %d row error(s)", len(accounts), len(load_errors))

    assessments = evaluate_accounts(
        accounts, reference_date=reference_date, medium_threshold=medium_threshold
    )
    logger.info("%d account(s) flagged at risk", len(assessments))

    reports = build_reports(assessments, providers)
    llm_fallbacks = sum(1 for r in reports if r.summary_generation_failed)
    provider_usage = {p.name: 0 for p in providers}
    for r in reports:
        if r.summary_source in provider_usage:
            provider_usage[r.summary_source] += 1

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
        "llm_fallbacks_used": llm_fallbacks,
        "provider_usage": provider_usage,
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


def _build_providers() -> list[LLMProvider]:
    """
    Builds the ordered list of LLM providers based on configured API keys.

    Order matters: OpenAI is tried first, Gemini second, purely as a
    default -- there's no technical reason it couldn't be the other way
    around. Making this order configurable via an env var (e.g.
    LLM_PROVIDER_ORDER=gemini,openai) is a reasonable next step, left
    out here to keep the configuration surface small for the prototype.

    A provider is only added to the list if its API key is present.
    This means the system degrades gracefully: two keys configured ->
    automatic failover; one key -> current single-provider behavior;
    zero keys -> caught explicitly below as a configuration error.
    """
    providers: list[LLMProvider] = []

    openai_api_key = os.environ.get("OPENAI_API_KEY")
    if openai_api_key:
        from openai import OpenAI

        openai_model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
        providers.append(OpenAIProvider(OpenAI(api_key=openai_api_key), openai_model))

    gemini_api_key = os.environ.get("GEMINI_API_KEY")
    if gemini_api_key:
        from google import genai

        gemini_model = os.environ.get("GEMINI_MODEL", "gemini-3.1-flash-lite")
        providers.append(GeminiProvider(genai.Client(api_key=gemini_api_key), gemini_model))

    return providers


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    load_dotenv()

    args = _build_arg_parser().parse_args()

    slack_webhook_url = os.environ.get("SLACK_WEBHOOK_URL")
    medium_threshold = int(os.environ.get("RISK_SCORE_THRESHOLD", MEDIUM_THRESHOLD))

    providers = _build_providers()
    if not providers:
        # We fail fast here rather than letting every single LLM call
        # fail one by one -- if no provider is configured, ALL accounts
        # would hit the fallback path, which is misleading output for
        # what is actually a configuration problem, not a per-account
        # LLM issue.
        logger.error(
            "No LLM provider configured. Set OPENAI_API_KEY and/or "
            "GEMINI_API_KEY in your .env file."
        )
        sys.exit(1)

    logger.info("LLM providers configured (in order): %s", [p.name for p in providers])

    try:
        summary = run_pipeline(
            csv_path=args.csv_path,
            providers=providers,
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
