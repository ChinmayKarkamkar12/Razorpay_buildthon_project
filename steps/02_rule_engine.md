# Step 2 — Deterministic Rule Engine

**Read also:** `DECISION_RULES.md` in the project root — this step implements it
directly. Nothing else needed.

## Goal
Build the core decision logic as pure functions — no database, no network, no AI.
This is the compliance-critical part of the whole project; get it fully correct and
tested before anything else touches it.

## Tasks
- [ ] Implement `classify(decline_code) -> bucket` using the table in
      `DECISION_RULES.md` Step 1. Any code not in the table must return the
      `compliance` bucket (most conservative), never an error or a guess.
- [ ] Implement `check_afa_threshold(amount, mandate_category) -> AFA_REQUIRED |
      AFA_NOT_REQUIRED` using `DECISION_RULES.md` Step 2. Remember: priority
      categories (`mutual_fund_sip`, `insurance`, `credit_card_bill`) use the
      ₹1,00,000 limit; everything else uses ₹15,000.
- [ ] Implement `decide_action(bucket, afa_check, attempt_count) -> action, rule_fired`
      using the logic in `DECISION_RULES.md` Step 3. Note the AFA check takes
      precedence over the bucket — check it first.
- [ ] Keep all three functions pure (same input always gives same output, no side
      effects) so they're trivial to unit test.

## Required unit tests (do not skip any of these)
- [ ] Amount exactly ₹15,000 (1,500,000 paise) on a `general` mandate → should NOT
      require AFA.
- [ ] Amount 1,500,001 paise on a `general` mandate → SHOULD require AFA.
- [ ] Amount exactly ₹1,00,000 (10,000,000 paise) on a `mutual_fund_sip` mandate →
      should NOT require AFA.
- [ ] An unmapped/unknown decline code → must return `compliance` bucket, not an
      error and not `soft`.
- [ ] `attempt_count` exactly equal to `MAX_RETRY_ATTEMPTS` (3) on a soft decline →
      must return `stopped_permanent`, not another retry.
- [ ] A hard-bucket decline → must always return `stopped_permanent` or
      `reauth_link_sent`, never `retry_scheduled`, regardless of `attempt_count`.

## Definition of done
- [ ] All six test cases above pass.
- [ ] No database or network calls anywhere in these three functions.
- [ ] Constants (`AFA_FREE_LIMIT_GENERAL`, `AFA_FREE_LIMIT_PRIORITY`,
      `MAX_RETRY_ATTEMPTS`, etc.) live in one small config module, not hardcoded
      inline.
