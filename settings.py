"""Edit /etc/expense-agent-v2/env. The next run reads the file. No reload."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path


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
