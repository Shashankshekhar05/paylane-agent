# Paylane AI Support Agent

A small, local, testable AI customer-support prototype for the fictional
fintech app Paylane (hiring assignment). The Groq-hosted model
(`openai/gpt-oss-20b`) interprets customer messages and proposes tool calls;
deterministic Python code enforces identity verification, refund eligibility,
duplicate-refund prevention, and escalation before any mock tool runs.

- System prompt + rulebook: `app/prompts.py`
- Mock tools (`get_customer`, `get_transaction`, `issue_refund`,
  `escalate_to_human`): `app/tools.py`
- Policy gates (verification, refund, escalation, duplicates): `app/policy.py`
- Orchestrator (Groq tool calling, safety guards): `app/agent.py`
- CLI: `app/runner.py`
- 12 scripted scenarios: `scenarios/conversations.json`
- Offline evaluator + results: `eval/evaluate.py`, `eval/results.json`
- Readable transcripts: `transcripts/conversations.md`
- Policy assumptions and open questions: `docs/POLICY_DECISIONS.md`
- Failure/fix/retest record: `docs/FAILURE_AND_RETEST.md`
- Note to the head of support: `docs/customer_note.md`

Out of scope by design: no web UI, no FastAPI/HTTP server, no database, no
real payments, no real ticketing integration. See `ARCHITECTURE.md`.

## Setup

Requirements: Python 3.12+ (per `AGENTS.md`), `pip`.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[test]"
```

This installs the runtime dependency (`pydantic`) and the test dependency
(`pytest`).

## Environment configuration

The API key is read from `GROQ_API_KEY` (environment first, untracked local
`.env` file as fallback). Placeholders only are committed.

```powershell
Copy-Item .env.example .env
# then edit .env and set a real GROQ_API_KEY
```

| Variable        | Default                            | Purpose                          |
|-----------------|------------------------------------|----------------------------------|
| `GROQ_API_KEY`  | (required for live calls)          | Groq API credential              |
| `GROQ_MODEL`    | `openai/gpt-oss-20b`               | Model name                       |
| `GROQ_BASE_URL` | `https://api.groq.com/openai/v1`   | OpenAI-compatible endpoint (HTTPS)|
| `GROQ_TIMEOUT_S`| `30`                               | Request timeout in seconds       |

Check configuration without calling any API:

```powershell
python -m app.runner --check-config
```

Expected output with a real key set (key value is never printed):

```text
config OK: GROQ_API_KEY: set; model=openai/gpt-oss-20b; host=api.groq.com; timeout_s=30.0
```

Without a usable key it exits non-zero with a setup error naming
`GROQ_API_KEY`. Never commit `.env` or any real key (`.env` is gitignored;
see `.env.example` for placeholder values only).

## Offline CLI usage (no network, no key needed)

Run one offline CLI turn (no network or API key required):

```powershell
python -m app.runner --message "Hello" --no-live
```

Expected output on a fresh run: the agent asks for the email address on file,
followed by a `--- audit ---` trace similar to:

```text
To help with your account, please reply with the email address on file.
--- audit ---
1 turn_start - - - -
2 policy_decision - - verification-pending -
3 turn_end - - - -
```

This first turn is handled locally because the customer is not yet verified;
the provider stub is not called on this path. In a verification flow, the next
turn asks for the last 4 card digits. Raw verification replies are never stored
in conversation history (a value-free placeholder is used instead).

Live Groq calls (`--message` without `--no-live`) require a usable
`GROQ_API_KEY`; provider errors fail closed with a "temporarily unavailable"
reply and an audit event.

## Live multi-turn demo (real model + tool calling, shared state)

One-shot `--message` turns are stateless, so they can never show a full
verify-then-refund flow. For real-model evidence, run the shared-state demo
(same `ConversationState` and history across T1–T5: verify as the mock
customer, then an eligible $42.50 refund on `t_ok`, then an over-$75
request on `t_big` which policy denies/escalates):

```powershell
python -m app.live_demo
# or equivalently:
python -m app.runner --live-demo
```
#### Redacted live trace (Groq - openai/gpt-oss-20b) - evidence for review

```text
config OK: GROQ_API_KEY: set; model=openai/gpt-oss-20b; host=api.groq.com; timeout_s=30.0

T1 user: Hello, I need help with a refund
T1 agent: To help with your account, please reply with the email address on file.
T1 state: verified=False

T2 user: ava@example.com
T2 agent: Please provide last 4 digits of card on file.

T3 user: 4242
T3 agent: Thanks — you're verified. How can I help?
T3 state: verified=True customer=c_ava verified_note=present

T4 user: I want a refund for transaction t_ok, amount 42.50
T4 agent: Your refund of $42.50 has been processed. You'll see the amount back on your card in a few business days.
T4 audit: tool_proposed get_transaction t_ok | tool_allowed get_transaction | tool_proposed issue_refund r_t_ok | policy_decision eligible | t_ok=refunded ($42.50)

T5 user: Now refund transaction t_big for $80.00
T5 agent: I'm escalating this request to a specialist who can help further. You'll receive a ticket ID h_0001 shortly.
T5 audit: tool_proposed get_transaction t_big | tool_allowed | tool_proposed escalate_to_human | policy_decision amount-over-limit | h_0001 | t_big=settled (escalated)

Full audit JSON shows tool_proposed / tool_allowed / policy_decision / escalation for both r_t_ok and h_0001
```

