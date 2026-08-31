"""Bounded AI layer (Google Gemini, free tier).

Per CLAUDE.md Rule 1 and DECISION_RULES.md Step 5 the LLM is allowed exactly two jobs:

  1. draft_reauth_message() — write the customer-facing text for a re-auth link.
     Pure text. It never influences which action was chosen.
  2. classify_decline_code() — SUGGEST a bucket for a code missing from the
     hardcoded taxonomy. The suggestion is logged; routing still treats the row as
     'compliance' no matter what the model says (CLAUDE.md Rule 2).

Every call is time-boxed. Any timeout / error / unparseable reply degrades to the
conservative fallback and is reported as such — the pipeline never hangs or crashes
because of the AI (CLAUDE.md Rule 2, DECISION_RULES.md Step 4).

No key (GEMINI_API_KEY unset) => the client is simply `disabled` and every call
returns a fallback result immediately.
"""

import logging
import os
from dataclasses import dataclass

from src.config import AI_HTTP_TIMEOUT_SECONDS, GEMINI_MODEL

# The SDK logs a noisy "AFC is not recommended" warning on every plain-string call.
logging.getLogger("google_genai.models").setLevel(logging.ERROR)

VALID_BUCKETS = ("soft", "hard", "technical", "compliance")

# Token the model is told to write where the rupee amount belongs, so one drafted
# message can be reused for every row in the same (rule, category) scenario.
AMOUNT_TOKEN = "AMOUNT_INR"


@dataclass(frozen=True)
class AIResult:
    ok: bool
    value: str | None
    status: str   # "ok" | "disabled" | "timeout" | "unparseable" | "error:<Type>"


_CLASSIFY_PROMPT = """You assist a payment-recovery system. A recurring UPI / RBI \
e-mandate debit failed with decline code "{code}", which is not in our taxonomy.

Pick exactly one bucket:
- soft: temporary or business decline, safe to retry on a schedule (e.g. low balance)
- hard: permanent decline, do not retry (e.g. invalid account, customer revoked)
- technical: transient system/network error, safe to retry quickly
- compliance: regulatory or mandate problem; must not auto-retry, needs customer re-auth

Context: amount ~Rs {amount_rupees}, mandate category "{mandate_category}".
Answer with ONLY the one bucket word in lowercase. Nothing else."""

_MESSAGE_PROMPT = """Write a short plain-language notification (max 3 sentences, no \
greeting, no signature) to an Indian customer whose recurring payment could not be \
completed and now needs their manual re-authorisation.

Reason: {reason_hint}
Service type: {mandate_category}

Where the payment amount belongs, write the literal token AMOUNT_INR (uppercase, no \
number) — it will be filled in later. Be calm and clear. Do not invent transaction \
IDs, dates, numbers, or phone numbers. Do not promise anything. End by telling them \
to approve the re-authorisation request in their banking or UPI app."""

_REASON_HINT = {
    "afa_threshold_exceeded": (
        "This payment is above the RBI limit for automatic recurring debits, so the "
        "bank now needs the customer to approve it directly."
    ),
    "compliance_decline_no_retry": (
        "The payment mandate needs to be renewed or re-approved before this payment "
        "can be collected."
    ),
}
_DEFAULT_REASON_HINT = _REASON_HINT["compliance_decline_no_retry"]


def _rupees(paise: int) -> str:
    return f"{paise / 100:,.2f}"


def fallback_message(rule_fired: str, ctx: dict) -> str:
    """Deterministic template used whenever the AI is unavailable or fails."""
    amt = _rupees(ctx["amount"])
    category = ctx["mandate_category"]
    if rule_fired == "afa_threshold_exceeded":
        return (
            f"We could not automatically collect your Rs {amt} payment for {category} "
            f"because it is above the RBI limit for automatic recurring debits. "
            f"Please open your bank or UPI app and approve the re-authorisation "
            f"request so the payment can be completed."
        )
    return (
        f"We could not collect your Rs {amt} payment for {category} because your "
        f"payment mandate needs to be re-approved. Please open your bank or UPI app "
        f"and approve the re-authorisation request to continue this subscription."
    )


def _parse_bucket(raw: str) -> str | None:
    for token in raw.lower().replace("*", " ").replace("`", " ").split():
        token = token.strip(".,:;'\"")
        if token in VALID_BUCKETS:
            return token
    return None


class AIClient:
    def __init__(
        self,
        api_key: str | None = None,
        *,
        model: str = GEMINI_MODEL,
        timeout_s: float = AI_HTTP_TIMEOUT_SECONDS,
    ) -> None:
        self.model = model
        self.timeout_s = timeout_s
        self._client = None
        key = api_key or os.environ.get("GEMINI_API_KEY")
        if key:
            try:
                from google import genai

                self._client = genai.Client(
                    api_key=key,
                    http_options={"timeout": int(timeout_s * 1000)},  # milliseconds
                )
            except Exception:  # noqa: BLE001 — never let SDK init break the pipeline
                self._client = None

    @classmethod
    def disabled(cls) -> "AIClient":
        """A client that is deliberately off — every call returns a fallback result."""
        obj = cls.__new__(cls)
        obj.model = GEMINI_MODEL
        obj.timeout_s = 0
        obj._client = None
        return obj

    @property
    def enabled(self) -> bool:
        return self._client is not None

    # ── low-level ────────────────────────────────────────────────────────────
    def _generate(self, prompt: str) -> str:
        resp = self._client.models.generate_content(model=self.model, contents=prompt)
        return (getattr(resp, "text", None) or "").strip()

    def _try(self, prompt: str) -> tuple[str | None, str]:
        if not self.enabled:
            return None, "disabled"
        try:
            return self._generate(prompt), "ok"
        except Exception as exc:  # noqa: BLE001
            name = type(exc).__name__
            status = "timeout" if "timeout" in name.lower() else f"error:{name}"
            return None, status

    # ── public API ───────────────────────────────────────────────────────────
    def classify_decline_code(self, code: str, ctx: dict) -> AIResult:
        raw, status = self._try(
            _CLASSIFY_PROMPT.format(
                code=code,
                amount_rupees=_rupees(ctx["amount"]),
                mandate_category=ctx["mandate_category"],
            )
        )
        if status != "ok":
            return AIResult(False, None, status)
        bucket = _parse_bucket(raw or "")
        if bucket is None:
            return AIResult(False, None, "unparseable")
        return AIResult(True, bucket, "ok")

    def draft_reauth_message(self, ctx: dict) -> AIResult:
        """Draft a re-auth message for a (rule_fired, mandate_category) scenario. The
        result contains the AMOUNT_TOKEN placeholder; the caller fills in the rupee
        figure per transaction."""
        raw, status = self._try(
            _MESSAGE_PROMPT.format(
                reason_hint=_REASON_HINT.get(ctx.get("rule_fired"), _DEFAULT_REASON_HINT),
                mandate_category=ctx["mandate_category"],
            )
        )
        if status != "ok" or not raw:
            return AIResult(False, None, status if status != "ok" else "unparseable")
        return AIResult(True, raw, "ok")
