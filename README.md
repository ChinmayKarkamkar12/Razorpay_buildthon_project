# RBI E-Mandate & UPI Recovery Coordinator

**Razorpay AI Buildathon — Track: AI Revenue Recovery**

An AI agent that picks up failed recurring payments *after* the standard retry window
runs out, works out **why** each one failed, and takes the single correct bounded
action — retry, re-authentication request, or human escalation — while respecting
RBI's e-mandate rules. Every decision is written to an immutable audit trail that can
be read back in plain language.

---

## 1. The problem (say this in 15 seconds)

> Razorpay retries a failed recurring payment for 3 days (T+1, T+2, T+3), then goes
> silent (`halted`). Our agent picks up exactly there. It reads *why* a payment
> failed, tells the difference between "just retry it" and "RBI law says the customer
> must re-approve this," and takes the correct bounded action — with a full audit
> trail proving every decision.

Failed recurring payments in India are not one problem. A debit can fail because of:

| Cause | Right response | Wrong response |
|---|---|---|
| Temporary technical issue (bank timeout) | quick retry | give up |
| Genuine business decline (insufficient funds) | retry on a schedule, then stop | retry forever |
| RBI e-mandate needs re-authentication (amount over threshold, mandate expired) | send re-auth link | retry — legally pointless, just burns attempts |

Treating these identically is the core failure mode of naive dunning systems. This
agent classifies the cause first, then acts.

---

## 2. What it does (end to end)

```
 Synthetic seeder                Postgres (Supabase)              Paginated worker                    Read-only dashboard
 ────────────────                ───────────────────              ───────────────                    ───────────────────
 repeatable script   ──inserts──▶  transactions                    pulls pages of 200 pending rows     live batch progress
 chunks of 500                     decline_events        ──poll──▶  1. deterministic rule engine  ──▶  decisions + audit trail
 all synthetic                     agent_decisions                  2. bounded AI (message / unknown   money-recovered metric
 integer paise                     mandates                            decline code only)              per-transaction "prove it"
                                   audit_log (append-only)          3. mock action executor            view in plain language
                                                                    4. atomic decision + audit write
```

**Per-transaction flow:**

1. Worker reads a page of `pending` transactions (keyset pagination on an index —
   never a full-table scan, never all rows at once).
2. Skip any row that already has a decision — **idempotency guard**, DB-enforced by a
   `UNIQUE` constraint on `agent_decisions.transaction_id`.
3. **Deterministic rule engine** (`src/rules.py`) — pure functions, no DB / network /
   AI / clock:
   - map the decline code to a bucket (`soft` / `hard` / `technical` / `compliance`)
     via a hardcoded taxonomy; any unknown code → `compliance` (most conservative).
   - run the **AFA threshold check** independently: amount strictly above ₹15,000
     (₹1,00,000 for SIP / insurance / credit-card-bill) → re-auth required. This
     check **outranks** the bucket.
   - decide the action, enforcing hard guardrails: max 3 retries for soft/technical,
     zero retries for hard/compliance.
4. **Bounded AI enrichment** (`src/ai.py`, Google Gemini free tier) — only where
   allowed: draft the customer-facing re-auth message text, or *suggest* a bucket for
   an unmapped decline code. The AI never changes the routing decision. Timeout /
   error / no API key → deterministic fallback, logged as such. The pipeline never
   hangs on it.
5. **Mock executor** (`src/executor.py`) — simulates the remaining T+1/T+2/T+3 retry
   attempts a PSP would make (per-bucket success odds, seeded from the transaction id
   so a batch is reproducible). Nothing touches a real payment rail.
6. **One atomic write per row** — a single CTE statement inserts the `agent_decisions`
   row, inserts the matching `audit_log` row, and updates `transactions.status`.
   A decision that isn't logged didn't happen.
7. **Circuit breaker** — if >20% of a page errors, the worker stops and raises an
   alert instead of chewing through a bad batch.

---

## 3. Decision logic (the compliance core)

The full spec is in [`DECISION_RULES.md`](DECISION_RULES.md); constants live in one
place, [`src/config.py`](src/config.py). Summary:

**Decline-code buckets** (hardcoded lookup, never an AI call for known codes):

| Bucket | Meaning | Action |
|---|---|---|
| `soft` | insufficient funds, limit exceeded | retry on T+1/T+2/T+3, then stop |
| `technical` | bank downtime, timeout | quick retry (within 60 min), then escalate |
| `hard` | wrong PIN, invalid VPA, customer decline | stop permanently, no auto-retry |
| `compliance` | AFA required, mandate cancelled/expired, **any unknown code** | re-auth link or escalate — never auto-retry |

**AFA threshold check** (runs independently, takes precedence):

