# Step 3 — Worker Loop (Pagination + Idempotency)

**Depends on:** Step 2's rule engine functions must exist and pass their tests first.
**Read also:** the `transactions`, `agent_decisions`, and `audit_log` table
definitions in `SCHEMA.sql` — you don't need the rest of the schema file for this step.

## Goal
Wire the rule engine up to the database in a way that processes large batches without
freezing, and can never double-process the same transaction even if it's re-run.

## Tasks
- [ ] Worker pulls one page at a time — `LIMIT 200` — of transactions where
      `status = 'pending'`, ordered by `scheduled_at`, using the existing
      `idx_transactions_status_scheduled` index. Never query the whole table at once.
- [ ] For each row in the page: check whether an `agent_decisions` row already exists
      for that `transaction_id` (or track processed `idempotency_key`s) — if so, skip
      it. This is the idempotency guard.
- [ ] For each unprocessed row: call Step 2's `classify`, `check_afa_threshold`, and
      `decide_action` functions.
- [ ] In a **single database transaction**, do all three of: insert the
      `agent_decisions` row, insert the `audit_log` row (with a JSON snapshot of the
      decision), and update the `transactions` row's `status` and `attempt_count`.
      If any part fails, the whole set should roll back — never leave a decision
      half-written.
- [ ] After finishing a page, log progress (e.g. "page 3/25 processed, 200 rows") so
      later the dashboard can show live progress.
- [ ] Move to the next page until no `pending` rows remain.

## Testing sequence (do this before scaling up)
- [ ] Seed or use 500 rows only.
- [ ] Run the worker once — confirm all 500 get a decision.
- [ ] Run the worker again immediately on the same data — confirm ZERO new decisions
      are created (idempotency working).
- [ ] Kill the worker mid-batch (simulate a crash) and restart it — confirm it picks
      up only the untouched rows, doesn't duplicate anything.
- [ ] Only after all three checks pass, try a larger batch (a few thousand rows).

## Definition of done
- [ ] Re-running the worker on already-processed data never creates duplicate
      `agent_decisions` or double-updates `attempt_count`.
- [ ] A forced crash mid-batch and restart doesn't corrupt or duplicate data.
- [ ] Worker never issues an unbounded/unindexed query.
