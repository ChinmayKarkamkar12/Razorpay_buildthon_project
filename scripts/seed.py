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

Mandate status mix:
    active  94% · paused 4% · revoked 2%

Decline-code bucket mix, BASE draw (before the mandate-status rule below):
    soft        55%   insufficient funds / limit exceeded dominate real dunning data
    technical   20%   bank downtime / timeouts
    hard        15%   wrong PIN, invalid VPA, customer decline
    compliance  10%   AFA-required, mandate cancelled/expired

Mandate-status consistency rule: a debit attempt against a paused/revoked mandate
cannot realistically come back as a retryable "insufficient funds" — the mandate
itself is the problem. So every transaction on a non-active mandate is forced to a
compliance-bucket code (revoked -> mandate_cancelled, paused -> mandate_expired).
This lifts the effective compliance share above the 10% base; the seed run prints
the actual final split.

AFA boundary rows are always placed on ACTIVE mandates (split deliberately across a
general and a priority mandate) so step 2's threshold tests get clean data.
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

# Reproducible runs (does not affect uuid4, which uses os.urandom).
random.seed(42)

MANDATE_CATEGORY_WEIGHTS = {
    "general": 55,
    "mutual_fund_sip": 20,
    "insurance": 15,
    "credit_card_bill": 10,
}

MANDATE_STATUS_WEIGHTS = {"active": 94, "paused": 4, "revoked": 2}

BUCKET_WEIGHTS = {
    "soft": 55,
    "technical": 20,
    "hard": 15,
    "compliance": 10,
}

# Decline code forced onto a transaction whose mandate is not active.
NON_ACTIVE_MANDATE_CODE = {"revoked": "mandate_cancelled", "paused": "mandate_expired"}

PRIORITY_CATEGORIES = ("mutual_fund_sip", "insurance", "credit_card_bill")

# Codes to draw from per bucket (kept in sync with config.DECLINE_CODE_BUCKETS).
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
def build_mandates(n: int) -> list[dict]:
    now = datetime.now(timezone.utc)
    mandates = []
    for _ in range(n):
        category = weighted_choice(MANDATE_CATEGORY_WEIGHTS)
        mandates.append(
            {
                "mandate_id": str(uuid.uuid4()),
                "customer_id": str(uuid.uuid4()),
                "category": category,
                "afa_free_limit": afa_free_limit_for(category),
                "status": weighted_choice(MANDATE_STATUS_WEIGHTS),
                "registered_at": now - timedelta(days=random.randint(30, 900)),
            }
        )
    # Guarantee at least one active mandate of each "shape" for boundary placement.
    if not any(m["status"] == "active" and m["category"] == "general" for m in mandates):
        mandates[0].update(status="active", category="general", afa_free_limit=AFA_FREE_LIMIT_GENERAL)
    if not any(m["status"] == "active" and m["category"] in PRIORITY_CATEGORIES for m in mandates):
        mandates[1].update(status="active", category="mutual_fund_sip", afa_free_limit=AFA_FREE_LIMIT_PRIORITY)
    return mandates


def mandate_row(m: dict) -> tuple:
    return (
        m["mandate_id"], m["customer_id"], m["category"],
        m["afa_free_limit"], m["status"], m["registered_at"],
    )


# ── transactions + decline events ──────────────────────────────────────────────
def pick_amount(category: str, boundary: str | None) -> int:
    """Integer paise. `boundary` deliberately places the row on / over the AFA
    threshold so step 2's boundary tests have real data."""
    limit = afa_free_limit_for(category)
    if boundary == "at":
        return limit                                  # exactly at -> AFA NOT required
    if boundary == "just_over":
        return limit + 1                              # 1 paisa over -> AFA required
    if boundary == "well_over":
        return limit + random.randint(50_000, 5_000_000)

    if category == "general":
        return random.choice(
            [random.randint(4_900, 300_000)] * 9 + [random.randint(300_000, 2_500_000)]
        )
    return random.randint(50_000, 12_000_000)


