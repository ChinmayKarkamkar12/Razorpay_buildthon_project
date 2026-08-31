"""Launch the Step 6 dashboard.

    python -m scripts.run_dashboard [--port 5000] [--host 127.0.0.1]
"""

import argparse

from app import app


def main() -> None:
    ap = argparse.ArgumentParser(description="Recovery Coordinator dashboard")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=5000)
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()
    print(f"dashboard: http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, debug=args.debug)


if __name__ == "__main__":
    main()
