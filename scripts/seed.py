"""Synthetic data seeder for the Recovery Coordinator.

Mimics real ingestion: rows are inserted in chunks of SEED_CHUNK_SIZE (500), never
one bulk dump, so later steps can exercise pagination behavior against realistic data.

Usage:
    python -m scripts.seed                       # default 2000 transactions
    python -m scripts.seed --transactions 12000  # bigger batch (step 7 load test)
    python -m scripts.seed --reset               # wipe all tables first, then seed

All data is synthetic (CLAUDE.md Rule 8). Money is integer paise (Rule 3).

────────────────────────────────────────────────────────────────────────────────
CHOSEN SYNTHETIC DISTRIBUTIONS  (reasoned estimates, NOT official published stats —
must be described as such in the pitch, see steps/08_pitch_prep.md)

Mandate category mix:
    general            55%   (typical low-value app subscriptions)
    mutual_fund_sip    20%
    insurance          15%
    credit_card_bill   10%

Decline-code bucket mix (of failed recurring payments):
    soft        55%   insufficient funds / limit exceeded dominate real dunning data
    technical   20%   bank downtime / timeouts
    hard        15%   wrong PIN, invalid VPA, customer decline
    compliance  10%   AFA-required, mandate cancelled/expired
────────────────────────────────────────────────────────────────────────────────
"""

import argparse
import random
import sys
import uuid
from datetime import datetime, timedelta, timezone

from src.config import (
    AFA_FREE_LIMIT_GENERAL,
    AFA_FREE_LIMIT_PRIORITY,
    DECLINE_CODE_BUCKETS,
    SEED_CHUNK_SIZE,
    afa_free_limit_for,
)
from src.db import connect

# Reproducible runs.
random.seed(42)

MANDATE_CATEGORY_WEIGHTS = {
    "general": 55,
    "mutual_fund_sip": 20,
    "insurance": 15,
    "credit_card_bill": 10,
}

BUCKET_WEIGHTS = {
    "soft": 55,
    "technical": 20,
    "hard": 15,
    "compliance": 10,
}

# Codes to draw from per bucket (subset of config.DECLINE_CODE_BUCKETS, kept in sync).
CODES_BY_BUCKET: dict[str, list[str]] = {"soft": [], "hard": [], "technical": [], "compliance": []}
for _code, _bucket in DECLINE_CODE_BUCKETS.items():
    CODES_BY_BUCKET.setdefault(_bucket, []).append(_code)

PDN_CHANNELS = ["sms", "email", "app"]


def weighted_choice(weights: dict[str, int]) -> str:
    keys = list(weights)
    return random.choices(keys, weights=[weights[k] for k in keys], k=1)[0]


