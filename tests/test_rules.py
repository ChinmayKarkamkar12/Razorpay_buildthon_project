"""Unit tests for the deterministic rule engine (src/rules.py).

The six cases required by steps/02_rule_engine.md are marked `# REQUIRED` and must
never be deleted or weakened. Everything else is additional edge coverage.
"""

import pytest

from src.config import MAX_RETRY_ATTEMPTS
from src.rules import (
    ACTION_ESCALATED,
    ACTION_REAUTH_LINK_SENT,
    ACTION_RETRY_SCHEDULED,
    ACTION_STOPPED_PERMANENT,
    AFA_NOT_REQUIRED,
    AFA_REQUIRED,
    check_afa_threshold,
    classify,
    decide_action,
    is_mandate_issue,
)

# ── Step 2: AFA threshold boundaries ──────────────────────────────────────────

def test_amount_exactly_at_general_limit_does_not_require_afa():  # REQUIRED
    assert check_afa_threshold(1_500_000, "general") == AFA_NOT_REQUIRED


def test_one_paisa_over_general_limit_requires_afa():  # REQUIRED
    assert check_afa_threshold(1_500_001, "general") == AFA_REQUIRED


def test_amount_exactly_at_priority_limit_for_sip_does_not_require_afa():  # REQUIRED
    assert check_afa_threshold(10_000_000, "mutual_fund_sip") == AFA_NOT_REQUIRED


@pytest.mark.parametrize("category", ["mutual_fund_sip", "insurance", "credit_card_bill"])
def test_priority_categories_use_the_higher_limit(category):
    assert check_afa_threshold(1_500_001, category) == AFA_NOT_REQUIRED
    assert check_afa_threshold(10_000_001, category) == AFA_REQUIRED


def test_general_category_uses_the_lower_limit():
    assert check_afa_threshold(9_999_999, "general") == AFA_REQUIRED


def test_zero_amount_never_requires_afa():
    assert check_afa_threshold(0, "general") == AFA_NOT_REQUIRED


def test_amount_must_be_integer_paise():
    with pytest.raises(TypeError):
        check_afa_threshold(15000.0, "general")


# ── Step 1: decline code classification ──────────────────────────────────────

def test_unmapped_decline_code_defaults_to_compliance():  # REQUIRED
    result = classify("TOTALLY_UNKNOWN_CODE")
    assert result == "compliance"


@pytest.mark.parametrize("code,bucket", [
    ("Z9", "soft"), ("U19", "soft"), ("Z7", "soft"),
    ("ZM", "hard"), ("ZA", "hard"), ("U01", "hard"),
    ("U30", "technical"), ("YB", "technical"), ("M0", "technical"),
    ("V3", "compliance"), ("mandate_cancelled", "compliance"), ("mandate_expired", "compliance"),
])
def test_known_codes_map_to_expected_bucket(code, bucket):
    assert classify(code) == bucket


def test_classify_never_raises_on_none_or_blank():
    assert classify(None) == "compliance"
    assert classify("") == "compliance"
    assert classify("   ") == "compliance"


def test_classify_tolerates_surrounding_whitespace():
    assert classify("  Z9 ") == "soft"


def test_is_mandate_issue_only_true_for_known_compliance_codes():
    assert is_mandate_issue("mandate_expired") is True
    assert is_mandate_issue("V3") is True
    assert is_mandate_issue("UNKNOWN") is False   # fell through to compliance, not a mandate issue
    assert is_mandate_issue("Z9") is False


# ── Step 3: decide_action ────────────────────────────────────────────────────

def test_soft_decline_at_retry_cap_stops_instead_of_retrying():  # REQUIRED
    d = decide_action("soft", AFA_NOT_REQUIRED, attempt_count=MAX_RETRY_ATTEMPTS)
    assert d.action == ACTION_STOPPED_PERMANENT
    assert d.rule_fired == "soft_retries_exhausted"


@pytest.mark.parametrize("attempt_count", [0, 1, 2, 3, 4, 99])
def test_hard_decline_never_retries_regardless_of_attempt_count(attempt_count):  # REQUIRED
    d = decide_action("hard", AFA_NOT_REQUIRED, attempt_count=attempt_count)
    assert d.action in (ACTION_STOPPED_PERMANENT, ACTION_REAUTH_LINK_SENT)
    assert d.action != ACTION_RETRY_SCHEDULED


@pytest.mark.parametrize("attempt_count", [0, 1, 2])
def test_soft_decline_below_cap_schedules_a_retry_on_the_right_day(attempt_count):
    d = decide_action("soft", AFA_NOT_REQUIRED, attempt_count=attempt_count)
    assert d.action == ACTION_RETRY_SCHEDULED
    assert d.rule_fired == "soft_decline_scheduled_retry"
    assert d.retry_after.days == attempt_count + 1   # T+1, T+2, T+3


def test_technical_decline_retries_quickly_then_escalates():
    early = decide_action("technical", AFA_NOT_REQUIRED, attempt_count=0)
    assert early.action == ACTION_RETRY_SCHEDULED
    assert early.retry_after.total_seconds() == 60 * 60

    exhausted = decide_action("technical", AFA_NOT_REQUIRED, attempt_count=MAX_RETRY_ATTEMPTS)
    assert exhausted.action == ACTION_ESCALATED
    assert exhausted.rule_fired == "technical_retries_exhausted"


def test_afa_required_outranks_a_soft_bucket():
    # Even a retryable "insufficient funds" above the threshold must go to re-auth.
    d = decide_action("soft", AFA_REQUIRED, attempt_count=0)
    assert d.action == ACTION_REAUTH_LINK_SENT
    assert d.rule_fired == "afa_threshold_exceeded"
    assert d.retry_after is None


def test_afa_required_outranks_a_hard_bucket_too():
    d = decide_action("hard", AFA_REQUIRED, attempt_count=0)
    assert d.action == ACTION_REAUTH_LINK_SENT


def test_compliance_mandate_issue_sends_reauth_link():
    d = decide_action("compliance", AFA_NOT_REQUIRED, attempt_count=0, is_mandate_issue=True)
    assert d.action == ACTION_REAUTH_LINK_SENT
    assert d.rule_fired == "compliance_decline_no_retry"


def test_compliance_unknown_code_escalates_to_human():
    d = decide_action("compliance", AFA_NOT_REQUIRED, attempt_count=0, is_mandate_issue=False)
    assert d.action == ACTION_ESCALATED
    assert d.rule_fired == "compliance_decline_no_retry"


def test_no_decision_is_ever_missing_a_rule_fired():
    for bucket in ("soft", "hard", "technical", "compliance"):
        for afa in (AFA_REQUIRED, AFA_NOT_REQUIRED):
            for ac in (0, 3):
                d = decide_action(bucket, afa, attempt_count=ac)
                assert d.rule_fired  # never empty — a stop with no reason is a bug


def test_terminal_actions_carry_no_retry_delay():
    for d in (
        decide_action("hard", AFA_NOT_REQUIRED, 0),
        decide_action("soft", AFA_NOT_REQUIRED, MAX_RETRY_ATTEMPTS),
        decide_action("compliance", AFA_NOT_REQUIRED, 0),
    ):
        assert d.retry_after is None


# ── purity: functions must not touch DB / network / clock ────────────────────

def test_rules_module_has_no_io_imports():
    import src.rules as rules_mod

    src = __import__("inspect").getsource(rules_mod)
    for forbidden in ("import psycopg", "import requests", "import socket", "datetime.now", "time.time"):
        assert forbidden not in src, f"rule engine must stay pure: found {forbidden!r}"
