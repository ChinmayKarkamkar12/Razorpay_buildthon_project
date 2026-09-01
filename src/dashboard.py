"""Step 6 — read-only dashboard data layer.

Pure-ish query helpers for the Flask app in `app.py`. Every query is bounded:
the summary is a handful of aggregates cached on a short interval, the transactions
list is always `LIMIT`ed, and the detail view is keyed by a single id. Nothing here
writes (PRINCIPLES.md: the dashboard is read-only).

`humanize_decision()` turns an `audit_log` snapshot into plain-language lines — the
"prove it" view for judges — and is a pure function with no DB, so it is unit-tested
directly.
"""

import json
import pathlib
import time

import psycopg
from psycopg.rows import dict_row

from src.config import DASHBOARD_PAGE_SIZE, DASHBOARD_SUMMARY_TTL_SECONDS
from src.db import get_dsn

_PROGRESS_FILE = pathlib.Path(__file__).resolve().parent.parent / "worker_progress.json"


def get_worker_progress() -> dict | None:
    """Live batch progress written by the worker in Step 3. None if it never ran."""
    try:
        data = json.loads(_PROGRESS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    done, est = data.get("pages_done"), data.get("pages_estimated")
    data["label"] = f"page {done}/{est}" if done is not None and est else "—"
    data["pct"] = round(100 * done / est) if done and est else (100 if data.get("running") is False else 0)
    return data

# ── connection: one lazily-opened autocommit connection, reused across requests ──
_conn: psycopg.Connection | None = None


def _connection() -> psycopg.Connection:
    global _conn
    if _conn is None or _conn.closed:
        _conn = psycopg.connect(get_dsn())
        _conn.autocommit = True
    return _conn


def _rows(sql: str, params: tuple = ()) -> list[dict]:
    with _connection().cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def _one(sql: str, params: tuple = ()) -> dict:
    rows = _rows(sql, params)
    return rows[0] if rows else {}


# ── summary — cached on a short interval, not recomputed every render ───────────
_SUMMARY_SQL = """
SELECT
    (SELECT count(*) FROM transactions)                              AS total,
    (SELECT count(*) FROM transactions WHERE status = 'recovered')   AS recovered,
    (SELECT count(*) FROM transactions WHERE status = 'halted')      AS halted,
    (SELECT count(*) FROM transactions WHERE status = 'escalated')   AS escalated,
    (SELECT count(*) FROM transactions WHERE status = 'pending')     AS pending,
    (SELECT coalesce(sum(amount), 0) FROM transactions WHERE status = 'recovered')
                                                                     AS recovered_paise
"""

_BUCKET_SQL = "SELECT bucket, count(*) AS n FROM decline_events GROUP BY bucket ORDER BY bucket"

_summary_cache: dict = {"at": 0.0, "data": None}


def get_summary(*, force: bool = False) -> dict:
    now = time.monotonic()
    if (
        not force
        and _summary_cache["data"] is not None
        and now - _summary_cache["at"] < DASHBOARD_SUMMARY_TTL_SECONDS
    ):
        return _summary_cache["data"]

    totals = _one(_SUMMARY_SQL)
    buckets = {r["bucket"]: r["n"] for r in _rows(_BUCKET_SQL)}
    data = {
        "total": totals.get("total", 0),
        "recovered": totals.get("recovered", 0),
        "halted": totals.get("halted", 0),
        "escalated": totals.get("escalated", 0),
        "pending": totals.get("pending", 0),
        "recovered_rupees": (totals.get("recovered_paise") or 0) / 100,
        "buckets": {b: buckets.get(b, 0) for b in ("soft", "hard", "technical", "compliance")},
        "cached_at": time.time(),
    }
    _summary_cache.update(at=now, data=data)
    return data


# ── transactions table — always paginated, never a full-table load ─────────────
_PAGE_SQL = """
SELECT t.transaction_id,
       t.amount,
       t.status,
       t.attempt_count,
       m.category                AS mandate_category,
       ad.action_taken,
       ad.rule_fired,
       de.decline_code
FROM transactions t
JOIN mandates m ON m.mandate_id = t.mandate_id
LEFT JOIN agent_decisions ad ON ad.transaction_id = t.transaction_id
LEFT JOIN LATERAL (
    SELECT decline_code FROM decline_events e
    WHERE e.transaction_id = t.transaction_id
    ORDER BY occurred_at DESC LIMIT 1
) de ON true
WHERE (%(status)s::text IS NULL OR t.status::text = %(status)s::text)
ORDER BY t.scheduled_at DESC, t.transaction_id
LIMIT %(limit)s OFFSET %(offset)s
"""

_COUNT_SQL = (
    "SELECT count(*) AS n FROM transactions "
    "WHERE (%(status)s::text IS NULL OR status::text = %(status)s::text)"
)


def get_transactions_page(page: int = 1, status: str | None = None) -> dict:
    page = max(1, int(page))
    limit = DASHBOARD_PAGE_SIZE
    offset = (page - 1) * limit
    rows = _rows(_PAGE_SQL, {"status": status, "limit": limit, "offset": offset})
    total = _one(_COUNT_SQL, {"status": status}).get("n", 0)
    for r in rows:
        r["amount_rupees"] = r["amount"] / 100
        r["transaction_id"] = str(r["transaction_id"])
    total_pages = max(1, -(-total // limit))  # ceil
    return {
        "rows": rows,
        "page": page,
        "total_pages": total_pages,
        "total": total,
        "status": status,
        "has_prev": page > 1,
        "has_next": page < total_pages,
    }


# ── single transaction — full audit trail in plain language ────────────────────
_TXN_SQL = """
SELECT t.transaction_id, t.amount, t.status, t.attempt_count, t.scheduled_at,
       m.category AS mandate_category, m.status AS mandate_status
FROM transactions t
JOIN mandates m ON m.mandate_id = t.mandate_id
WHERE t.transaction_id = %s
"""

_DECISION_SQL = """
SELECT rule_fired, action_taken, reasoning_source, next_action_at, decided_at
FROM agent_decisions WHERE transaction_id = %s
"""

_DECLINES_SQL = """
SELECT decline_code, bucket, occurred_at
FROM decline_events WHERE transaction_id = %s ORDER BY occurred_at
"""

_AUDIT_SQL = """
SELECT event_type, payload_snapshot, created_at
FROM audit_log WHERE transaction_id = %s ORDER BY created_at
"""

# rule_fired -> one plain sentence a non-engineer can read
_RULE_PLAIN = {
    "afa_threshold_exceeded": "Amount is above the RBI limit for auto-debit, so the "
    "customer must re-approve it before any charge.",
    "compliance_decline_no_retry": "The mandate itself needs the customer to act "
    "(expired / cancelled / re-authorisation required) — retrying would not help; "
    "send a re-authorisation link.",
    "hard_decline_no_retry": "The bank gave a hard refusal (blocked card, wrong PIN, "
    "invalid account). Auto-retrying a hard decline is not allowed.",
    "soft_decline_scheduled_retry": "A soft decline (e.g. insufficient funds). Retry "
    "on the RBI T+1 / T+2 / T+3 schedule.",
    "soft_retries_exhausted": "Soft decline, but all 3 scheduled retries were used up. "
    "Stop and leave it alone.",
    "technical_quick_retry": "A transient technical error (bank unreachable, timeout). "
    "Retry quickly.",
    "technical_retries_exhausted": "Technical error that did not clear after 3 retries "
    "— hand to a human.",
    "unclassified_fallback": "Could not be classified — routed to a human to be safe.",
}

_ACTION_PLAIN = {
    "retry_scheduled": "Scheduled another payment attempt",
    "reauth_link_sent": "Sent the customer a re-authorisation link",
    "escalated": "Escalated to a human agent",
    "stopped_permanent": "Stopped permanently — no further attempts",
}


def humanize_decision(snapshot: dict) -> list[str]:
    """Turn an `agent_decision` audit snapshot into ordered plain-language lines.
    Pure — no DB, no network. Missing keys degrade to a readable line, never crash."""
    s = snapshot or {}
    amount = s.get("amount_paise")
    rupees = f"₹{amount / 100:,.2f}" if isinstance(amount, (int, float)) else "the amount"
    category = s.get("mandate_category", "unknown")
    code = s.get("decline_code") or "unknown"
    bucket = s.get("bucket", "unknown")
    mapped = s.get("decline_code_mapped")
    rule = s.get("rule_fired", "")
    action = s.get("action_taken", "")

    lines = [f"A {rupees} payment on a '{category}' mandate was attempted and failed."]

    if mapped is False:
        suggestion = s.get("ai_bucket_suggestion")
        if suggestion:
            lines.append(
                f"Decline code '{code}' is not in our taxonomy. The AI suggested it "
                f"looks like a '{suggestion}' decline, but routing stays conservative."
            )
        else:
            lines.append(
                f"Decline code '{code}' is not in our taxonomy — treated as the most "
                f"conservative bucket ('{bucket}')."
            )
    else:
        lines.append(f"Decline code '{code}' maps to a '{bucket}' decline.")

    afa = s.get("afa_check")
    if afa == "AFA_REQUIRED":
        lines.append("AFA check: amount is ABOVE the RBI auto-debit limit — re-auth required.")
    elif afa == "AFA_NOT_REQUIRED":
        lines.append("AFA check: amount is within the RBI auto-debit limit.")

    if rule == "compliance_decline_no_retry" and action == "escalated":
        lines.append(
            "Rule applied: This decline is not in our taxonomy, so it is treated as "
            "compliance and sent to a human — never auto-retried."
        )
    elif rule:
        lines.append(f"Rule applied: {_RULE_PLAIN.get(rule, rule)}")

    sim = s.get("retry_simulation")
    if sim:
        made = sim.get("attempts_made", "?")
        ok = "succeeded" if sim.get("recovered") else "did not succeed"
        lines.append(f"Retry simulation: {made} attempt(s) made — {ok}.")

    if action:
        lines.append(f"Action taken: {_ACTION_PLAIN.get(action, action)}.")

    outcome = s.get("status_after")
    if outcome:
        tail = f" ({s['outcome']})" if s.get("outcome") else ""
        lines.append(f"Final status: {outcome}{tail}.")

    src = s.get("reasoning_source")
    if src:
        label = {
            "deterministic_rule": "a hard-coded rule (no AI)",
            "ai_classifier": "a hard-coded rule, with an AI bucket suggestion for an unknown code",
            "ai_fallback": "the conservative fallback (AI unavailable or unusable)",
        }.get(src, src)
        lines.append(f"Decision was made by {label}.")

    # Note: the actual customer_message text is surfaced separately as its own
    # callout on the detail page (see get_transaction_detail), not repeated here.

    return lines


def get_transaction_detail(transaction_id: str) -> dict | None:
    txn = _one(_TXN_SQL, (transaction_id,))
    if not txn:
        return None
    txn["transaction_id"] = str(txn["transaction_id"])
    txn["amount_rupees"] = txn["amount"] / 100

    decision = _one(_DECISION_SQL, (transaction_id,)) or None
    declines = _rows(_DECLINES_SQL, (transaction_id,))
    audit = _rows(_AUDIT_SQL, (transaction_id,))

    decision_snapshot = next(
        (a["payload_snapshot"] for a in audit if a["event_type"] == "agent_decision"), None
    )
    action_plain = None
    if decision and decision.get("action_taken"):
        action_plain = _ACTION_PLAIN.get(decision["action_taken"], decision["action_taken"])

    customer_message = None
    if decision_snapshot and decision_snapshot.get("customer_message"):
        customer_message = {
            "text": decision_snapshot["customer_message"],
            "source": decision_snapshot.get("message_source", "n/a"),
        }

    return {
        "transaction": txn,
        "decision": decision,
        "declines": declines,
        "audit": audit,
        "story": humanize_decision(decision_snapshot) if decision_snapshot else [],
        # Plain-English one-liner for the subtitle under the status pill — e.g.
        # "Sent the customer a re-authorisation link." Judges/support agents read
        # this before they read the full narrative below.
        "action_plain": action_plain,
        # The actual outbound customer message, pulled out of the narrative so it
        # can be shown as its own callout instead of buried in a list of sentences.
        "customer_message": customer_message,
    }
