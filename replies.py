"""What the capture model decided. The user's words are not matched here."""

from __future__ import annotations

import json
import re
from typing import Any, Callable

REPLY_ACTIONS = {"skip", "confirm", "update", "ask"}
DUPLICATE_ACTIONS = {"keep", "remove_old", "drop_new", "ask"}


def coerce_duplicate_action(value: object) -> str:
    """keep both, replace the old line, or drop the new one. Anything else asks again."""
    action = str(value or "").strip().lower()
    if action in DUPLICATE_ACTIONS:
        return action
    return "ask"


def coerce_reply_action(value: object) -> str:
    """The model's word for what to do. Anything unrecognised is not a log or a skip."""
    action = str(value or "").strip().lower()
    if action in REPLY_ACTIONS:
        return action
    return "ask"


_REPLY_FLAGS = {
    "--action": "action",
    "--merchant": "merchant",
    "--comment": "comment",
    "--journal": "journal",
    "--claim-date": "claim_date",
    "--personal": "personal_amount",
    "--company": "company_amount",
    "--vat": "vat",
}


def parse_reply_command(argv: list[str]) -> dict[str, Any]:
    """Flags before --text. Everything after --text is the user's words."""
    if "--text" not in argv:
        raise RuntimeError("reply needs --text")
    text_at = argv.index("--text")
    text = " ".join(argv[text_at + 1 :]).strip()
    if not text:
        raise RuntimeError("reply needs --text")
    values: dict[str, str] = {}
    index = 0
    while index < text_at:
        flag = argv[index]
        if flag not in _REPLY_FLAGS:
            raise RuntimeError(f"unexpected reply argument: {flag}")
        if index + 1 >= text_at:
            raise RuntimeError(f"{flag} needs a value")
        values[_REPLY_FLAGS[flag]] = argv[index + 1]
        index += 2
    overrides = {
        key: values[key]
        for key in (
            "merchant",
            "comment",
            "journal",
            "claim_date",
            "personal_amount",
            "company_amount",
            "vat",
        )
        if key in values
    }
    return {"text": text, "action": values.get("action"), "overrides": overrides}


def parse_setting(text: str) -> dict[str, str] | None:
    """A chat request to change the high-value mark, add a journal, or change the interval."""
    raw = " ".join(text.strip().split())
    if not raw:
        return None
    amount = re.search(
        r"\bhigh[- ]value(?: mark| amount)?(?: to| is|=)?\s*R?\s*([0-9][0-9 ,.]*)",
        raw,
        re.IGNORECASE,
    )
    if amount:
        number = amount.group(1).replace(" ", "").replace(",", "")
        if number.endswith("."):
            number = number[:-1]
        return {"kind": "high_value", "value": number}
    journal = re.search(
        r"\badd (?:a )?journal(?: named| called)?\s+(.+)$",
        raw,
        re.IGNORECASE,
    )
    if journal:
        name = journal.group(1).strip(" .")
        if name:
            return {"kind": "journal", "value": name}
    if re.search(r"\b(poll|interval|check mail|mail check|how often)\b", raw, re.IGNORECASE):
        minutes = re.search(r"(\d+)\s*(?:min|mins|minute|minutes)\b", raw, re.IGNORECASE)
        if minutes:
            return {"kind": "interval", "value": minutes.group(1)}
    return None


_DECISION_FIELDS = (
    "merchant",
    "comment",
    "journal",
    "claim_date",
    "personal_amount",
    "company_amount",
    "vat",
)


def _money(value: object) -> str:
    if value is None or value == "":
        return "(none)"
    try:
        return f"R{float(value):g}"
    except (TypeError, ValueError):
        return str(value)


