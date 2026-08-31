# Step 6 — Dashboard

**Depends on:** Step 3's worker pipeline producing real decisions to display.
No need to read `DECISION_RULES.md` or `ARCHITECTURE.md` in full for this step —
just the table shapes in `SCHEMA.sql` for `transactions`, `agent_decisions`,
`decline_events`, and `audit_log`.

## Goal
A simple, fast, read-only view that proves the system works — this is what you'll
actually be looking at during the pitch.

## Implementation
Flask (Node is not installed on this machine — Python-only project). `src/dashboard.py`
= read-only data layer, `app.py` = routes + Jinja templates in `templates/`,
`python -m scripts.run_dashboard` to launch. `flask==3.1.3` added to requirements.

## Tasks
- [x] Summary view: processed / recovered / halted / escalated / pending + value
      recovered + counts by decline bucket. `get_summary()` caches the aggregates for
      `DASHBOARD_SUMMARY_TTL_SECONDS` (3s) and the page polls `/api/summary` every 3s.
- [x] Paginated transactions table (`DASHBOARD_PAGE_SIZE = 25`, `LIMIT/OFFSET`, never a
      full load) — shows id, amount, category, decline code, action, status, with
      status filters.
- [x] Click-through detail view: `humanize_decision()` turns the `audit_log` snapshot
      into an ordered plain-language story (decline → classification → AFA check →
      rule → retry sim → action → outcome → who decided), plus a timeline and the raw
      snapshot underneath.
- [x] Live batch-progress bar sourced from `worker_progress.json` (Step 3), refreshed
      by the same 3s poll.
- [x] UI kept to one stylesheet in `base.html`, no build step, no JS framework.

## Definition of done
- [x] No dashboard query loads an unpaginated full table (summary = fixed aggregates;
      list = always `LIMIT`ed; detail = single id).
- [x] Summary numbers update via `/api/summary` poll, no full reload.
- [x] A judge can open any transaction and read, in plain English, why the agent acted.

**Done 2026-08-31.** Tests: `tests/test_dashboard.py` — `humanize_decision` covered
pure; queries + all four routes covered under `RUN_DB_TESTS=1`. Full suite 155 passed
(140 pure + 15 DB). Verified live against the clean 2000-row batch
(857 recovered / 1086 halted / 57 escalated).
