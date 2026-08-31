# Step 7 — Load Test Before Demo Day

**Depends on:** Steps 1–6 all complete. This step doesn't need any of the spec docs
open — it's a verification pass over the whole system.

## Goal
Prove — not assume — that the system holds up under more data than your dev testing
used, the night before you present.

## Harness
`scripts/load_test.py` does the whole pass end to end and writes `load_test_report.txt`:

    python -m scripts.load_test --transactions 12000   # seed + reset + run + verify
    python -m scripts.load_test --no-seed              # verify existing data only

## Tasks
- [x] Seeder already takes `--transactions N`; load test seeds 12,000 (`--reset`).
- [x] Runs the full worker pipeline against the whole batch in one `run()`.
- [x] Zero duplicate `agent_decisions` — checked via `GROUP BY transaction_id
      HAVING count(*) > 1`. Also: 1 decision + 1 audit row per transaction, 0 pending left.
- [x] No page stalled — a background thread polls `worker_progress.json` and records
      when each page completed; worst inter-page gap must stay under `STALL_SECONDS`
      (30s). Observed: 17.1s worst, and that is page 1 only (the AI scenario cache is
      cold — every re-auth message + unknown-code classification for the whole run is
      drafted up front, then reused). Pages 2–60 run at ~0.5s each.
- [x] Dashboard load with 12k rows: `/` cold 1.4s (Supabase round-trips from Tokyo),
      then 2–5ms from the summary cache; `/transactions` ~0.32s per page, deep page
      (page 40) same as page 1 — offset pagination holds.
- [x] Independent spot-check: 15 random rows, re-run the pure rule engine
      (`classify` → `check_afa_threshold` → `decide_action`) on the raw inputs from
      each immutable audit snapshot and assert it still matches the recorded
      `action_taken` + `rule_fired`. 0/15 mismatched.

## Definition of done
- [x] A full 12,000-row run completes with no manual intervention (53.3s, 225 txn/s,
      0 errors, 0 AI fallbacks).
- [x] Spot-checked audit trails all correct.
- [x] `load_test_report.txt` committed as pitch-day backup (5,284 recovered /
      ₹11.56 cr, 6,360 halted, 356 escalated on the 12k run).

**Done 2026-08-31.** 12/12 checks green. `tests/test_load_test.py` covers the gap
math + check accumulator. DB restored to the 2,000-row demo batch afterwards; re-run
the load test any time with the command above.
