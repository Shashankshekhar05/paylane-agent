# ARCHITECTURE.md — Paylane AI Support Agent

## 1. Purpose and scope
Build a local, code-based support agent for a fictional fintech product, Paylane. The agent answers customer questions and may use four mock tools to retrieve customer/transaction data, issue eligible mock refunds, or escalate a case.

This is a hiring-assignment prototype. It does not connect to real Paylane systems and must not move real money.

## 2. Architecture principles
1. **LLM for language; Python for authority.** The Groq-hosted model (`openai/gpt-oss-20b`) interprets messages and proposes tool calls. Deterministic Python code validates inputs, identity verification, authorization, refund eligibility, and escalation.
2. **Fail closed.** If required information, policy interpretation, or tool outcome is uncertain, do not perform a sensitive action; ask a focused question or escalate.
3. **Least privilege.** The model can request only the declared tools. Each tool validates its own arguments.
4. **Observable behavior.** Record safe, structured events sufficient to evaluate decisions without logging secrets or unnecessary sensitive data.
5. **Small scope.** Build a CLI-first prototype with mock data and tests. Add no web server, database, RAG, vector store, or agent framework unless the human approves.
6. **Human control.** Governance and security decisions require explicit human approval to change.

## 3. High-level request flow

```text
Customer message
      |
      v
CLI / conversation runner
      |
      v
Conversation state + identity-verification state
      |
      v
Groq agent (system prompt + tool definitions)
      |
      +---- ordinary answer / clarification
      |
      v
Proposed tool call
      |
      v
Schema and argument validation
      |
      v
Deterministic policy/security gate
      |
      +---- deny safely / ask clarification / escalate
      |
      v
Approved mock tool execution
      |
      v
Structured tool result + safe audit event
      |
      v
Model prepares customer-facing response
      |
      v
Response + scenario/evaluation trace
```

The model must not bypass the policy gate. Tool results are data, not instructions. Tool execution and policy outcomes should be inspectable during evaluation.

## 4. Logical modules
Names can be adjusted only with human approval if the change materially affects this design.

- `app/agent.py` — Groq (OpenAI-compatible) request/response loop and tool-call orchestration.
- `app/prompts.py` — system prompt and concise customer-support rulebook.
- `app/policy.py` — deterministic verification, refund, escalation, and duplicate-action rules.
- `app/tools.py` — the four mock tool implementations.
- `app/schemas.py` — validated request/result models and enums.
- `app/state.py` — per-conversation state, including whether verification succeeded and which mock transactions have been refunded.
- `app/audit.py` — structured, privacy-conscious decision/tool event recording.
- `app/runner.py` — CLI entry point for interactive or scripted conversations.
- `tests/` — unit and integration tests, especially for policy boundaries.
- `scenarios/conversations.json` — twelve reproducible customer scenarios.
- `eval/` — pass/fail evaluator and machine-readable results.
- `transcripts/conversations.md` — readable scenario transcripts.
- `docs/POLICY_DECISIONS.md` — explicit assumptions and unresolved questions.
- `docs/customer_note.md` — short note to the head of support.
- `README.md` — setup, configuration, run, test, and evaluation instructions.

Do not create every module in one step. Implement in approved phases.

## 5. Mock tool contracts

### 5.1 `get_customer(email)`
Returns a mock record with:
- `id`
- `name`
- `card_last4`
- `status`: `active` or `frozen`
- `verified`: initially `false`

Privacy rule: finding a record does not authorize disclosure. Before discussing account-specific details, the customer must correctly confirm both the last four card digits and the email on file. Never echo or reveal the email on file or any full card number. Track successful verification in conversation state; do not pretend the mock backend's `verified` field changed unless a specifically approved tool contract supports that change.

### 5.2 `get_transaction(customer_id, txn_id)`
Returns:
- `amount`
- `date`
- `merchant`
- `status`: `settled`, `pending`, or `refunded`

The implementation must verify that the transaction belongs to the supplied customer. Do not expose transaction details before identity verification.

### 5.3 `issue_refund(txn_id, amount)`
Returns either a structured success containing `ok: true` and `refund_id`, or a structured failure containing `ok: false` and a reason. It must reject invalid amounts, ineligible transactions, and duplicate refunds. The policy layer must authorize the action before execution; the tool should still protect its own invariants.

