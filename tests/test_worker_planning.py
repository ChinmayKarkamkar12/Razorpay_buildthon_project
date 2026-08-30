"""Unit tests for worker planning (src/worker.plan_row).

Pure: rule engine + mock execution + snapshot building with fake DB rows, no
connection and no AI.
"""

import json
import uuid
from datetime import datetime, timezone

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
    assert p.action == ACTION_RETRY_SCHEDULED
    assert p.next_action_at is not None
    assert p.final_status in ("recovered", "halted")
    assert p.ai_task is None


def test_amount_over_afa_threshold_forces_reauth_regardless_of_bucket():
    p = plan_row(row(decline_code="Z9", amount=1_500_001, mandate_category="general"))
    assert p.action == ACTION_REAUTH_LINK_SENT
    assert p.rule_fired == "afa_threshold_exceeded"
    assert p.final_status == "halted"
    assert p.next_action_at is None
    assert p.ai_task == "message"


def test_hard_decline_stops_permanently_and_halts():
    p = plan_row(row(decline_code="ZA", amount=10_000))
    assert p.action == ACTION_STOPPED_PERMANENT
    assert p.final_status == "halted"
    assert p.ai_task is None


def test_known_compliance_code_sends_reauth_link_and_needs_a_message():
    p = plan_row(row(decline_code="mandate_expired", amount=10_000))
    assert p.action == ACTION_REAUTH_LINK_SENT
    assert p.rule_fired == "compliance_decline_no_retry"
    assert p.final_status == "halted"
    assert p.ai_task == "message"
    assert p.reasoning_source == "deterministic_rule"


def test_unknown_decline_code_escalates_and_asks_ai_to_classify():
    p = plan_row(row(decline_code="XX99", amount=10_000))
    assert p.action == ACTION_ESCALATED
    assert p.final_status == "escalated"
    assert p.ai_task == "classify"
    assert p.reasoning_source == "ai_fallback"     # until the model answers
    assert p.snapshot["decline_code_mapped"] is False


def test_technical_retries_exhausted_escalates():
    p = plan_row(row(decline_code="YB", amount=10_000, attempt_count=3))
    assert p.action == ACTION_ESCALATED
    assert p.final_status == "escalated"


def test_snapshot_is_json_serialisable_and_complete():
    p = plan_row(row(decline_code="Z9"))
    json.dumps(p.snapshot)  # must not raise
    for key in (
        "transaction_id", "amount_paise", "decline_code", "decline_code_mapped",
        "bucket", "afa_check", "rule_fired", "action_taken", "reasoning_source",
        "attempt_count_before", "attempt_count_after", "status_before",
        "status_after", "outcome", "ai_bucket_suggestion", "customer_message",
        "message_source", "ai_status", "decided_at",
    ):
        assert key in p.snapshot
    assert p.snapshot["reasoning_source"] == "deterministic_rule"


def test_attempt_count_only_advances_on_retries():
    stop = plan_row(row(decline_code="ZA", attempt_count=1))
    assert stop.attempt_count_after == 1

    retry = plan_row(row(decline_code="YB", attempt_count=0))
    assert retry.attempt_count_after >= 1
