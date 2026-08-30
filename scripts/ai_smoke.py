"""One-shot check that the Gemini key works. Run after adding GEMINI_API_KEY to .env:

    python -m scripts.ai_smoke
"""

import sys

from src.ai import AIClient


def main() -> int:
    ai = AIClient()
    if not ai.enabled:
        print("AI is DISABLED — GEMINI_API_KEY is not set (or the SDK failed to init).")
        return 1

    ctx = {"amount": 2_500_000, "mandate_category": "insurance", "rule_fired": "afa_threshold_exceeded"}

    cls = ai.classify_decline_code("BANK_ERR_9931", ctx)
    print(f"classify_decline_code -> ok={cls.ok} value={cls.value!r} status={cls.status}")

    msg = ai.draft_reauth_message(ctx)
    print(f"draft_reauth_message -> ok={msg.ok} status={msg.status}")
    if msg.ok:
        print("  message:", msg.value)

    return 0 if (cls.ok or msg.ok) else 2


if __name__ == "__main__":
    sys.exit(main())
