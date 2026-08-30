"""Worker loop: paginated, idempotent, audit-logged, with a bounded AI layer.

Pulls pages of `pending` transactions (keyset pagination on the
idx_transactions_status_scheduled index), runs the deterministic rule engine, adds
bounded AI enrichment where allowed (re-auth message text, unmapped-code bucket
suggestion), mock-executes the resulting action, and writes — per row — the decision
+ an audit row + the status update as ONE atomic statement (CLAUDE.md Rules 4, 6, 7).

The AI never changes the routing decision (CLAUDE.md Rule 1). A timeout / error /
missing key degrades to the conservative fallback, recorded in the audit snapshot;
the pipeline never hangs on it (CLAUDE.md Rule 2).

Atomicity + idempotency: each row's three writes are one CTE statement. The decision
insert is `ON CONFLICT (transaction_id) DO NOTHING`; the audit insert and status
update only fire when that insert happened. So the statement is a no-op on a
transaction that already has a decision — re-running, or a mid-batch crash + restart,
never double-processes or double-counts.
"""

import json
import math
import pathlib
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from src.ai import AIClient, AIResult, fallback_message
from src.config import AI_MAX_CONCURRENCY, AI_TIMEOUT_SECONDS, CIRCUIT_BREAKER_THRESHOLD, DECLINE_CODE_BUCKETS, PAGE_SIZE
from src.db import get_dsn
from src.executor import simulate_retry_schedule
from src.rules import (
    ACTION_ESCALATED,
    ACTION_REAUTH_LINK_SENT,
    ACTION_RETRY_SCHEDULED,
    check_afa_threshold,
    classify,
    decide_action,
    is_mandate_issue,
)

PROGRESS_FILE = pathlib.Path(__file__).resolve().parent.parent / "worker_progress.json"

CIRCUIT_BREAKER_MIN_ERRORS = 5
_PAGE_AI_DEADLINE = AI_TIMEOUT_SECONDS + 3  # hard cap on a page's whole AI phase

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_ZERO_UUID = "00000000-0000-0000-0000-000000000000"

PAGE_QUERY = """
SELECT t.transaction_id, t.amount, t.attempt_count, t.scheduled_at,
       m.category AS mandate_category,
       d.decline_code
FROM transactions t
JOIN mandates m ON m.mandate_id = t.mandate_id
LEFT JOIN LATERAL (
    SELECT decline_code
    FROM decline_events de
    WHERE de.transaction_id = t.transaction_id
    ORDER BY occurred_at DESC
    LIMIT 1
) d ON true
WHERE t.status = 'pending'
  AND NOT EXISTS (
      SELECT 1 FROM agent_decisions ad WHERE ad.transaction_id = t.transaction_id
  )
  AND (t.scheduled_at, t.transaction_id) > (%s, %s)
ORDER BY t.scheduled_at, t.transaction_id
LIMIT %s
"""

WRITE_ROW = """
WITH dec AS (
    INSERT INTO agent_decisions
        (transaction_id, rule_fired, action_taken, reasoning_source, next_action_at)
    VALUES (%(txn)s, %(rule)s, %(action)s, %(reasoning)s, %(next_at)s)
    ON CONFLICT (transaction_id) DO NOTHING
    RETURNING 1
),
aud AS (
    INSERT INTO audit_log (transaction_id, event_type, payload_snapshot)
    SELECT %(txn)s, 'agent_decision', %(snapshot)s
    WHERE EXISTS (SELECT 1 FROM dec)
    RETURNING 1
)
UPDATE transactions
SET status = %(status)s, attempt_count = %(attempts)s
WHERE transaction_id = %(txn)s
  AND status = 'pending'
  AND EXISTS (SELECT 1 FROM dec)
"""


class CircuitBreakerTripped(RuntimeError):
    """Too many rows on one page errored — the worker stops instead of chewing
    through a bad batch (DECISION_RULES.md Step 4)."""


