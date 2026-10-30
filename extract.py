"""Read a slip with the local vision model, then apply the expense rules in code."""

from __future__ import annotations

import base64
import json
import os
import re
import urllib.error
import urllib.request
from datetime import date
from io import BytesIO
from pathlib import Path
from typing import Any

from mail import subject_prefix


def _csv_env(name: str, default: str = "") -> list[str]:
    raw = os.environ.get(name, default)
    return [item.strip() for item in raw.split(",") if item.strip()]


def journals_from_env() -> list[str]:
    return _csv_env("JOURNALS", "Entertainment")


def vat_journals_from_env() -> list[str]:
    return _csv_env("VAT_JOURNALS", "")


def apply_vat_policy(fields: dict[str, Any], vat_journals: list[str] | None = None) -> dict[str, Any]:
    """Only journals on VAT_JOURNALS may claim VAT. Others get 0."""
    allowed = {name.lower() for name in (vat_journals if vat_journals is not None else vat_journals_from_env())}
    journal = fields.get("journal")
    if not isinstance(journal, str) or journal.lower() not in allowed:
        fields["vat"] = 0.0
    return fields


def _journal_blob(fields: dict[str, Any], extra: str = "") -> str:
    return " ".join(
        [extra, str(fields.get("comment") or ""), str(fields.get("merchant") or "")]
    ).lower()


def staff_welfare_hints() -> list[str]:
    defaults = [
        "staff welfare",
        "office kitchen",
        "office supplies",
        "kitchen",
        "rusks",
        "grocer",
        "grocery",
        "groceries",
        "cleaning",
        "detergent",
        "dishwasher",
        "bleach",
        "sponge",
        "handy andy",
        "domestos",
        "sunlight",
        "paper towel",
        "refuse bag",
        "bin bag",
        "tea bag",
        "biscuits",
    ]
    extra = [
        item.strip().lower()
        for item in os.environ.get("STAFF_WELFARE_HINTS", "").split(",")
        if item.strip()
    ]
    seen = {item: True for item in extra}
    return extra + [item for item in defaults if item not in seen]


def looks_staff_kitchen(blob: str) -> bool:
    text = blob.lower()
    return any(hint in text for hint in staff_welfare_hints())


def apply_meal_entertainment(fields: dict[str, Any], journals: list[str], extra: str = "") -> dict[str, Any]:
    names = {item.lower(): item for item in journals}
    entertainment = names.get("entertainment")
    if not entertainment:
        return fields
    blob = _journal_blob(fields, extra)
    if looks_staff_kitchen(blob):
        return fields
    needles = (
        "coffee", "cafe", "caff", "espresso", "americano", "latte", "cappuccino",
        "breakfast", "brunch", "lunch", "dinner", "supper", "restaurant", "resto",
        "bistro", "eatery", "drinks", "meal",
    )
    if any(word in blob for word in needles):
        fields["journal"] = entertainment
    return fields


def apply_staff_welfare(fields: dict[str, Any], journals: list[str], extra: str = "") -> dict[str, Any]:
    names = {item.lower(): item for item in journals}
    staff = names.get("staff welfare")
    if not staff:
        return fields
    if looks_staff_kitchen(_journal_blob(fields, extra)):
        fields["journal"] = staff
    return fields


def refresh_review(fields: dict[str, Any], journals: list[str], extra: str = "") -> dict[str, Any]:
    allowed = {item.lower(): item for item in journals}
    journal = fields.get("journal")
    if isinstance(journal, str) and journal.strip():
        fields["journal"] = allowed.get(journal.strip().lower())
    else:
        fields["journal"] = None
    apply_meal_entertainment(fields, journals, extra)
    apply_staff_welfare(fields, journals, extra)
    reasons: list[str] = []
    if not fields.get("claim_date"):
        reasons.append("missing claim date")
    if fields.get("personal_amount") is None:
        reasons.append("missing amount")
    if not fields.get("merchant"):
        reasons.append("missing merchant")
    if not fields.get("journal"):
        reasons.append("journal not in list")
    apply_vat_policy(fields)
    fields["review_reasons"] = reasons
    fields["needs_review"] = bool(reasons)
    return fields


def notify_reasons(fields: dict[str, Any], crop_status: str) -> list[str]:
    reasons: list[str] = []
    for item in fields.get("review_reasons") or []:
        text = str(item)
        if text not in reasons:
            reasons.append(text)
    if not fields.get("merchant") and "missing merchant" not in reasons:
        reasons.append("missing merchant")
    if crop_status.startswith("failed") and crop_status not in reasons:
        reasons.append(crop_status)
    return reasons


