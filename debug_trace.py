"""Publish a model trace to the claim topic plus -debug, and optionally to a file.

Ntfy is off unless DEBUG_NTFY=1. The file is off unless DEBUG_LOG is a path.
Both are read from the env file on each publish, not from the process environment,
so a long-running process sees a change without a restart. Failures are swallowed.
A file error does not stop the ntfy notes.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.parse
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterator

from ntfy_out import send_text

ENV_FILE_DEFAULT = "/etc/expense-agent-v2/env"
_CHUNK = 3000
_GAP_SECONDS = 2.0
_CACHE_SECONDS = 2.0
_URL_RE = re.compile(r"https?://[^\s)>'\"]+")
_SECRET_LINE = re.compile(
    r"^(\s*(?:export\s+)?([A-Za-z][A-Za-z0-9_]*)\s*=\s*)(.*)$"
)

_purpose: ContextVar[str] = ContextVar("expense_model_purpose", default="model")
_cache_lock = threading.Lock()
_send_lock = threading.Lock()
_log_lock = threading.Lock()
_cache_at = 0.0
_cache: dict[str, Any] | None = None
_last_send = 0.0


def debug_topic(claim_topic: str) -> str:
    topic = claim_topic.strip()
    if not topic or topic.endswith("-debug"):
        return topic
    return topic + "-debug"


def split_thinking(content: str) -> tuple[str, str]:
    """Pull <think> blocks out of a finished model reply. The rest is the answer."""
    thoughts = re.findall(r"<think>(.*?)</think>", content, flags=re.DOTALL | re.IGNORECASE)
    visible = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL | re.IGNORECASE).strip()
    thinking = "\n\n".join(part.strip() for part in thoughts if part.strip())
    return thinking, visible


def describe_prompt(content: list[dict[str, Any]] | str) -> str:
    """Text sent to the model. Image bytes are counted, not copied."""
    if isinstance(content, str):
        return content
    texts: list[str] = []
    images = 0
    for part in content:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "image_url":
            images += 1
        elif part.get("type") == "text":
            texts.append(str(part.get("text") or ""))
    head = f"{images} image(s) not copied.\n" if images else ""
    return head + "\n".join(texts)


@contextmanager
def model_purpose(name: str) -> Iterator[None]:
    token = _purpose.set(name)
    try:
        yield
    finally:
        _purpose.reset(token)


def current_purpose() -> str:
    return _purpose.get()


def _env_path(path: Path | None) -> Path:
    if path is not None:
        return path
    raw = os.environ.get("ENV_FILE", "").strip()
    return Path(raw or ENV_FILE_DEFAULT)


def _read_env(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    found: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        key, value = line.split("=", 1)
        found[key.strip()] = value.strip().strip("'\"")
    return found


def _is_secret_key(key: str) -> bool:
    upper = key.upper()
    return upper.endswith("_KEY") or any(marker in upper for marker in ("PASSWORD", "SECRET", "TOKEN"))


def _hosts(values: list[str]) -> list[str]:
    hosts: list[str] = []
    for value in values:
        if "://" not in value:
            continue
        netloc = urllib.parse.urlsplit(value).netloc
        if netloc and netloc not in hosts:
            hosts.append(netloc)
    return hosts


def redact(text: str, *, hosts: list[str], hidden_values: list[str]) -> str:
    """Hide secret assignments and the notification and model server addresses."""
    lines: list[str] = []
    for line in text.splitlines():
        match = _SECRET_LINE.match(line)
        if match and _is_secret_key(match.group(2)):
            lines.append(match.group(1) + "(hidden)")
        else:
            lines.append(line)
    cleaned = "\n".join(lines)

    def hide_url(match: re.Match[str]) -> str:
        netloc = urllib.parse.urlsplit(match.group(0)).netloc
        if netloc in hosts:
            return "[url omitted]"
        return match.group(0)

    cleaned = _URL_RE.sub(hide_url, cleaned)
    for value in hidden_values:
        if len(value) >= 8:
            cleaned = cleaned.replace(value, "[hidden]")
    return cleaned


def _settings(path: Path | None, *, fresh: bool = False) -> dict[str, Any]:
    """Cache the file for a couple of seconds so a token stream does not reread it."""
    global _cache_at, _cache
    now = time.monotonic()
    with _cache_lock:
        if not fresh and _cache is not None and now - _cache_at < _CACHE_SECONDS:
            return _cache
    env = _read_env(_env_path(path))
    hide_urls = [env.get("NTFY_URL", ""), env.get("OLLAMA_BASE_URL", "")]
    hidden_values = [value for value in hide_urls if value]
    for key, value in env.items():
        if _is_secret_key(key) and value:
            hidden_values.append(value)
    loaded = {
        "enabled": env.get("DEBUG_NTFY", "").strip() == "1",
        "log_path": env.get("DEBUG_LOG", "").strip(),
        "base": env.get("NTFY_URL", "").strip(),
        "topic": debug_topic(env.get("NTFY_TOPIC", "")),
        "hosts": _hosts(hide_urls),
        "hidden_values": hidden_values,
    }
    with _cache_lock:
        _cache_at = now
        _cache = loaded
    return loaded


def _chunks(text: str) -> list[str]:
    body = text.strip()
    if not body:
        return []
    parts: list[str] = []
    while body:
        parts.append(body[:_CHUNK])
        body = body[_CHUNK:]
        if len(parts) >= 8 and body:
            parts.append("… cut")
            break
    return parts


def tool_return_code(result: Any, extra: dict[str, Any] | None = None) -> str | None:
    """Exit code from a tool result, when the tool reports one."""
    for source in (extra or {},):
        for key in ("exit_code", "returncode", "return_code"):
            if source.get(key) is not None:
                return str(source[key])
    parsed = result
    if isinstance(result, str):
        try:
            parsed = json.loads(result)
        except (json.JSONDecodeError, TypeError):
            parsed = None
    if isinstance(parsed, dict):
        for key in ("exit_code", "returncode", "return_code"):
            if parsed.get(key) is not None:
                return str(parsed[key])
    return None


def _append_log(path: str, title: str, body: str) -> bool:
    target = Path(path)
    stamp = datetime.now().astimezone().isoformat(timespec="seconds")
    record = f"{stamp} {title}\n{body.rstrip()}\n\n"
    with _log_lock:
        with target.open("a", encoding="utf-8") as handle:
            handle.write(record)
    return True


def publish_debug(
    title: str,
    body: str,
    *,
    env_path: Path | None = None,
    sender: Callable[..., None] | None = None,
) -> bool:
    """Send one trace. Returns False when nothing was written or published."""
    global _last_send
    try:
        settings = _settings(env_path, fresh=env_path is not None)
        ntfy = bool(settings["enabled"] and settings["base"] and settings["topic"])
        log_path = str(settings["log_path"] or "")
        if not ntfy and not log_path:
            return False
        cleaned = redact(body, hosts=list(settings["hosts"]), hidden_values=list(settings["hidden_values"]))
        if not cleaned.strip():
            return False
        logged = False
        if log_path:
            try:
                logged = _append_log(log_path, title, cleaned)
            except Exception:
                logged = False
        if not ntfy:
            return logged
        parts = _chunks(cleaned)
        if not parts:
            return logged
        post = sender or send_text
        for index, part in enumerate(parts):
            with _send_lock:
                wait = _GAP_SECONDS - (time.monotonic() - _last_send)
                if wait > 0 and sender is None:
                    time.sleep(wait)
                label = title if index == 0 else f"{title} (continued)"
                post(settings["base"], settings["topic"], title=label[:180], message=part)
                _last_send = time.monotonic()
        return True
    except Exception:
        return False


def note_model_start(content: list[dict[str, Any]] | str) -> None:
    publish_debug(f"{current_purpose()}: started", describe_prompt(content))


def debug_enabled(path: Path | None = None) -> bool:
    try:
        settings = _settings(path, fresh=path is not None)
        return bool(settings["enabled"] and settings["base"] and settings["topic"])
    except Exception:
        return False


def trace_enabled(path: Path | None = None) -> bool:
    """True when ntfy debug is on or a log file path is set."""
    try:
        settings = _settings(path, fresh=path is not None)
        return debug_enabled(path) or bool(settings.get("log_path"))
    except Exception:
        return False


def note_model_failure(exc: BaseException) -> None:
    publish_debug(f"{current_purpose()}: failed", f"{type(exc).__name__}: {exc}")


def note_model_result(content: str, extra_thinking: str = "") -> None:
    thinking, visible = split_thinking(content)
    if extra_thinking.strip():
        thinking = (extra_thinking.strip() + "\n\n" + thinking).strip()
    label = current_purpose()
    if thinking:
        publish_debug(f"{label}: thinking", thinking)
    if visible:
        publish_debug(f"{label}: output", visible)