@dataclass
class WorkerRun:
    pages: int = 0
    processed: int = 0
    recovered: int = 0
    halted: int = 0
    escalated: int = 0
    errors: int = 0
    error_ids: list[str] = field(default_factory=list)
    recovered_paise: int = 0
    ai_messages: int = 0        # re-auth messages actually drafted by the model
    ai_classifications: int = 0  # unmapped codes the model returned a bucket for
    ai_fallbacks: int = 0        # AI tasks that fell back to a template / default
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def as_progress(self, total_pages: int | None, running: bool) -> dict:
        return {
            "running": running,
            "started_at": self.started_at.isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "pages_done": self.pages,
            "pages_estimated": total_pages,
            "rows_processed": self.processed,
            "recovered": self.recovered,
            "halted": self.halted,
            "escalated": self.escalated,
            "errors": self.errors,
            "recovered_paise": self.recovered_paise,
            "ai_messages": self.ai_messages,
            "ai_classifications": self.ai_classifications,
            "ai_fallbacks": self.ai_fallbacks,
        }


# ── planning: rule engine + mock execution (deterministic, no DB, no AI) ──────
@dataclass
class RowPlan:
    transaction_id: object
    amount: int
    final_status: str
    attempt_count_after: int
    action: str
    rule_fired: str
    next_action_at: datetime | None
    reasoning_source: str
    snapshot: dict
    ai_task: str | None          # "classify" | "message" | None
    ai_context: dict


def _is_unmapped(decline_code: str | None) -> bool:
    return bool(decline_code) and decline_code.strip() not in DECLINE_CODE_BUCKETS


def _resolve_outcome(action: str, bucket: str, transaction_id: str, attempt_count: int):
    if action == ACTION_RETRY_SCHEDULED:
        sim = simulate_retry_schedule(
            transaction_id=transaction_id, bucket=bucket, attempt_count=attempt_count
        )
        if sim.recovered:
            return "recovered", sim.final_attempt_count, "recovered_on_retry", sim
        return "halted", sim.final_attempt_count, "retries_exhausted", sim
    if action == ACTION_ESCALATED:
        return "escalated", attempt_count, "escalated_to_human", None
    label = "reauth_link_sent" if action == ACTION_REAUTH_LINK_SENT else "stopped_permanent"
    return "halted", attempt_count, label, None


def plan_row(row: dict) -> RowPlan:
    decline_code = row["decline_code"]
    unmapped = _is_unmapped(decline_code)
    bucket = classify(decline_code)
    afa_check = check_afa_threshold(row["amount"], row["mandate_category"])
    decision = decide_action(
        bucket, afa_check, row["attempt_count"],
        is_mandate_issue=is_mandate_issue(decline_code),
    )
    status, attempts_after, label, sim = _resolve_outcome(
        decision.action, bucket, str(row["transaction_id"]), row["attempt_count"]
    )

    next_action_at = None
    if decision.action == ACTION_RETRY_SCHEDULED and decision.retry_after is not None:
        next_action_at = row["scheduled_at"] + decision.retry_after

    ai_context = {
        "decline_code": decline_code,
        "amount": row["amount"],
        "mandate_category": row["mandate_category"],
        "rule_fired": decision.rule_fired,
        "bucket": bucket,
    }

    if unmapped:
        ai_task = "classify"
        reasoning_source = "ai_fallback"  # upgraded to ai_classifier if the model answers
    elif decision.action == ACTION_REAUTH_LINK_SENT:
        ai_task = "message"
        reasoning_source = "deterministic_rule"
    else:
        ai_task = None
        reasoning_source = "deterministic_rule"

    snapshot = {
        "transaction_id": str(row["transaction_id"]),
        "amount_paise": row["amount"],
        "mandate_category": row["mandate_category"],
        "decline_code": decline_code,
        "decline_code_mapped": not unmapped,
        "bucket": bucket,
        "afa_check": afa_check,
        "rule_fired": decision.rule_fired,
        "action_taken": decision.action,
        "reasoning_source": reasoning_source,
        "attempt_count_before": row["attempt_count"],
        "attempt_count_after": attempts_after,
        "retry_simulation": (
            {"attempts_made": sim.attempts_made, "recovered": sim.recovered} if sim else None
        ),
        "status_before": "pending",
        "status_after": status,
        "outcome": label,
        "ai_bucket_suggestion": None,
        "customer_message": None,
        "message_source": "none",
        "ai_status": "not_needed" if ai_task is None else "pending",
        "decided_at": datetime.now(timezone.utc).isoformat(),
    }

    return RowPlan(
        transaction_id=row["transaction_id"],
        amount=row["amount"],
        final_status=status,
        attempt_count_after=attempts_after,
        action=decision.action,
        rule_fired=decision.rule_fired,
        next_action_at=next_action_at,
        reasoning_source=reasoning_source,
        snapshot=snapshot,
        ai_task=ai_task,
        ai_context=ai_context,
    )


