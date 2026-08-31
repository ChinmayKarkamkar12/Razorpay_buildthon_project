"""Step 6 — read-only dashboard (Flask).

Run:  python -m scripts.run_dashboard   (or: flask --app app run)

Three views: a summary, a paginated transactions table, and a single-transaction
audit trail in plain language. All data comes from `src/dashboard.py`; this file is
only routing + templates. Nothing here writes to the database.
"""

from flask import Flask, abort, jsonify, render_template, request

from src import dashboard

app = Flask(__name__)


@app.route("/")
def index():
    return render_template(
        "index.html",
        summary=dashboard.get_summary(),
        progress=dashboard.get_worker_progress(),
    )


@app.route("/transactions")
def transactions():
    status = request.args.get("status") or None
    if status and status not in ("recovered", "halted", "escalated", "pending"):
        abort(400)
    page = dashboard.get_transactions_page(page=request.args.get("page", 1), status=status)
    return render_template("transactions.html", **page)


@app.route("/transactions/<transaction_id>")
def transaction_detail(transaction_id):
    detail = dashboard.get_transaction_detail(transaction_id)
    if detail is None:
        abort(404)
    return render_template("detail.html", **detail)


@app.route("/api/summary")
def api_summary():
    """Polled by the dashboard so the numbers refresh without a full page reload."""
    return jsonify(
        summary=dashboard.get_summary(),
        progress=dashboard.get_worker_progress(),
    )


if __name__ == "__main__":
    app.run(debug=True, port=5000)
