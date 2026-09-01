# Engineering Journal — what broke, and what we did about it

The design in [`ARCHITECTURE.md`](ARCHITECTURE.md) is the plan. This is the part that
didn't go to plan. Each entry: symptom → diagnosis → fix.

---

## 1. Gemini `flash` was too slow to sit inside a batch

**Symptom.** With `gemini-flash-latest` wired into the worker, single AI calls took
5–20 s on the free tier. A page that needed a few classifications or messages stalled
for a minute-plus; a 2,000-row batch was projected at 10+ minutes.

**Diagnosis.** The two AI jobs here are tiny — pick one word from four, or write three
plain sentences. `flash` is far more model than that needs, and its free-tier queue
latency dominated the actual inference time.

**Fix.** Switched to `gemini-flash-lite-latest` (`src/config.py`, `GEMINI_MODEL`).
~1 s per call, and no measurable quality drop on either task. The reasoning is written
into the config comment so the choice isn't mistaken for an accident.

---

## 2. Gemini rejected the 8-second timeout the spec asked for

**Symptom.** `DECISION_RULES.md` fixes `AI_TIMEOUT_SECONDS = 8` ("the AI must never
stall the batch"). Passing an 8 s HTTP deadline to the current Gemini endpoint made it
return an error — *the deadline itself was rejected as too short.*

**Diagnosis.** The real requirement isn't "the HTTP call must finish in 8 s," it's
"one page's AI phase must not hold up the pipeline." Enforcing that at the socket
level was the wrong layer.

**Fix.** Moved the bound up to the pipeline. `apply_ai()` in `src/worker.py` runs the
page's AI calls in a thread pool with `as_completed(..., timeout=AI_PAGE_DEADLINE_SECONDS)`;
anything still pending when the deadline hits is cancelled and recorded as a
`timeout` fallback. The `ThreadPoolExecutor` is deliberately *not* used as a `with`
block so shutdown never waits on a straggler. `AI_HTTP_TIMEOUT_SECONDS` stays as a
dead-socket backstop only. The 8 in the spec is now an intent, enforced where it
actually matters, with a comment saying so.

---

## 3. One AI call per row would have burned the free-tier quota — and page 1 stalled

**Symptom.** First working version called the model per transaction. Free-tier rate
limits were an obvious wall at a few thousand rows, and even under the limit, page 1
of the load test took ~17 s while every later page took milliseconds.

**Diagnosis.** The AI output only depends on the *scenario*, not the row:
classification is keyed by decline code, a message is keyed by
`(rule_fired, mandate_category)` with the rupee amount slotted in afterward via a
placeholder token. Thousands of rows collapse to a dozen or so distinct scenarios.
Page 1 was paying the entire cost of warming that set.

**Fix.** `apply_ai()` dedupes rows to scenario keys and calls the model once per key,
into a cache shared across the whole run. A 2,000-row batch now makes ~15 AI calls
total (~14 s), all on page 1, then reuses them. The worst-page-gap check in the load
test explicitly tolerates this: page 1's 17.1 s gap is expected and documented, every
other page is well under the 30 s stall threshold.

---

## 4. The Supabase database wouldn't resolve from this machine

**Symptom.** Every connection to `db.<ref>.supabase.co` failed instantly with
`getaddrinfo failed`.

**Diagnosis.** That direct host publishes an AAAA (IPv6) record only. The dev machine
has no working IPv6 route, so the name never resolved — nothing to do with
credentials or firewall.

**Fix.** Switched the DSN to the **session pooler** host
(`aws-0-ap-northeast-1.pooler.supabase.com`, IPv4, region ap-northeast-1). Separately,
the DB password contains `@` and `$`, which broke URI parsing until they were
URL-encoded (`%40`, `%24`) — `.env.example` now warns about this explicitly.

---

## 5. A worker restart mid-batch could double-count recovered money

**Symptom.** Killing the worker halfway through a batch and re-running it risked
processing the same transaction twice — a second `agent_decisions` row, a second
`audit_log` row, and the recovered-money metric counted twice.

**Diagnosis.** An application-level "have I seen this key?" check has a race window and
doesn't survive a crash between the check and the write.

**Fix.** Pushed idempotency into the database. `agent_decisions.transaction_id` has a
`UNIQUE` constraint, and each row's three writes are one CTE statement: the decision
insert is `ON CONFLICT (transaction_id) DO NOTHING`, and the audit insert + status
update only fire `WHERE EXISTS (SELECT 1 FROM dec)`. On an already-decided row the
whole statement is a no-op. Re-running a finished batch changes nothing; the load test
asserts zero duplicate decisions and exactly one audit row per transaction.

---

## 6. The circuit breaker tripped on a single unlucky row

**Symptom.** Early on, the "pause if >20% of a page errors" guard fired on a tiny
final page where one row out of three failed.

**Diagnosis.** A pure ratio has no floor. On a 3-row page, one error is 33% and looks
like a catastrophe; it usually isn't.

**Fix.** Added `CIRCUIT_BREAKER_MIN_ERRORS = 5` as a floor alongside the ratio
(`src/worker.py`, `_circuit_tripped`). The breaker now needs both *and* five or more
errors before it stops the run — so a small page can't trip it, but a genuinely bad
full page still does.

---

## 7. A batched write that failed took the whole page down with it

**Symptom.** Writing a page via `executemany` meant one bad row aborted the batch and
left the rest of the page unprocessed.

**Fix.** `write_page()` tries the fast batched path first; on any exception it falls
back to writing the page row by row, collecting per-row errors instead of losing the
page. Because every write is idempotent, retrying the page this way is safe. Those
per-row errors are what feed the circuit breaker in entry 6.
