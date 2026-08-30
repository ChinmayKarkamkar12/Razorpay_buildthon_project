# RBI E-Mandate & UPI Recovery Coordinator — Build Spec

Spec pack for building this with Claude Code. Point Claude Code at this folder and it
will read `CLAUDE.md` automatically for project rules.

## Files

| File | Purpose |
|---|---|
| `CLAUDE.md` | Rules Claude Code follows on every task — read this first |
| `ARCHITECTURE.md` | Full system design: requirements, diagram, data flow, trade-offs |
| `SCHEMA.sql` | Postgres DDL — run this first against Supabase/Neon |
| `DECISION_RULES.md` | Exact policy logic (thresholds, buckets, retry caps) — the source of truth for the rule engine |
| `steps/` | The build plan, split into one file per step — see `steps/00_INDEX.md` |

## Quick start

1. Read `CLAUDE.md` and `ARCHITECTURE.md` for context.
2. Stand up Postgres (Supabase or Neon free tier), run `SCHEMA.sql`.
3. Open `steps/00_INDEX.md`, then work through `steps/01_...` to `steps/08_...` in
   order — **point Claude Code at one step file per session**, not the whole plan, to
   keep context (and token usage) small. Each step file lists only the other files it
   actually needs.
4. Reference `DECISION_RULES.md` any time you're implementing or modifying the
   decision logic — it's the single source of truth, don't improvise thresholds.

## One-line pitch

Razorpay retries a failed payment for 3 days, then goes silent. Our agent picks up
exactly there — it reads *why* a payment failed, tells the difference between "just
retry it" and "RBI law says the customer must re-approve this," and takes the correct
bounded action, with a full audit trail proving every decision.