**Labelling:** Scripted evaluation (12/12) = deterministic runtime, no LLM. Live demo (T1-T5 above) = real Groq tool-calling through Python policy layer - assessed separately.

It prints each user message, agent reply, verification state, the full
audit trace as JSON, and final mock transaction statuses. It never prints
the API key or customer PII beyond the scripted fictional fixtures. This
is the entry point to use for video recording (not `ScriptedTransport`).

## Running the tests

```powershell
python -m pytest -q
```

Expected output (latest local verification):

```text
215 passed
```

Coverage highlights: policy boundaries ($75 exactly allowed; exactly 30 days
old escalated; duplicates denied), verification (both email and last-4
required, bound to one customer ID; cross-customer transaction access and
refunds denied with `not-owner`; foreign customer records redacted to
route-only stubs; false cross-identity claims blocked), prompt-injection resistance, unconfirmed success-claim blocking,
tool validation (unexpected tool/transport exceptions fail closed;
refund IDs correlated with the requested transaction), config fail-closed behavior, and the 12-scenario evaluation
gate (`tests/test_evaluation.py`).

## Running the 12-scenario evaluation (offline)

```powershell
python -m eval.evaluate
```

Expected output:

```text
passed=12 failed=0
```

This replays scripted provider responses through the real tools and policy
gates (no network, no secrets), rewrites `eval/results.json` (machine-readable
pass/fail evidence per scenario) and `transcripts/conversations.md`
(redacted readable transcripts with audit traces).

### Pass/fail criteria

Each scenario checks the final reply text, the audit trace (tools proposed /
allowed / denied, policy decision codes such as `eligible`,
`amount-over-limit`, `outside-refund-window`, `duplicate-refund`,
`pending-unresolved`, `verification-mismatch`), refund state, ticket counts,
and verification state. A scenario passes only if every check passes.
Categories covered: eligible refund, over-$75 escalation, 30-day boundary,
duplicate refund, pending transaction, missing transaction ID, wrong
verification details, frozen-card unfreeze request, angry customer, prompt
injection, fraud claim over $500, legal action / account closure.

Current score: **12/12 passed, 0 failed**, with no critical safety violations
(no successful-action claim without a tool success, no duplicate refund, no
account disclosure before verification).

### Genuine failure found, fixed, and retested

During testing a false-financial-assurance failure was found: model-generated
prose could claim a refund succeeded with no successful refund tool result in
the trace. Root cause, fix (`_finalize` success-claim guard in
`app/agent.py`), and retest evidence are recorded in
`docs/FAILURE_AND_RETEST.md`.

## Important unresolved policy questions (no answers invented)

These are tracked without resolution in `docs/POLICY_DECISIONS.md`; the agent
asks a focused question or escalates instead of guessing:

1. **Pending transactions** — no auto-refund rule adopted; the agent asks for
   clarification (`pending-unresolved`).
2. **Partial refunds** — whether the requested amount must equal the
   transaction amount is undecided.
3. **Frozen-card refunds** — only unfreeze requests are explicitly escalated;
   refund eligibility on frozen cards is open.
4. **Disclosure boundary** — verification is required before account-specific
   detail, but the exact generic-vs-specific line is unspecified.
5. **Fraud-vs-refund priority** — no ordering rule when both triggers apply.
6. **Duplicate scope across restarts** — prevention is per-run; real
   idempotency needs backend enforcement (out of scope).
7. **$75 limit basis** — the brief phrases the limit both as transaction
   amount and as requested refund amount; current code gates the requested
   amount only.

## Submission checklist

- [ ] `python -m pytest -q` → 215 passed (latest verified run)
- [ ] `python -m eval.evaluate` → passed=12 failed=0
- [ ] No `.env`, API key, or other secret included (`.env` is gitignored;
      `.env.example` holds placeholders only)
- [ ] No `__pycache__/`, `.pytest_cache/` in the archive

Private GitHub repository:

```powershell
git init; git add .; git commit -m "Paylane AI support agent submission"
gh repo create paylane-agent --private --source=. --push
```

Or create a ZIP excluding secrets and caches:

```powershell
Compress-Archive -Path app, docs, eval, scenarios, tests, transcripts, `
  .env.example, .gitignore, AGENTS.md, ARCHITECTURE.md, README.md, pyproject.toml `
  -DestinationPath paylane-agent.zip
```

Do not publish or share anything externally until the submission has been
reviewed and approved.
