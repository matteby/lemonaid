"""Compare a Slack channel's or thread's messages with what a lemon was last told.

The cursor is the `ts` of the newest message the lemon has seen. A first poll takes the
newest message as seen and reports nothing; a channel watch reads only the newest message
for that, and a thread watch starts from the thread's parent. Later polls ask Slack for
messages after the cursor. A channel watch reports top-level messages (and replies also
sent to the channel); a thread watch reports replies. Joins, leaves and other channel
notices are skipped, as is a message signed `me`, but the cursor moves past them all.
"""

import hashlib
import html
import json
import pathlib
import re
import time
from collections import abc

from . import cursors, delivery, slack_api

_SHOWN = frozenset({"", "thread_broadcast", "bot_message", "file_share", "me_message"})
_EXCERPT = 300
_MARKERS = ("🍋", ":lemon:")


def state_stem(state_dir: pathlib.Path, channel: str, thread: str, me: str) -> pathlib.Path:
    key = channel + (f"/{thread}" if thread else "") + (f"\0{me}" if me else "")
    return state_dir / ("slack-" + hashlib.sha1(key.encode()).hexdigest()[:16])


def signed(text: str, me: str) -> bool:
    """Whether *text* starts with the signature *me*, after an optional lemon marker."""
    if not me:
        return False

    text = text.lstrip()
    for marker in _MARKERS:
        if text.startswith(marker):
            text = text[len(marker) :].lstrip()
            break
    me = me.strip()
    for marker in _MARKERS:
        if me.startswith(marker):
            me = me[len(marker) :].lstrip()
            break
    return text.startswith(me)


def _excerpt(text: str) -> str:
    text = re.sub(r"\s+", " ", html.unescape(text)).strip()
    return text if len(text) <= _EXCERPT else text[: _EXCERPT - 1] + "…"


def permalink(base: str, channel: str, message: slack_api.Message, thread: str) -> str:
    if not base:
        return ""

    link = f"{base.rstrip('/')}/archives/{channel}/p{message.ts.replace('.', '')}"
    return f"{link}?thread_ts={thread}&cid={channel}" if thread else link


def describe(
    label: str, channel: str, thread: str, message: slack_api.Message, base: str = ""
) -> str:
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(float(message.ts)))
    where = f"slack #{label}" + (f" thread {thread}" if thread else "")
    what = "reply" if thread else "message"
    line = f'{where}: {what} from {message.user} at {when}: "{_excerpt(message.text)}" (ts {message.ts})'
    link = permalink(base, channel, message, thread)
    return f"{line} {link}" if link else line


def events(
    label: str,
    channel: str,
    thread: str,
    cursor: str | None,
    messages: abc.Iterable[slack_api.Message],
    me: str = "",
    base: str = "",
) -> tuple[list[str], str]:
    """The lines to report, oldest first, and the cursor to save once they are delivered."""
    messages = [m for m in messages if m.ts and m.ts != thread]
    newest = max((m.ts for m in messages), key=float, default=None)
    if cursor is None:
        return [], newest or thread or "0"

    fresh = sorted((m for m in messages if float(m.ts) > float(cursor)), key=lambda m: float(m.ts))
    lines = [
        describe(label, channel, thread, m, base)
        for m in fresh
        if m.subtype in _SHOWN and not signed(m.text, me)
    ]
    return lines, max(newest or cursor, cursor, key=float)


def load(path: pathlib.Path) -> str | None:
    try:
        saved = json.loads(path.read_text())
    except (OSError, ValueError):
        return None

    return saved.get("ts") if isinstance(saved, dict) and isinstance(saved.get("ts"), str) else None


def fetch(
    client: slack_api.Client, channel: str, thread: str, cursor: str | None
) -> list[slack_api.Message]:
    if thread:
        return client.replies(channel, thread, cursor or thread)

    return client.history(channel, cursor)


def poll(
    stem: pathlib.Path,
    client: slack_api.Client,
    label: str,
    channel: str,
    thread: str,
    deliver: delivery.Deliver,
    me: str = "",
    base: str = "",
) -> bool:
    """True if an event was delivered. A delivery that raises leaves the cursor unchanged.

    Slack failures (`slack_api.Unavailable`, `slack_api.Rejected`) propagate to the caller.
    """
    path = stem.with_suffix(".seen.json")
    cursor = load(path)
    lines, now = events(
        label, channel, thread, cursor, fetch(client, channel, thread, cursor), me, base
    )
    if lines:
        deliver("\n".join(lines))
    cursors.write_json(path, {"ts": now})
    return bool(lines)
