"""Unit tests for worker planning (src/worker.plan_row + _resolve_outcome).

Pure: exercises rule engine + mock execution + snapshot building with fake DB rows,
no connection required.
"""

import json
import uuid
from datetime import datetime, timezone

import pytest

from src.rules import (
    ACTION_ESCALATED,
    ACTION_REAUTH_LINK_SENT,
    ACTION_RETRY_SCHEDULED,
    ACTION_STOPPED_PERMANENT,
)
from src.worker import plan_row


def row(**over):
    base = {
        "transaction_id": uuid.uuid4(),
        "amount": 50_000,
        "attempt_count": 0,
        "scheduled_at": datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc),
        "mandate_category": "general",
        "decline_code": "Z9",
    }
    base.update(over)
    return base


def test_soft_under_threshold_plans_a_retry_and_a_terminal_status():
    p = plan_row(row(decline_code="Z9", amount=50_000))
    assert p.params["action"] == ACTION_RETRY_SCHEDULED
    assert p.params["next_at"] is not None
    assert p.final_status in ("recovered", "halted")


def test_amount_over_afa_threshold_forces_reauth_regardless_of_bucket():
    p = plan_row(row(decline_code="Z9", amount=1_500_001, mandate_category="general"))
    assert p.params["action"] == ACTION_REAUTH_LINK_SENT
    assert p.params["rule"] == "afa_threshold_exceeded"
    assert p.final_status == "halted"
    assert p.params["next_at"] is None


def test_hard_decline_stops_permanently_and_halts():
    p = plan_row(row(decline_code="ZA", amount=10_000))
    assert p.params["action"] == ACTION_STOPPED_PERMANENT
    assert p.final_status == "halted"


def test_known_compliance_code_sends_reauth_link():
    p = plan_row(row(decline_code="mandate_expired", amount=10_000))
    assert p.params["action"] == ACTION_REAUTH_LINK_SENT
    assert p.params["rule"] == "compliance_decline_no_retry"
    assert p.final_status == "halted"


def test_unknown_decline_code_escalates_to_human():
    p = plan_row(row(decline_code="XX99", amount=10_000))
    assert p.params["action"] == ACTION_ESCALATED
    assert p.final_status == "escalated"


def test_technical_retries_exhausted_escalates():
    p = plan_row(row(decline_code="YB", amount=10_000, attempt_count=3))
    assert p.params["action"] == ACTION_ESCALATED
    assert p.final_status == "escalated"


def test_snapshot_is_json_serialisable_and_complete():
    p = plan_row(row(decline_code="Z9"))
    snap = p.params["snapshot"].obj  # unwrap Jsonb
    json.dumps(snap)  # must not raise
    for key in (
        "transaction_id", "amount_paise", "decline_code", "bucket", "afa_check",
        "rule_fired", "action_taken", "reasoning_source",
        "attempt_count_before", "attempt_count_after", "status_before",
        "status_after", "outcome", "decided_at",
    ):
        assert key in snap
    assert snap["reasoning_source"] == "deterministic_rule"


def test_reauth_snapshot_records_no_retry_simulation():
    p = plan_row(row(decline_code="mandate_expired"))
    assert p.params["snapshot"].obj["retry_simulation"] is None


def test_attempt_count_only_advances_on_retries():
    stop = plan_row(row(decline_code="ZA", attempt_count=1))
    assert stop.params["attempts"] == 1  # unchanged

    retry = plan_row(row(decline_code="YB", attempt_count=0))
    assert retry.params["attempts"] >= 1  # advanced by simulated attempts
