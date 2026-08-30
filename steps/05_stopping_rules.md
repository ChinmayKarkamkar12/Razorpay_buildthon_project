# Step 5 — Stopping Rules as Hard Guardrails

**Depends on:** Steps 2 and 3. This step is mostly verification + one addition
(circuit breaker), not new core logic — most of the stopping logic already exists
inside Step 2's `decide_action`.

## Goal
Make sure the retry/stop limits can't be silently bypassed, and add a safety valve
for when something is going wrong at the batch level.

## Tasks
- [ ] Confirm `MAX_RETRY_ATTEMPTS = 3` is enforced inside `decide_action` (from Step
      2) and cannot be overridden by anything in the AI layer (Step 4).
- [ ] Confirm hard and compliance bucket declines never produce `retry_scheduled` —
      write a quick check/assertion for this if you don't already have the unit test
      from Step 2 covering it.
- [ ] Confirm every `stopped_permanent` decision has a non-null `rule_fired` value
      and a corresponding `audit_log` row — spot check a handful after a test run.
- [ ] Add a **page-level circuit breaker** to the worker (Step 3): if more than 20%
      of a page (i.e. more than 40 out of 200 rows) throws an unexpected error during
      processing, stop the worker and surface a clear alert/log message instead of
      continuing to the next page. This protects against a bad batch silently
      corrupting a lot of data.

## Testing sequence
- [ ] Manually create a test transaction already at `attempt_count = 3` with a soft
      decline — confirm it stops instead of retrying a 4th time.
- [ ] Manually create a test transaction with a hard decline and `attempt_count = 0`
      — confirm it never gets `retry_scheduled`.
- [ ] Simulate a bad page (e.g. seed a few rows that will throw during processing) —
      confirm the circuit breaker triggers once the 20% threshold is crossed.

## Definition of done
- [ ] Retry caps and hard/compliance no-retry rules verified by test, not just
      assumed from Step 2's code.
- [ ] Circuit breaker exists and is tested against a forced bad-page scenario.
- [ ] Every stop reason is traceable in `audit_log`.
