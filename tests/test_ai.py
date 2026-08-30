"""Unit tests for the AI layer (src/ai.py). No network — the client is disabled
when GEMINI_API_KEY is unset, which is exactly the fallback path we must prove.
"""

import pytest

from src.ai import AIClient, fallback_message
from src.ai import _parse_bucket


@pytest.fixture(autouse=True)
def _no_key(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)


def test_client_without_a_key_is_disabled():
    assert AIClient().enabled is False


def test_disabled_client_returns_a_fallback_result_not_an_error():
    ai = AIClient()
    r1 = ai.classify_decline_code("XX99", {"amount": 10_000, "mandate_category": "general"})
    assert r1.ok is False and r1.value is None and r1.status == "disabled"

    r2 = ai.draft_reauth_message(
        {"amount": 10_000, "mandate_category": "general", "rule_fired": "afa_threshold_exceeded"}
    )
    assert r2.ok is False and r2.status == "disabled"


def test_fallback_message_afa_variant_mentions_the_rule_and_amount():
    msg = fallback_message("afa_threshold_exceeded", {"amount": 2_500_00, "mandate_category": "insurance"})
    assert "RBI limit" in msg
    assert "2,500.00" in msg
    assert "insurance" in msg
    assert msg.strip().endswith("completed.") or "app" in msg


def test_fallback_message_compliance_variant_mentions_re_approval():
    msg = fallback_message("compliance_decline_no_retry", {"amount": 50_000, "mandate_category": "general"})
    assert "re-approved" in msg
    assert "500.00" in msg


@pytest.mark.parametrize("raw,expected", [
    ("soft", "soft"),
    ("SOFT\n", "soft"),
    ("The correct bucket is **compliance**.", "compliance"),
    ("`technical`", "technical"),
    ("hard.", "hard"),
    ("banana", None),
    ("", None),
])
def test_parse_bucket(raw, expected):
    assert _parse_bucket(raw) == expected
