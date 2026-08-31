"""Step 7 — full-system load test before demo day.

Seeds a large synthetic batch (default 12,000 transactions), runs the whole worker
pipeline once, then runs a verification pass:

  * every pending transaction got exactly one decision and one audit row
  * zero duplicate agent_decisions (idempotency guard holding at scale)
  * status <-> action consistency, every terminal decision carries a rule
  * no page stalled — inter-page gaps from worker_progress.json stay bounded
  * an independent spot-check: re-run the pure rule engine on a random sample and
    confirm it still matches what the worker recorded

Writes `load_test_report.txt` with the final numbers for pitch-day backup.

    python -m scripts.load_test                     # seed 12k, reset, run, verify
    python -m scripts.load_test --transactions 20000
    python -m scripts.load_test --no-seed           # verify against existing data
"""

import argparse
import json
import pathlib
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone

import psycopg
from psycopg.rows import dict_row

from src.config import PAGE_SIZE
from src.db import get_dsn
from src.rules import check_afa_threshold, classify, decide_action, is_mandate_issue
from src.worker import PROGRESS_FILE, CircuitBreakerTripped, run

REPORT_FILE = pathlib.Path(__file__).resolve().parent.parent / "load_test_report.txt"
STALL_SECONDS = 30.0  # a single page should never take longer than this


class _ProgressMonitor(threading.Thread):
    """Polls worker_progress.json and records the wall-clock time each page completed."""

    def __init__(self) -> None:
        super().__init__(daemon=True)
        self._stop = threading.Event()
        self.page_completed_at: dict[int, float] = {}

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                data = json.loads(PROGRESS_FILE.read_text(encoding="utf-8"))
                done = data.get("pages_done", 0)
                if done and done not in self.page_completed_at:
                    self.page_completed_at[done] = time.monotonic()
            except (OSError, ValueError):
                pass
            time.sleep(0.5)

    def stop(self) -> None:
        self._stop.set()

    def gaps(self) -> list[tuple[int, float]]:
        ordered = sorted(self.page_completed_at.items())
        return [
            (page, t - prev_t)
            for (_, prev_t), (page, t) in zip(ordered, ordered[1:])
        ]


def _seed(n: int) -> None:
    print(f"\n=== seeding {n:,} transactions (--reset) ===")
    r = subprocess.run(
        [sys.executable, "-m", "scripts.seed", "--transactions", str(n), "--reset"],
        cwd=str(pathlib.Path(__file__).resolve().parent.parent),
    )
    if r.returncode != 0:
        sys.exit(f"seed failed with exit code {r.returncode}")


def _conn() -> psycopg.Connection:
    c = psycopg.connect(get_dsn())
    c.autocommit = True
    return c


CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def _verify_db(seeded_total: int) -> None:
    print("\n=== database verification ===")
    with _conn() as c, c.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT count(*) AS n FROM transactions")
        total = cur.fetchone()["n"]
        cur.execute("SELECT count(*) AS n FROM transactions WHERE status = 'pending'")
        pending = cur.fetchone()["n"]
        cur.execute("SELECT count(*) AS n FROM agent_decisions")
        decisions = cur.fetchone()["n"]
        cur.execute("SELECT count(*) AS n FROM audit_log WHERE event_type = 'agent_decision'")
        audits = cur.fetchone()["n"]

        check("all transactions seeded", total == seeded_total, f"{total} in table")
        check("no pending transactions left", pending == 0, f"{pending} still pending")
        check("one decision per transaction", decisions == total, f"{decisions} decisions / {total} txns")
        check("one audit row per decision", audits == decisions, f"{audits} audit rows")

        cur.execute(
            "SELECT transaction_id, count(*) AS n FROM agent_decisions "
            "GROUP BY transaction_id HAVING count(*) > 1"
        )
        dups = cur.fetchall()
        check("zero duplicate agent_decisions", not dups, f"{len(dups)} duplicated ids")

        cur.execute(
            "SELECT count(*) AS n FROM agent_decisions WHERE rule_fired IS NULL OR rule_fired = ''"
        )
        check("every decision carries a rule", cur.fetchone()["n"] == 0)

        cur.execute(
            """
            SELECT DISTINCT ad.action_taken, t.status
            FROM agent_decisions ad JOIN transactions t USING (transaction_id)
            """
        )
        allowed = {
            ("retry_scheduled", "recovered"), ("retry_scheduled", "halted"),
            ("reauth_link_sent", "halted"),
            ("stopped_permanent", "halted"),
            ("escalated", "escalated"),
        }
        pairs = {(r["action_taken"], r["status"]) for r in cur.fetchall()}
        bad = pairs - allowed
        check("status <-> action consistency", not bad, f"unexpected: {bad}" if bad else "")

        cur.execute(
            "SELECT count(*) AS n FROM agent_decisions ad WHERE ad.action_taken = 'stopped_permanent' "
            "AND NOT EXISTS (SELECT 1 FROM audit_log al WHERE al.transaction_id = ad.transaction_id "
            "                AND al.event_type = 'agent_decision')"
        )
        check("every stop is traceable in audit_log", cur.fetchone()["n"] == 0)


