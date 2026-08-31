"""Step 6 — dashboard.

`humanize_decision` is pure and always runs. The route / query tests need a
populated database (RUN_DB_TESTS=1) and assume the worker has already produced
decisions on the current data.
"""

import os

import pytest

from src.config import DASHBOARD_PAGE_SIZE
from src.dashboard import humanize_decision


# ── pure: plain-language decision story ─────────────────────────────────────
def test_humanize_reads_a_mapped_soft_decline():
    story = humanize_decision(
        {
            "amount_paise": 5000_00,
            "mandate_category": "general",
            "decline_code": "Z9",
            "decline_code_mapped": True,
            "bucket": "soft",
            "afa_check": "AFA_NOT_REQUIRED",
            "rule_fired": "soft_decline_scheduled_retry",
            "action_taken": "retry_scheduled",
            "retry_simulation": {"attempts_made": 2, "recovered": True},
            "status_after": "recovered",
            "outcome": "recovered_on_retry",
            "reasoning_source": "deterministic_rule",
        }
    )
    joined = " ".join(story).lower()
    assert "z9" in joined and "soft" in joined
    assert "within the rbi auto-debit limit" in joined
    assert "2 attempt(s) made — succeeded" in joined
    assert "final status: recovered" in joined
    assert "no ai" in joined


def test_humanize_flags_an_unknown_code_with_ai_suggestion():
    story = " ".join(
        humanize_decision(
            {
                "decline_code": "XX99",
                "decline_code_mapped": False,
                "ai_bucket_suggestion": "soft",
                "bucket": "compliance",
                "action_taken": "escalated",
                "status_after": "escalated",
            }
        )
    ).lower()
    assert "not in our taxonomy" in story
    assert "routing stays conservative" in story


def test_humanize_never_crashes_on_a_sparse_snapshot():
    assert humanize_decision({}) == [
        "A the amount payment on a 'unknown' mandate was attempted and failed.",
        "Decline code 'unknown' maps to a 'unknown' decline.",
    ]
    assert humanize_decision(None) != []


# ── DB-backed: queries + Flask routes ──────────────────────────────────────
db = pytest.mark.skipif(os.environ.get("RUN_DB_TESTS") != "1", reason="set RUN_DB_TESTS=1")


@pytest.fixture
def client():
    from app import app

    app.config.update(TESTING=True)
    with app.test_client() as c:
        yield c


@db
def test_summary_numbers_add_up():
    from src.dashboard import get_summary

    s = get_summary(force=True)
    assert s["total"] == s["recovered"] + s["halted"] + s["escalated"] + s["pending"]
    assert s["recovered_rupees"] >= 0
    assert set(s["buckets"]) == {"soft", "hard", "technical", "compliance"}


@db
def test_transactions_page_is_always_bounded():
    from src.dashboard import get_transactions_page

    p = get_transactions_page(page=1)
    assert len(p["rows"]) <= DASHBOARD_PAGE_SIZE
    assert p["page"] == 1 and p["total_pages"] >= 1


@db
def test_status_filter_only_returns_that_status():
    from src.dashboard import get_transactions_page

    p = get_transactions_page(page=1, status="halted")
    assert all(r["status"] == "halted" for r in p["rows"])


@db
def test_index_and_transactions_routes_render(client):
    assert client.get("/").status_code == 200
    assert client.get("/transactions").status_code == 200
    assert client.get("/transactions?status=recovered").status_code == 200
    assert client.get("/transactions?status=bogus").status_code == 400


@db
def test_detail_route_shows_the_plain_language_story(client):
    from src.dashboard import get_transactions_page

    txn_id = get_transactions_page(page=1, status="escalated")["rows"][0]["transaction_id"]
    r = client.get(f"/transactions/{txn_id}")
    assert r.status_code == 200
    assert b"What the agent did, and why" in r.data


@db
def test_unknown_transaction_is_404(client):
    assert client.get("/transactions/00000000-0000-0000-0000-000000000000").status_code == 404
