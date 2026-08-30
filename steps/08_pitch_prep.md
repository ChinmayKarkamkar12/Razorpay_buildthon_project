# Step 8 — Pitch Prep

**Depends on:** nothing technical — this is prep, not code. Do this once Step 7 has
given you real numbers to talk about.

## Goal
Turn the working system into a tight, honest, confident 5-minute pitch.

## Tasks
- [ ] Write down the one-line problem statement (see project `README.md` for a
      starting version) and rehearse saying it in under 15 seconds.
- [ ] Pull the real before/after number from your Step 7 load test: "Out of ₹X in
      failed payments, we recovered ₹Y — here's the breakdown by reason."
- [ ] Plan one live demo moment: click into a single transaction on the dashboard and
      narrate its audit trail out loud (decline code → rule fired → action → why).
- [ ] Prepare a short, honest "what's real vs. what we designed" note:
      - Real/verified: the ₹15,000 and ₹1,00,000 AFA thresholds, the pre-debit
        notice requirement, Razorpay's T+1/T+2/T+3 retry-then-halt pattern.
      - Reasoned/extrapolated: the exact decline-code taxonomy (NPCI's full code
        list isn't public), the synthetic failure-rate distribution used to seed
        data.
      Say this proactively in the pitch — it reads as rigor, not weakness.
- [ ] Prepare a one-sentence answer for the likely toughest question: "why doesn't
      the AI just decide everything?" → because compliance-critical decisions (the
      AFA threshold, retry caps) must be deterministic and auditable, not left to a
      model that could hallucinate a wrong action with real money on the line.
- [ ] Time the pitch. Cut anything that isn't the problem, the demo moment, or the
      number.

## Definition of done
- [ ] Pitch fits comfortably in the allotted time with room to spare.
- [ ] You can say the before/after number and the one honest-limitations line from
      memory, without reading off a slide.