def chunked(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


# ── mandates ───────────────────────────────────────────────────────────────────
def build_mandates(n: int) -> list[tuple]:
    rows = []
    now = datetime.now(timezone.utc)
    for _ in range(n):
        category = weighted_choice(MANDATE_CATEGORY_WEIGHTS)
        rows.append(
            (
                str(uuid.uuid4()),                       # mandate_id
                str(uuid.uuid4()),                       # customer_id (synthetic)
                category,                                # category
                afa_free_limit_for(category),            # afa_free_limit (paise)
                random.choices(["active", "paused", "revoked"], weights=[92, 5, 3])[0],
                now - timedelta(days=random.randint(30, 900)),  # registered_at
            )
        )
    return rows


# ── transactions + decline events ──────────────────────────────────────────────
def pick_amount(category: str, force_boundary: str | None) -> int:
    """Return an integer paise amount. force_boundary places the row exactly on / just
    over an AFA threshold so step 2's boundary tests have real data to check."""
    limit = afa_free_limit_for(category)
    if force_boundary == "at":
        return limit                     # exactly at the threshold -> AFA NOT required
    if force_boundary == "just_over":
        return limit + 1                 # 1 paisa over -> AFA required
    if force_boundary == "well_over":
        return limit + random.randint(50_000, 5_000_000)

    if category == "general":
        # Rs 49 - Rs 3,000 typical app subscriptions, occasional larger.
        return random.choice(
            [random.randint(4_900, 300_000)] * 9 + [random.randint(300_000, 2_500_000)]
        )
    # priority categories: SIP / premium / card bill, Rs 500 - Rs 1,20,000
    return random.randint(50_000, 12_000_000)


def build_transactions(mandates: list[tuple], n: int) -> tuple[list[tuple], list[tuple]]:
    """Returns (transaction_rows, decline_event_rows)."""
    txns: list[tuple] = []
    events: list[tuple] = []
    now = datetime.now(timezone.utc)

    # Guarantee a spread of boundary rows regardless of batch size.
    n_at = max(5, n // 200)
    n_just_over = max(5, n // 200)
    n_well_over = max(10, n // 100)
    boundary_plan = (
        ["at"] * n_at + ["just_over"] * n_just_over + ["well_over"] * n_well_over
    )
    boundary_plan += [None] * (n - len(boundary_plan))
    random.shuffle(boundary_plan)

    for i in range(n):
        mandate = random.choice(mandates)
        mandate_id, _customer, category = mandate[0], mandate[1], mandate[2]

        txn_id = str(uuid.uuid4())
        amount = pick_amount(category, boundary_plan[i])

        scheduled_at = now + timedelta(
            days=random.randint(-10, 3), hours=random.randint(0, 23)
        )
        has_pdn = random.random() < 0.9
        pdn_sent_at = (
            scheduled_at - timedelta(days=random.randint(1, 3)) if has_pdn else None
        )
        pdn_channel = random.choice(PDN_CHANNELS) if has_pdn else None

        # Most rows are fresh (attempt 0); some already mid-retry to exercise caps later.
        attempt_count = random.choices([0, 1, 2, 3], weights=[80, 10, 7, 3])[0]

        txns.append(
            (
                txn_id,
                mandate_id,
                amount,
                scheduled_at,
                pdn_sent_at,
                pdn_channel,
                "pending",            # status — worker will decide from here
                attempt_count,
                uuid.uuid4().hex,     # idempotency_key (unique)
            )
        )

        bucket = weighted_choice(BUCKET_WEIGHTS)
        code = random.choice(CODES_BY_BUCKET[bucket])
        events.append(
            (
                str(uuid.uuid4()),   # event_id
                txn_id,
                code,
                bucket,
                scheduled_at + timedelta(minutes=random.randint(1, 120)),  # occurred_at
            )
        )

    return txns, events


# ── DB writes (chunked) ────────────────────────────────────────────────────────
def insert_chunked(cur, label: str, sql: str, rows: list[tuple]) -> None:
    total = len(rows)
    done = 0
    for chunk in chunked(rows, SEED_CHUNK_SIZE):
        cur.executemany(sql, chunk)
        done += len(chunk)
        print(f"  {label}: {done}/{total}")


def reset(cur) -> None:
    print("Truncating all tables (--reset)...")
    cur.execute(
        "TRUNCATE audit_log, agent_decisions, decline_events, transactions, mandates "
        "RESTART IDENTITY CASCADE"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--transactions", type=int, default=2000)
    parser.add_argument(
        "--mandates",
        type=int,
        default=None,
        help="Number of mandates (default: ~1 per 6 transactions, min 50).",
    )
    parser.add_argument("--reset", action="store_true", help="Truncate all tables first.")
    args = parser.parse_args()

    n_txn = args.transactions
    n_mandate = args.mandates or max(50, n_txn // 6)

    print(f"Building {n_mandate} mandates and {n_txn} transactions...")
    mandates = build_mandates(n_mandate)
    txns, events = build_transactions(mandates, n_txn)

    mandate_sql = (
        "INSERT INTO mandates "
        "(mandate_id, customer_id, category, afa_free_limit, status, registered_at) "
        "VALUES (%s, %s, %s, %s, %s, %s)"
    )
    txn_sql = (
        "INSERT INTO transactions "
        "(transaction_id, mandate_id, amount, scheduled_at, pdn_sent_at, pdn_channel, "
        " status, attempt_count, idempotency_key) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)"
    )
    event_sql = (
        "INSERT INTO decline_events "
        "(event_id, transaction_id, decline_code, bucket, occurred_at) "
        "VALUES (%s, %s, %s, %s, %s)"
    )

    with connect() as conn:
        with conn.cursor() as cur:
            if args.reset:
                reset(cur)
            insert_chunked(cur, "mandates", mandate_sql, mandates)
            insert_chunked(cur, "transactions", txn_sql, txns)
            insert_chunked(cur, "decline_events", event_sql, events)
        conn.commit()

    # Quick sanity readout.
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT bucket, count(*) FROM decline_events GROUP BY bucket ORDER BY bucket")
        by_bucket = cur.fetchall()
        cur.execute(
            "SELECT count(*) FROM transactions t JOIN mandates m USING (mandate_id) "
            "WHERE t.amount = CASE WHEN m.category IN ('mutual_fund_sip','insurance','credit_card_bill') "
            f"     THEN {AFA_FREE_LIMIT_PRIORITY} ELSE {AFA_FREE_LIMIT_GENERAL} END"
        )
        exactly_at_boundary = cur.fetchone()[0]

    print("\nSeed complete.")
    print("  decline_events by bucket:", dict(by_bucket))
    print(f"  transactions sitting exactly on an AFA threshold: {exactly_at_boundary}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