| Mandate category | AFA-free limit | Above it → |
|---|---|---|
| general | ₹15,000 (`1_500_000` paise) | re-auth link sent |
| `mutual_fund_sip`, `insurance`, `credit_card_bill` | ₹1,00,000 (`10_000_000` paise) | re-auth link sent |

Strictly *above* the limit requires re-auth; exactly *at* the limit does not.

**Actions:** `retry_scheduled` · `reauth_link_sent` · `escalated` · `stopped_permanent`.
Every action carries a `rule_fired` string (e.g. `afa_threshold_exceeded`,
`soft_retries_exhausted`) and a `reasoning_source` (`deterministic_rule` /
`ai_classifier` / `ai_fallback`).

---

## 4. Design principles (from [`PRINCIPLES.md`](PRINCIPLES.md))

1. **Deterministic rules before AI, always.** AFA thresholds, bucketing, retry caps,
   stop conditions are plain code — never an LLM call.
2. **Fail toward caution, never toward action.** AI failure or an unknown decline
   code degrades to "flag for human review" / `compliance`, never "auto-retry".
3. **Money is always integer paise**, never float. Converted to ₹ only at display.
4. **Idempotency is mandatory.** Re-running a batch or a mid-batch crash never
   double-processes or double-counts money recovered.
5. **Audit log is append-only.** No `UPDATE` / `DELETE` on `audit_log`.
6. **Process in pages** (200 rows), always on an index — no unbounded `SELECT *`.
7. **Every state change writes a matching `audit_log` row in the same transaction.**
8. **No real financial data, ever.** All data synthetic; no real gateway or PII.

---

## 5. Tech stack

| Layer | Choice | Why |
|---|---|---|
| Language | Python 3.12 | compliance rule engine needs heavy unit testing (pytest) |
| DB | Postgres — Supabase free tier | relational integrity for audit joins; generous free tier |
| DB driver | `psycopg[binary]` 3.x | — |
| Worker | plain Python module, paginated batch loop | predictable, avoids free-tier timeout cliffs |
| AI | Google Gemini (`gemini-flash-lite-latest`), free tier, no card | narrow, bounded use only |
| Dashboard | Flask + server-rendered templates | read-only, no build step |
| Tests | pytest — 143 pure + ~15 opt-in DB integration | — |

---

## 6. Repository layout

```
recovery-coordinator/
├── PRINCIPLES.md          Engineering principles — the non-negotiables
├── ARCHITECTURE.md        Full ADR: context, requirements, diagram, trade-offs
├── DECISION_RULES.md      Canonical policy spec (thresholds, buckets, actions)
├── SCHEMA.sql             Postgres DDL (5 tables, enums, indexes)
├── load_test_report.txt   Committed result of the Step 7 load test (pitch backup)
│
├── src/
│   ├── config.py          Single source of truth for every constant
│   ├── db.py              Connection helper (secrets from env only)
│   ├── rules.py           Deterministic policy engine — pure functions
│   ├── ai.py              Bounded AI layer (2 jobs, fallback-safe)
│   ├── executor.py        Mock PSP retry simulation (no real rail)
│   ├── worker.py          Paginated, idempotent, audit-logged worker loop
│   └── dashboard.py       Read-only query helpers + plain-language audit view
│
├── app.py                 Flask routes for the dashboard
├── templates/             base / index / transactions / detail
│
├── scripts/
│   ├── apply_schema.py     Create/reset schema (--reset drops + recreates)
│   ├── seed.py            Chunked synthetic seeder (--transactions N --reset)
│   ├── run_worker.py       Run the worker once (--no-ai to disable AI)
│   ├── run_dashboard.py    Launch the Flask dashboard
│   ├── load_test.py        Full-system load test + 12 integrity checks
│   └── ai_smoke.py         Verify the Gemini API key works
│
├── steps/                 The build plan, one file per step (01–08)
└── tests/                 pytest suite (rules, worker, AI, dashboard, load test)
```

---

## 7. Running it

```bash
# 1. Setup
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt
cp .env.example .env          # then fill in DATABASE_URL (Supabase) and optionally GEMINI_API_KEY

# 2. Database
python -m scripts.apply_schema --reset      # create the 5 tables + indexes
python -m scripts.seed --transactions 2000 --reset   # synthetic batch

# 3. Process the batch
python -m scripts.run_worker                 # add --no-ai to run deterministic-only

# 4. Dashboard
python -m scripts.run_dashboard              # http://localhost:5000

# Tests
.venv/Scripts/python.exe -m pytest                       # 143 pure tests
RUN_DB_TESTS=1 .venv/Scripts/python.exe -m pytest        # + DB integration tests
```

Set `PYTHONIOENCODING=utf-8` on Windows for scripts that print the ₹ symbol. The AI
layer is optional — with no `GEMINI_API_KEY` the pipeline runs fully on the
deterministic path and uses templated fallback messages.

---

## 8. Proof it works — Step 7 load test

