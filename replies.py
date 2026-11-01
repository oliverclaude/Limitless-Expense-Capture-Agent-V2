"""Duplicate sentences stay exact. Logging or dropping any other claim is not decided here."""

from __future__ import annotations

import re
from typing import Any

REPLY_ACTIONS = {"skip", "confirm", "update", "ask"}
KEEP_BOTH = {"keep both"}
REMOVE_OLD = {"remove the old one"}
DROP_NEW = {
    "don't capture the new one",
    "dont capture the new one",
    "do not capture the new one",
}


def normalize(text: str) -> str:
    compact = text.strip().lower().replace("\u2019", "'")
    compact = re.sub(r"[.!?]+$", "", compact).strip()
    return " ".join(compact.split())


def duplicate_action(text: str) -> str | None:
    """keep, remove_old, drop_new, or None when the reply is not one of the three sentences."""
    compact = normalize(text)
    if compact in KEEP_BOTH:
        return "keep"
    if compact in REMOVE_OLD:
        return "remove_old"
    if compact in DROP_NEW:
        return "drop_new"
    return None


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
