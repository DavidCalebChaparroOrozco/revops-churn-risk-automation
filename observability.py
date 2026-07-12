"""
Structured observability for the per-account decision pipeline.

Why this is a separate module from the human-readable logging already
used throughout app.py/llm.py/risk.py (logger.info/warning): those logs
are good for a person watching the console in real time, but they are
NOT good for reconstructing "why did the system decide X for account Y
on this run" after the fact, feeding a dashboard, or noticing a
provider quietly degrading (e.g. summaries getting shorter, or one
provider's failure rate creeping up) before it fully breaks. That needs
one structured, machine-parseable record per account, with a stable
schema -- not scattered free-text log lines.

Why this matters specifically for an LLM-based pipeline: unlike a
purely deterministic system, the LLM step here is non-deterministic
and not perfectly reproducible -- re-running the same CSV through the
same provider can produce a differently-worded summary. Without a
structured audit trail captured at the moment of the decision, "why
was this account flagged as high risk, and why does this summary say
what it says" becomes unanswerable after the fact except by re-running
the pipeline and hoping the LLM says something similar. The audit
record captures the deterministic part (score, signals, level -- from
risk.py, fully reproducible) alongside the non-deterministic part
(which provider was used, whether it succeeded, summary length) so a
human debugging a bad summary six weeks from now can see exactly what
inputs and provider produced it, without needing the original LLM call
to still be reproducible.

Format: one JSON object per line (JSON Lines). Chosen over a bespoke
text format because it's trivially greppable, pipeable into `jq`, and
ingestible by any log aggregator (Datadog, CloudWatch, etc.) without a
custom parser -- the lowest-effort format that's still genuinely
structured.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from models import AccountRiskReport

# Separate logger + separate handler (configured in app.py::main) so
# these structured lines are NOT interleaved with, or prefixed by, the
# human-readable "%(asctime)s %(levelname)s ..." format used by the
# rest of the app. Mixing the two would break the "one clean JSON
# object per line" property that makes this useful for tooling.
audit_logger = logging.getLogger("audit")


def log_account_decision(report: AccountRiskReport) -> None:
    """
    Emits one structured JSON record capturing the full decision trail
    for a single account: which risk signals fired and why (from
    risk.py, fully deterministic), the final score/level, which LLM
    provider produced the summary (or whether every provider failed
    and the rule-based fallback was used instead), and basic shape
    metadata about the output (length) that's cheap to alert on later
    (e.g. "provider X's summaries just got suspiciously short").
    """
    assessment = report.assessment
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "account_id": assessment.account.account_id,
        "account_name": assessment.account.account_name,
        "risk_score": assessment.score,
        "risk_level": assessment.level.value,
        "signals": [
            {"name": s.name, "points": s.points} for s in assessment.signals
        ],
        "summary_source": report.summary_source,
        "summary_generation_failed": report.summary_generation_failed,
        "summary_length_chars": len(report.summary),
    }
    audit_logger.info(json.dumps(record))
