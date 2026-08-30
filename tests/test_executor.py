"""Unit tests for the mock retry executor (src/executor.py). No DB, no network."""

from src.config import MAX_RETRY_ATTEMPTS
from src.executor import rng_for, simulate_retry_schedule


def _sim(txn="t-1", bucket="soft", attempt_count=0):
    return simulate_retry_schedule(transaction_id=txn, bucket=bucket, attempt_count=attempt_count)


def test_result_is_deterministic_for_a_given_transaction_id():
    a = _sim("txn-abc", "soft", 0)
    b = _sim("txn-abc", "soft", 0)
    assert a == b


def test_different_transactions_can_differ():
    outcomes = {_sim(f"txn-{i}", "soft", 0).recovered for i in range(50)}
    assert outcomes == {True, False}  # not all the same


def test_never_exceeds_the_remaining_retry_budget():
    for attempt_count in range(0, MAX_RETRY_ATTEMPTS + 2):
        for i in range(30):
            sim = _sim(f"txn-{i}", "soft", attempt_count)
            remaining = max(0, MAX_RETRY_ATTEMPTS - attempt_count)
            assert sim.attempts_made <= remaining
            assert sim.final_attempt_count == attempt_count + sim.attempts_made


def test_at_the_cap_there_is_no_attempt_and_no_recovery():
    sim = _sim("txn-x", "soft", MAX_RETRY_ATTEMPTS)
    assert sim.attempts_made == 0
    assert sim.recovered is False
    assert sim.final_attempt_count == MAX_RETRY_ATTEMPTS


def test_non_retryable_bucket_never_recovers_here():
    for bucket in ("hard", "compliance"):
        sim = _sim("txn-y", bucket, 0)
        assert sim.attempts_made == 0
        assert sim.recovered is False


def test_technical_recovers_more_often_than_soft():
    def rate(bucket):
        hits = sum(_sim(f"txn-{i}", bucket, 0).recovered for i in range(400))
        return hits / 400

    assert rate("technical") > rate("soft")


def test_rng_is_seeded_and_repeatable():
    assert rng_for("abc").random() == rng_for("abc").random()
