"""Run the recovery worker over all pending transactions.

Usage:
    python -m scripts.run_worker                    # process everything
    python -m scripts.run_worker --max-pages 1      # one page only (crash-test setup)
    python -m scripts.run_worker --page-size 50
    python -m scripts.run_worker --no-circuit-breaker
"""

import argparse
import sys

from src.config import PAGE_SIZE
from src.worker import CircuitBreakerTripped, run


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--page-size", type=int, default=PAGE_SIZE)
    p.add_argument("--max-pages", type=int, default=None)
    p.add_argument("--no-circuit-breaker", action="store_true")
    args = p.parse_args()

    try:
        result = run(
            page_size=args.page_size,
            max_pages=args.max_pages,
            circuit_breaker=not args.no_circuit_breaker,
        )
    except CircuitBreakerTripped as exc:
        print(f"\nCIRCUIT BREAKER: {exc}", file=sys.stderr)
        return 2

    print(
        f"\ndone: {result.pages} page(s), {result.processed} processed | "
        f"recovered {result.recovered} (₹{result.recovered_paise / 100:,.2f}) | "
        f"halted {result.halted} | escalated {result.escalated} | errors {result.errors}"
    )
    if result.error_ids:
        print(f"errored transaction ids: {result.error_ids}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