# ── AI enrichment (bounded, concurrent, fallback-safe) ───────────────────────
def _merge_ai(plan: RowPlan, res: AIResult, run: WorkerRun) -> None:
    plan.snapshot["ai_status"] = res.status
    if plan.ai_task == "classify":
        plan.snapshot["ai_bucket_suggestion"] = res.value
        if res.ok:
            plan.reasoning_source = "ai_classifier"
            run.ai_classifications += 1
        else:
            plan.reasoning_source = "ai_fallback"
            run.ai_fallbacks += 1
        plan.snapshot["reasoning_source"] = plan.reasoning_source
        # Routing is unchanged: unmapped code stays compliance -> escalated.
    else:  # message
        if res.ok and res.value:
            plan.snapshot["customer_message"] = res.value
            plan.snapshot["message_source"] = "ai"
            run.ai_messages += 1
        else:
            plan.snapshot["customer_message"] = fallback_message(plan.rule_fired, plan.ai_context)
            plan.snapshot["message_source"] = "fallback"
            run.ai_fallbacks += 1


def apply_ai(plans: list[RowPlan], ai: AIClient, run: WorkerRun) -> None:
    tasks = [p for p in plans if p.ai_task]
    if not tasks:
        return

    def call(p: RowPlan) -> AIResult:
        if p.ai_task == "classify":
            return ai.classify_decline_code(p.ai_context["decline_code"], p.ai_context)
        return ai.draft_reauth_message(p.ai_context)

    # No key -> every call returns instantly; skip the thread pool.
    if not ai.enabled:
        for p in tasks:
            _merge_ai(p, call(p), run)
        return

    # Not a `with` block: we must not wait on stragglers at shutdown. A call that
    # overruns the page deadline is abandoned here and finishes (or times out on the
    # SDK's own 8s HTTP cap) harmlessly in the background.
    ex = ThreadPoolExecutor(max_workers=AI_MAX_CONCURRENCY)
    futs = {ex.submit(call, p): p for p in tasks}
    pending = set(futs)
    try:
        for fut in as_completed(futs, timeout=_PAGE_AI_DEADLINE):
            pending.discard(fut)
            p = futs[fut]
            try:
                res = fut.result()
            except Exception as exc:  # noqa: BLE001
                res = AIResult(False, None, f"error:{type(exc).__name__}")
            _merge_ai(p, res, run)
    except FuturesTimeout:
        pass
    finally:
        for fut in pending:
            fut.cancel()
            _merge_ai(futs[fut], AIResult(False, None, "timeout"), run)
        ex.shutdown(wait=False, cancel_futures=True)


# ── writing: batched, with a row-by-row fallback ────────────────────────────
def _params(p: RowPlan) -> dict:
    return {
        "txn": p.transaction_id,
        "rule": p.rule_fired,
        "action": p.action,
        "reasoning": p.reasoning_source,
        "next_at": p.next_action_at,
        "snapshot": Jsonb(p.snapshot),
        "status": p.final_status,
        "attempts": p.attempt_count_after,
    }


