#!/usr/bin/env python3
"""Version 2 expense capture. One run reads the env file, then stops.

poll: look for one claimv2: message. Prints nothing. Sends ntfy itself.
reply: apply a chat reply or a settings change. Prints nothing. Sends ntfy itself.
capture: the program takes one reply from the claim topic. Prints nothing. Sends ntfy itself.
listen: read the claim topic and pass each reply to capture. Stays running.
install-cron: record the scheduled job. Run once, by hand, not from the bot.
install-listen: record the user service that runs listen. Run once, by hand.
"""

from __future__ import annotations

import contextlib
import fcntl
import imaplib
import os
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Iterator

import state as claim_state
from done_mail import send_done_mail
from extract import (
    _post_chat,
    apply_decision,
    extract_claim,
    journals_from_env,
    notify_reasons,
)
from mail import (
    PDF_SUFFIXES,
    body_has_password,
    claim_id_for,
    configured_folders,
    decode_mime_header,
    ensure_mail_folders,
    extract_proofs,
    file_mail,
    find_latest_claim,
    folder_name,
    message_date,
    message_key,
    proof_dest,
    atomic_write,
    sender_allowed,
    text_body,
)
from ntfy_out import CHAT_TOPIC, claim_reply_text, poll_messages, send_claim
from replies import coerce_duplicate_action, interpret_reply, parse_reply_command, parse_setting
from settings import (
    append_csv_value,
    apply_assignments,
    format_settings,
    hermes_bin,
    job_id_from_store,
    parse_created_job_id,
    parse_settings_command,
    read_env,
    read_job_id,
    set_cron_schedule,
    update_key,
    write_job_id,
)
from sheet import (
    confirm_proofs,
    delete_claim_row,
    find_duplicate,
    month_sheet_path,
    proof_pair_paths,
    proofs_month_dir,
    relocate_proof,
    write_claim_row,
)

ENV_FILE_DEFAULT = "/etc/expense-agent-v2/env"
LOCK_PATH = Path.home() / ".hermes" / "expense-v2.lock"
OUTAGE_STAMP = Path.home() / ".hermes" / "expense-v2-mailbox-error"


def log(message: str) -> None:
    print(message, file=sys.stderr)


class Busy(RuntimeError):
    pass


