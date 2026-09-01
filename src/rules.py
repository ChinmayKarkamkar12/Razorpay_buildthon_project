"""Deterministic policy engine — the compliance-critical core.

Implements DECISION_RULES.md directly as PURE functions: same input always yields the
same output, no database, no network, no AI, no clock reads. This is what PRINCIPLES.md
Rule 1 means by "deterministic rules before AI, always" — nothing in here may ever be
replaced by an LLM call.

Public API:
    classify(decline_code)                       -> bucket
    check_afa_threshold(amount, mandate_category) -> "AFA_REQUIRED" | "AFA_NOT_REQUIRED"
    decide_action(bucket, afa_check, attempt_count, *, is_mandate_issue=False)
                                                 -> RuleDecision
"""

from dataclasses import dataclass
from datetime import timedelta

from src.config import (
    CONSERVATIVE_BUCKET,
    DECLINE_CODE_BUCKETS,
    MAX_RETRY_ATTEMPTS,
    RETRY_SCHEDULE_DAYS,
    TECHNICAL_RETRY_MINUTES,
    afa_free_limit_for,
)

# ── action + afa constants (values match the DB enums in SCHEMA.sql) ────────────
ACTION_RETRY_SCHEDULED = "retry_scheduled"
ACTION_REAUTH_LINK_SENT = "reauth_link_sent"
ACTION_ESCALATED = "escalated"
ACTION_STOPPED_PERMANENT = "stopped_permanent"

AFA_REQUIRED = "AFA_REQUIRED"
AFA_NOT_REQUIRED = "AFA_NOT_REQUIRED"

VALID_BUCKETS = ("soft", "hard", "technical", "compliance")


@dataclass(frozen=True)
class RuleDecision:
    """Result of decide_action. `retry_after` is the delay until the next attempt
    (None for terminal actions) — step 3 turns it into an absolute next_action_at."""

    action: str
    rule_fired: str
    retry_after: timedelta | None = None


# ── Step 1 — decline code -> bucket ────────────────────────────────────────────
def classify(decline_code: str | None) -> str:
    """Map a decline code to its bucket. Any code not in the hardcoded taxonomy
    returns the most conservative bucket ('compliance') — never an error, never a
    guess (PRINCIPLES.md Rule 2)."""
    if not decline_code:
        return CONSERVATIVE_BUCKET
    return DECLINE_CODE_BUCKETS.get(decline_code.strip(), CONSERVATIVE_BUCKET)


def is_mandate_issue(decline_code: str | None) -> bool:
    """True only for a *known* compliance code (mandate cancelled/expired, AFA
    required, etc.) — those route to a re-auth / re-registration link. An unknown
    code that merely fell through to the compliance bucket is NOT a mandate issue;
    it goes to a human (escalated)."""
    if not decline_code:
        return False
    return DECLINE_CODE_BUCKETS.get(decline_code.strip()) == "compliance"


# ── Step 2 — AFA threshold check (independent of, and prior to, the bucket) ─────
def check_afa_threshold(amount: int, mandate_category: str) -> str:
    """Amount STRICTLY ABOVE the category's AFA-free limit requires re-authentication.
    Exactly at the limit does not. `amount` is integer paise (PRINCIPLES.md Rule 3)."""
    if not isinstance(amount, int):
        raise TypeError(f"amount must be int paise, got {type(amount).__name__}")
    limit = afa_free_limit_for(mandate_category)
    return AFA_REQUIRED if amount > limit else AFA_NOT_REQUIRED


# ── Step 3 — decide the bounded action ─────────────────────────────────────────
def decide_action(
    bucket: str,
    afa_check: str,
    attempt_count: int,
    *,
    is_mandate_issue: bool = False,
) -> RuleDecision:
    """Pure decision function. Order matters: the AFA check outranks the bucket.

    Guardrails enforced here and nowhere else:
      * hard / compliance declines never produce a retry
      * soft / technical declines retry at most MAX_RETRY_ATTEMPTS times
    """
    # AFA re-auth requirement takes precedence over everything — retrying a payment
    # above the threshold is pointless until the customer re-authenticates.
    if afa_check == AFA_REQUIRED:
        return RuleDecision(ACTION_REAUTH_LINK_SENT, "afa_threshold_exceeded")

    if bucket == "compliance":
        if is_mandate_issue:
            return RuleDecision(ACTION_REAUTH_LINK_SENT, "compliance_decline_no_retry")
        return RuleDecision(ACTION_ESCALATED, "compliance_decline_no_retry")

    if bucket == "hard":
        return RuleDecision(ACTION_STOPPED_PERMANENT, "hard_decline_no_retry")

    if bucket == "technical":
        if attempt_count < MAX_RETRY_ATTEMPTS:
            return RuleDecision(
                ACTION_RETRY_SCHEDULED,
                "technical_quick_retry",
                timedelta(minutes=TECHNICAL_RETRY_MINUTES),
            )
        return RuleDecision(ACTION_ESCALATED, "technical_retries_exhausted")

    if bucket == "soft":
        if attempt_count < MAX_RETRY_ATTEMPTS:
            next_day = RETRY_SCHEDULE_DAYS[attempt_count]
            return RuleDecision(
                ACTION_RETRY_SCHEDULED,
                "soft_decline_scheduled_retry",
                timedelta(days=next_day),
            )
        return RuleDecision(ACTION_STOPPED_PERMANENT, "soft_retries_exhausted")

    # Unreachable if the bucket taxonomy stays exhaustive. If somehow reached, treat
    # as the conservative path — escalate to a human, never silently retry.
    return RuleDecision(ACTION_ESCALATED, "unclassified_fallback")