`scripts/load_test.py` seeds N transactions, runs the worker once (timed), then runs
12 independent integrity checks. Full output is committed to `load_test_report.txt`.

**Latest 12,000-transaction run:**

| Metric | Result |
|---|---|
| Processed | 12,000 in 53.3 s (225 txn/s), 60 pages |
| Recovered | 5,284 — **₹11,56,26,194** |
| Halted | 6,360 |
| Escalated | 356 |
| Errors | 0 |
| AI usage | 2,957 messages, 356 classifications, **0 fallbacks** |
| Integrity checks | **12 / 12 passed** |

Checks include: no duplicate decisions, exactly one decision + one audit row per
transaction, zero pending rows left, status ↔ action consistency, every stop
traceable in `audit_log`, no stalled page (worst inter-page gap 17.1 s vs 30 s
threshold), and an **independent spot-check** — re-run the pure rule engine on 15
random audit snapshots and confirm the recorded action + rule still match (0/15
mismatched).

---

## 9. Live demo moment

On the dashboard, click into a single transaction's detail view and narrate its audit
trail out loud:

> decline code `V3` → rule engine bucketed it as `compliance` → AFA check independent →
> action `reauth_link_sent`, rule `compliance_decline_no_retry` → customer message
> drafted by AI → written to `audit_log` in the same DB transaction as the status
> change.

The `humanize_decision()` helper turns the raw JSONB snapshot into those plain
sentences — that's the "prove it" view.

---

## 10. What's real vs. what we designed (say this proactively — it's rigor, not weakness)

**Real / verified:**
- The ₹15,000 and ₹1,00,000 AFA-free thresholds (RBI e-mandate framework).
- The pre-debit notification requirement.
- Razorpay's own T+1 / T+2 / T+3 retry-then-`halted` behavior (`MAX_RETRY_ATTEMPTS = 3`
  is grounded in this).

**Reasoned / extrapolated (stated openly):**
- The exact decline-code → bucket taxonomy. NPCI's full operating circular isn't
  public; this is modeled on documented PSP integration guides.
- The synthetic seed distributions (category mix 55/20/15/10, bucket mix
  55/20/15/10). Reasoned estimates, not published statistics.
- `TECHNICAL_RETRY_MINUTES`, `PAGE_SIZE`, `AI_TIMEOUT_SECONDS`,
  `CIRCUIT_BREAKER_THRESHOLD` — sensible engineering defaults for a hackathon build,
  not sourced.

---

## 11. Toughest question, pre-answered

**"Why doesn't the AI just decide everything?"**
Because compliance-critical decisions — the AFA threshold, the retry caps — must be
deterministic and auditable, not left to a model that could hallucinate a wrong
action with real money on the line. The AI does two narrow things: word a customer
message, and suggest a bucket for a decline code we haven't seen. Neither can change
what the agent actually does.

---

## 12. What broke during the build

The honest version is in [`ENGINEERING_JOURNAL.md`](ENGINEERING_JOURNAL.md) — seven
incidents, each as symptom → diagnosis → fix. The ones worth mentioning in the pitch:

- **Gemini `flash` was too slow** (5–20 s/call on the free tier) to sit inside a
  batch → switched to `flash-lite`, ~1 s/call, no quality loss on these two tiny tasks.
- **Gemini rejected an 8-second HTTP timeout** as too short → enforced the "AI never
  stalls the batch" rule one layer up, at the page level (deadline + future cancel),
  not on the socket.
- **One AI call per row would have blown the quota** and made page 1 stall → dedupe
  rows to ~15 distinct *scenarios* per run and cache the results run-wide.
- **A worker restart mid-batch could double-count recovered money** → pushed
  idempotency into the DB (`UNIQUE` + `ON CONFLICT DO NOTHING` in a single CTE);
  re-running a finished batch is now a provable no-op.

---

## 13. What we'd revisit if this became real

- Replace the polling worker with a real event queue (SQS / Kafka).
- Cryptographically chained append-only audit log.
- Real bank/PSP webhook signature verification.
- India data-localization for the database (in-region hosting) — a known real
  constraint, named but not built for the hackathon.

---

## 14. Further reading

| File | What's in it |
|---|---|
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | Full ADR — context, requirements, high-level design, data flow, trade-off analysis |
| [`DECISION_RULES.md`](DECISION_RULES.md) | The canonical policy engine spec with worked pseudocode and the minimum test set |
| [`ENGINEERING_JOURNAL.md`](ENGINEERING_JOURNAL.md) | What broke during the build and how each issue was fixed |
| [`PRINCIPLES.md`](PRINCIPLES.md) | The 10 non-negotiable project rules and the definition of done |
| [`SCHEMA.sql`](SCHEMA.sql) | Postgres DDL with inline notes on why each constraint exists |
| [`steps/00_INDEX.md`](steps/00_INDEX.md) | The 8-step build plan |
