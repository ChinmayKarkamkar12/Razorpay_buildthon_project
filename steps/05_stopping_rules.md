# Step 5 — Stopping Rules as Hard Guardrails

**Depends on:** Steps 2 and 3. This step is mostly verification + one addition
(circuit breaker), not new core logic — most of the stopping logic already exists
inside Step 2's `decide_action`.

## Goal
Make sure the retry/stop limits can't be silently bypassed, and add a safety valve
for when something is going wrong at the batch level.

## Tasks
- [x] Confirm `MAX_RETRY_ATTEMPTS = 3` is enforced inside `decide_action` (from Step
      2) and cannot be overridden by anything in the AI layer (Step 4).
      → `tests/test_stopping_rules.py`: `test_retryable_buckets_never_retry_at_or_past_the_cap`,
      `test_ai_layer_leaves_a_capped_soft_decline_terminal`,
      `test_ai_calling_an_unknown_code_soft_does_not_produce_a_retry`.
- [x] Confirm hard and compliance bucket declines never produce `retry_scheduled`.
      → `test_hard_and_compliance_never_schedule_a_retry` (full parametrised sweep over
      afa state / attempt_count / mandate_issue).
- [x] Confirm every `stopped_permanent` decision has a non-null `rule_fired` value
      and a corresponding `audit_log` row.
      → `tests/test_worker_db.py::test_every_stopped_permanent_decision_has_a_rule_and_an_audit_row`.
- [x] Add a **page-level circuit breaker** to the worker (Step 3): if more than 20%
      of a page throws an unexpected error during processing, stop the worker and
      surface a clear alert/log message instead of continuing to the next page.
      → already built in `src/worker.py`; extracted to the pure `_circuit_tripped()`
      predicate (threshold `CIRCUIT_BREAKER_THRESHOLD = 0.20` + `CIRCUIT_BREAKER_MIN_ERRORS`
      floor so a tiny page can't trip on one unlucky row).

## Testing sequence
- [x] Test transaction already at `attempt_count = 3` with a soft decline — confirms
      it stops instead of retrying a 4th time
      (`test_soft_decline_already_at_cap_stops_instead_of_a_fourth_retry`).
- [x] Test transaction with a hard decline and `attempt_count = 0` — confirms it
      never gets `retry_scheduled`
      (`test_hard_decline_from_attempt_zero_never_gets_a_retry`).
- [x] Simulate a bad page — `test_circuit_breaker_stops_the_worker_on_a_bad_page`
      forces ~25% of every page to error and asserts `CircuitBreakerTripped` is raised
      with nothing committed; `test_circuit_breaker_can_be_switched_off` asserts the
      `circuit_breaker=False` escape hatch still completes. Predicate itself covered by
      `test_circuit_breaker_needs_both_the_ratio_and_the_floor`.

## Definition of done
- [x] Retry caps and hard/compliance no-retry rules verified by test, not just
      assumed from Step 2's code.
- [x] Circuit breaker exists and is tested against a forced bad-page scenario.
- [x] Every stop reason is traceable in `audit_log`.

**Done 2026-08-31.** Full suite: 146 passed (137 pure + 9 DB integration). Clean
2000-row batch reprocessed afterwards: 866 recovered / 1077 halted / 57 escalated,
0 errors, 0 AI fallbacks.
