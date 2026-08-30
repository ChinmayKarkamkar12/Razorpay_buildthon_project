# Step 7 — Load Test Before Demo Day

**Depends on:** Steps 1–6 all complete. This step doesn't need any of the spec docs
open — it's a verification pass over the whole system.

## Goal
Prove — not assume — that the system holds up under more data than your dev testing
used, the night before you present.

## Tasks
- [ ] Seed (or extend the Step 1 seeder to generate) 10,000+ synthetic transactions.
- [ ] Run the full worker pipeline against the whole batch.
- [ ] Confirm: zero duplicate `agent_decisions` rows (same idempotency guard from
      Step 3 holding at scale).
- [ ] Confirm: no page gets stuck — check the progress logs for gaps or long pauses.
- [ ] Confirm: the dashboard (Step 6) still loads quickly with this much data in the
      tables.
- [ ] Spot-check a random sample (10–15 transactions) across different buckets and
      manually verify their audit trail matches what `DECISION_RULES.md` says should
      have happened.
- [ ] If anything breaks here, fix it and re-run the full load test — don't patch
      around it under time pressure right before presenting.

## Definition of done
- [ ] A full 10,000+ row run completes without manual intervention.
- [ ] Spot-checked audit trails are all correct.
- [ ] You have a screenshot or note of the final numbers (total recovered, etc.) to
      use as backup in case you need to re-run live during the pitch and something
      goes wrong.
