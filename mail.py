"""IMAP intake. Only a subject that starts with the configured prefix is a version 2 claim."""

from __future__ import annotations

import hashlib
import imaplib
import os
import re
import tempfile
from datetime import datetime
from email import policy
from email.header import decode_header, make_header
from email.message import Message
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime
from pathlib import Path

PHOTO_SUFFIXES = (".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp", ".gif", ".tif", ".tiff")
PDF_SUFFIXES = (".pdf",)
CONTENT_TYPE_EXT = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/heic": ".heic",
    "image/heif": ".heif",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/tiff": ".tiff",
    "application/pdf": ".pdf",
}
FOLDER_PROCESSED = "processed"
FOLDER_REVIEW = "needs_review"
FOLDER_FAILED = "failed"
FOLDER_REJECTED = "rejected"
MAIL_FOLDERS = (FOLDER_PROCESSED, FOLDER_REVIEW, FOLDER_FAILED, FOLDER_REJECTED)


def subject_prefix() -> str:
    raw = os.environ.get("CLAIM_SUBJECT_PREFIX", "claimv2:").strip()
    return raw or "claimv2:"


def decode_mime_header(value: str | None) -> str:
    if not value:
        return ""
    return str(make_header(decode_header(value)))


def is_claim_subject(subject: str) -> bool:
    prefix = subject_prefix().lower()
    return subject.strip().lower().startswith(prefix)


def sender_addresses(msg: Message) -> set[str]:
    found: set[str] = set()
    for header in ("From", "Sender", "Reply-To", "Return-Path", "Resent-From"):
        values = msg.get_all(header) or []
        decoded = [decode_mime_header(v) if isinstance(v, str) else str(v) for v in values]
        for _, addr in getaddresses(decoded):
            addr = addr.strip().strip("<>").lower()
            if addr:
                found.add(addr)
    return found


def sender_allowed(msg: Message, allowed: list[str]) -> bool:
    if not allowed:
        return False
    allow = {item.lower() for item in allowed}
    return bool(sender_addresses(msg) & allow)


def text_body(msg: Message) -> str:
    chunks: list[str] = []
    for part in iter_file_parts(msg):
        if part.get_content_type() != "text/plain":
            continue
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        charset = part.get_content_charset() or "utf-8"
        chunks.append(bytes(payload).decode(charset, errors="replace"))
    if chunks:
        return "\n".join(chunks).strip()
    if not msg.is_multipart():
        payload = msg.get_payload(decode=True)
        if payload and (msg.get_content_type() or "").startswith("text/"):
            charset = msg.get_content_charset() or "utf-8"
            return bytes(payload).decode(charset, errors="replace").strip()
    return ""


def body_has_password(msg: Message, secret: str) -> bool:
    if not secret:
        return False
    if secret in text_body(msg):
        return True
    for part in iter_file_parts(msg):
        ctype = (part.get_content_type() or "").lower()
        if not ctype.startswith("text/"):
            continue
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        charset = part.get_content_charset() or "utf-8"
        text = bytes(payload).decode(charset, errors="replace")
        if secret in text:
            return True
        if ctype == "text/html" and secret in re.sub(r"<[^>]+>", " ", text):
            return True
    return False


def fetch_part(imap: imaplib.IMAP4, seq: bytes, spec: str) -> bytes | None:
    typ, data = imap.uid("FETCH", seq, spec)
    if typ != "OK" or not data:
        return None
    for item in data:
        if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], (bytes, bytearray)):
            return bytes(item[1])
    return None


def parse_message(raw: bytes) -> Message:
    return BytesParser(policy=policy.default).parsebytes(raw)


def iter_file_parts(msg: Message) -> list[Message]:
    if msg.is_multipart():
        return [part for part in msg.walk() if part.get_content_maintype() != "multipart"]
    return [msg]


def part_filename(part: Message) -> str:
    filename = part.get_filename()
    return decode_mime_header(filename) if filename else ""


def is_photo_part(part: Message) -> bool:
    name = part_filename(part).lower()
    ctype = (part.get_content_type() or "").lower()
    return ctype.startswith("image/") or name.endswith(PHOTO_SUFFIXES)


def is_pdf_part(part: Message) -> bool:
    name = part_filename(part).lower()
    ctype = (part.get_content_type() or "").lower()
    return ctype == "application/pdf" or name.endswith(PDF_SUFFIXES)


def is_file_part(part: Message) -> bool:
    name = part_filename(part)
    disposition = (part.get_content_disposition() or "").lower()
    return bool(name) or disposition in ("attachment", "inline") or is_photo_part(part) or is_pdf_part(part)


def proof_extension(part: Message) -> str:
    name = part_filename(part).lower()
    suffix = Path(name).suffix if name else ""
    if suffix in PHOTO_SUFFIXES or suffix in PDF_SUFFIXES:
        if suffix == ".jpeg":
            return ".jpg"
        return suffix
    ctype = (part.get_content_type() or "").lower()
    return CONTENT_TYPE_EXT.get(ctype, "")