def comment_from_subject(subject: str) -> str:
    text = subject.strip()
    prefix = subject_prefix()
    if text.lower().startswith(prefix.lower()):
        text = text[len(prefix):].strip()
    text = re.sub(r"^(slip|invoice|receipt|bill)\s+for\s+", "", text, flags=re.IGNORECASE).strip()
    if not text:
        return ""
    return " ".join(word.capitalize() if word.islower() else word for word in text.split())


def _is_noise_reason(reason: str) -> bool:
    text = reason.lower()
    if "future" in text:
        return True
    if "tip" in text:
        return True
    if "handwrit" in text and "printed" in text:
        return True
    if "printed" in text and ("claimed" in text or " vs " in text):
        return True
    return False


def _as_number(value: Any) -> float | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        cleaned = value.replace("R", "").replace(",", "").strip()
        try:
            return float(cleaned)
        except ValueError:
            return None
    return None


def _preview_jpeg(path: Path, max_side: int = 1600) -> bytes:
    from PIL import Image, ImageOps

    with Image.open(path) as image:
        image = ImageOps.exif_transpose(image).convert("RGB")
        image.thumbnail((max_side, max_side))
        buf = BytesIO()
        image.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


def _model_name() -> str:
    name = os.environ.get("OLLAMA_MODEL", "").strip()
    if not name:
        raise RuntimeError("missing env: OLLAMA_MODEL")
    return name


def _chat_url() -> str:
    base = os.environ.get("OLLAMA_BASE_URL", "").strip().rstrip("/")
    if not base:
        raise RuntimeError("missing env: OLLAMA_BASE_URL")
    if base.endswith("/chat/completions"):
        return base
    if base.endswith("/v1"):
        return base + "/chat/completions"
    return base + "/v1/chat/completions"


def _timeout() -> int:
    raw = os.environ.get("OLLAMA_TIMEOUT_SECONDS", "3600").strip() or "3600"
    try:
        return max(30, int(raw))
    except ValueError:
        return 3600


def _post_chat(content: list[dict[str, Any]] | str) -> str:
    payload = {
        "model": _model_name(),
        "temperature": 0.1,
        "messages": [{"role": "user", "content": content}],
    }
    raw = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        _chat_url(),
        data=raw,
        method="POST",
        headers={
            "Authorization": f"Bearer {os.environ.get('OLLAMA_API_KEY', 'ollama').strip() or 'ollama'}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=_timeout()) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:800]
        raise RuntimeError(f"model HTTP {exc.code}: {detail}") from exc
    except TimeoutError as exc:
        raise RuntimeError("model timed out") from exc
    return _message_text(data)


def _message_text(data: dict[str, Any]) -> str:
    choices = data.get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        raise RuntimeError("model returned no choices")
    message = choices[0].get("message") or {}
    content = message.get("content") or ""
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                parts.append(part["text"])
        content = "\n".join(parts)
    if not isinstance(content, str):
        content = str(content)
    content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL | re.IGNORECASE).strip()
    if not content:
        raise RuntimeError("model returned an empty reply")
    return content


