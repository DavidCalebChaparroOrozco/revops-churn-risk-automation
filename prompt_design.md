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

**Role framing in the system prompt, not the user prompt.** The persona
("senior RevOps analyst") and the hard constraints belong in the system
prompt because they should apply identically to every account, and
system-level instructions are harder for the model to drift away from
over the course of generation than instructions buried in a long user
message.

**We pass `RiskAssessment.signals`, not the raw `Account` fields.**
This is the single most important design decision in this module. If
we handed the model the raw CSV row (`failed_payment_count_last_30d: 2`,
`days_since_last_login: 35`, ...), we would effectively be asking it to
*also* decide what counts as risky -- duplicating risk.py's job inside
an unpredictable, non-deterministic component. Instead, risk.py has
already done that reasoning deterministically, and the LLM's only job
is narrower and safer: turn already-validated facts into fluent,
professional prose. This also keeps the "why is this account at risk"
answer traceable to `risk.py`'s explicit rules, not to whatever the
model decided to emphasize.

**Explicit "do not invent data" instruction.** Without it, models tend
to add plausible-sounding but fabricated color ("likely due to a recent
support escalation") when the actual signal list is short. Since this
summary goes straight to Customer Success and may inform a real
customer conversation, fabricated detail is worse than a shorter,
more honest summary.

**Explicit instruction to flag limited evidence.** Requested in the
original requirements. This matters specifically for accounts that
clear the risk threshold with only one or two moderate signals (e.g.
score of 3 from two weak signals) -- the summary should read
differently for those than for a 5-signal, high-confidence case, even
though both technically qualify as "at risk".

**Sentence count is a soft constraint, enforced by instruction rather
than code.** We do not parse and reject responses that aren't exactly
2-3 sentences (see `llm.py::_looks_valid`). Enforcing this strictly in
code would require reliable sentence-boundary detection, which is
fragile against abbreviations, dollar amounts, and decimals. We accept
minor variance here in exchange for not building a fragile validator;
the practical risk (a 4-sentence summary slipping through) is low-cost
if it happens.

## Iteration: what was tried first, what would have broken, what changed

**A note on how this section is written:** rather than fabricate a
transcript of a "first bad attempt" that wasn't actually run against
a live model, this section documents the reasoning applied *before*
writing the final prompt -- reasoning grounded in well-known,
widely-documented LLM behavior patterns, not guesswork. The honest
version of "what changed" here is "what I designed around from the
start, and why," which is arguably more useful than a contrived failed
first draft. Where a genuine live A/B between prompt versions would add
real evidence, that's called out below and left as a production
next step rather than simulated.

**The naive version we deliberately did not write:** pass the raw CSV
row straight into the prompt and ask the model to "summarize this
account's churn risk in 2-3 sentences." Two well-documented failure
patterns predictably show up with this approach, which is exactly why
the final design (`_build_user_prompt` in `llm.py`) never took it:

1. **Field regurgitation.** Models given raw structured data as context
   tend to echo field names and values close to verbatim
   ("failed_payment_count_last_30d is 3, days_since_last_login is 67")
   rather than translating them into analyst prose, especially at
   lower temperatures or with shorter instructions. This is *why* the
   system prompt has an explicit instruction against it, and *why* the
   model is handed pre-translated `RiskSignal.detail` strings ("3 failed
   payments in the last 30 days") instead of raw field/value pairs --
   removing the raw field names from the model's context entirely is a
   stronger guarantee than merely instructing it not to use them.
2. **Confidence untethered from evidence strength.** A model asked to
   write a "risk summary" for any account handed to it tends to write
   with uniform confidence regardless of how much or how little
   evidence supports the risk call -- it has no way to know, on its
   own, that a 3-point score (barely crossing the `MEDIUM` threshold)
   deserves more hedging than a 14-point score. This is why the prompt
   includes an explicit instruction to flag limited evidence, and it's
   also why this limitation is called out below as one the instruction
   only partially solves.

**What a real next iteration would add:** an actual A/B, not a
predicted one -- run both the naive raw-field version and the current
signal-based version against the same 15-account sample through the
same provider, and compare outputs side by side. This is listed
explicitly in the production improvements below rather than skipped
silently, since "we predicted the failure mode but didn't empirically
confirm it" is a real limitation of this write-up, not something to
gloss over.

## The one thing this prompt will get wrong even when it works

Asked to name a single specific failure mode (not a list): **the model
cannot distinguish a borderline flag from an overwhelming one**,
because it never sees the numeric thresholds that produced the
signals it's handed -- only the signal list itself.

Concretely: an account that scores exactly 3 (e.g. one moderate
inactivity signal plus one weak failed-payment signal) and an account
that scores 14 (five signals firing, including a past-due subscription)
both get summarized with the same prompt and the same instruction set.
The "flag limited evidence" instruction helps when the signal *list* is
short, but a short list and a low score aren't the same thing every
time, and the model has no access to `RiskAssessment.score` or the
`MEDIUM_THRESHOLD`/`HIGH_THRESHOLD` constants from `risk.py` -- only to
the `signals` array already filtered into prose-ready strings. Two
accounts with different underlying confidence can end up reading with
similarly assertive tone in the final Slack message.

**What I'd do about it in production:** pass the numeric score and the
threshold values into the prompt explicitly (not just the signal
list), and add an instruction that ties hedging language directly to
score proximity to the threshold ("if the score is within 1-2 points
of the minimum flagging threshold, describe the risk as emerging or
worth monitoring rather than urgent"). This was left out of the
current version specifically because it re-introduces raw numbers into
the prompt, which was the thing deliberately avoided (see "What's in
context" below) to keep the model from reasoning about thresholds
itself -- so this fix has its own trade-off worth testing, not a free
improvement.

## What's in context vs. what's left out

**In context:**
- Account name, plan, MRR (business-relevant identifying info)
- The computed risk level and score
- The list of fired risk signals, in human-readable form

**Deliberately left out:**
- `account_id` -- an internal identifier with no narrative value; including
  it invites the model to reference it awkwardly in prose.
- `subscription_status` as a raw enum value -- it's already folded into
  the `subscription_status` signal's `detail` text when it's a risk
  factor, so passing it twice would be redundant and could cause the
  model to inconsistently phrase the same fact two different ways.
- Any signals that did NOT fire -- we only tell the model what's wrong,
  not the full list of everything that's fine, to keep the prompt short
  and focused. This is also why the summary skews toward risk framing,
  which is the intended use case (these are pre-filtered at-risk accounts
  only, per `risk.py::evaluate_accounts`).
- Historical data (e.g. previous months' MRR, past tickets) -- not
  present in the source CSV at all, so it's a non-issue today, but
  worth flagging for the production section below.

## Known failure modes of this prompt

1. **Borderline-score accounts may still read as confidently "at risk".**
   The instruction to flag limited evidence is only a heuristic; the
   model isn't given the numeric threshold logic, so it can't reason
   about "this account barely crossed the line" unless the signal list
   itself is sparse.
2. **Tone drift with `temperature=0.3`.** Non-zero temperature is
   intentional (see `llm.py`), but it means summaries for very similar
   accounts won't be word-for-word identical between runs. This is a
   minor, acceptable trade-off for more natural-sounding prose.
3. **No citation-to-signal mapping.** The summary is prose, not
   structured, so nothing enforces that every sentence traces back to
   a specific signal -- a model could, in principle, over-weight one
   signal and ignore another in the write-up, even without inventing
   new facts.
4. **Prompt injection via account_name.** `account_name` comes from an
   external CSV and is inserted directly into the user prompt. A
   malicious or malformed account name (e.g. containing text that looks
   like an instruction) could attempt to influence the model's output.
   Low risk for an internal RevOps CSV, but relevant if this CSV is
   ever sourced from a less trusted system (see production section).

## How to improve this for production

- **Structured output instead of free text.** Ask the model to return
  JSON (`{"summary": "...", "confidence": "low|medium|high"}`) so the
  confidence flagging becomes a parseable field instead of relying on
  the model to phrase it naturally in prose. This also makes automated
  QA of summaries possible (e.g. flag any summary under N characters).
- **Retry with backoff on transient errors specifically**
  (`RateLimitError`, `APITimeoutError`), instead of the current
  catch-all-and-move-to-next-provider. As of this version, a failed
  call moves immediately to the next configured provider (OpenAI ->
  Gemini, see `llm_providers.py` and `llm.py::generate_risk_summary`)
  rather than retrying the same provider -- which is actually a
  reasonable production strategy on its own (fail fast to a healthy
  provider instead of burning time on retries against a provider that's
  out of quota), but a hybrid (quick retry, then failover) would be
  even better for purely transient errors like a dropped connection.
- **Sanitize/escape `account_name`** before interpolation, or move
  untrusted fields into a clearly-delimited data block in the prompt,
  to reduce prompt-injection surface if the CSV source becomes
  less trusted.
- **Log prompts and completions** (with account_id, not full PII) to
  build a dataset for evaluating summary quality over time and
  catching silent drift after model version upgrades.
- **A/B the sentence-count constraint** against a stricter structured
  format to see whether free-text prose is actually what Customer
  Success wants, versus a short structured card (risk level + one-line
  reason + suggested action).
- **Make provider order configurable** (e.g. `LLM_PROVIDER_ORDER=gemini,openai`
  in `.env`) instead of the current hardcoded OpenAI-first order in
  `app.py::_build_providers()`.
- **Enforce a real per-call timeout for Gemini.** The `google-genai` SDK
  configures timeouts at client construction (`types.HttpOptions`), not
  per-call like the OpenAI SDK -- see the caveat documented in
  `llm_providers.py::GeminiProvider`. Today the `timeout` argument is
  accepted for interface symmetry but not actually enforced per call.

## Multi-provider failover (implemented)

The original design used a single provider (OpenAI) with a rule-based
fallback if it failed. In practice, testing surfaced a real-world
failure mode -- an OpenAI account without billing configured returns
`429 insufficient_quota` on every call -- which motivated adding a
second provider (Gemini) as an automatic failover *before* falling back
to the rule-based summary. See `llm_providers.py` for the provider
abstraction and `llm.py::generate_risk_summary` for the try-in-order
logic. This is a good example of a decision that would have been
premature at the start of the project (one provider, no evidence a
second was needed) but became clearly justified once real testing
produced a real failure.