def extract_proofs(msg: Message) -> list[tuple[str, bytes]]:
    """Photos win over PDFs when both are attached."""
    photos: list[tuple[str, bytes]] = []
    pdfs: list[tuple[str, bytes]] = []
    for part in iter_file_parts(msg):
        if is_photo_part(part):
            bucket = photos
        elif is_pdf_part(part):
            bucket = pdfs
        else:
            continue
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        ext = proof_extension(part)
        if not ext:
            continue
        bucket.append((ext, bytes(payload)))
    return photos or pdfs


def message_date(msg: Message) -> datetime | None:
    raw = msg.get("Date")
    if not raw:
        return None
    try:
        return parsedate_to_datetime(raw)
    except (TypeError, ValueError, IndexError):
        return None


def proof_dest(root: Path, when: datetime, ext: str, index: int) -> Path:
    month_dir = root / when.strftime("%Y-%m") / "proofs"
    stamp = when.strftime("%Y-%m-%d")
    suffix = "" if index == 1 else f"-{index}"
    return month_dir / f"{stamp}_pending_orig{suffix}{ext}"


def atomic_write(dest: Path, data: bytes) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", suffix=".part", dir=dest.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, dest)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def message_key(msg: Message) -> str:
    mid = decode_mime_header(msg.get("Message-ID")).strip()
    if mid:
        return mid
    return f"{decode_mime_header(msg.get('Subject'))}|{decode_mime_header(msg.get('Date'))}"


def claim_id_for(message_id: str) -> str:
    return hashlib.sha256(message_id.encode("utf-8")).hexdigest()[:8]


def folder_name(name: str, default: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "", (name or "").strip())
    return cleaned or default


def configured_folders(cfg: dict) -> tuple[str, ...]:
    names = [
        str(cfg.get("folder_processed") or FOLDER_PROCESSED),
        str(cfg.get("folder_review") or FOLDER_REVIEW),
        str(cfg.get("folder_failed") or FOLDER_FAILED),
        str(cfg.get("folder_rejected") or FOLDER_REJECTED),
    ]
    out: list[str] = []
    for name in names:
        if name not in out:
            out.append(name)
    return tuple(out)


def ensure_mail_folders(imap: imaplib.IMAP4, names: tuple[str, ...]) -> None:
    for name in names:
        typ, data = imap.create(name)
        if typ == "OK":
            continue
        detail = b" ".join(item for item in (data or []) if isinstance(item, bytes)).decode(
            "utf-8", errors="replace"
        ).lower()
        if "exists" not in detail and "already" not in detail:
            print(f"folder {name}: {typ} {detail}", file=__import__("sys").stderr)


def _imap_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _uid_search(imap: imaplib.IMAP4, *criteria: str) -> list[bytes]:
    typ, data = imap.uid("SEARCH", None, *criteria)
    if typ != "OK" or not data or not data[0]:
        return []
    return data[0].split()


def find_uid_by_message_id(imap: imaplib.IMAP4, mailbox: str, message_id: str) -> bytes | None:
    typ, _ = imap.select(mailbox)
    if typ != "OK":
        return None
    for candidate in (message_id, message_id.strip("<>")):
        uids = _uid_search(imap, "HEADER", "Message-ID", _imap_quote(candidate))
        if uids:
            return uids[-1]
    for uid in reversed(_uid_search(imap, "ALL")):
        raw = fetch_part(imap, uid, "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID SUBJECT DATE)])")
        if raw and message_key(parse_message(raw)) == message_id:
            return uid
    return None


def imap_move(imap: imaplib.IMAP4, uid: bytes, dest: str) -> None:
    typ, _ = imap.uid("MOVE", uid, dest)
    if typ == "OK":
        return
    typ, _ = imap.uid("COPY", uid, dest)
    if typ != "OK":
        raise imaplib.IMAP4.error(f"copy to {dest} failed")
    imap.uid("STORE", uid, "+FLAGS", r"(\Deleted)")
    imap.expunge()


def file_mail(imap: imaplib.IMAP4, message_id: str, dest: str, cfg: dict) -> None:
    sources = ("INBOX",) + configured_folders(cfg)
    if dest not in sources:
        sources = sources + (dest,)
    for box in sources:
        uid = find_uid_by_message_id(imap, box, message_id)
        if uid is None:
            continue
        if box == dest:
            return
        imap.select(box)
        imap_move(imap, uid, dest)
        return


def find_latest_claim(imap: imaplib.IMAP4, skip_ids: set[str]) -> Message | None:
    typ, data = imap.select("INBOX")
    if typ != "OK":
        raise RuntimeError("could not open INBOX")
    typ, data = imap.uid("SEARCH", None, "ALL")
    if typ != "OK" or not data or not data[0]:
        return None
    for seq in reversed(data[0].split()):
        raw_headers = fetch_part(imap, seq, "(BODY.PEEK[HEADER.FIELDS (SUBJECT DATE MESSAGE-ID)])")
        if not raw_headers:
            continue
        headers = parse_message(raw_headers)
        subject = decode_mime_header(headers.get("Subject"))
        if not is_claim_subject(subject):
            continue
        raw_full = fetch_part(imap, seq, "(BODY.PEEK[])")
        if not raw_full:
            continue
        msg = parse_message(raw_full)
        if message_key(msg) in skip_ids:
            continue
        return msg
    return None
