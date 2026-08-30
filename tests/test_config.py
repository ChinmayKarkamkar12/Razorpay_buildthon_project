"""Guards that src/config.py stays in lockstep with DECISION_RULES.md.

If the policy doc changes, these break — forcing a deliberate update rather than
silent drift between the spec and the code.
"""

from src.config import (
    AFA_FREE_LIMIT_GENERAL,
    AFA_FREE_LIMIT_PRIORITY,
    DECLINE_CODE_BUCKETS,
    MAX_RETRY_ATTEMPTS,
    RETRY_SCHEDULE_DAYS,
    afa_free_limit_for,
)

# ── constants (DECISION_RULES.md "Constants" block) ───────────────────────────

def test_afa_limits_match_the_regulation():
    assert AFA_FREE_LIMIT_GENERAL == 1_500_000     # Rs 15,000 in paise
    assert AFA_FREE_LIMIT_PRIORITY == 10_000_000   # Rs 1,00,000 in paise


def test_retry_policy_matches_razorpay_t1_t2_t3():
    assert MAX_RETRY_ATTEMPTS == 3
    assert tuple(RETRY_SCHEDULE_DAYS) == (1, 2, 3)


def test_priority_categories_get_the_higher_limit():
    for cat in ("mutual_fund_sip", "insurance", "credit_card_bill"):
        assert afa_free_limit_for(cat) == AFA_FREE_LIMIT_PRIORITY
    assert afa_free_limit_for("general") == AFA_FREE_LIMIT_GENERAL


# ── decline-code taxonomy (DECISION_RULES.md Step 1 table) ────────────────────

EXPECTED_TAXONOMY = {
    "Z9": "soft", "U19": "soft", "Z7": "soft",
    "ZM": "hard", "ZA": "hard", "U01": "hard", "U69": "hard", "ZE": "hard", "U66": "hard",
    "U30": "technical", "U28": "technical", "U67": "technical", "XH": "technical",
    "YB": "technical", "M0": "technical",
    "V3": "compliance", "AFA_required": "compliance",
    "mandate_cancelled": "compliance", "mandate_expired": "compliance",
}


def test_declined_code_table_matches_decision_rules_doc():
    assert DECLINE_CODE_BUCKETS == EXPECTED_TAXONOMY


def test_every_bucket_is_a_valid_enum_value():
    assert set(DECLINE_CODE_BUCKETS.values()) <= {"soft", "hard", "technical", "compliance"}