@contextlib.contextmanager
def exclusive() -> Iterator[None]:
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    handle = LOCK_PATH.open("a", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        handle.close()
        raise Busy() from exc
    try:
        yield
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def load_env() -> None:
    path = os.environ.get("ENV_FILE", "").strip() or ENV_FILE_DEFAULT
    if not os.path.isfile(path):
        raise RuntimeError(f"missing env file: {path}")
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    return int(raw)


def _env_float(name: str) -> float | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    return float(raw)


def load_config() -> dict[str, Any]:
    load_env()
    missing = [name for name in ("IMAP_HOST", "IMAP_USER", "IMAP_PASSWORD") if not os.environ.get(name)]
    if missing:
        raise RuntimeError("missing env: " + ", ".join(missing))
    root = os.environ.get("EXPENSES_ROOT", "").strip() or "/g-drive"
    state = os.environ.get("AGENT_STATE_DIR", "").strip() or "/g-drive/agent-state-v2"
    host = os.environ["IMAP_HOST"].strip()
    return {
        "env_path": Path(os.environ.get("ENV_FILE", "").strip() or ENV_FILE_DEFAULT),
        "host": host,
        "port": _env_int("IMAP_PORT", 993),
        "user": os.environ["IMAP_USER"].strip(),
        "password": os.environ["IMAP_PASSWORD"],
        "smtp_host": os.environ.get("SMTP_HOST", "").strip() or host,
        "smtp_port": _env_int("SMTP_PORT", 465),
        "work_email": os.environ.get("WORK_EMAIL", "").strip(),
        "expenses_root": Path(root),
        "state_dir": Path(state),
        "ntfy_url": os.environ.get("NTFY_URL", "").strip(),
        "ntfy_topic": os.environ.get("NTFY_TOPIC", "").strip(),
        "poll_interval_minutes": _env_int("POLL_INTERVAL_MINUTES", 30),
        "state_retention_days": _env_int("STATE_RETENTION_DAYS", 60),
        "delete_completed_state": os.environ.get("DELETE_COMPLETED_STATE", "1").strip()
        not in ("0", "false", "no", "off"),
        "high_value_zar": _env_float("HIGH_VALUE_ZAR"),
        "agent_home": os.environ.get("AGENT_HOME", "").strip(),
        "claim_from": [
            item.strip().lower() for item in os.environ.get("CLAIM_FROM", "").split(",") if item.strip()
        ],
        "claim_password": os.environ.get("CLAIM_PASSWORD", ""),
        "folder_processed": folder_name(os.environ.get("CLAIM_FOLDER_PROCESSED", ""), "processed"),
        "folder_review": folder_name(os.environ.get("CLAIM_FOLDER_REVIEW", ""), "needs_review"),
        "folder_failed": folder_name(os.environ.get("CLAIM_FOLDER_FAILED", ""), "failed"),
        "folder_rejected": folder_name(os.environ.get("CLAIM_REJECT_FOLDER", ""), "rejected"),
    }


def connect(cfg: dict[str, Any]) -> imaplib.IMAP4_SSL:
    imap = imaplib.IMAP4_SSL(str(cfg["host"]), int(cfg["port"]))
    imap.login(str(cfg["user"]), str(cfg["password"]))
    ensure_mail_folders(imap, configured_folders(cfg))
    return imap


def say(
    cfg: dict[str, Any],
    title: str,
    message: str,
    image: Path | None = None,
    *,
    topic: str | None = None,
) -> bool:
    """Claim messages use the expense topic. Pass topic for the assistant stream."""
    try:
        send_claim(
            str(cfg["ntfy_url"]),
            topic or str(cfg["ntfy_topic"]),
            title=title,
            message=message,
            image=image,
        )
        return True
    except Exception as exc:
        log(f"ntfy failed: {exc}")
        return False


def money(value: Any) -> str:
    if value is None or value == "":
        return "(none)"
    try:
        return f"R{float(value):g}"
    except (TypeError, ValueError):
        return str(value)


def format_fields(fields: dict[str, Any]) -> str:
    return "\n".join(
        [
            f"claim date: {fields.get('claim_date') or '(none)'}",
            f"personal: {money(fields.get('personal_amount'))}",
            f"company: {money(fields.get('company_amount'))}",
            f"vat: {money(fields.get('vat'))}",
            f"comment: {fields.get('comment') or '(none)'}",
            f"journal: {fields.get('journal') or '(none)'}",
            f"proof file: {fields.get('proof_file') or '(none)'}",
        ]
    )


def format_existing(row: dict[str, Any]) -> str:
    return "\n".join(
        [
            f"claim date: {row.get('claim_date') or '(none)'}",
            f"personal: {money(row.get('personal'))}",
            f"company: {money(row.get('company'))}",
            f"vat: {money(row.get('vat'))}",
            f"comment: {row.get('comment') or '(none)'}",
            f"journal: {row.get('journal') or '(none)'}",
            f"proof file: {row.get('proof_file') or '(none)'}",
        ]
    )


def who_it_is_for() -> str:
    return f"Reply on this topic. Questions and settings go to {CHAT_TOPIC}."


def duplicate_text(row: dict[str, Any], *, again: bool) -> str:
    lead = "That was not a decision on this duplicate.\n\n" if again else "This looks like a duplicate of an existing line.\n\n"
    return (
        lead
        + "Existing line:\n"
        + format_existing(row)
        + "\n\nReply in your own words. You can keep both lines, use the new line instead of the old one, or drop the new one.\n\n"
        + who_it_is_for()
    )


def review_text(fields: dict[str, Any], reasons: list[str]) -> str:
    why = "\n".join(reasons) if reasons else "needs a decision"
    return (
        "This claim needs you.\n\n"
        + why
        + "\n\n"
        + format_fields(fields)
        + "\n\nReply in your own words to log it, drop it, or correct a field.\n\n"
        + who_it_is_for()
    )


def logged_text(fields: dict[str, Any], done_ok: bool) -> str:
    tail = "" if done_ok else "\n\nThe sheet was updated, but the Done email failed."
    return "Logged.\n\n" + format_fields(fields) + tail


def skipped_text(fields: dict[str, Any], mail_ok: bool) -> str:
    tail = "" if mail_ok else "\n\nThe proof was deleted, but the Skipped email failed."
    return "Skipped. The new proof was deleted.\n\n" + format_fields(fields) + tail


def failure_text(subject: str, reason: str) -> str:
    return (
        "Claim failed. The mail is in the failed folder and the proof was deleted. "
        "Move the mail back to the inbox to try again.\n\n"
        f"subject: {subject or '(none)'}\nreason: {reason}"
    )


def image_of(claim: dict[str, Any]) -> Path | None:
    for key in ("scan_path", "orig_path"):
        raw = str(claim.get(key) or "")
        if not raw:
            continue
        path = Path(raw)
        if path.is_file() and path.suffix.lower() != ".pdf":
            return path
    return None


def delete_paths(paths: list[str | Path]) -> None:
    for raw in paths:
        path = Path(raw)
        if path.is_file():
            path.unlink()


def claim_paths(claim: dict[str, Any]) -> list[str]:
    paths = [str(item) for item in claim.get("proof_paths") or [] if item]
    for key in ("orig_path", "scan_path"):
        raw = str(claim.get(key) or "")
        if raw and raw not in paths:
            paths.append(raw)
    return paths


def close_mail(cfg: dict[str, Any], fields: dict[str, Any], *, subject: str, message_id: str, skipped: bool) -> None:
    send_done_mail(
        host=str(cfg["smtp_host"]),
        port=int(cfg["smtp_port"]),
        user=str(cfg["user"]),
        password=str(cfg["password"]),
        work_email=str(cfg["work_email"]),
        subject=subject,
        fields=fields,
        skipped=skipped,
        in_reply_to=message_id,
    )


def write_sheet(fields: dict[str, Any], orig: Path) -> None:
    write_claim_row(month_sheet_path(orig.parent.parent), fields)


def remove_old(fields: dict[str, Any], orig: Path, row: dict[str, Any]) -> str:
    """Drop the old sheet row and its proof files. The new claim's files stay."""
    sheet = month_sheet_path(orig.parent.parent)
    proof = str(row.get("proof_file") or "")
    removed = delete_claim_row(sheet, proof)
    keep = {
        str(fields.get("proof_file") or ""),
        orig.name,
        scan_path_for(orig).name,
    }
    deleted: list[str] = []
    for path in proof_pair_paths(orig.parent, proof):
        if path.is_file() and path.name not in keep:
            path.unlink()
            deleted.append(path.name)
    if removed:
        return "Removed the old line from the sheet and deleted its proof files."
    if deleted:
        return "The old line was not on the sheet. Its proof files were deleted."
    return "The old line was not on the sheet, and no proof files were found to delete."


def save_originals(msg: Any, root: Path) -> list[Path]:
    when = message_date(msg)
    if when is None:
        raise RuntimeError("no usable Date header")
    proofs = extract_proofs(msg)
    if not proofs:
        raise RuntimeError("no photo or PDF proof")
    written: list[Path] = []
    try:
        for index, (ext, payload) in enumerate(proofs, start=1):
            dest = proof_dest(root, when, ext, index)
            atomic_write(dest, payload)
            written.append(dest)
    except Exception:
        delete_paths(written)
        raise
    return written


def scan_path_for(orig: Path) -> Path:
    stem = orig.stem
    if "_orig-" in stem:
        stem = stem.replace("_orig-", "_scan-", 1)
    elif stem.endswith("_orig"):
        stem = stem[: -len("_orig")] + "_scan"
    else:
        stem = f"{stem}_scan"
    return orig.with_name(stem + ".jpg")


def save_scan(orig: Path) -> tuple[Path | None, str]:
    if orig.suffix.lower() in PDF_SUFFIXES:
        return None, "skipped (pdf)"
    from crop import straighten_slip_hybrid

    jpeg, status = straighten_slip_hybrid(orig)
    if jpeg is None:
        return None, status
    dest = scan_path_for(orig)
    atomic_write(dest, jpeg)
    return dest, status


def pick_primary(prepared: list[dict[str, Any]]) -> dict[str, Any]:
    def score(item: dict[str, Any]) -> int:
        blob = f"{item['orig'].name} {item.get('text') or ''}".lower()
        points = 0
        if "statement" in blob or "stmt" in blob:
            points -= 20
        if "invoice" in blob or "tax invoice" in blob:
            points += 20
        if item.get("scan") is not None:
            points += 5
        if item["orig"].suffix.lower() not in PDF_SUFFIXES:
            points += 2
        return points

    return max(prepared, key=score)


def _proof_paths(prepared: list[dict[str, Any]]) -> list[Path]:
    paths: list[Path] = []
    for item in prepared:
        orig = item.get("orig")
        scan = item.get("scan")
        if isinstance(orig, Path):
            paths.append(orig)
        if isinstance(scan, Path):
            paths.append(scan)
    return paths


def prepare_proofs(paths: list[Path], live: list[Path] | None = None) -> list[dict[str, Any]]:
    prepared: list[dict[str, Any]] = []
    for path in paths:
        pdf_text = ""
        scan = None
        status = ""
        if path.suffix.lower() in PDF_SUFFIXES:
            from pdf_proof import prepare_pdf

            pdf_text, scan, status = prepare_pdf(path)
        else:
            scan, status = save_scan(path)
        prepared.append({"orig": path, "scan": scan, "text": pdf_text, "status": status})
        if live is not None and isinstance(scan, Path) and scan not in live:
            live.append(scan)
    return prepared


def notify_outage(cfg: dict[str, Any], reason: str) -> None:
    if OUTAGE_STAMP.is_file():
        return
    say(
        cfg,
        "Expense check failed",
        "The mailbox check failed. Nothing was moved. The next check will try again.\n\nreason: " + reason,
    )
    OUTAGE_STAMP.parent.mkdir(parents=True, exist_ok=True)
    OUTAGE_STAMP.write_text("1", encoding="utf-8")


def clear_outage() -> None:
    OUTAGE_STAMP.unlink(missing_ok=True)


def major_failure(cfg: dict[str, Any], imap: imaplib.IMAP4 | None, mid: str, paths: list[Path], subject: str, reason: str) -> None:
    delete_paths(paths)
    if imap is not None and mid:
        try:
            file_mail(imap, mid, str(cfg["folder_failed"]), cfg)
        except Exception as exc:
            log(f"could not file failed mail: {exc}")
    say(cfg, "Claim failed", failure_text(subject, reason))


def finish_logged(cfg: dict[str, Any], imap: imaplib.IMAP4, claim: dict[str, Any], note: str = "") -> None:
    fields = dict(claim.get("fields") or {})
    orig = Path(str(claim.get("orig_path") or ""))
    if orig.is_file():
        write_sheet(fields, orig)
    done_ok = True
    try:
        close_mail(cfg, fields, subject=str(claim.get("subject") or ""), message_id=str(claim.get("message_id") or ""), skipped=False)
    except Exception as exc:
        done_ok = False
        log(f"done mail failed: {exc}")
    claim["status"] = "logged"
    claim["done_sent"] = done_ok
    claim["notified"] = True
    claim_state.save_claim(Path(str(cfg["state_dir"])), claim)
    try:
        file_mail(imap, str(claim.get("message_id") or ""), str(cfg["folder_processed"]), cfg)
    except Exception as exc:
        log(f"file processed failed: {exc}")
    message = logged_text(fields, done_ok)
    if note:
        message = note + "\n\n" + message
    say(cfg, "Claim logged", message, image_of(claim))


def finish_skipped(cfg: dict[str, Any], imap: imaplib.IMAP4, claim: dict[str, Any]) -> None:
    fields = dict(claim.get("fields") or {})
    delete_paths(claim_paths(claim))
    mail_ok = True
    try:
        close_mail(cfg, fields, subject=str(claim.get("subject") or ""), message_id=str(claim.get("message_id") or ""), skipped=True)
    except Exception as exc:
        mail_ok = False
        log(f"skipped mail failed: {exc}")
    claim["status"] = "skipped"
    claim["done_sent"] = mail_ok
    claim["notified"] = True
    claim_state.save_claim(Path(str(cfg["state_dir"])), claim)
    try:
        file_mail(imap, str(claim.get("message_id") or ""), str(cfg["folder_processed"]), cfg)
    except Exception as exc:
        log(f"file processed failed: {exc}")
    say(cfg, "Claim skipped", skipped_text(fields, mail_ok))


def hold(cfg: dict[str, Any], imap: imaplib.IMAP4, claim: dict[str, Any], message: str) -> None:
    claim["status"] = "awaiting_reply"
    claim["notified"] = False
    root = Path(str(cfg["state_dir"]))
    claim_state.save_claim(root, claim)
    try:
        file_mail(imap, str(claim.get("message_id") or ""), str(cfg["folder_review"]), cfg)
    except Exception as exc:
        log(f"file review failed: {exc}")
    if say(cfg, "Claim needs you", message, image_of(claim)):
        claim["notified"] = True
        claim_state.save_claim(root, claim)


def resend_waiting(cfg: dict[str, Any]) -> bool:
    waiting = claim_state.awaiting(Path(str(cfg["state_dir"])))
    if not waiting:
        return False
    claim = waiting[0]
    if claim.get("notified"):
        log(f"waiting on {claim.get('id')}")
        return True
    if claim.get("hold_kind") == "duplicate":
        message = duplicate_text(dict(claim.get("duplicate") or {}), again=bool(claim.get("ask_count")))
    else:
        message = review_text(dict(claim.get("fields") or {}), list(claim.get("reasons") or []))
    if say(cfg, "Claim needs you", message, image_of(claim)):
        claim["notified"] = True
        claim_state.save_claim(Path(str(cfg["state_dir"])), claim)
    return True


def process_message(cfg: dict[str, Any], imap: imaplib.IMAP4, msg: Any) -> None:
    mid = message_key(msg)
    subject = decode_mime_header(msg.get("Subject"))
    if not sender_allowed(msg, list(cfg.get("claim_from") or [])):
        file_mail(imap, mid, str(cfg["folder_rejected"]), cfg)
        log("rejected: sender")
        return
    if not body_has_password(msg, str(cfg.get("claim_password") or "")):
        file_mail(imap, mid, str(cfg["folder_rejected"]), cfg)
        log("rejected: password")
        return
    live: list[Path] = []
    proofs_saved = False
    try:
        live = save_originals(msg, Path(str(cfg["expenses_root"])))
        proofs_saved = True
        prepared = prepare_proofs(list(live), live)
        primary = pick_primary(prepared)
        texts = [f"--- {item['orig'].name} ---\n{item['text'].strip()}" for item in prepared if str(item.get("text") or "").strip()]
        extra: list[Path] = []
        for item in prepared:
            image = item["scan"] or (item["orig"] if item["orig"].suffix.lower() not in PDF_SUFFIXES else None)
            chosen = primary["scan"] or primary["orig"]
            if image is not None and image != chosen:
                extra.append(image)
        image = primary["scan"]
        if image is None and primary["orig"].suffix.lower() not in PDF_SUFFIXES:
            image = primary["orig"]
        fields = extract_claim(
            subject=subject,
            body=text_body(msg),
            image_path=image,
            proof_name=(primary["scan"] or primary["orig"]).name,
            journals=journals_from_env(),
            pdf_text="\n\n".join(texts),
            extra_images=extra,
        )
        for item in prepared:
            item["orig"], item["scan"], _ = confirm_proofs(
                item["orig"], item["scan"], fields.get("claim_date"), fields.get("merchant")
            )
            live = _proof_paths(prepared)
        current_month = prepared[0]["orig"].parent.parent
        month_dir = proofs_month_dir(Path(str(cfg["expenses_root"])), fields.get("claim_date"), current_month)
        if month_dir.resolve() != current_month.resolve():
            for item in prepared:
                item["orig"] = relocate_proof(item["orig"], month_dir)
                if item["scan"] is not None:
                    item["scan"] = relocate_proof(item["scan"], month_dir)
                live = _proof_paths(prepared)
        live = _proof_paths(prepared)
        primary = pick_primary(prepared)
        fields["proof_file"] = (primary["scan"] or primary["orig"]).name
    except Exception as exc:
        if not proofs_saved and isinstance(exc, (OSError, imaplib.IMAP4.error)):
            delete_paths(live)
            raise
        log(traceback.format_exc())
        major_failure(cfg, imap, mid, live, subject, str(exc))
        return

    proof_paths = []
    for item in prepared:
        proof_paths.append(str(item["orig"]))
        if item["scan"] is not None:
            proof_paths.append(str(item["scan"]))
    dup = find_duplicate(month_sheet_path(primary["orig"].parent.parent), fields)
    reasons = notify_reasons(fields, str(primary.get("status") or ""))
    hold_kind = "review"
    if dup:
        reasons = [
            f"possible duplicate of {dup.get('proof_file')} ({dup.get('claim_date')} {money(dup.get('personal'))})"
        ] + reasons
        hold_kind = "duplicate"
    elif cfg.get("high_value_zar") is not None:
        total = float(fields.get("personal_amount") or 0) + float(fields.get("company_amount") or 0)
        if total > float(cfg["high_value_zar"]):
            reasons.append(f"amount {money(total)} is over the high-value mark {money(cfg['high_value_zar'])}")
            hold_kind = "high_value"
    claim = {
        "id": claim_id_for(mid),
        "message_id": mid,
        "subject": subject,
        "fields": fields,
        "orig_path": str(primary["orig"]),
        "scan_path": str(primary["scan"]) if primary["scan"] is not None else "",
        "proof_paths": proof_paths,
        "reasons": reasons,
        "hold_kind": hold_kind,
        "duplicate": dup or {},
        "ask_count": 0,
    }
    if not reasons:
        finish_logged(cfg, imap, claim)
        return
    message = duplicate_text(dup or {}, again=False) if hold_kind == "duplicate" else review_text(fields, reasons)
    hold(cfg, imap, claim, message)


def apply_reply(
    cfg: dict[str, Any],
    imap: imaplib.IMAP4,
    claim: dict[str, Any],
    text: str,
    action: str | None = None,
    overrides: dict[str, Any] | None = None,
) -> None:
    fields = dict(claim.get("fields") or {})
    journals = journals_from_env()
    if claim.get("hold_kind") == "duplicate":
        chosen = coerce_duplicate_action(action)
        if chosen == "ask":
            claim["ask_count"] = int(claim.get("ask_count") or 0) + 1
            claim["notified"] = False
            claim_state.save_claim(Path(str(cfg["state_dir"])), claim)
            message = duplicate_text(dict(claim.get("duplicate") or {}), again=True)
            if say(cfg, "Claim needs you", message, image_of(claim)):
                claim["notified"] = True
                claim_state.save_claim(Path(str(cfg["state_dir"])), claim)
            return
        if chosen == "drop_new":
            finish_skipped(cfg, imap, claim)
            return
        note = ""
        if chosen == "remove_old":
            orig = Path(str(claim.get("orig_path") or ""))
            if orig.is_file():
                note = remove_old(fields, orig, dict(claim.get("duplicate") or {}))
            else:
                note = "The new proof is missing, so the old line was left as it is."
        fields["needs_review"] = False
        fields["review_reasons"] = []
        claim["fields"] = fields
        finish_logged(cfg, imap, claim, note)
        return

    result = apply_decision(fields, journals, action, overrides, extra=text)
    action = str(result["action"])
    fields = dict(result["fields"])
    claim["fields"] = fields
    if action == "skip":
        finish_skipped(cfg, imap, claim)
        return
    if action == "ask" or (action == "update" and fields.get("needs_review")):
        reasons = list(fields.get("review_reasons") or claim.get("reasons") or ["needs a decision"])
        preface = ""
        if action == "ask":
            preface = "That was not a decision on this claim."
            reasons = list(claim.get("reasons") or reasons)
        claim["reasons"] = reasons
        claim["notified"] = False
        claim_state.save_claim(Path(str(cfg["state_dir"])), claim)
        message = review_text(fields, reasons)
        if preface:
            message = preface + "\n\n" + message
        if say(cfg, "Claim needs you", message, image_of(claim)):
            claim["notified"] = True
            claim_state.save_claim(Path(str(cfg["state_dir"])), claim)
        return
    fields["needs_review"] = False
    fields["review_reasons"] = []
    claim["fields"] = fields
    finish_logged(cfg, imap, claim)


def apply_setting(cfg: dict[str, Any], change: dict[str, str]) -> str:
    path = Path(str(cfg["env_path"]))
    kind = change["kind"]
    value = change["value"]
    if kind == "high_value":
        amount = float(value)
        if amount < 0:
            raise RuntimeError("high value cannot be negative")
        update_key(path, "HIGH_VALUE_ZAR", f"{amount:g}")
        return f"High-value mark is now {money(amount)}. The next mail check will use it."
    if kind == "journal":
        added = append_csv_value(path, "JOURNALS", value)
        if added:
            return f"Added journal {value}. The next mail check will use it."
        return f"Journal {value} is already on the list."
    minutes = int(value)
    if minutes < 1:
        raise RuntimeError("interval must be at least 1 minute")
    set_cron_schedule(minutes)
    update_key(path, "POLL_INTERVAL_MINUTES", str(minutes))
    return f"Mail check is now every {minutes} minutes. The next check uses that schedule."


def poll() -> None:
    cfg = load_config()
    root = Path(str(cfg["state_dir"]))
    try:
        claim_state.prune(
            root,
            retention_days=int(cfg["state_retention_days"]),
            delete_completed=bool(cfg["delete_completed_state"]),
        )
    except OSError as exc:
        log(f"prune failed: {exc}")
    if resend_waiting(cfg):
        return
    imap: imaplib.IMAP4 | None = None
    try:
        imap = connect(cfg)
        clear_outage()
        msg = find_latest_claim(imap, claim_state.known_message_ids(root))
        if msg is None:
            log("no new claim")
            return
        process_message(cfg, imap, msg)
    except Exception as exc:
        log(traceback.format_exc())
        notify_outage(cfg, str(exc))
    finally:
        if imap is not None:
            try:
                imap.logout()
            except Exception:
                pass


def reply(text: str, action: str | None = None, overrides: dict[str, Any] | None = None) -> None:
    cfg = load_config()
    change = parse_setting(text)
    waiting = claim_state.awaiting(Path(str(cfg["state_dir"])))
    # A claim decision is not dropped just because the same sentence also changes a setting.
    if change is not None and not action:
        try:
            message = apply_setting(cfg, change)
        except Exception as exc:
            message = f"Could not change that setting: {exc}"
        if waiting:
            message += f" The claim is still waiting on {cfg['ntfy_topic']}."
        say(cfg, "Expense settings", message, topic=CHAT_TOPIC)
        return
    if change is not None and action:
        try:
            say(cfg, "Expense settings", apply_setting(cfg, change), topic=CHAT_TOPIC)
        except Exception as exc:
            say(cfg, "Expense settings", f"Could not change that setting: {exc}", topic=CHAT_TOPIC)
    if not waiting:
        say(
            cfg,
            "Expense agent",
            f"Nothing is waiting. Send a claimv2: email. Questions and settings go to {CHAT_TOPIC}.",
        )
        return
    if not (action or "").strip():
        if waiting[0].get("hold_kind") == "duplicate":
            print(
                "Need --action before --text. Use keep to log both lines, "
                "remove_old to use the new line and delete the old line and its proof files, "
                "drop_new to drop the new claim, or ask when it is not a decision. "
                "ok and yes are not a decision. Judge the meaning.",
                flush=True,
            )
        else:
            print(
                "Need --action before --text. Use confirm when they want the claim logged, "
                "skip when they do not, update when they only correct a field, "
                "ask when it is not a decision. Judge the meaning. There is no required phrase.",
                flush=True,
            )
        raise RuntimeError("reply needs --action")
    imap = connect(cfg)
    try:
        apply_reply(cfg, imap, waiting[0], text, action, overrides)
    finally:
        try:
            imap.logout()
        except Exception:
            pass


def settings_command(argv: list[str]) -> None:
    cfg = load_config()
    path = Path(str(cfg["env_path"]))
    try:
        pairs, journals = parse_settings_command(argv)
    except Exception as exc:
        say(cfg, "Expense settings", f"Could not change that setting: {exc}", topic=CHAT_TOPIC)
        return
    waiting = claim_state.awaiting(Path(str(cfg["state_dir"])))
    if not pairs and not journals:
        message = format_settings(read_env(path))
    else:
        try:
            message = apply_assignments(path, pairs, journals)
        except Exception as exc:
            message = f"Could not change that setting: {exc}"
    if waiting:
        message += f"\n\nThe claim is still waiting on {cfg['ntfy_topic']}."
    say(cfg, "Expense settings", message, topic=CHAT_TOPIC)


def capture(text: str) -> None:
    """One reply from the claim topic. The model runs only to decide the waiting claim."""
    cfg = load_config()
    waiting = claim_state.awaiting(Path(str(cfg["state_dir"])))
    if not waiting:
        say(
            cfg,
            "Expense agent",
            "Nothing is waiting. Send a claimv2: email. "
            f"Questions and settings go to {CHAT_TOPIC}.",
        )
        return
    say(cfg, "Capture program", "This reply is with the capture program, not the assistant.")
    claim = waiting[0]
    try:
        decided = interpret_reply(text, claim, _post_chat)
    except Exception as exc:
        log(f"capture could not read the reply: {exc}")
        say(cfg, "Capture program", "Could not read that reply. The claim is still waiting.")
        return
    imap = connect(cfg)
    try:
        apply_reply(cfg, imap, claim, text, decided["action"], decided.get("overrides"))
    finally:
        try:
            imap.logout()
        except Exception:
            pass


def capture_locked(text: str) -> None:
    with exclusive():
        capture(text)


def listen() -> None:
    """Follow the claim topic. The assistant is not subscribed to it."""
    cfg = load_config()
    root = Path(str(cfg["state_dir"]))
    since = claim_state.load_cursor(root, claim_state.LISTEN_CURSOR_NAME)
    if not since:
        since = str(int(time.time()))
        claim_state.save_cursor(root, since, claim_state.LISTEN_CURSOR_NAME)
    log(f"listening for claim replies on {cfg['ntfy_topic']}")
    while True:
        try:
            items = poll_messages(str(cfg["ntfy_url"]), str(cfg["ntfy_topic"]), since)
        except Exception as exc:
            log(f"listen poll failed: {exc}")
            time.sleep(5)
            continue
        for item in items:
            msg_id = str(item.get("id") or "")
            if msg_id and msg_id == since:
                continue
            text = claim_reply_text(item)
            if text:
                try:
                    capture_locked(text)
                except Busy:
                    log("listen busy")
                    time.sleep(5)
                    break
                except Exception as exc:
                    log(f"listen capture failed: {exc}")
            if msg_id:
                since = msg_id
                claim_state.save_cursor(root, since, claim_state.LISTEN_CURSOR_NAME)
        else:
            time.sleep(2)
            continue


def install_listen() -> None:
    """User service for the claim listener. No sudo. Does not touch the gateway unit."""
    import subprocess

    source = Path(__file__).resolve().parent / "templates" / "expense-v2-listen.service"
    if not source.is_file():
        raise RuntimeError(f"missing {source}")
    unit_dir = Path.home() / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    target = unit_dir / "expense-v2-listen.service"
    target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    env = os.environ.copy()
    env["XDG_RUNTIME_DIR"] = env.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    for args in (
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "--now", "expense-v2-listen.service"],
        ["systemctl", "--user", "restart", "expense-v2-listen.service"],
    ):
        result = subprocess.run(args, capture_output=True, text=True, env=env, timeout=60)
        output = (result.stdout or "") + "\n" + (result.stderr or "")
        if output.strip():
            log(output.strip())
        if result.returncode != 0:
            raise RuntimeError(f"{' '.join(args)} failed ({result.returncode})")


