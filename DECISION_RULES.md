# Decision Rules — Policy Engine Spec

This is the canonical source of truth for the agent's decision logic. Implement this
as plain, unit-testable code — **not** as an LLM prompt. See `CLAUDE.md` Rule 1.

## Constants (single config module — no magic numbers elsewhere)

```
AFA_FREE_LIMIT_GENERAL   = 1_500_000   -- ₹15,000 in paise
AFA_FREE_LIMIT_PRIORITY  = 10_000_000  -- ₹1,00,000 in paise (SIP/insurance/CC-bill)
MAX_RETRY_ATTEMPTS       = 3           -- matches verified Razorpay T+1/T+2/T+3 behavior
RETRY_SCHEDULE_DAYS      = [1, 2, 3]   -- daily retries, matching Razorpay's own
                                        -- verified T+1/T+2/T+3 pattern exactly —
                                        -- deliberately NOT spread out, so this
                                        -- number is defensible if a judge who
                                        -- knows Razorpay's product asks about it
TECHNICAL_RETRY_MINUTES  = 60          -- quick retry window for technical declines
PAGE_SIZE                = 200         -- worker pagination size
AI_TIMEOUT_SECONDS       = 8
CIRCUIT_BREAKER_THRESHOLD = 0.20       -- pause worker if >20% of a page errors
```

### Note on these numbers

`MAX_RETRY_ATTEMPTS` and `RETRY_SCHEDULE_DAYS` are grounded in verified Razorpay
behavior — cite them with confidence. `TECHNICAL_RETRY_MINUTES`, `PAGE_SIZE`,
`AI_TIMEOUT_SECONDS`, and `CIRCUIT_BREAKER_THRESHOLD` are reasonable engineering
defaults, not sourced from research — if asked, the honest answer is "a sensible
default for a hackathon build," not "sourced." That's a fine answer to give; don't
overstate these as researched.

## Step 1 — Decline code → bucket mapping

Use a hardcoded lookup table (do not call AI for known codes). Suggested taxonomy —
present this to judges as "modeled on documented PSP/NPCI failure patterns," not as
verified official NPCI codes (public sources disagree; see note below).

| Code | Bucket | Notes |
|---|---|---|
| Z9 | soft | insufficient funds — retry on schedule |
| U19, Z7 | soft | limit/frequency exceeded — retry after 24h |
| ZM | hard (short-term) | wrong PIN, ~24h lock — stop, prompt reset |
| ZA | hard | declined by customer/bank — stop, no auto-retry |
| U01, U69, ZE, U66 | hard | invalid VPA / expired collect / permission / device — stop |
| U30, U28, U67, XH (bank unavailable), YB, M0 | technical | quick retry within `TECHNICAL_RETRY_MINUTES` |
| V3, AFA_required, mandate_cancelled, mandate_expired | compliance | never auto-retry — trigger re-auth or re-registration |
| *(any unmapped code)* | compliance | **default to most conservative bucket** — see CLAUDE.md Rule 2 |

> Note for the pitch: NPCI's full Operating Circulars aren't publicly available; this
> taxonomy is built from documented PSP integration guides. State this openly — it's
> a sign of rigor, not a weakness.

## Step 2 — AFA threshold check (runs independently of bucket)

```
function check_afa_threshold(amount, mandate_category):
    limit = AFA_FREE_LIMIT_PRIORITY if mandate_category in
            ['mutual_fund_sip', 'insurance', 'credit_card_bill']
            else AFA_FREE_LIMIT_GENERAL

    if amount > limit:
        return "AFA_REQUIRED"   -- must re-authenticate, retrying is pointless
    else:
        return "AFA_NOT_REQUIRED"
```

This check is **independent of and takes precedence over** the decline-code bucket.
Even a "soft" decline (e.g. insufficient funds) on a transaction above the threshold
still requires re-auth once the customer has funds — auto-retry alone can't satisfy
the regulation.

## Step 3 — Decide action

```
function decide_action(bucket, afa_check, attempt_count):

    if afa_check == "AFA_REQUIRED":
        return "reauth_link_sent", rule="afa_threshold_exceeded"

    if bucket == "compliance":
        return "reauth_link_sent" if is_mandate_issue else "escalated",
               rule="compliance_decline_no_retry"

    if bucket == "hard":
        return "stopped_permanent", rule="hard_decline_no_retry"

    if bucket == "technical":
        if attempt_count < MAX_RETRY_ATTEMPTS:
            return "retry_scheduled" (in TECHNICAL_RETRY_MINUTES),
                   rule="technical_quick_retry"
        else:
            return "escalated", rule="technical_retries_exhausted"

    if bucket == "soft":
        if attempt_count < MAX_RETRY_ATTEMPTS:
            next_day = RETRY_SCHEDULE_DAYS[attempt_count]
            return "retry_scheduled" (in next_day days),
                   rule="soft_decline_scheduled_retry"
        else:
            return "stopped_permanent", rule="soft_retries_exhausted"

    -- unreachable if bucket mapping is exhaustive; if reached, treat as compliance
    return "escalated", rule="unclassified_fallback"
```

## Step 4 — Stopping rules (hard guardrails, always enforced)

- Max `MAX_RETRY_ATTEMPTS` (3) for soft/technical declines — matches verified
  Razorpay T+1/T+2/T+3 behavior. No exceptions, no AI override.
- Zero retries for hard or compliance declines — always re-auth or escalate, never
  silent retry.
- Every stop must write `stop_reason` to `agent_decisions.rule_fired` and an
  `audit_log` row — a stop with no reason logged is a bug.
- Circuit breaker: if more than 20% of a page (see `PAGE_SIZE`) errors unexpectedly
  during processing, pause the worker and surface an alert rather than continuing.

## Step 5 — Where AI is allowed (bounded, per CLAUDE.md Rule 1)

1. **Drafting the customer-facing re-auth/retry message** — tone and wording only.
   Never decides the action itself.
2. **Classifying a decline code not present in the Step 1 table** — the AI's job is
   to *suggest* a bucket; the fallback if the AI call fails, times out
   (`AI_TIMEOUT_SECONDS`), or returns something unparseable is always
   `bucket = "compliance"` (most conservative), logged with
   `reasoning_source = "ai_fallback"`.

## Test cases to cover (minimum set)

- Amount exactly ₹15,000 (boundary — should NOT require AFA)
- Amount ₹15,000.01 / 1,500,001 paise (should require AFA)
- Amount exactly ₹1,00,000 for a `mutual_fund_sip` mandate (boundary)
- attempt_count exactly at `MAX_RETRY_ATTEMPTS` (should stop, not retry once more)
- Unmapped decline code (should default to compliance bucket, not soft)
- Same `idempotency_key` processed twice (second run must be a no-op)
- AI call simulated as timing out (must fall back to conservative path, never hang)