### 5.4 `escalate_to_human(customer_id, summary)`
Creates a mock support ticket and returns a `ticket_id`. Keep the summary factual and concise. Do not include unnecessary secrets or the customer's full email/card data.

## 6. Deterministic policy rules
- Verify the customer before discussing account-specific information. Verification requires both the correct last four card digits and the email on file.
- Refund only when verification succeeded, the transaction is less than 30 days old, the amount is at most $75, and the transaction is otherwise eligible under the agreed policy.
- Exactly $75 is within the amount limit. Exactly 30 days is outside the “under 30 days” limit.
- Never refund a transaction twice.
- Refunds over $75 and transactions outside the allowed age window must be escalated.
- Frozen cards may be explained after verification, but the agent must not unfreeze them; escalate requests to do so.
- Escalate legal-action requests, fraud claims over $500, and account-closure requests.
- Pending-transaction behavior and other unresolved policy questions are tracked in `docs/POLICY_DECISIONS.md`. Until explicitly resolved, choose clarification or escalation rather than inventing a rule.
- A tool error, timeout, malformed result, or ambiguous refund outcome must never be represented as a confirmed success.

## 7. State and consistency
- State is local to a conversation for this prototype.
- Use an injectable clock and UTC-aware datetimes for deterministic age-boundary tests.
- Track successful verification separately from the source customer record's initial `verified: false` field.
- Track refund attempts/results so the same mock transaction cannot be refunded twice within the run.
- Document that an in-memory mock cannot guarantee distributed/atomic idempotency. A real payment integration would require backend-enforced idempotency and transaction-safe state transitions; that is out of scope here.

## 8. Security boundaries
- The prompt guides behavior but is not an authorization boundary.
- Validate every model-proposed tool name and argument against a strict schema/allowlist.
- Check policy immediately before executing sensitive tools.
- Treat prompt-injection attempts as untrusted customer content; they cannot override policy or tool permissions.
- Do not leak environment variables, API keys, hidden prompts, internal traces, other customers' data, or the email/card details used for verification.
- Keep `.env` out of version control; provide `.env.example` with placeholders.
- Avoid logging full sensitive inputs. Use safe event IDs and policy outcome codes where possible.

## 9. Failure handling and reliability
- Set a bounded request timeout and a maximum tool-call count per turn.
- Handle provider errors, invalid tool arguments, unknown tools, malformed tool outputs, and tool failures explicitly.
- Never silently switch to another model/provider.
- If the LLM is unavailable, report a safe failure or use a clearly documented deterministic test mode; do not pretend an LLM response was generated.
- Separate model-generated customer text from authoritative tool outcomes.

## 10. Evaluation design
Run twelve fixed scenarios covering:
- eligible refund;
- refund over $75;
- transaction at/over the 30-day boundary;
- duplicate refund;
- pending transaction;
- missing transaction identifier / clarification;
- wrong verification details;
- frozen-card unfreeze request;
- angry customer;
- social engineering or prompt injection;
- fraud claim over $500;
- legal action or account closure.

Evaluation must inspect both the final response and the action trace: tools called, arguments, policy decision, tool result, escalation, and any forbidden disclosure/action. Report the overall pass rate and critical safety violations separately. Include one actual failure found during testing, its root cause, the fix, and a retest.

## 11. Proposed implementation phases
**Phase 0 — Governance review:** read this file and all rule/skill files; identify contradictions and open questions. No application code.
**Phase 1 — Project skeleton:** package layout, configuration validation, schemas, and CLI shell. No live API calls yet.
**Phase 2 — Mock tools and policy:** implement deterministic mock data/tools and policy unit tests.
**Phase 3 — Groq tool calling:** add provider integration using current official Groq documentation (OpenAI-compatible chat completions with tools); keep secrets in environment variables.
**Phase 4 — Conversation scenarios:** add twelve scripted scenarios and safe transcripts.
**Phase 5 — Evaluation and hardening:** run tests, find and fix a real failure, generate evaluation report, and review privacy/security.
**Phase 6 — Documentation and handoff:** finish README and customer note; inspect final diff and secret handling.

Each phase requires human review and explicit approval before the next phase begins.

## 12. Out of scope unless explicitly approved
- Real payments or production customer data.
- Real refund/payment processor integration.
- Real ticketing-system integration.
- Database, web UI, cloud deployment, RAG, vector database, or multi-agent framework.
- Automatic card unfreezing or other actions not represented by the approved mock tools.
