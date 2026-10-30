"""Exact reply phrases. Anything else is not a decision."""

from __future__ import annotations

import re

CONFIRM_REPLIES = {"ok", "okay", "yes", "y", "proceed", "log", "logged", "confirm", "confirmed"}
SKIP_REPLIES = {"skip", "skipped", "ignore", "discard"}
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


def simple_action(text: str) -> str | None:
    """confirm or skip for a claim that is not a duplicate. Confirm wins if both appear."""
    compact = normalize(text)
    if compact in CONFIRM_REPLIES:
        return "confirm"
    if compact in SKIP_REPLIES:
        return "skip"
    return None


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
