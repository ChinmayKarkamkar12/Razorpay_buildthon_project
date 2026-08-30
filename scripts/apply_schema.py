"""Apply SCHEMA.sql to the configured Postgres database.

Usage:
    python -m scripts.apply_schema            # create objects (fails if they exist)
    python -m scripts.apply_schema --reset    # drop existing objects first, then recreate

SCHEMA.sql itself is intentionally not idempotent (CREATE TYPE has no IF NOT EXISTS);
--reset gives you a clean slate for dev iteration.
"""

import argparse
import pathlib
import sys

from src.db import connect

PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
SCHEMA_PATH = PROJECT_ROOT / "SCHEMA.sql"

# Drop in FK-safe / dependency-safe order. CASCADE covers indexes and defaults.
TEARDOWN_SQL = """
DROP TABLE IF EXISTS audit_log        CASCADE;
DROP TABLE IF EXISTS agent_decisions  CASCADE;
DROP TABLE IF EXISTS decline_events   CASCADE;
DROP TABLE IF EXISTS transactions     CASCADE;
DROP TABLE IF EXISTS mandates         CASCADE;

DROP TYPE IF EXISTS reasoning_source   CASCADE;
DROP TYPE IF EXISTS agent_action       CASCADE;
DROP TYPE IF EXISTS decline_bucket     CASCADE;
DROP TYPE IF EXISTS transaction_status CASCADE;
DROP TYPE IF EXISTS mandate_status     CASCADE;
DROP TYPE IF EXISTS mandate_category   CASCADE;
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Drop all schema objects before recreating them (destroys all data).",
    )
    args = parser.parse_args()

    schema_sql = SCHEMA_PATH.read_text(encoding="utf-8")

    with connect() as conn:
        with conn.cursor() as cur:
            if args.reset:
                print("Dropping existing schema objects (--reset)...")
                cur.execute(TEARDOWN_SQL)
            print(f"Applying {SCHEMA_PATH.name}...")
            cur.execute(schema_sql)
        conn.commit()

    print("Schema applied successfully.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
