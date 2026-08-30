"""AI enrichment in the worker (src/worker.apply_ai) — pure, no DB.

Proves the two allowed AI jobs work AND that a slow / failing / absent model never
changes the routing decision and never blocks the page (CLAUDE.md Rules 1 & 2).
"""

import time
import uuid
from datetime import datetime, timezone

import pytest

from src.ai import AIClient, AIResult
from src.worker import WorkerRun, apply_ai, plan_row


def row(**over):
    base = {
        "transaction_id": uuid.uuid4(),
        "amount": 60_000,
        "attempt_count": 0,
        "scheduled_at": datetime(2026, 8, 1, tzinfo=timezone.utc),
        "mandate_category": "general",
        "decline_code": "Z9",
    }
    base.update(over)
    return base


class FakeAI:
    enabled = True

    def __init__(self, classify=None, message=None, delay=0.0):
        self._classify = classify or AIResult(True, "soft", "ok")
        self._message = message or AIResult(True, "Please re-approve the payment.", "ok")
        self._delay = delay

    def classify_decline_code(self, code, ctx):
        time.sleep(self._delay)
        return self._classify

    def draft_reauth_message(self, ctx):
        time.sleep(self._delay)
        return self._message


def _plans():
    return [
        plan_row(row(decline_code="XX99", amount=10_000)),          # unmapped -> classify
        plan_row(row(decline_code="mandate_expired", amount=10_000)),  # reauth -> message
        plan_row(row(decline_code="Z9", amount=60_000)),            # no AI task
    ]


def test_happy_path_fills_message_and_classification():
    unmapped, reauth, plain = _plans()
    run = WorkerRun()
    apply_ai([unmapped, reauth, plain], FakeAI(), run)

    assert unmapped.reasoning_source == "ai_classifier"
    assert unmapped.snapshot["ai_bucket_suggestion"] == "soft"
    assert reauth.snapshot["customer_message"] == "Please re-approve the payment."
    assert reauth.snapshot["message_source"] == "ai"
    assert run.ai_classifications == 1 and run.ai_messages == 1


def test_ai_never_changes_the_routing_decision():
    unmapped, reauth, _ = _plans()
    before = [(p.action, p.final_status, p.rule_fired) for p in (unmapped, reauth)]
    apply_ai([unmapped, reauth], FakeAI(classify=AIResult(True, "soft", "ok")), WorkerRun())
    after = [(p.action, p.final_status, p.rule_fired) for p in (unmapped, reauth)]
    assert before == after
    # unmapped stays on the conservative path no matter what the model said
    assert unmapped.action == "escalated"
    assert unmapped.final_status == "escalated"


def test_disabled_client_uses_templates_and_marks_fallback():
    monkey_off = AIClient(api_key=None)
    assert monkey_off.enabled is False
    unmapped, reauth, _ = _plans()
    run = WorkerRun()
    apply_ai([unmapped, reauth], monkey_off, run)

    assert unmapped.reasoning_source == "ai_fallback"
    assert unmapped.snapshot["ai_bucket_suggestion"] is None
    assert unmapped.snapshot["ai_status"] == "disabled"
    assert reauth.snapshot["message_source"] == "fallback"
    assert "re-approved" in reauth.snapshot["customer_message"]
    assert run.ai_fallbacks == 2


def test_slow_model_hits_the_page_deadline_and_falls_back(monkeypatch):
    monkeypatch.setattr("src.worker._PAGE_AI_DEADLINE", 0.5)
    unmapped, reauth, _ = _plans()
    run = WorkerRun()
    start = time.monotonic()
    apply_ai([unmapped, reauth], FakeAI(delay=5.0), run)
    elapsed = time.monotonic() - start

    assert elapsed < 3.0  # did NOT wait for the 5s calls
    assert unmapped.reasoning_source == "ai_fallback"
    assert reauth.snapshot["message_source"] == "fallback"
    assert unmapped.snapshot["ai_status"] == "timeout"


def test_model_error_falls_back_cleanly():
    unmapped, reauth, _ = _plans()
    run = WorkerRun()
    apply_ai(
        [unmapped, reauth],
        FakeAI(
            classify=AIResult(False, None, "error:ServerError"),
            message=AIResult(False, None, "error:ServerError"),
        ),
        run,
    )
    assert unmapped.reasoning_source == "ai_fallback"
    assert reauth.snapshot["message_source"] == "fallback"
    assert run.ai_fallbacks == 2


def test_unparseable_classification_falls_back():
    unmapped = plan_row(row(decline_code="ZZZ_WAT", amount=10_000))
    run = WorkerRun()
    apply_ai([unmapped], FakeAI(classify=AIResult(False, None, "unparseable")), run)
    assert unmapped.reasoning_source == "ai_fallback"
    assert unmapped.snapshot["ai_status"] == "unparseable"