def install_cron() -> None:
    import subprocess

    cfg = load_config()
    home = Path(str(cfg["agent_home"])).expanduser()
    if not home.is_dir():
        raise RuntimeError("AGENT_HOME is not a directory")
    source = home / "scripts" / "expense-v2-poll.py"
    if not source.is_file():
        raise RuntimeError(f"missing {source}")
    scripts = Path.home() / ".hermes" / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    # A symlink is rejected: Hermes resolves the script and requires it to stay
    # inside this directory. The file here only delegates to AGENT_HOME.
    link = scripts / "expense-v2-poll.py"
    if link.is_symlink() or link.exists():
        link.unlink()
    link.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    link.chmod(source.stat().st_mode & 0o755)
    python = home / ".venv" / "bin" / "python"
    if not python.is_file():
        raise RuntimeError(f"missing {python}")
    minutes = max(1, int(cfg["poll_interval_minutes"]))
    job_id = read_job_id() or job_id_from_store("expense-v2")
    command = [hermes_bin(), "cron", "edit" if job_id else "create"]
    if job_id:
        command.append(job_id)
    else:
        command.append(f"{minutes}m")
    command.extend(
        [
            "--name",
            "expense-v2",
            "--no-agent",
            "--script",
            link.name,
            "--deliver",
            "local",
            "--interpreter",
            str(python),
        ]
    )
    if job_id:
        command.extend(["--schedule", f"{minutes}m"])
    result = subprocess.run(command, capture_output=True, text=True, timeout=120)
    output = (result.stdout or "") + "\n" + (result.stderr or "")
    log(output.strip())
    if result.returncode != 0:
        raise RuntimeError(f"hermes cron failed ({result.returncode})")
    if not job_id:
        created = parse_created_job_id(output) or job_id_from_store("expense-v2")
        if not created:
            raise RuntimeError("cron create did not print a job id")
        write_job_id(created)
        log(f"recorded job {created}")
    elif not read_job_id():
        write_job_id(job_id)


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] in {"-h", "--help"}:
        log(
            "usage: agent.py poll | listen | reply [--action ACTION] [--merchant TEXT] "
            "[--comment TEXT] [--journal TEXT] [--claim-date DATE] [--personal N] [--company N] "
            "[--vat N] --text TEXT | capture --text TEXT | settings [--set KEY=VALUE] "
            "[--add-journal NAME] | install-cron | install-listen"
        )
        sys.exit(2)
    command = sys.argv[1]
    try:
        if command == "install-cron":
            install_cron()
            return
        if command == "install-listen":
            install_listen()
            return
        if command == "listen":
            listen()
            return
        if command == "capture":
            capture_locked(parse_reply_command(sys.argv[2:])["text"])
            return
        with exclusive():
            if command == "poll":
                poll()
            elif command == "reply":
                parsed = parse_reply_command(sys.argv[2:])
                reply(parsed["text"], action=parsed.get("action"), overrides=parsed.get("overrides"))
            elif command == "settings":
                settings_command(sys.argv[2:])
            else:
                raise RuntimeError(f"unknown command: {command}")
    except Busy:
        if command in {"reply", "settings", "capture"}:
            try:
                cfg = load_config()
                say(
                    cfg,
                    "Expense agent",
                    "Still working on the last check. Try again in a minute.",
                    topic=CHAT_TOPIC if command == "settings" else None,
                )
            except Exception as exc:
                log(str(exc))
        else:
            log("busy")
    except Exception as exc:
        log(str(exc))
        sys.exit(1)


if __name__ == "__main__":
    main()
