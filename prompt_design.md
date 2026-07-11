# Prompt Design

This document explains the reasoning behind the prompt used in `llm.py`
to generate per-account risk summaries.

## The prompt

**System prompt** (fixed, defines the role and hard constraints):

```
You are a senior Revenue Operations analyst writing internal churn-risk
summaries for the Customer Success team.

Rules you must follow strictly:
- Write exactly 2 to 3 sentences. No more, no less.
- Base your summary ONLY on the facts provided in the user message.
  Never invent, assume, or infer information that is not explicitly given.
- Do not simply restate the raw field values...
- If the provided signals are limited or borderline, say so explicitly...
- Tone: professional, concise, and actionable...
- Do not include greetings, headers, bullet points, or any text other
  than the summary itself.
```

**User prompt** (per-account, built from `RiskAssessment`):

```
Account: {account_name}
Plan: {plan_name}
MRR: ${mrr}
Risk level: {risk_level}
Risk score: {score}

Risk signals detected:
- {signal_1}
- {signal_2}
...

Write the 2-3 sentence risk summary now.
```

## Why it's written this way

**We pass `RiskAssessment.signals`, not the raw `Account` fields.**
If we handed the model the raw CSV row, we would effectively be asking
it to also decide what counts as risky -- duplicating risk.py's job
inside an unpredictable, non-deterministic component. Instead,
risk.py has already done that reasoning deterministically, and the
LLM's only job is narrower and safer: turn already-validated facts
into fluent, professional prose.

**Explicit "do not invent data" instruction.** Without it, models tend
to add plausible-sounding but fabricated color when the actual signal
list is short. Since this summary goes straight to Customer Success,
fabricated detail is worse than a shorter, more honest summary.

**Sentence count is a soft constraint, enforced by instruction rather
than code.** Enforcing this strictly in code would require reliable
sentence-boundary detection, which is fragile against abbreviations
and dollar amounts.

## What's in context vs. what's left out

**In context:** account name, plan, MRR, computed risk level/score,
and the list of fired risk signals in human-readable form.

**Left out:** `account_id` (no narrative value), raw
`subscription_status` enum (already folded into a signal), signals
that did NOT fire, and any historical data not present in the CSV.

## Known failure modes

1. Borderline-score accounts may still read as confidently "at risk".
2. Minor tone drift with `temperature=0.3`.
3. No citation-to-signal mapping in free-text prose.
4. Prompt injection via `account_name`, which comes directly from an
   external CSV.

## How to improve this for production

- Structured output (JSON) instead of free text.
- Retry with backoff on transient errors specifically.
- Sanitize/escape `account_name` before interpolation.
- Log prompts and completions for quality evaluation over time.
- A/B the sentence-count constraint against a structured format.
