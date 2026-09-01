"""Mock action executor.

This stands in for the real payment rail. NOTHING here touches a live gateway
(PRINCIPLES.md Rule 8). It only simulates what a PSP would return for a scheduled retry,
so the pipeline and dashboard have a realistic recovered-vs-halted split to show.

The rule engine's DECISION is deterministic (PRINCIPLES.md Rule 1). Only the simulated
bank response is randomised — and even that is seeded from the transaction id, so a
given batch always produces the same numbers.
"""

import random
from dataclasses import dataclass

from src.config import MAX_RETRY_ATTEMPTS, RETRY_SUCCESS_PROB


@dataclass(frozen=True)
class RetrySimulation:
    recovered: bool
    attempts_made: int          # how many retry attempts were simulated this run
    final_attempt_count: int    # transactions.attempt_count after this run


def rng_for(transaction_id: str) -> random.Random:
    """Deterministic per-transaction RNG so batch results are reproducible."""
    return random.Random(f"recovery-coordinator::{transaction_id}")


def simulate_retry_schedule(
    *,
    transaction_id: str,
    bucket: str,
    attempt_count: int,
) -> RetrySimulation:
    """Simulate the remaining T+1/T+2/T+3 retries for a 'retry_scheduled' decision.

    Runs at most (MAX_RETRY_ATTEMPTS - attempt_count) mock attempts, stopping at the
    first success. Buckets other than soft/technical never reach here (hard/compliance
    do not retry) — if one does, treat it as unrecoverable, no attempts made.
    """
    per_attempt_p = RETRY_SUCCESS_PROB.get(bucket)
    remaining = max(0, MAX_RETRY_ATTEMPTS - attempt_count)

    if per_attempt_p is None or remaining == 0:
        return RetrySimulation(False, 0, attempt_count)

    rng = rng_for(transaction_id)
    made = 0
    for _ in range(remaining):
        made += 1
        if rng.random() < per_attempt_p:
            return RetrySimulation(True, made, attempt_count + made)
    return RetrySimulation(False, made, attempt_count + made)
