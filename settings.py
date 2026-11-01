"""Edit /etc/expense-agent-v2/env. The next run reads the file. No reload."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path


_SECRET_MARKERS = ("PASSWORD", "SECRET", "TOKEN")


def is_secret_key(key: str) -> bool:
    """Password-like values are not repeated back on the chat."""
    upper = key.upper()
    return upper.endswith("_KEY") or any(marker in upper for marker in _SECRET_MARKERS)


def read_env(path: Path) -> list[tuple[str, str]]:
    """Setting lines in file order. Comments and blank lines are skipped."""
    if not path.is_file():
        return []
    items: list[tuple[str, str]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped[len("export ") :].strip()
        if "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        if re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            items.append((key, value))
    return items


def display_value(key: str, value: str) -> str:
    if is_secret_key(key):
        return "(set)" if value else "(empty)"
    return value


def format_settings(items: list[tuple[str, str]]) -> str:
    lines = ["Current settings:"]
    for key, value in items:
        lines.append(f"{key}={display_value(key, value)}")
    return "\n".join(lines)


def parse_assignment(raw: str) -> tuple[str, str]:
    if "=" not in raw:
        raise RuntimeError("--set needs KEY=VALUE")
    key, value = raw.split("=", 1)
    key = key.strip()
    if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
        raise RuntimeError(f"not a setting name: {key}")
    if "\n" in value or "\r" in value:
        raise RuntimeError("a setting value cannot contain a new line")
    return key, value.strip()


def normalize_value(key: str, value: str) -> str:
    """Reject values that would break the next run. Other keys are stored as given."""
    try:
        if key == "POLL_INTERVAL_MINUTES":
            minutes = int(value)
            if minutes < 1:
                raise RuntimeError("interval must be at least 1 minute")
            return str(minutes)
        if key == "HIGH_VALUE_ZAR":
            amount = float(value)
            if amount < 0:
                raise RuntimeError("high value cannot be negative")
            return f"{amount:g}"
        if key == "DELETE_COMPLETED_STATE":
            if value not in {"0", "1"}:
                raise RuntimeError("DELETE_COMPLETED_STATE must be 0 or 1")
            return value
        if key in {"IMAP_PORT", "SMTP_PORT", "OLLAMA_TIMEOUT_SECONDS", "STATE_RETENTION_DAYS"}:
            number = int(value)
            if number < 0:
                raise RuntimeError(f"{key} cannot be negative")
            if key == "OLLAMA_TIMEOUT_SECONDS" and number < 30:
                raise RuntimeError("OLLAMA_TIMEOUT_SECONDS must be at least 30")
            return str(number)
    except ValueError as exc:
        raise RuntimeError(f"{key} needs a number") from exc
    return value


def parse_settings_command(argv: list[str]) -> tuple[list[tuple[str, str]], list[str]]:
    """--set KEY=VALUE, repeated, and --add-journal NAME. Anything else is refused."""
    pairs: list[tuple[str, str]] = []
    journals: list[str] = []
    index = 0
    while index < len(argv):
        flag = argv[index]
        if flag not in {"--set", "--add-journal"}:
            raise RuntimeError(f"unexpected settings argument: {flag}")
        if index + 1 >= len(argv):
            raise RuntimeError(f"{flag} needs a value")
        if flag == "--set":
            pairs.append(parse_assignment(argv[index + 1]))
        else:
            name = argv[index + 1].strip(" .")
            if not name:
                raise RuntimeError("--add-journal needs a name")
            journals.append(name)
        index += 2
    return pairs, journals


def apply_assignments(path: Path, pairs: list[tuple[str, str]], journals: list[str] | None = None) -> str:
    """Write keys that are already in the file. The poll interval changes the job first."""
    known = {key for key, _ in read_env(path)}
    latest: dict[str, str] = {}
    order: list[str] = []
    for key, value in pairs:
        if key not in known:
            raise RuntimeError(f"{key} is not a setting in the env file")
        if key not in latest:
            order.append(key)
        latest[key] = normalize_value(key, value)
    extra_journals = list(journals or [])
    if not order and not extra_journals:
        return format_settings(read_env(path))
    if "POLL_INTERVAL_MINUTES" in latest:
        set_cron_schedule(int(latest["POLL_INTERVAL_MINUTES"]))
    for key in order:
        update_key(path, key, latest[key])
    added: list[str] = []
    for name in extra_journals:
        if append_csv_value(path, "JOURNALS", name):
            added.append(name)
    lines: list[str] = []
    for key in order:
        lines.append(f"{key} is now {display_value(key, latest[key])}.")
    for name in added:
        lines.append(f"Added journal {name}.")
    for name in extra_journals:
        if name not in added:
            lines.append(f"Journal {name} is already on the list.")
    if "POLL_INTERVAL_MINUTES" in latest:
        lines.append("The next mail check uses that schedule.")
    if any(key in {"NTFY_URL", "NTFY_TOPIC"} for key in order):
        lines.append("The program sends there. The gateway subscription was not changed.")
    lines.append(format_settings(read_env(path)))
    return "\n".join(lines)


def update_key(path: Path, key: str, value: str) -> None:
    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    found = False
    out: list[str] = []
    prefix = f"{key}="
    export_prefix = f"export {key}="
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(prefix) or stripped.startswith(export_prefix):
            out.append(f"{key}={value}")
            found = True
        else:
            out.append(line)
    if not found:
        if out and out[-1] != "":
            out.append("")
        out.append(f"{key}={value}")
    text = "\n".join(out)
    if not text.endswith("\n"):
        text += "\n"
    path.write_text(text, encoding="utf-8")
    os.chmod(path, 0o600)


def append_csv_value(path: Path, key: str, item: str) -> bool:
    """Add one comma-separated item if it is not already there. Returns whether it was added."""
    current = ""
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith(f"{key}=") or stripped.startswith(f"export {key}="):
                current = stripped.split("=", 1)[1]
                break
    items = [part.strip() for part in current.split(",") if part.strip()]
    if any(part.lower() == item.lower() for part in items):
        return False
    items.append(item)
    update_key(path, key, ",".join(items))
    return True


def job_id_path() -> Path:
    return Path.home() / ".hermes" / "expense-v2-job"


def read_job_id() -> str:
    path = job_id_path()
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8").strip()


def write_job_id(job_id: str) -> None:
    path = job_id_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(job_id.strip() + "\n", encoding="utf-8")


def parse_created_job_id(output: str) -> str:
    """Job id from `hermes cron create` stdout. Ids are 12 hex characters."""
    plain = re.sub(r"\x1b\[[0-9;]*m", "", output)
    match = re.search(r"Created job:\s*([0-9a-fA-F]{8,})", plain)
    return match.group(1) if match else ""


def job_id_from_store(name: str, path: Path | None = None) -> str:
    """Read a job id from ~/.hermes/cron/jobs.json when create's text is unusable."""
    store = path if path is not None else Path.home() / ".hermes" / "cron" / "jobs.json"
    if not store.is_file():
        return ""
    try:
        data = json.loads(store.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    jobs = data.get("jobs") if isinstance(data, dict) else data
    if not isinstance(jobs, list):
        return ""
    for job in jobs:
        if isinstance(job, dict) and str(job.get("name") or "") == name and job.get("id"):
            return str(job["id"])
    return ""


def hermes_bin() -> str:
    found = shutil.which("hermes")
    if found:
        return found
    candidate = Path.home() / ".local" / "bin" / "hermes"
    if candidate.is_file():
        return str(candidate)
    raise RuntimeError("hermes is not on PATH")


def set_cron_schedule(minutes: int) -> None:
    job_id = read_job_id()
    if not job_id:
        raise RuntimeError("no scheduled job id is recorded yet")
    if minutes < 1:
        raise RuntimeError("interval must be at least 1 minute")
    subprocess.run(
        [hermes_bin(), "cron", "edit", job_id, "--schedule", f"{minutes}m"],
        check=True,
        timeout=120,
    )
