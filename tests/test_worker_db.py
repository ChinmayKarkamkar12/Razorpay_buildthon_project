"""Integration tests for the worker — the step 3 test sequence, automated.

Opt-in (RUN_DB_TESTS=1): these truncate and reseed the database.

    RUN_DB_TESTS=1 python -m pytest tests/test_worker_db.py -v
"""

import pytest

from src.worker import run

pytestmark = pytest.mark.db

SEED_N = 400


@pytest.fixture
def seeded(db_conn):
    from scripts.seed import build_mandates, build_transactions, mandate_row, reset

    with db_conn.cursor() as cur:
        reset(cur)
    mandates = build_mandates(80)
    txns, events = build_transactions(mandates, SEED_N)
    with db_conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO mandates (mandate_id, customer_id, category, afa_free_limit, "
            "status, registered_at) VALUES (%s,%s,%s,%s,%s,%s)",
            [mandate_row(m) for m in mandates],
        )
        cur.executemany(
            "INSERT INTO transactions (transaction_id, mandate_id, amount, scheduled_at, "
            "pdn_sent_at, pdn_channel, status, attempt_count, idempotency_key) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            txns,
        )
        cur.executemany(
            "INSERT INTO decline_events (event_id, transaction_id, decline_code, bucket, "
            "occurred_at) VALUES (%s,%s,%s,%s,%s)",
            events,
        )
    return db_conn


def _counts(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM agent_decisions")
        decisions = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM audit_log WHERE event_type = 'agent_decision'")
        audits = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM transactions WHERE status = 'pending'")
        pending = cur.fetchone()[0]
    return decisions, audits, pending


def test_every_transaction_gets_exactly_one_decision_and_one_audit_row(seeded):
    run(page_size=150, write_progress=False, use_ai=False)
    decisions, audits, pending = _counts(seeded)
    assert decisions == SEED_N
    assert audits == SEED_N
    assert pending == 0


def test_rerun_is_a_complete_no_op(seeded):
    run(page_size=150, write_progress=False, use_ai=False)
    before = _counts(seeded)
    second = run(page_size=150, write_progress=False, use_ai=False)
    assert second.processed == 0
    assert _counts(seeded) == before


def test_crash_midway_then_restart_never_duplicates(seeded):
    # process only the first page, as if the worker was killed afterwards
    partial = run(page_size=100, max_pages=1, write_progress=False, use_ai=False)
    assert partial.processed == 100
    d1, a1, p1 = _counts(seeded)
    assert d1 == 100 and a1 == 100 and p1 == SEED_N - 100

    # restart — must finish the rest with no duplicates
    rest = run(page_size=100, write_progress=False, use_ai=False)
    assert rest.processed == SEED_N - 100
    d2, a2, p2 = _counts(seeded)
    assert d2 == SEED_N and a2 == SEED_N and p2 == 0

    with seeded.cursor() as cur:
        cur.execute(
            "SELECT transaction_id, count(*) FROM agent_decisions "
            "GROUP BY transaction_id HAVING count(*) > 1"
        )
        assert cur.fetchall() == []


def test_recovered_total_matches_sum_of_recovered_amounts(seeded):
    result = run(page_size=150, write_progress=False, use_ai=False)
    with seeded.cursor() as cur:
        cur.execute("SELECT coalesce(sum(amount), 0) FROM transactions WHERE status = 'recovered'")
        db_total = cur.fetchone()[0]
    assert result.recovered_paise == db_total


def test_status_and_decision_action_are_consistent(seeded):
    run(page_size=150, write_progress=False, use_ai=False)
    with seeded.cursor() as cur:
        # a stopped_permanent / reauth decision must leave the txn halted;
        # escalated -> escalated; retry -> recovered or halted
        cur.execute(
            """
            SELECT ad.action_taken, t.status, count(*)
            FROM agent_decisions ad JOIN transactions t USING (transaction_id)
            GROUP BY 1, 2 ORDER BY 1, 2
            """
        )
        pairs = {(a, s) for a, s, _ in cur.fetchall()}
    allowed = {
        ("retry_scheduled", "recovered"), ("retry_scheduled", "halted"),
        ("reauth_link_sent", "halted"),
        ("stopped_permanent", "halted"),
        ("escalated", "escalated"),
    }
    assert pairs <= allowed


def test_no_stopped_decision_is_missing_a_rule(seeded):
    run(page_size=150, write_progress=False, use_ai=False)
    with seeded.cursor() as cur:
        cur.execute("SELECT count(*) FROM agent_decisions WHERE rule_fired IS NULL OR rule_fired = ''")
        assert cur.fetchone()[0] == 0


# ── step 5: stopping rules are traceable + the circuit breaker works ─────────
def test_every_stopped_permanent_decision_has_a_rule_and_an_audit_row(seeded):
    run(page_size=150, write_progress=False, use_ai=False)
    with seeded.cursor() as cur:
        cur.execute("SELECT count(*) FROM agent_decisions WHERE action_taken = 'stopped_permanent'")
        assert cur.fetchone()[0] > 0  # the seed actually produces some
        cur.execute(
            """
            SELECT count(*) FROM agent_decisions ad
            WHERE ad.action_taken = 'stopped_permanent'
              AND (
                ad.rule_fired IS NULL OR ad.rule_fired = ''
                OR NOT EXISTS (
                    SELECT 1 FROM audit_log al
                    WHERE al.transaction_id = ad.transaction_id
                      AND al.event_type = 'agent_decision'
                )
              )
            """
        )
        assert cur.fetchone()[0] == 0


def _quarter_of_every_page_fails(conn, plans):
    n = len(plans) // 4 + 1  # ~25% -> over the 20% threshold and past the error floor
    return {str(p.transaction_id): RuntimeError("forced bad row") for p in plans[:n]}


def test_circuit_breaker_stops_the_worker_on_a_bad_page(seeded, monkeypatch):
    from src import worker

    monkeypatch.setattr(worker, "write_page", _quarter_of_every_page_fails)
    with pytest.raises(worker.CircuitBreakerTripped):
        run(page_size=150, write_progress=False, use_ai=False)

    # nothing was committed on the aborted page
    decisions, _, pending = _counts(seeded)
    assert decisions == 0 and pending == SEED_N


def test_circuit_breaker_can_be_switched_off(seeded, monkeypatch):
    from src import worker

    monkeypatch.setattr(worker, "write_page", _quarter_of_every_page_fails)
    result = run(page_size=150, circuit_breaker=False, write_progress=False, use_ai=False)
    assert result.errors > 0  # errors were recorded, but the run ran to completion
