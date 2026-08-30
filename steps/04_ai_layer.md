# Step 4 — AI Layer (Bounded, Added Last)

**Depends on:** Steps 2 and 3 working and tested on their own first. Do not start
this step until the deterministic pipeline runs correctly without any AI involved.
**Read also:** `DECISION_RULES.md` Step 5 for the exact scope boundaries.

## AI provider — use Gemini (free), not a paid API

Use the **Google Gemini API via Google AI Studio** — free tier, no credit card
required, no expiration (1,500 requests/day, 15 RPM is far more than this project
needs, since AI is only called for message drafting and unmapped codes per Rule 1).

**Important:** do not enable billing on that Google Cloud project for any reason —
doing so removes the free tier entirely for the whole project, not just the paid
feature. Keep this project billing-free.

If you hit a rate limit or need a backup, Groq's free tier is a fine alternative —
same "no card required" deal, slightly different limits.

## Goal
Add AI only where it earns its place — drafting messages and handling genuinely
unmapped codes — without ever letting it make the actual retry/stop decision.

## Tasks
- [ ] Add an AI call for **drafting the customer-facing message** when the action is
      `reauth_link_sent` (e.g. explain why re-authentication is needed, in plain
      language). This call only produces text — it never influences `action_taken`.
- [ ] Add an AI call for **suggesting a bucket** only when `classify()` from Step 2
      would otherwise fall through to the unmapped/default case. The AI's suggested
      bucket can be logged for reference, but for this hackathon, treat the actual
      routing decision as `compliance` regardless of what the AI suggests
      (per `CLAUDE.md` Rule 2 — fail toward caution). Log `reasoning_source =
      "ai_classifier"` if you use its suggestion for anything user-facing, or
      `"ai_fallback"` if the call failed and you used the default.
- [ ] Wrap every AI call with an 8-second timeout.
- [ ] On timeout, error, or unparseable response: use the safe default (conservative
      bucket / generic fallback message) and log `reasoning_source = "ai_fallback"`
      — never let the pipeline hang or crash.
- [ ] Cap concurrent AI calls (e.g. max 5 in flight at once) so a large batch doesn't
      blow through free-tier rate limits mid-run.

## Testing sequence
- [ ] Run a small batch with the AI layer working normally — confirm messages are
      generated and logged correctly.
- [ ] Deliberately force a timeout (e.g. point at an invalid endpoint temporarily) —
      confirm the pipeline doesn't hang, falls back correctly, and logs
      `ai_fallback`.
- [ ] Confirm the deterministic action (retry/stop/escalate) is never changed by the
      AI call — only the message text or the unmapped-code bucket suggestion is.

## Definition of done
- [ ] No AI call can block the pipeline for more than 8 seconds.
- [ ] A failed AI call never results in a silently skipped or crashed transaction —
      always falls back visibly.
- [ ] `reasoning_source` is correctly logged as `deterministic_rule`,
      `ai_classifier`, or `ai_fallback` on every decision.
