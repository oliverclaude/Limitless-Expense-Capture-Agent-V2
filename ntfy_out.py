"""Send ntfy messages tagged so the Hermes gateway does not treat them as the user.

Picture first, as its own message. Words are a second message.
The curl equivalent, which the program runs itself, is:

  curl -sS -D - -X PUT \\
    -H "Title: ..." \\
    -H "Priority: 4" \\
    -H "Filename: slip.jpg" \\
    -H "Message: ..." \\
    -H "X-Tags: hermes-agent" \\
    --data-binary @slip.jpg \\
    "$NTFY_URL/$NTFY_TOPIC"

  curl -sS -D - -X POST \\
    -H "Title: ..." \\
    -H "Priority: 4" \\
    -H "Content-Type: text/plain; charset=utf-8" \\
    -H "X-Tags: hermes-agent" \\
    --data-binary @message.txt \\
    "$NTFY_URL/$NTFY_TOPIC"
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ECHO_TAG = "hermes-agent"
CHAT_TOPIC = "Fin-Limitless-V2-chat"


def _header(value: str) -> str:
    return value.replace("\r", " ").replace("\n", " ").strip()


def _url(base: str, topic: str) -> str:
    return f"{base.rstrip('/')}/{urllib.parse.quote(topic, safe='')}"


def _send(base: str, topic: str, *, method: str, body: bytes, headers: dict[str, str]) -> None:
    if not base.strip() or not topic.strip():
        raise RuntimeError("NTFY_URL or NTFY_TOPIC is empty")
    headers = dict(headers)
    headers["X-Tags"] = ECHO_TAG
    headers["Tags"] = ECHO_TAG
    headers["Priority"] = "4"
    headers["Accept"] = "application/json"
    request = urllib.request.Request(_url(base, topic), data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:400]
        raise RuntimeError(f"ntfy HTTP {exc.code}: {detail}") from exc


def send_text(base: str, topic: str, *, title: str, message: str) -> None:
    _send(
        base,
        topic,
        method="POST",
        body=message.encode("utf-8"),
        headers={
            "Title": _header(title),
            "Content-Type": "text/plain; charset=utf-8",
        },
    )


def send_image(base: str, topic: str, *, title: str, caption: str, image: Path) -> None:
    _send(
        base,
        topic,
        method="PUT",
        body=image.read_bytes(),
        headers={
            "Title": _header(title),
            "Filename": _header(image.name),
            "Message": _header(caption),
        },
    )


def poll_messages(base: str, topic: str, since: str | None) -> list[dict]:
    """Cached messages on the claim topic since a message id or unix time. No publish."""
    if not base.strip() or not topic.strip():
        raise RuntimeError("NTFY_URL or NTFY_TOPIC is empty")
    query = {"poll": "1"}
    if since:
        query["since"] = since
    request = urllib.request.Request(
        _url(base, topic) + "/json?" + urllib.parse.urlencode(query),
        method="GET",
        headers={"Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"ntfy HTTP {exc.code}") from exc
    except TimeoutError as exc:
        raise RuntimeError("ntfy poll timed out") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError("ntfy poll failed") from exc
    found: list[dict] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict) and item.get("event") == "message":
            found.append(item)
    return found


def claim_reply_text(item: dict) -> str | None:
    """The person's words, or None for an empty body or a message this program sent."""
    tags = item.get("tags") or []
    if isinstance(tags, str):
        tags = [part.strip() for part in tags.split(",") if part.strip()]
    if ECHO_TAG in tags:
        return None
    text = item.get("message") or ""
    if not isinstance(text, str):
        return None
    text = text.strip()
    return text or None


def send_claim(base: str, topic: str, *, title: str, message: str, image: Path | None) -> None:
    """Picture first when there is one, then the words. Never one combined post."""
    if image is not None and image.is_file() and image.suffix.lower() != ".pdf":
        caption = message.splitlines()[0][:180] if message.strip() else title
        send_image(base, topic, title=title, caption=caption, image=image)
    send_text(base, topic, title=title, message=message)


