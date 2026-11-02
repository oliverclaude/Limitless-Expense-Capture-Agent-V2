"""Observer plugin. Publishes the assistant's work to the debug topic. Never blocks a tool."""

from __future__ import annotations

import json
import queue
import sys
import threading
from pathlib import Path
from typing import Any

_CHUNK = 1800
_queue: queue.Queue[tuple[str, str]] = queue.Queue(maxsize=40)
_started = False
_lock = threading.Lock()
_buffers: dict[str, dict[str, str]] = {}
_trace = None


def _load_trace():
    global _trace
    if _trace is not None:
        return _trace
    root = Path(__file__).resolve().parents[2]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    import debug_trace

    _trace = debug_trace
    return _trace


def _worker() -> None:
    while True:
        title, body = _queue.get()
        try:
            _load_trace().publish_debug(title, body)
        except Exception:
            pass


def _enqueue(title: str, body: str) -> None:
    text = body.strip()
    if not text:
        return
    try:
        if not _load_trace().debug_enabled():
            return
    except Exception:
        return
    try:
        _queue.put_nowait((title, text))
    except queue.Full:
        try:
            _queue.get_nowait()
        except queue.Empty:
            pass
        try:
            _queue.put_nowait((title, text))
        except queue.Full:
            pass


def _session(kwargs: dict[str, Any]) -> str:
    return str(kwargs.get("session_id") or "assistant")


def _take(session: str, key: str) -> str:
    with _lock:
        slot = _buffers.get(session)
        if not slot:
            return ""
        return slot.pop(key, "")


def _add(session: str, key: str, text: str) -> str | None:
    """Append. Return a chunk once it is long enough to publish."""
    with _lock:
        slot = _buffers.setdefault(session, {})
        slot[key] = slot.get(key, "") + text
        if len(slot[key]) < _CHUNK:
            return None
        chunk = slot[key]
        slot[key] = ""
        return chunk


def _on_stream_start(**kwargs: Any) -> None:
    try:
        _enqueue(
            "Assistant turn",
            f"model={kwargs.get('model') or ''}\niteration={kwargs.get('iteration') or ''}",
        )
    except Exception:
        return


def _on_stream_delta(**kwargs: Any) -> None:
    try:
        delta = kwargs.get("delta") or ""
        if not isinstance(delta, str) or not delta:
            return
        kind = str(kwargs.get("kind") or "text")
        key = "reasoning" if kind == "reasoning" else "text"
        chunk = _add(_session(kwargs), key, delta)
        if chunk and key == "reasoning":
            _enqueue("Assistant thinking", chunk)
    except Exception:
        return


def _on_stream_end(**kwargs: Any) -> None:
    try:
        session = _session(kwargs)
        thinking = _take(session, "reasoning")
        streamed = _take(session, "text")
        if thinking.strip():
            _enqueue("Assistant thinking", thinking)
        final = kwargs.get("final_text")
        if not isinstance(final, str) or not final.strip():
            final = streamed
        if isinstance(final, str) and final.strip():
            _enqueue("Assistant output", final)
        error = kwargs.get("error")
        if error:
            _enqueue("Assistant error", str(error)[:1000])
    except Exception:
        return


def _dump(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except TypeError:
        return str(value)


def _on_pre_tool(**kwargs: Any) -> None:
    try:
        thinking = _take(_session(kwargs), "reasoning")
        if thinking.strip():
            _enqueue("Assistant thinking", thinking)
        name = str(kwargs.get("tool_name") or "tool")
        _enqueue(f"Tool start: {name}", _dump(kwargs.get("args")))
    except Exception:
        return None
    return None


def _on_post_tool(**kwargs: Any) -> None:
    try:
        name = str(kwargs.get("tool_name") or "tool")
        duration = kwargs.get("duration_ms")
        body = _dump(kwargs.get("result"))
        if duration is not None:
            body = f"{duration} ms\n{body}"
        _enqueue(f"Tool result: {name}", body)
    except Exception:
        return None
    return None


def register(ctx) -> None:
    global _started
    try:
        _load_trace()
    except Exception:
        return
    with _lock:
        if not _started:
            threading.Thread(target=_worker, name="expense-v2-debug", daemon=True).start()
            _started = True
    ctx.register_hook("pre_tool_call", _on_pre_tool)
    ctx.register_hook("post_tool_call", _on_post_tool)
    ctx.register_hook("on_stream_start", _on_stream_start)
    ctx.register_hook("on_stream_delta", _on_stream_delta)
    ctx.register_hook("on_stream_end", _on_stream_end)
