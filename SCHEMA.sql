-- RBI E-Mandate & UPI Recovery Coordinator — Schema
-- Postgres (Supabase / Neon free tier)
-- Money is ALWAYS integer paise. Never float. See CLAUDE.md Rule 3.

CREATE EXTENSION IF NOT EXISTS "pgcrypto"; -- for gen_random_uuid()

-- ─────────────────────────────────────────────
-- mandates
-- ─────────────────────────────────────────────
CREATE TYPE mandate_category AS ENUM (
    'general', 'mutual_fund_sip', 'insurance', 'credit_card_bill'
);

CREATE TYPE mandate_status AS ENUM ('active', 'paused', 'revoked');

CREATE TABLE mandates (
    mandate_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    customer_id      UUID NOT NULL,               -- synthetic
    category         mandate_category NOT NULL DEFAULT 'general',
    afa_free_limit   INTEGER NOT NULL,             -- paise: 1500000 or 10000000
    status           mandate_status NOT NULL DEFAULT 'active',
    registered_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ─────────────────────────────────────────────
-- transactions
-- ─────────────────────────────────────────────
CREATE TYPE transaction_status AS ENUM (
    'pending', 'recovered', 'halted', 'escalated'
);

CREATE TABLE transactions (
    transaction_id   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    mandate_id       UUID NOT NULL REFERENCES mandates(mandate_id),
    amount           INTEGER NOT NULL,             -- paise, integer only
    scheduled_at     TIMESTAMPTZ NOT NULL,
    pdn_sent_at      TIMESTAMPTZ,                  -- pre-debit notice
    pdn_channel      TEXT,                         -- 'sms' | 'email' | 'app'
    status           transaction_status NOT NULL DEFAULT 'pending',
    attempt_count    INTEGER NOT NULL DEFAULT 0,
    idempotency_key  TEXT NOT NULL UNIQUE,          -- CRITICAL: prevents double-processing
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Main worker query pattern: "give me the next page of pending transactions"
CREATE INDEX idx_transactions_status_scheduled
    ON transactions (status, scheduled_at);

-- ─────────────────────────────────────────────
-- decline_events
-- ─────────────────────────────────────────────
CREATE TYPE decline_bucket AS ENUM ('soft', 'hard', 'technical', 'compliance');

CREATE TABLE decline_events (
    event_id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    transaction_id   UUID NOT NULL REFERENCES transactions(transaction_id),
    decline_code     TEXT NOT NULL,                -- e.g. 'Z9', 'ZM', 'V3'
    bucket           decline_bucket NOT NULL,
    occurred_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_decline_events_transaction
    ON decline_events (transaction_id);

-- ─────────────────────────────────────────────
-- agent_decisions  (the pitch centerpiece — shows the reasoning)
-- ─────────────────────────────────────────────
CREATE TYPE agent_action AS ENUM (
    'retry_scheduled', 'reauth_link_sent', 'escalated', 'stopped_permanent'
);

CREATE TYPE reasoning_source AS ENUM ('deterministic_rule', 'ai_classifier', 'ai_fallback');

CREATE TABLE agent_decisions (
    decision_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    transaction_id    UUID NOT NULL REFERENCES transactions(transaction_id),
    rule_fired        TEXT NOT NULL,               -- e.g. 'afa_threshold_exceeded'
    action_taken      agent_action NOT NULL,
    reasoning_source  reasoning_source NOT NULL,
    next_action_at    TIMESTAMPTZ,
    decided_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_agent_decisions_transaction
    ON agent_decisions (transaction_id);

-- ─────────────────────────────────────────────
-- audit_log  (append-only — never UPDATE/DELETE)
-- ─────────────────────────────────────────────
CREATE TABLE audit_log (
    log_id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    transaction_id    UUID NOT NULL REFERENCES transactions(transaction_id),
    event_type        TEXT NOT NULL,
    payload_snapshot  JSONB NOT NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_audit_log_transaction ON audit_log (transaction_id);

-- Enforce append-only at the role level once you've created your app's DB role:
-- REVOKE UPDATE, DELETE ON audit_log FROM app_role;
