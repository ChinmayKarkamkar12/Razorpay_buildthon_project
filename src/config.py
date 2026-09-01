"""Canonical configuration — the single source of truth for every hardcoded limit.

Per PRINCIPLES.md Rule 9 (explicit over implicit): retry caps, AFA thresholds, page size
and timeouts live here and nowhere else. Values are copied verbatim from
DECISION_RULES.md — do not change them here without changing that doc.

Money is ALWAYS integer paise (PRINCIPLES.md Rule 3).
"""

# ── AFA-free thresholds (RBI e-mandate additional factor of authentication) ──────
# Verified regulatory values. Amount strictly ABOVE the limit requires re-auth.
AFA_FREE_LIMIT_GENERAL = 1_500_000   # Rs 15,000 in paise
AFA_FREE_LIMIT_PRIORITY = 10_000_000  # Rs 1,00,000 in paise (SIP / insurance / CC-bill)

# Mandate categories that get the higher priority AFA-free limit.
PRIORITY_CATEGORIES = ("mutual_fund_sip", "insurance", "credit_card_bill")

# ── Retry policy ────────────────────────────────────────────────────────────────
# Verified: matches Razorpay's own T+1 / T+2 / T+3 retry-then-halt behavior.
MAX_RETRY_ATTEMPTS = 3
RETRY_SCHEDULE_DAYS = (1, 2, 3)       # soft declines: retry on these day offsets
TECHNICAL_RETRY_MINUTES = 60         # engineering default (not sourced)

# ── Worker / batch processing ──────────────────────────────────────────────────
PAGE_SIZE = 200                      # worker pagination size (PRINCIPLES.md Rule 6)
SEED_CHUNK_SIZE = 500               # seeder inserts in chunks of this many rows
CIRCUIT_BREAKER_THRESHOLD = 0.20     # pause worker if > 20% of a page errors

# ── Dashboard (Step 6 — read-only demo view) ───────────────────────────────────
DASHBOARD_PAGE_SIZE = 25             # rows per page in the dashboard transactions table
DASHBOARD_SUMMARY_TTL_SECONDS = 3    # cache the summary numbers this long between recomputes

# ── AI layer (bounded — see DECISION_RULES.md Step 5 / PRINCIPLES.md Rule 1) ───────
# The LLM only (a) drafts customer-facing re-auth messages and (b) SUGGESTS a bucket
# for a decline code missing from the taxonomy. It never decides an action, never
# overrides the AFA check or a retry cap. Key comes from GEMINI_API_KEY env only.
# flash-lite: ~1s/call and fully capable for these two tiny tasks. Plain "flash"
# averaged 5-20s on the free tier, which is too slow for the batch.
GEMINI_MODEL = "gemini-flash-lite-latest"
AI_MAX_CONCURRENCY = 5                  # cap on in-flight AI calls

# DECISION_RULES.md fixes AI_TIMEOUT_SECONDS at 8. Current Gemini endpoints reject an
# 8s HTTP deadline as "too short", so the 8s intent ("AI must never stall the batch")
# is enforced at the PIPELINE level: apply_ai abandons anything past the page
# deadline and falls back. AI_HTTP_TIMEOUT_SECONDS is only a dead-socket backstop.
AI_TIMEOUT_SECONDS = 8
AI_HTTP_TIMEOUT_SECONDS = 30
AI_PAGE_DEADLINE_SECONDS = 40           # hard cap on one page's whole AI phase
                                       # (only page 1 pays it — scenarios are cached)

# ── Mock PSP retry outcome (simulation only — NOT a real payment rail) ─────────
# Per-attempt probability that a scheduled retry succeeds at the mock gateway.
# Reasoned engineering guesses for a demo, NOT sourced statistics — say so if asked.
#   soft:      customer may top up between attempts, but often does not
#   technical: transient bank/network issue usually clears on a quick retry
RETRY_SUCCESS_PROB = {"soft": 0.35, "technical": 0.65}

# ── Decline code -> bucket taxonomy ────────────────────────────────────────────
# Modeled on documented PSP/NPCI integration guides (NPCI's full circular is not
# public). Any code NOT in this table maps to the most conservative bucket,
# 'compliance' (PRINCIPLES.md Rule 2 — fail toward caution).
DECLINE_CODE_BUCKETS = {
    # soft — genuine business decline, retry on schedule
    "Z9": "soft",     # insufficient funds
    "U19": "soft",    # per-transaction limit exceeded
    "Z7": "soft",     # frequency / velocity limit exceeded
    # hard — do not auto-retry, stop or prompt re-auth
    "ZM": "hard",     # wrong PIN (~24h lock)
    "ZA": "hard",     # declined by customer / bank
    "U01": "hard",    # invalid VPA
    "U69": "hard",    # collect request expired
    "ZE": "hard",     # mandate/permission not available
    "U66": "hard",    # device / registration issue
    # technical — transient, quick retry
    "U30": "technical",
    "U28": "technical",
    "U67": "technical",
    "YB": "technical",  # bank unavailable
    "M0": "technical",
    "XH": "technical",  # bank unavailable
    # compliance — regulation requires customer action, never auto-retry
    "V3": "compliance",
    "AFA_required": "compliance",
    "mandate_cancelled": "compliance",
    "mandate_expired": "compliance",
}

CONSERVATIVE_BUCKET = "compliance"


def afa_free_limit_for(category: str) -> int:
    """Return the AFA-free limit (paise) for a mandate category."""
    return AFA_FREE_LIMIT_PRIORITY if category in PRIORITY_CATEGORIES else AFA_FREE_LIMIT_GENERAL