def _claim_summary(claim: dict[str, Any]) -> str:
    fields = claim.get("fields") if isinstance(claim.get("fields"), dict) else {}
    reasons = claim.get("reasons") if isinstance(claim.get("reasons"), list) else []
    lines = [
        f"hold: {claim.get('hold_kind') or 'review'}",
        f"reasons: {'; '.join(str(item) for item in reasons) or '(none)'}",
        f"merchant: {fields.get('merchant') or '(none)'}",
        f"claim date: {fields.get('claim_date') or '(none)'}",
        f"personal: {_money(fields.get('personal_amount'))}",
        f"company: {_money(fields.get('company_amount'))}",
        f"vat: {_money(fields.get('vat'))}",
        f"journal: {fields.get('journal') or '(none)'}",
        f"comment: {fields.get('comment') or '(none)'}",
    ]
    if claim.get("hold_kind") == "duplicate":
        row = claim.get("duplicate") if isinstance(claim.get("duplicate"), dict) else {}
        lines.append(
            "existing line: "
            + ", ".join(
                [
                    f"date {row.get('claim_date') or '(none)'}",
                    f"personal {_money(row.get('personal'))}",
                    f"company {_money(row.get('company'))}",
                    f"journal {row.get('journal') or '(none)'}",
                    f"proof {row.get('proof_file') or '(none)'}",
                ]
            )
        )
    return "\n".join(lines)


def decision_prompt(text: str, claim: dict[str, Any]) -> str:
    """Ask the local model what the words mean. The program does not match phrases."""
    duplicate = claim.get("hold_kind") == "duplicate"
    if duplicate:
        actions = "keep, remove_old, drop_new, or ask"
        rules = """Rules for this duplicate:
- keep: they want both lines.
- remove_old: they want the new line instead of the old one.
- drop_new: they do not want the new claim.
- ask: it is not one of those. ok and yes alone are ask, not a decision.
Return JSON only: {"action": "keep|remove_old|drop_new|ask"}"""
    else:
        actions = "confirm, skip, update, or ask"
        rules = """Rules:
- confirm: they want it logged. No particular phrase is required. A high-value amount is not a reason to refuse once they approve.
- skip: they do not want it logged.
- update: they correct a field and do not say whether to log it. Include only the fields they changed.
- ask: it is not an approval, a rejection, or a correction.
"My half" means personal_amount is half the spend and company_amount is 0. Use the action their other words call for. If those are the only words, use update.
Include a field only when their words give a new value. Otherwise omit it or use null. Do not copy the current value.
Return JSON only, with this shape:
{"action": "confirm|skip|update|ask", "merchant": null, "comment": null, "journal": null, "claim_date": null, "personal_amount": null, "company_amount": null, "vat": null}"""
    return (
        "You decide what this person wants done with one waiting expense claim. "
        f"The action is one of: {actions}. Return JSON only, no markdown.\n\n"
        f"Their message:\n{text.strip()}\n\n"
        f"Waiting claim:\n{_claim_summary(claim)}\n\n"
        + rules
    )


def _load_object(raw: str) -> dict[str, Any] | None:
    cleaned = re.sub(r"<think>.*?</think>", "", raw or "", flags=re.DOTALL | re.IGNORECASE).strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)\s*```", cleaned, re.DOTALL | re.IGNORECASE)
    if fenced:
        cleaned = fenced.group(1).strip()
    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    try:
        parsed = json.loads(match.group(0) if match else cleaned)
    except (json.JSONDecodeError, AttributeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def parse_decision(raw: str, *, duplicate: bool) -> dict[str, Any]:
    """The model's JSON. Anything unreadable or unrecognised asks again and changes nothing."""
    data = _load_object(raw)
    if data is None:
        return {"action": "ask", "overrides": {}}
    if duplicate:
        return {"action": coerce_duplicate_action(data.get("action")), "overrides": {}}
    overrides: dict[str, Any] = {}
    for key in _DECISION_FIELDS:
        value = data.get(key)
        if value is None or value == "":
            continue
        overrides[key] = value
    return {"action": coerce_reply_action(data.get("action")), "overrides": overrides}


def interpret_reply(text: str, claim: dict[str, Any], complete: Callable[[str], str]) -> dict[str, Any]:
    """One model call. complete(prompt) returns the model text. No phrase list."""
    raw = complete(decision_prompt(text, claim))
    if not isinstance(raw, str):
        raw = str(raw or "")
    return parse_decision(raw, duplicate=claim.get("hold_kind") == "duplicate")
