# Steps Index

Each file below is self-contained — it has everything Claude Code needs for that one
step, without pulling in the whole plan. Work through them in order.

**How to use with Claude Code:** open a session and point it at ONE step file at a
time (e.g. "read steps/01_schema_and_seed.md and do this"), instead of pasting the
whole plan. Each file tells Claude Code which other files (if any) it actually needs
to read for that step. This keeps context small and cheap per session.

| # | File | Depends on |
|---|---|---|
| 1 | `01_schema_and_seed.md` | `SCHEMA.sql` |
| 2 | `02_rule_engine.md` | `DECISION_RULES.md` |
| 3 | `03_worker_loop.md` | output of step 2 |
| 4 | `04_ai_layer.md` | output of step 2 + 3 |
| 5 | `05_stopping_rules.md` | output of step 2 + 3 |
| 6 | `06_dashboard.md` | output of step 3 |
| 7 | `07_load_test.md` | everything built so far |
| 8 | `08_pitch_prep.md` | none — just prep, no coding |

`CLAUDE.md` (in the project root) applies to every step automatically — don't repeat
those rules per file, Claude Code reads it once per session.
