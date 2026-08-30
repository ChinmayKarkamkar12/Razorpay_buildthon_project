# ADR: RBI E-Mandate & UPI Recovery Coordinator — System Architecture

**Status:** Accepted
**Track:** Razorpay AI Buildathon — AI Revenue Recovery
**Deciders:** Solo/small-team build

## Context

Failed recurring payments in India are not one problem — they're several, and treating
them identically is the core failure mode of naive dunning systems. A payment can fail
because of a temporary technical issue (retryable), a genuine business decline like
insufficient funds (retryable on a schedule), or because RBI's e-mandate regulation
requires the customer to actively re-authenticate (retrying does nothing — it's not a
technical problem, it's a legal one). Razorpay's own subscription engine retries for
3 days (T+1, T+2, T+3) and then goes silent (`halted` state). This agent sits in that
gap: it classifies *why* a payment failed and takes the *correct* bounded action,
logging every decision for audit.

## Requirements

**Functional**
- Ingest a batch of failed/at-risk recurring payments (synthetic).
- Classify each failure by decline code into soft / hard / technical / compliance.
- Enforce the ₹15,000 AFA-free threshold (₹1,00,000 for SIP/insurance/credit-card-bill
  categories) as a hard rule, never an AI judgment call.
- Decide and execute a bounded action: retry (with schedule), send re-auth link,
  escalate, or stop permanently.
- Log every decision with full audit trail.
- Show a live dashboard: batch in → decisions made → money recovered.

**Non-functional**
- Process 5,000–50,000 synthetic transactions without freezing or timing out.
- Idempotent — re-running a batch or receiving a duplicate event never double-processes.
- Degrades gracefully — AI failure never halts the deterministic core.
- Fully traceable — nothing fails silently; unresolved cases become visible exceptions.
- Runs entirely on free tiers.

**Constraints**
- Hackathon timeline (days, not weeks).
- Synthetic data only — no real PII or payment data.

## High-Level Design

```
                         ┌─────────────────────────┐
                         │   Synthetic Data Seeder   │  (repeatable script)
                         └────────────┬─────────────┘
                                      │ inserts in chunks of 500
                                      ▼
                         ┌─────────────────────────┐
                         │      Postgres (Supabase/  │
                         │      Neon free tier)       │
                         │  transactions               │
                         │  decline_events              │
                         │  agent_decisions               │
                         │  mandates                        │
                         │  audit_log                         │
                         └────────────┬─────────────┘
                                      │ polls in pages of 200 (never all at once)
                                      ▼
                         ┌─────────────────────────┐
                         │   Worker / Processing Loop  │  (Render/Railway free tier)
                         │  1. Rule Engine (deterministic)│
                         │  2. AI Classifier (bounded,     │
                         │     fallback-safe)                │
                         │  3. Action Executor (mock)          │
                         │  4. Audit Writer                      │
                         └────────────┬─────────────┘
                                      │ writes results back, paginated
                                      ▼
                         ┌─────────────────────────┐
                         │   Dashboard (Vercel free tier)│
                         │   live batch progress            │
                         │   decisions + audit trail          │
                         │   money-recovered metric             │
                         └─────────────────────────┘
```

**Key decision: paginated batch worker, not one call per transaction.** A hackathon
build calling an endpoint per-transaction across thousands of rows will hit free-tier
rate limits and timeouts. The worker pulls fixed-size pages, fully processes each page
(rules → AI where needed → write), then moves on. This is what prevents lag/glitches
as data volume grows.

## Data Flow (per transaction)

1. Worker reads a page of `status = pending` transactions, ordered by `scheduled_at`.
2. Skip any row whose `idempotency_key` already has a decision (idempotency guard).
3. Run deterministic rule engine (see `DECISION_RULES.md`) → bucket + AFA check.
4. If code is unmapped or message drafting is needed → bounded AI call with timeout
   and safe fallback.
5. Determine action (retry/reauth/escalate/stop) per `DECISION_RULES.md`.
6. In a single DB transaction: write `agent_decisions` row, write `audit_log` row,
   update `transactions.status` and `attempt_count`.
7. Move to next row; log page progress for live dashboard visibility.

## Security & Compliance Posture

- All data synthetic — never real card numbers, PII, or live payment rails.
- `audit_log` is append-only (no UPDATE/DELETE grants on that table).
- Money stored as integer paise, never float.
- Secrets via environment variables only.
- Note for pitch: a production version would need India data-localization (DB hosted
  in-region) — named as a known real constraint, not built for the hackathon.

## Trade-off Analysis

| Decision | Why | Cost |
|---|---|---|
| Deterministic rules before AI | Compliance logic must be auditable, not hallucinated | Less "AI-native" looking, more defensible |
| Paginated worker vs. per-event serverless | Predictable, avoids free-tier timeout cliffs | Not true real-time (acceptable for batch recovery) |
| Postgres over NoSQL | Strong relational integrity for audit joins; generous free tiers | More upfront schema design |
| Fail-toward-caution on AI uncertainty | Prevents wrong financial action | Occasionally over-conservative — acceptable trade-off |

## What to Revisit If This Became Real

- Replace polling worker with a real event queue (SQS/Kafka).
- Append-only, cryptographically chained audit log.
- Real bank/PSP webhook signature verification.
- India data localization for the actual database.
