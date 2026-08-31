"""Step 5 — stopping rules as hard guardrails (steps/05_stopping_rules.md).

Step 2 already implements the retry caps and the hard/compliance no-retry rules
inside `decide_action`. This file locks them down with *explicit* tests instead of
assuming they follow from step 2, and covers the page-level circuit breaker added
to the worker in step 3.

The cases the step file calls out are marked `# STEP 5` and must not be deleted or
weakened.
"""

import uuid
from datetime import datetime, timezone

import pytest

from src.ai import AIClient, AIResult
from src.config import CIRCUIT_BREAKER_THRESHOLD, MAX_RETRY_ATTEMPTS
from src.rules import (
    ACTION_ESCALATED,
    ACTION_RETRY_SCHEDULED,
    ACTION_STOPPED_PERMANENT,
    AFA_NOT_REQUIRED,
    AFA_REQUIRED,
    decide_action,
)
from src.worker import (
    CIRCUIT_BREAKER_MIN_ERRORS,
    WorkerRun,
    _circuit_tripped,
    apply_ai,
    plan_row,
)


def _row(**over):
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


# ── retry cap can never be exceeded ──────────────────────────────────────────
@pytest.mark.parametrize("bucket", ["soft", "technical"])
@pytest.mark.parametrize(
    "attempt_count", [MAX_RETRY_ATTEMPTS, MAX_RETRY_ATTEMPTS + 1, 10, 99]
)
def test_retryable_buckets_never_retry_at_or_past_the_cap(bucket, attempt_count):  # STEP 5
    d = decide_action(bucket, AFA_NOT_REQUIRED, attempt_count=attempt_count)
    assert d.action != ACTION_RETRY_SCHEDULED
    assert d.retry_after is None


@pytest.mark.parametrize("bucket", ["soft", "technical"])
def test_retryable_buckets_do_retry_one_below_the_cap(bucket):
    d = decide_action(bucket, AFA_NOT_REQUIRED, attempt_count=MAX_RETRY_ATTEMPTS - 1)
    assert d.action == ACTION_RETRY_SCHEDULED


# ── hard / compliance never schedule a retry, whatever the inputs ────────────
@pytest.mark.parametrize("bucket", ["hard", "compliance"])
@pytest.mark.parametrize("afa", [AFA_NOT_REQUIRED, AFA_REQUIRED])
@pytest.mark.parametrize("attempt_count", [0, 1, 2, 3, 50])
@pytest.mark.parametrize("mandate_issue", [False, True])
def test_hard_and_compliance_never_schedule_a_retry(
    bucket, afa, attempt_count, mandate_issue
):  # STEP 5
    d = decide_action(
        bucket, afa, attempt_count=attempt_count, is_mandate_issue=mandate_issue
    )
    assert d.action != ACTION_RETRY_SCHEDULED
    assert d.retry_after is None
    assert d.rule_fired  # a stop with no reason is a bug


# ── step 5 testing sequence, at the planning layer ──────────────────────────
def test_soft_decline_already_at_cap_stops_instead_of_a_fourth_retry():  # STEP 5
    p = plan_row(_row(decline_code="Z9", attempt_count=MAX_RETRY_ATTEMPTS))
    assert p.action == ACTION_STOPPED_PERMANENT
    assert p.rule_fired == "soft_retries_exhausted"
    assert p.final_status == "halted"
    assert p.next_action_at is None


def test_hard_decline_from_attempt_zero_never_gets_a_retry():  # STEP 5
    p = plan_row(_row(decline_code="ZA", attempt_count=0))
    assert p.action == ACTION_STOPPED_PERMANENT
    assert p.next_action_at is None


# ── the AI layer (step 4) cannot loosen a stop ──────────────────────────────
class _FakeAI:
    """Model that eagerly calls everything 'soft' — the most retry-happy answer."""

    enabled = True

    def classify_decline_code(self, code, ctx):
        return AIResult(True, "soft", "ok")

    def draft_reauth_message(self, ctx):
        return AIResult(True, "Please re-approve.", "ok")


def test_ai_layer_leaves_a_capped_soft_decline_terminal():  # STEP 5
    p = plan_row(_row(decline_code="Z9", attempt_count=MAX_RETRY_ATTEMPTS))
    assert p.ai_task is None  # a terminal stop asks for no AI help
    before = (p.action, p.final_status, p.attempt_count_after, p.rule_fired)
    apply_ai([p], AIClient.disabled(), {}, WorkerRun())
    apply_ai([p], _FakeAI(), {}, WorkerRun())
    assert (p.action, p.final_status, p.attempt_count_after, p.rule_fired) == before


def test_ai_calling_an_unknown_code_soft_does_not_produce_a_retry():  # STEP 5
    p = plan_row(_row(decline_code="WAT_UNKNOWN_CODE", amount=10_000))
    apply_ai([p], _FakeAI(), {}, WorkerRun())
    assert p.snapshot["ai_bucket_suggestion"] == "soft"  # model answered
    assert p.action == ACTION_ESCALATED  # routing unchanged
    assert p.final_status == "escalated"
    assert p.next_action_at is None


# ── page-level circuit breaker predicate ────────────────────────────────────
def test_circuit_breaker_needs_both_the_ratio_and_the_floor():
    assert _circuit_tripped(page_errors=50, page_len=200) is True  # 25%, over the floor
    assert _circuit_tripped(page_errors=41, page_len=200) is True  # just over 20%
    assert _circuit_tripped(page_errors=40, page_len=200) is False  # exactly 20%, not over
    # 40% of a tiny page, but below the absolute error floor -> does not trip
    assert _circuit_tripped(page_errors=CIRCUIT_BREAKER_MIN_ERRORS - 1, page_len=10) is False
    assert _circuit_tripped(page_errors=0, page_len=0) is False


def test_circuit_breaker_threshold_constant_is_twenty_percent():
    assert CIRCUIT_BREAKER_THRESHOLD == 0.20