def build_transactions(mandates: list[dict], n: int) -> tuple[list[tuple], list[tuple]]:
    """Returns (transaction_rows, decline_event_rows)."""
    txns: list[tuple] = []
    events: list[tuple] = []
    now = datetime.now(timezone.utc)

    active_general = [m for m in mandates if m["status"] == "active" and m["category"] == "general"]
    active_priority = [
        m for m in mandates if m["status"] == "active" and m["category"] in PRIORITY_CATEGORIES
    ]

    # Boundary plan: explicit general/priority split so BOTH thresholds are covered.
    per = max(4, n // 300)
    plan: list[tuple[str | None, str | None]] = []
    plan += [("at", "general")] * per + [("at", "priority")] * per
    plan += [("just_over", "general")] * per + [("just_over", "priority")] * per
    plan += [("well_over", None)] * max(10, n // 100)
    plan += [(None, None)] * (n - len(plan))
    plan = plan[:n]
    random.shuffle(plan)

    for boundary, cat_hint in plan:
        if cat_hint == "general":
            mandate = random.choice(active_general)
        elif cat_hint == "priority":
            mandate = random.choice(active_priority)
        else:
            mandate = random.choice(mandates)

        txn_id = str(uuid.uuid4())
        amount = pick_amount(mandate["category"], boundary)

        scheduled_at = now + timedelta(days=random.randint(-10, 3), hours=random.randint(0, 23))
        has_pdn = random.random() < 0.9
        pdn_sent_at = scheduled_at - timedelta(days=random.randint(1, 3)) if has_pdn else None
        pdn_channel = random.choice(PDN_CHANNELS) if has_pdn else None

        attempt_count = random.choices([0, 1, 2, 3], weights=[80, 11, 7, 2])[0]

        txns.append(
            (
                txn_id,
                mandate["mandate_id"],
                amount,
                scheduled_at,
                pdn_sent_at,
                pdn_channel,
                "pending",
                attempt_count,
                uuid.uuid4().hex,   # idempotency_key (unique)
            )
        )

        # Decline code must be consistent with the mandate's own state.
        if mandate["status"] != "active":
            code = NON_ACTIVE_MANDATE_CODE[mandate["status"]]
            bucket = "compliance"
        else:
            bucket = weighted_choice(BUCKET_WEIGHTS)
            code = random.choice(CODES_BY_BUCKET[bucket])

        events.append(
            (
                str(uuid.uuid4()),
                txn_id,
                code,
                bucket,
                scheduled_at + timedelta(minutes=random.randint(1, 120)),
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
            insert_chunked(cur, "mandates", mandate_sql, [mandate_row(m) for m in mandates])
            insert_chunked(cur, "transactions", txn_sql, txns)
            insert_chunked(cur, "decline_events", event_sql, events)
        conn.commit()

    _print_sanity()
    return 0


def _print_sanity() -> None:
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT bucket, count(*) FROM decline_events GROUP BY bucket ORDER BY bucket")
        by_bucket = dict(cur.fetchall())
        cur.execute(
            "SELECT count(*) FROM transactions t "
            "JOIN mandates m USING (mandate_id) "
            "JOIN decline_events d ON d.transaction_id = t.transaction_id "
            "WHERE m.status <> 'active' AND d.bucket IN ('soft','technical','hard')"
        )
        inconsistent = cur.fetchone()[0]
        cur.execute(
            f"""
            SELECT
              sum((t.amount = lim)::int)      AS at_threshold,
              sum((t.amount = lim + 1)::int)  AS just_over,
              sum((t.amount > lim + 1)::int)  AS well_over
            FROM (
              SELECT t.amount,
                     CASE WHEN m.category IN {PRIORITY_CATEGORIES}
                          THEN {AFA_FREE_LIMIT_PRIORITY} ELSE {AFA_FREE_LIMIT_GENERAL} END AS lim
              FROM transactions t JOIN mandates m USING (mandate_id)
            ) t
            """
        )
        at_thr, just_over, well_over = cur.fetchone()
        cur.execute(
            f"SELECT count(*) FROM transactions t JOIN mandates m USING (mandate_id) "
            f"WHERE m.category = 'general' AND t.amount = {AFA_FREE_LIMIT_GENERAL}"
        )
        at_general = cur.fetchone()[0]
        cur.execute(
            f"SELECT count(*) FROM transactions t JOIN mandates m USING (mandate_id) "
            f"WHERE m.category IN {PRIORITY_CATEGORIES} AND t.amount = {AFA_FREE_LIMIT_PRIORITY}"
        )
        at_priority = cur.fetchone()[0]

    print("\nSeed complete.")
    print(f"  decline_events by bucket: {by_bucket}")
    print(f"  non-active mandate + retryable decline (should be 0): {inconsistent}")
    print(f"  amounts exactly at threshold: {at_thr}  (general {at_general}, priority {at_priority})")
    print(f"  amounts +1 over: {just_over}   well over: {well_over}")


if __name__ == "__main__":
    sys.exit(main())