def _image_part(path: Path) -> dict[str, Any]:
    payload = _preview_jpeg(path)
    encoded = base64.b64encode(payload).decode("ascii")
    return {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{encoded}"}}


def _load_json(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)\s*```", cleaned, re.DOTALL | re.IGNORECASE)
    if fenced:
        cleaned = fenced.group(1).strip()
    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    try:
        parsed = json.loads(match.group(0) if match else cleaned)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"model did not return JSON: {text[:400]}") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError("model JSON was not an object")
    return parsed


def _prompt(subject: str, body: str, journals: list[str], vat_journals: list[str], pdf_text: str) -> str:
    prefix = subject_prefix()
    journal_list = ", ".join(journals) if journals else "(none configured)"
    vat_list = ", ".join(vat_journals) if vat_journals else "(none — no journal may claim VAT)"
    today = date.today().isoformat()
    return f"""You extract a South African expense claim. Return JSON only, no markdown.

Use ALL of: the slip/invoice image if present, any extracted PDF text, the email subject, and the email body (a free-text note to a clerk).
Never invent amount, date, or VAT. If a value is not on the slip or in the note, use null.
Today's date is {today}. A slip dated 2026 is not "in the future".

Rules:
- claim_date: calendar date printed on the slip/invoice (YYYY-MM-DD). Prefer "Printed At" / "Date" on the slip. Do not use the email date. Do not use the time-of-day (07:11 is not 11 September).
- journal: MUST be exactly one of these, or null if none fit: {journal_list}
- Staff Welfare: anything for the office kitchen — groceries, rusks, tea, biscuits, and kitchen/office cleaning supplies. SuperSpar/Checkers shop for the kitchen is Staff Welfare, not a missing journal.
- Entertainment: client restaurant/cafe meals and drinks, not office-kitchen stock.
- personal_amount: money spent from the operator's personal account.
- company_amount: money spent from the company account.
- If the note does not say whose account paid, the WHOLE amount is personal_amount and company_amount is 0.
- "My half" (or similar) means a third party paid the other half. personal_amount is the operator's half. company_amount is 0. Do not put the other half in either amount.
- If the slip has a handwritten total or tip (for example Total: R130 next to a printed Total Incl. R118), the handwritten figure is the amount spent. Use it. Do not set needs_review for printed total vs handwritten tip.
- vat: claimable VAT only. If the chosen journal is not in this list, vat MUST be 0 even if the slip prints VAT (South Africa: entertainment is not VAT-claimable): {vat_list}
- If the journal IS on that list, copy VAT only if printed on the slip or stated in the note. Keep that printed VAT even when a tip is added. Never calculate VAT from a rate unless the slip already shows the VAT figure.
- currency: ZAR unless another currency is explicit.
- comment: 2–5 words, the purpose from the subject after {prefix} (drop leading "Slip for" / "Invoice for"). Example: "Client Drinks". Do not summarise the merchant, mall, or amounts.
- needs_review: true only if amount, date, or merchant is missing, journal is null, or the note truly conflicts with the slip (not tip vs printed total).
- confidence: high, medium, or low.

Email subject:
{subject}

Email body:
{body if body.strip() else "(empty)"}

PDF text:
{pdf_text.strip() if pdf_text.strip() else "(none)"}

JSON shape:
{{
  "claim_date": "YYYY-MM-DD or null",
  "personal_amount": number or null,
  "company_amount": number or null,
  "vat": number or null,
  "currency": "ZAR",
  "merchant": "string or null",
  "comment": "string",
  "journal": "string or null",
  "confidence": "high|medium|low",
  "needs_review": true,
  "review_reasons": ["..."]
}}
"""


def _parse_fields(text: str, journals: list[str], subject: str) -> dict[str, Any]:
    parsed = _load_json(text)
    allowed = {item.lower(): item for item in journals}
    journal = parsed.get("journal")
    if isinstance(journal, str) and journal.strip():
        journal = allowed.get(journal.strip().lower())
    else:
        journal = None
    personal = _as_number(parsed.get("personal_amount"))
    company = _as_number(parsed.get("company_amount"))
    if personal is None and company is None:
        whole = _as_number(parsed.get("amount"))
        if whole is not None:
            personal, company = whole, 0.0
    reasons = parsed.get("review_reasons") or []
    if not isinstance(reasons, list):
        reasons = [str(reasons)]
    reasons = [str(item) for item in reasons if not _is_noise_reason(str(item))]
    claim_date = parsed.get("claim_date")
    if not isinstance(claim_date, str) or not claim_date.strip():
        claim_date = None
        reasons.append("missing claim date")
    if personal is None:
        reasons.append("missing amount")
    if journal is None:
        reasons.append("journal not in list")
    comment = comment_from_subject(subject)
    if not comment:
        raw_comment = parsed.get("comment")
        comment = raw_comment.strip() if isinstance(raw_comment, str) else ""
    merchant = parsed.get("merchant")
    if not isinstance(merchant, str) or not merchant.strip():
        merchant = None
    confidence = str(parsed.get("confidence") or "low").lower()
    if confidence not in ("high", "medium", "low"):
        confidence = "low"
    fields = {
        "claim_date": claim_date,
        "personal_amount": personal,
        "company_amount": 0.0 if company is None and personal is not None else company,
        "vat": _as_number(parsed.get("vat")),
        "currency": str(parsed.get("currency") or "ZAR"),
        "merchant": merchant,
        "comment": comment.strip(),
        "journal": journal,
        "confidence": confidence,
        "needs_review": bool(reasons),
        "review_reasons": reasons,
    }
    apply_meal_entertainment(fields, journals, subject)
    apply_staff_welfare(fields, journals, subject)
    return fields


def slip_corners(path: Path) -> list[tuple[float, float]] | None:
    """Ask the local model for the paper's four corners as fractions of the upright image."""
    prompt = """This photo contains a paper till slip or invoice. It may be rotated or shot at an angle.
Return JSON only, no markdown.
Give the four corners of the PAPER (include amounts and header; do not cut off text; exclude table/background).
Coordinates are fractions of image width and height, 0 to 1.
Order: top-left, top-right, bottom-right, bottom-left of the paper rectangle in the photo.

{"tl": [x, y], "tr": [x, y], "br": [x, y], "bl": [x, y]}
"""
    try:
        text = _post_chat([_image_part(path), {"type": "text", "text": prompt}])
        parsed = _load_json(text)
    except (RuntimeError, OSError, TimeoutError):
        return None
    points: list[tuple[float, float]] = []
    for key in ("tl", "tr", "br", "bl"):
        pair = parsed.get(key)
        if not (isinstance(pair, (list, tuple)) and len(pair) == 2):
            return None
        try:
            x, y = float(pair[0]), float(pair[1])
        except (TypeError, ValueError):
            return None
        if not (-0.05 <= x <= 1.05 and -0.05 <= y <= 1.05):
            return None
        points.append((min(1.0, max(0.0, x)), min(1.0, max(0.0, y))))
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    if (max(xs) - min(xs)) * (max(ys) - min(ys)) < 0.08:
        return None
    return points


def extract_claim(
    *,
    subject: str,
    body: str,
    image_path: Path | None,
    proof_name: str,
    journals: list[str],
    pdf_text: str = "",
    extra_images: list[Path] | None = None,
) -> dict[str, Any]:
    vat_journals = vat_journals_from_env()
    content: list[dict[str, Any]] = []
    images: list[Path] = []
    if image_path is not None:
        images.append(image_path)
    for extra in extra_images or []:
        if extra not in images:
            images.append(extra)
    for image in images[:4]:
        if image.is_file() and image.suffix.lower() != ".pdf":
            content.append(_image_part(image))
    if not content and not pdf_text.strip():
        raise RuntimeError("nothing to extract (no image or PDF text)")
    content.append({"type": "text", "text": _prompt(subject, body, journals, vat_journals, pdf_text)})
    text = _post_chat(content)
    fields = _parse_fields(text, journals, subject)
    apply_vat_policy(fields, vat_journals)
    fields["proof_file"] = proof_name
    return fields


def interpret_reply(reply: str, fields: dict[str, Any], journals: list[str]) -> dict[str, Any]:
    """Apply a free-text correction. Confirm and skip are decided before this is called."""
    snapshot = {
        "claim_date": fields.get("claim_date"),
        "personal_amount": fields.get("personal_amount"),
        "company_amount": fields.get("company_amount"),
        "vat": fields.get("vat"),
        "merchant": fields.get("merchant"),
        "comment": fields.get("comment"),
        "journal": fields.get("journal"),
        "review_reasons": fields.get("review_reasons"),
    }
    prompt = f"""The operator replied in English about this expense claim. Return JSON only.

Current claim:
{json.dumps(snapshot, default=str)}

Journals they may use: {", ".join(journals) or "(none)"}
Staff Welfare = items for the office kitchen (groceries, rusks, tea, milk, kitchen/office cleaning supplies). Entertainment = client restaurant/cafe meals, not office kitchen stock.

The operator may have sent several messages. Apply EVERY instruction in the text below.

Operator reply:
{reply}

Decide action:
- skip: they do not want this logged
- confirm: log it after applying any field changes in the same text
- update: they changed a field (merchant, half, journal, comment, amounts, date)

JSON:
{{
  "action": "skip|confirm|update",
  "claim_date": "YYYY-MM-DD or null",
  "personal_amount": number or null,
  "company_amount": number or null,
  "vat": number or null,
  "merchant": "string or null",
  "comment": "string or null",
  "journal": "string or null"
}}
Keep fields they did not change. "My half" means personal is half of the spend and company is 0.
"""
    parsed = _load_json(_post_chat(prompt))
    action = str(parsed.get("action") or "update").lower()
    if action not in ("skip", "confirm", "update"):
        action = "update"
    merged = dict(fields)
    if parsed.get("claim_date"):
        merged["claim_date"] = str(parsed["claim_date"]).strip()
    for key in ("personal_amount", "company_amount", "vat"):
        number = _as_number(parsed.get(key))
        if number is not None:
            merged[key] = number
    if isinstance(parsed.get("merchant"), str) and parsed["merchant"].strip():
        merged["merchant"] = parsed["merchant"].strip()
    if isinstance(parsed.get("journal"), str) and parsed["journal"].strip():
        merged["journal"] = parsed["journal"].strip()
    if isinstance(parsed.get("comment"), str) and parsed["comment"].strip():
        merged["comment"] = parsed["comment"].strip()
    refresh_review(merged, journals, extra=reply)
    if action == "confirm":
        merged["needs_review"] = False
        merged["review_reasons"] = []
    return {"action": action, "fields": merged}