def write_page(conn: psycopg.Connection, plans: list[RowPlan]) -> dict[str, Exception]:
    if not plans:
        return {}
    try:
        with conn.cursor() as cur:
            cur.executemany(WRITE_ROW, [_params(p) for p in plans])
        return {}
    except Exception as batch_exc:  # noqa: BLE001
        print(f"  batch write failed ({batch_exc!r}); retrying page row-by-row")
        errors: dict[str, Exception] = {}
        for p in plans:
            try:
                with conn.cursor() as cur:
                    cur.execute(WRITE_ROW, _params(p))  # idempotent — safe to repeat
            except Exception as row_exc:  # noqa: BLE001
                errors[str(p.transaction_id)] = row_exc
        return errors


# ── page loop ───────────────────────────────────────────────────────────────
def _estimate_total_pages(conn: psycopg.Connection, page_size: int) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM transactions t WHERE t.status = 'pending' "
            "AND NOT EXISTS (SELECT 1 FROM agent_decisions ad "
            "                WHERE ad.transaction_id = t.transaction_id)"
        )
        pending = cur.fetchone()[0]
    return math.ceil(pending / page_size) if pending else 0


def run(
    *,
    page_size: int = PAGE_SIZE,
    max_pages: int | None = None,
    circuit_breaker: bool = True,
    write_progress: bool = True,
    ai: AIClient | None = None,
) -> WorkerRun:
    ai = ai or AIClient()
    result = WorkerRun()
    conn = psycopg.connect(get_dsn())
    conn.autocommit = True
    try:
        total_pages = _estimate_total_pages(conn, page_size)
        print(
            f"worker: ~{total_pages} page(s) to process | AI: "
            f"{'enabled' if ai.enabled else 'disabled (fallback only)'}"
        )

        cursor_ts, cursor_id = _EPOCH, _ZERO_UUID
        while max_pages is None or result.pages < max_pages:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(PAGE_QUERY, (cursor_ts, cursor_id, page_size))
                page = cur.fetchall()
            if not page:
                break

            result.pages += 1
            cursor_ts, cursor_id = page[-1]["scheduled_at"], page[-1]["transaction_id"]

            plans = [plan_row(r) for r in page]
            apply_ai(plans, ai, result)
            by_id = {str(p.transaction_id): p for p in plans}
            errors = write_page(conn, plans)

            for txn_id, plan in by_id.items():
                if txn_id in errors:
                    result.errors += 1
                    result.error_ids.append(txn_id)
                    print(f"  ! row {txn_id} failed: {errors[txn_id]!r}")
                    continue
                result.processed += 1
                if plan.final_status == "recovered":
                    result.recovered += 1
                    result.recovered_paise += plan.amount
                elif plan.final_status == "escalated":
                    result.escalated += 1
                else:
                    result.halted += 1

            print(
                f"  page {result.pages}/{total_pages or '?'}: {len(page)} rows | "
                f"recovered {result.recovered} halted {result.halted} "
                f"escalated {result.escalated} errors {result.errors} | "
                f"ai_msg {result.ai_messages} ai_cls {result.ai_classifications} "
                f"ai_fb {result.ai_fallbacks}"
            )
            if write_progress:
                _write_progress(result, total_pages, running=True)

            page_errors = len(errors)
            if (
                circuit_breaker
                and page_errors >= CIRCUIT_BREAKER_MIN_ERRORS
                and page_errors / len(page) > CIRCUIT_BREAKER_THRESHOLD
            ):
                if write_progress:
                    _write_progress(result, total_pages, running=False)
                raise CircuitBreakerTripped(
                    f"{page_errors}/{len(page)} rows on page {result.pages} errored "
                    f"(> {CIRCUIT_BREAKER_THRESHOLD:.0%}) — stopping. "
                    f"Investigate before re-running."
                )

        if write_progress:
            _write_progress(result, total_pages, running=False)
        return result
    finally:
        conn.close()


def _write_progress(result: WorkerRun, total_pages: int | None, running: bool) -> None:
    PROGRESS_FILE.write_text(
        json.dumps(result.as_progress(total_pages, running), indent=2), encoding="utf-8"
    )
