# Step 1 — Schema + Seed Script

**Read also:** `SCHEMA.sql` in the project root. Nothing else needed for this step.

## Goal
Get the database standing up with realistic-looking synthetic data, in a way that
mimics real ingestion (chunks, not one dump) so later steps can rely on it.

## Tasks
- [ ] Run `SCHEMA.sql` against a fresh Supabase or Neon Postgres instance (free tier).
- [ ] Write a seeder script that inserts transactions in chunks of 500 rows at a time
      — not one bulk insert. This matters later for testing pagination behavior.
- [ ] Give each transaction a realistic `mandate_id` (create mandates first — mix of
      `general`, `mutual_fund_sip`, `insurance`, `credit_card_bill` categories, each
      with the correct `afa_free_limit`: 1,500,000 paise for general, 10,000,000 for
      the other three).
- [ ] Assign each transaction a decline code and use the mapping in `DECISION_RULES.md`
      Step 1 to derive its bucket for the `decline_events` table — but you only need
      the codes list here, not the full rules doc, so a short inline list is fine:
      `Z9, U19, Z7` → soft · `ZM, ZA, U01, U69, ZE, U66` → hard ·
      `U30, U28, U67, YB, M0` → technical · `V3, mandate_cancelled, mandate_expired`
      → compliance.
- [ ] Pick a synthetic failure-rate split across the four buckets (document your
      chosen percentages in a code comment — you'll need to explain in the pitch that
      this is a reasoned estimate, not an official published statistic).
- [ ] Set every `idempotency_key` to a unique value (e.g. a UUID) — required for step 3.
- [ ] Amounts must be integers (paise), including some deliberately placed right at
      the ₹15,000 and ₹1,00,000 boundaries so step 2's tests have real boundary data
      to check against.

## Definition of done
- [ ] Querying counts by bucket roughly matches your intended distribution.
- [ ] At least a few transactions sit exactly at the ₹15,000 / ₹1,00,000 boundaries.
- [ ] Re-running the seeder doesn't fail on the `idempotency_key` unique constraint
      (either clear the table first, or generate fresh keys each run).
