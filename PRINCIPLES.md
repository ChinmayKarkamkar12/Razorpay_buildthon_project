# Engineering Principles — RBI E-Mandate & UPI Recovery Coordinator

This is a finance-adjacent project; correctness and auditability matter more than
coding speed. These rules are referenced throughout the source (`PRINCIPLES.md Rule N`)
and in [`DECISION_RULES.md`](DECISION_RULES.md).

## What this project is

An AI agent that detects failed/at-risk recurring payments (synthetic Indian
subscription data), classifies why each one failed, and executes a bounded recovery
action — retry, re-authentication request, or escalation — while respecting RBI's
₹15,000 / ₹1,00,000 AFA-free thresholds. Every decision is logged to an immutable
audit trail. See [`ARCHITECTURE.md`](ARCHITECTURE.md) for the full system design and
[`DECISION_RULES.md`](DECISION_RULES.md) for the exact policy logic.

## Non-negotiable rules

1. **Deterministic rules before AI, always.** The AFA threshold check, decline-code
   bucketing, retry caps, and stop conditions are plain code (see `DECISION_RULES.md`),
   never an LLM call. The LLM is only used for: (a) drafting customer-facing messages,
   (b) classifying decline codes not present in the hardcoded taxonomy. Never let an AI
   call decide whether to retry a payment or bypass the AFA threshold.

2. **Fail toward caution, never toward action.** If an AI call times out, errors, or
   returns something unparseable, the fallback is always "flag for human review" —
   never "auto-retry" and never "treat as safe." Same for any decline code not in the
   taxonomy: default to the most conservative bucket (`compliance`), not `soft`.

3. **Money is always integer paise, never float.** No exceptions. All amount fields
   are `integer` (paise), converted to ₹ only at display time.

4. **Idempotency is mandatory.** Every transaction has a unique `idempotency_key`.
   Before processing, check whether it's already been decided. Re-running the same
   batch, or the worker restarting mid-batch, must never double-process or
   double-count "money recovered."

5. **Audit log is append-only.** Never UPDATE or DELETE rows in `audit_log`. If the
   DB role permissions can be set to enforce this (no UPDATE/DELETE grants on that
   table), do so.

6. **Process in pages, never in one giant query.** Any operation over the
   `transactions` table (worker processing, dashboard queries) must be paginated
   using the `PAGE_SIZE` constant defined in `DECISION_RULES.md` (200 rows per page
   by default) and must use the indexes defined in `SCHEMA.sql`. Never `SELECT *`
   without a `LIMIT`.

7. **Every write that changes transaction state must also write an audit_log row**,
   in the same DB transaction. A decision that isn't logged didn't happen.

8. **No real financial data, ever.** All data is synthetic. Never wire this to a real
   payment gateway, real card data, or real customer PII, even for testing.

9. **Explicit over implicit.** Hardcoded limits (retry caps, AFA thresholds, page
   size) live in one config module (`src/config.py`), not scattered as magic numbers.
   See `DECISION_RULES.md` for the canonical values.

10. **When something can't be classified or resolved, it becomes a visible
    "exception" row** — never silently dropped, never silently retried forever.

## Definition of done for any change

- [ ] Rule/logic has a unit test with at least one edge case (e.g. amount exactly at
      threshold, attempt_count exactly at cap)
- [ ] No unindexed full-table scans introduced
- [ ] Every state-changing write has a matching audit_log write in the same
      transaction
- [ ] Idempotency preserved (re-running the same input doesn't change the outcome)
- [ ] Ambiguous/unmapped inputs degrade to the conservative path, not silently ignored
