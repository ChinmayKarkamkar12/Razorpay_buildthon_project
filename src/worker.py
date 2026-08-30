"""Worker loop: paginated, idempotent, audit-logged.

Pulls pages of `pending` transactions (keyset pagination on the
idx_transactions_status_scheduled index), runs the deterministic rule engine, mock-
executes the resulting action, and writes — per row — the decision + an audit row +
the status update as ONE atomic statement (CLAUDE.md Rules 4, 6, 7).

Atomicity + idempotency: each row's three writes are a single CTE statement. The
INSERT into agent_decisions is `ON CONFLICT (transaction_id) DO NOTHING`; the audit
insert and the status update only fire when that insert actually happened. So the
statement is a no-op on a transaction that already has a decision — re-running the
worker, or a mid-batch crash + restart, never double-processes or double-counts.

Performance: a page's rows are written with one `executemany` (psycopg pipelines it),
turning ~200 network round-trips into ~1. If that batch raises, the page is retried
row-by-row to isolate the bad rows and feed the circuit breaker.
"""

import json
import math
import pathlib
from dataclasses import dataclass, field
from datetime import datetime, timezone

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from src.config import CIRCUIT_BREAKER_THRESHOLD, PAGE_SIZE
from src.db import get_dsn
from src.executor import RetrySimulation, simulate_retry_schedule
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

# Minimum errors on a page before the circuit breaker can trip — stops a tiny final
# page (e.g. 3 rows, 1 error) tripping it on proportion alone.
CIRCUIT_BREAKER_MIN_ERRORS = 5

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_ZERO_UUID = "00000000-0000-0000-0000-000000000000"  # keyset cursor start

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

# One atomic statement: decision (idempotent) -> audit (only if decision inserted)
# -> status update (only if decision inserted and row still pending).
WRITE_ROW = """
WITH dec AS (
    INSERT INTO agent_decisions
        (transaction_id, rule_fired, action_taken, reasoning_source, next_action_at)
    VALUES (%(txn)s, %(rule)s, %(action)s, 'deterministic_rule', %(next_at)s)
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
    """Raised when too many rows on one page error out — the worker stops instead of
    chewing through a bad batch (DECISION_RULES.md Step 4)."""


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
        }


# ── planning: rule engine + mock execution, no DB ────────────────────────────
@dataclass(frozen=True)
class RowPlan:
    transaction_id: str
    amount: int
    final_status: str        # transactions.status enum after this decision
    params: dict             # bound parameters for WRITE_ROW


def _resolve_outcome(action: str, bucket: str, transaction_id: str, attempt_count: int):
    """Return (final_status, attempt_count_after, outcome_label, RetrySimulation|None)."""
    if action == ACTION_RETRY_SCHEDULED:
        sim = simulate_retry_schedule(
            transaction_id=transaction_id, bucket=bucket, attempt_count=attempt_count
        )
        if sim.recovered:
            return "recovered", sim.final_attempt_count, "recovered_on_retry", sim
        return "halted", sim.final_attempt_count, "retries_exhausted", sim

    if action == ACTION_ESCALATED:
        return "escalated", attempt_count, "escalated_to_human", None

    # reauth_link_sent / stopped_permanent: automated recovery stops here. The DB
    # enum has no "waiting on customer" state, so both land on 'halted' — the
    # specific reason is in agent_decisions.rule_fired.
    label = "reauth_link_sent" if action == ACTION_REAUTH_LINK_SENT else "stopped_permanent"
    return "halted", attempt_count, label, None


def plan_row(row: dict) -> RowPlan:
    decline_code = row["decline_code"]
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

    snapshot = {
        "transaction_id": str(row["transaction_id"]),
        "amount_paise": row["amount"],
        "mandate_category": row["mandate_category"],
        "decline_code": decline_code,
        "bucket": bucket,
        "afa_check": afa_check,
        "rule_fired": decision.rule_fired,
        "action_taken": decision.action,
        "reasoning_source": "deterministic_rule",
        "attempt_count_before": row["attempt_count"],
        "attempt_count_after": attempts_after,
        "retry_simulation": (
            {"attempts_made": sim.attempts_made, "recovered": sim.recovered} if sim else None
        ),
        "status_before": "pending",
        "status_after": status,
        "outcome": label,
        "decided_at": datetime.now(timezone.utc).isoformat(),
    }

    return RowPlan(
        transaction_id=str(row["transaction_id"]),
        amount=row["amount"],
        final_status=status,
        params={
            "txn": row["transaction_id"],
            "rule": decision.rule_fired,
            "action": decision.action,
            "next_at": next_action_at,
            "snapshot": Jsonb(snapshot),
            "status": status,
            "attempts": attempts_after,
        },
    )


# ── writing: batched, with a row-by-row fallback ─────────────────────────────
def write_page(conn: psycopg.Connection, plans: list[RowPlan]) -> dict[str, Exception]:
    """Write every plan. Returns {transaction_id: exception} for rows that failed."""
    if not plans:
        return {}
    try:
        with conn.cursor() as cur:
            cur.executemany(WRITE_ROW, [p.params for p in plans])
        return {}
    except Exception as batch_exc:  # noqa: BLE001
        print(f"  batch write failed ({batch_exc!r}); retrying page row-by-row")
        errors: dict[str, Exception] = {}
        for p in plans:
            try:
                with conn.cursor() as cur:
                    cur.execute(WRITE_ROW, p.params)  # idempotent — safe to repeat
            except Exception as row_exc:  # noqa: BLE001
                errors[p.transaction_id] = row_exc
        return errors


# ── page loop ────────────────────────────────────────────────────────────────
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
) -> WorkerRun:
    result = WorkerRun()
    conn = psycopg.connect(get_dsn())
    conn.autocommit = True  # each WRITE_ROW statement is its own atomic transaction
    try:
        total_pages = _estimate_total_pages(conn, page_size)
        print(f"worker: ~{total_pages} page(s) of pending transactions to process")

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
            by_id = {p.transaction_id: p for p in plans}
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
                f"escalated {result.escalated} errors {result.errors}"
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