def _spot_check(sample_size: int = 15) -> None:
    print(f"\n=== independent spot-check ({sample_size} random transactions) ===")
    with _conn() as c, c.cursor(row_factory=dict_row) as cur:
        # pull a spread across buckets straight from the immutable audit snapshot
        cur.execute(
            """
            SELECT al.payload_snapshot AS s, ad.action_taken, ad.rule_fired
            FROM audit_log al
            JOIN agent_decisions ad USING (transaction_id)
            WHERE al.event_type = 'agent_decision'
            ORDER BY random()
            LIMIT %s
            """,
            (sample_size,),
        )
        rows = cur.fetchall()

    mismatches = 0
    for row in rows:
        s = row["s"]
        # recompute the decision from raw inputs using the pure rule engine
        bucket = classify(s["decline_code"])
        afa = check_afa_threshold(s["amount_paise"], s["mandate_category"])
        expected = decide_action(
            bucket, afa, s["attempt_count_before"],
            is_mandate_issue=is_mandate_issue(s["decline_code"]),
        )
        ok = expected.action == row["action_taken"] and expected.rule_fired == row["rule_fired"]
        mismatches += not ok
        tid = s["transaction_id"][:8]
        print(
            f"  {'ok ' if ok else 'BAD'} {tid} {s['decline_code']:>16} "
            f"₹{s['amount_paise'] / 100:>12,.2f} {s['mandate_category']:<16} "
            f"-> {row['action_taken']} ({row['rule_fired']})"
        )
        if not ok:
            print(f"      expected {expected.action} ({expected.rule_fired})")

    check("spot-check: rule engine still matches recorded decisions", mismatches == 0,
          f"{mismatches}/{len(rows)} mismatched")


def _write_report(result, wall: float, seeded_total: int, gaps: list[tuple[int, float]]) -> None:
    worst = max((g for _, g in gaps), default=0.0)
    passed = sum(1 for _, ok, _ in CHECKS if ok)
    lines = [
        "RBI E-Mandate Recovery Coordinator — Step 7 load test",
        f"run at: {datetime.now(timezone.utc).isoformat()}",
        f"transactions:      {seeded_total:,}",
        f"pages:             {result.pages}  (page size {PAGE_SIZE})",
        f"wall clock:        {wall:.1f}s  ({seeded_total / wall:,.0f} txn/s)",
        f"worst page gap:    {worst:.1f}s  (stall threshold {STALL_SECONDS:.0f}s)",
        "",
        f"processed:         {result.processed:,}",
        f"recovered:         {result.recovered:,}  (₹{result.recovered_paise / 100:,.2f})",
        f"halted:            {result.halted:,}",
        f"escalated:         {result.escalated:,}",
        f"errors:            {result.errors}",
        f"AI: {result.ai_messages} messages, {result.ai_classifications} classifications, "
        f"{result.ai_fallbacks} fallbacks",
        "",
        f"checks: {passed}/{len(CHECKS)} passed",
    ]
    for name, ok, detail in CHECKS:
        lines.append(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    REPORT_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nreport written to {REPORT_FILE}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--transactions", type=int, default=12_000)
    ap.add_argument("--no-seed", action="store_true", help="verify against existing data")
    ap.add_argument("--page-size", type=int, default=PAGE_SIZE)
    ap.add_argument("--no-ai", action="store_true")
    args = ap.parse_args()

    if not args.no_seed:
        _seed(args.transactions)

    with _conn() as c, c.cursor() as cur:
        cur.execute("SELECT count(*) FROM transactions")
        seeded_total = cur.fetchone()[0]

    print(f"\n=== running worker over {seeded_total:,} transactions ===")
    monitor = _ProgressMonitor()
    monitor.start()
    t0 = time.monotonic()
    try:
        result = run(page_size=args.page_size, use_ai=not args.no_ai)
    except CircuitBreakerTripped as exc:
        monitor.stop()
        check("worker completed without circuit-breaker trip", False, str(exc))
        print(f"\n{'=' * 50}\nLOAD TEST FAILED — circuit breaker tripped mid-run")
        return 1
    wall = time.monotonic() - t0
    monitor.stop()

    gaps = monitor.gaps()
    worst = max((g for _, g in gaps), default=0.0)
    print(f"\nworker finished in {wall:.1f}s ({seeded_total / wall:,.0f} txn/s)")
    print(f"page gaps recorded: {len(gaps)}, worst {worst:.1f}s")

    check("worker completed without circuit-breaker trip", True)
    check("no errored rows", result.errors == 0, f"{result.errors} errors")
    check("no page stalled", worst < STALL_SECONDS, f"worst gap {worst:.1f}s")

    _verify_db(seeded_total)
    _spot_check()
    _write_report(result, wall, seeded_total, gaps)

    failed = [name for name, ok, _ in CHECKS if not ok]
    print(f"\n{'=' * 50}")
    if failed:
        print(f"LOAD TEST FAILED — {len(failed)} check(s): {', '.join(failed)}")
        return 1
    print(f"LOAD TEST PASSED — {len(CHECKS)}/{len(CHECKS)} checks green")
    return 0


if __name__ == "__main__":
    sys.exit(main())
