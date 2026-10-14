# POLICY_DECISIONS.md — Explicit assumptions and unresolved questions

Source of truth for business policy is the original assignment brief
(`AGENTS.md`, `ARCHITECTURE.md`). This file records open questions **without
resolving them**. Nothing here invents a new business rule.

Mandatory escalation follows only the assignment's explicit triggers:
legal-action requests, fraud claims over $500, account-closure requests,
refunds over $75, transactions outside the under-30-day window, and card
unfreeze requests. Unresolved items below must use a safe clarification
(`needs_clarification`: ask a focused question) where appropriate and must
not be silently converted into new mandatory escalation rules.

In-memory duplicate prevention (`ConversationState.refunded_txn_ids` plus the
mock store's `refunded` status) is per conversation/run only. It is lost on
application restart and is not distributed or atomic. No persistence across
restarts is claimed.

## UNRESOLVED-1: Pending-transaction handling

- Status: UNRESOLVED.
- The brief defers this to this file and forbids inventing a policy.
- Phase 2 handling: `refund_decision` returns `needs_clarification` with
  reason `pending-unresolved` (ask a focused question or route to a human
  through normal conversation handling in a later phase). This is not a new
  mandatory escalation rule.

## UNRESOLVED-2: Partial-refund semantics

- Status: UNRESOLVED.
- `issue_refund(txn_id, amount)` accepts an amount, but the brief never
  states whether partial refunds are allowed, forbidden, or must equal the
  transaction amount.
- Current behavior (accurately stated, not approved as policy): the tool
  technically requires only a finite positive amount, and the policy gate
  applies the explicit over-$75 trigger to the requested amount only, with
  no requested-vs-transaction comparison. As a result, the current code
  can permit both under-refunds (requested < transaction amount) and
  over-refunds (requested > transaction amount) whenever the requested
  amount itself passes the explicit gates. This is a known gap, not a
  decision: no prohibition and no approval of partials has been adopted,
  and no test endorses partial-refund behavior as policy.
- Awaiting human ruling before any allow/deny rule for partials is added.

## UNRESOLVED-3: Frozen-card / refund interaction

- Status: UNRESOLVED.
- The brief says frozen cards may be explained after verification and must
  not be unfrozen (escalate such requests), but does not state whether an
  otherwise-eligible transaction on a frozen card may still be refunded.
- Phase 2 handling: no frozen-status rule exists in `refund_decision`.
  Unfreeze requests map to the explicit escalate path; anything beyond that
  is left open.

## UNRESOLVED-4: Disclosure scope and echo rules

- Status: UNRESOLVED.
- Verification (both last-4 and email) is required before account-specific
  disclosure, and full card numbers plus the email itself must never be
  revealed. The exact boundary between generic help text and
  account-specific detail is not fully specified.
- Phase 2 handling: `can_disclose` gates on `state.verified`; no
  disclosure-text rules are invented.

## UNRESOLVED-5: Fraud-vs-refund overlap priority

- Status: UNRESOLVED.
- A case can trigger both the fraud-over-$500 and refund-over-$75 paths.
  The brief sets no priority order.
- Phase 2 handling: both predicates exist independently; no priority rule
  is implemented.

## UNRESOLVED-6: Duplicate scope across restarts

- Status: UNRESOLVED (by design of the prototype architecture).
- Prevention is enforced within the run only. Cross-restart persistence
  would require backend-enforced idempotency, which is out of scope.

## UNRESOLVED-7: $75 rule — transaction amount vs. requested refund amount

- Status: UNRESOLVED — human ruling requested; no code change made.
- Exact original wording uses both phrasings: `AGENTS.md` section 5
  requires "a transaction amount of $75 or less" ("Exactly $75 satisfies
  the amount threshold"), while `AGENTS.md` section 4 and
  `ARCHITECTURE.md` section 6 require escalating "refunds over $75" /
  "when ... the amount is at most $75".
- Current behavior: `refund_decision` gates the requested refund amount
  only (`needs_escalation_for_amount(requested_amount)`), never
  `txn.amount`. A requested refund of $70 on an $80 transaction therefore
  returns `ALLOW` when all other explicit gates pass. This combination is
  deliberately left untested until ruled on.
- Choosing transaction-amount gating, requested-amount gating, or
  gate-on-both would each decide the unresolved partial-refund question
  by the back door, so no option was adopted unilaterally. Question for
  the human: which amount (or both) must satisfy the $75 limit when the
  requested refund differs from the transaction amount?

## Note: verification input normalization

- Comparing emails after stripping whitespace and casefolding, and last-4
  after stripping whitespace, is technical input normalization in
  `verify_identity`, not a business-policy decision. The business rule
  (both values must match) is unchanged.

## Phase 3 carry-over obligations (orchestrator requirements)

The Phase 2 functions are unenforced contracts until the Grok
orchestrator (`app/agent.py`, Phase 3) wires them in. That orchestrator
must:

- enforce `can_disclose()` before rendering any account-specific data to
  the customer (tool outputs are data, never authorization);
- call `ConversationState.mark_refunded()` after a confirmed successful
  refund so the state layer and the tool store stay consistent, and
  re-check `is_duplicate()` immediately before executing `issue_refund`;
- never claim success without a successful tool result: only
  `is_refund_success(result) is True` (a well-formed `ok: true` result
  with a non-empty `refund_id`) may be described as a successful refund;
  failed, malformed, or ambiguous outcomes must fail closed.
