# Step 6 — Dashboard

**Depends on:** Step 3's worker pipeline producing real decisions to display.
No need to read `DECISION_RULES.md` or `ARCHITECTURE.md` in full for this step —
just the table shapes in `SCHEMA.sql` for `transactions`, `agent_decisions`,
`decline_events`, and `audit_log`.

## Goal
A simple, fast, read-only view that proves the system works — this is what you'll
actually be looking at during the pitch.

## Tasks
- [ ] Build a summary view showing: total transactions processed, total marked
      `recovered`, total `halted`, total `escalated`, and counts by decline bucket.
      Precompute/cache these numbers on an interval (e.g. every few seconds) rather
      than recalculating on every page render.
- [ ] Build a paginated table of transactions (never load the full table at once) —
      show status, amount, mandate category, and the action taken.
- [ ] Clicking a transaction should show its full audit trail: the decline event →
      which rule fired → what action was taken → the outcome. This is the "prove it"
      moment for judges — make it clear and readable, not a raw JSON dump.
- [ ] Add a live "batch progress" indicator (e.g. "page 14/25 processed") while the
      worker is running, sourced from the progress logging built in Step 3.
- [ ] Keep the UI simple — a clean table and a few summary numbers beats a busy
      dashboard. This is a hackathon demo, not a full product.

## Definition of done
- [ ] No dashboard query loads an unpaginated full table.
- [ ] Summary numbers update without a full page reload feeling sluggish.
- [ ] A judge could click into any single transaction and see, in plain language, why
      the agent did what it did.
