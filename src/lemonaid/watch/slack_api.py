"""Read a Slack channel's or thread's messages with the Web API, over urllib.

`conversations.history` and `conversations.replies` are Tier 3 ("50+ per minute") for an
internal app, counted per method, per workspace and per app, so every watch through one
shared app shares them. A 429 raises `Unavailable` with Slack's `Retry-After`.
"""

import json
import typing as ty
import urllib.error
import urllib.parse
import urllib.request

_API = "https://slack.com/api/"
_PAGE = 200
_MAX_PAGES = 10
# Errors that no retry fixes: the token or the channel is wrong.
_REJECTIONS = frozenset(
    {
        "account_inactive",
        "channel_not_found",
        "invalid_auth",
        "missing_scope",
        "no_permission",
        "not_authed",
        "not_in_channel",
        "thread_not_found",
        "token_expired",
        "token_revoked",
    }
)


class Message(ty.NamedTuple):
    ts: str
    user: str
    text: str
    subtype: str = ""
    thread_ts: str = ""


class Rejected(RuntimeError):
    """Slack refused the token or the conversation; retrying will not help."""


class Unavailable(Exception):
    """A transient failure; `retry_after` is Slack's Retry-After, or 0."""

    def __init__(self, message: str, retry_after: float = 0.0):
        super().__init__(message)
        self.retry_after = retry_after


class Client(ty.Protocol):
    def workspace_url(self) -> str: ...

    def history(self, channel: str, oldest: str | None) -> list[Message]: ...

    def replies(self, channel: str, thread: str, oldest: str) -> list[Message]: ...


Opener = ty.Callable[..., ty.Any]


def _message(raw: dict) -> Message:
    return Message(
        str(raw.get("ts", "")),
        str(raw.get("user") or raw.get("bot_id") or raw.get("username") or "unknown"),
        str(raw.get("text", "")),
        str(raw.get("subtype", "")),
        str(raw.get("thread_ts", "")),
    )


def newest_first(messages: ty.Iterable[Message]) -> list[Message]:
    return sorted(messages, key=lambda m: float(m.ts), reverse=True)


class HttpClient:
    """Calls Slack with a user or bot token, which it keeps in memory only."""

    def __init__(self, token: str, opener: Opener = urllib.request.urlopen, timeout: float = 30):
        self._token = token
        self._open = opener
        self._timeout = timeout

    def __repr__(self) -> str:
        return "HttpClient(<token>)"

    def _call(self, method: str, **params: str) -> dict:
        request = urllib.request.Request(
            _API + method,
            data=urllib.parse.urlencode(params).encode(),
            headers={"Authorization": f"Bearer {self._token}"},
        )
        try:
            with self._open(request, timeout=self._timeout) as response:
                body = json.loads(response.read())
        except urllib.error.HTTPError as error:
            if error.code == 429:
                raise Unavailable(
                    f"{method} rate limited", float(error.headers.get("Retry-After") or 60)
                ) from None
            raise Unavailable(f"{method} returned HTTP {error.code}") from None
        except (urllib.error.URLError, OSError, ValueError) as error:
            raise Unavailable(f"{method} failed: {type(error).__name__}") from None

        if body.get("ok"):
            return body

        error = str(body.get("error", "unknown_error"))
        if error in _REJECTIONS:
            raise Rejected(f"Slack refused {method}: {error}")
        if error == "ratelimited":
            raise Unavailable(f"{method} rate limited", 60.0)
        raise Unavailable(f"Slack {method} failed: {error}")

    def _pages(self, method: str, key: str = "messages", **params: str) -> list[dict]:
        found: list[dict] = []
        cursor = ""
        for _ in range(_MAX_PAGES):
            body = self._call(
                method, **params, limit=str(_PAGE), **({"cursor": cursor} if cursor else {})
            )
            found.extend(body.get(key) or [])
            cursor = (body.get("response_metadata") or {}).get("next_cursor", "")
            if not cursor:
                break
        return found

    def workspace_url(self) -> str:
        """The workspace's URL from `auth.test`, which also checks the token."""
        return str(self._call("auth.test").get("url", ""))

    def history(self, channel: str, oldest: str | None) -> list[Message]:
        """Top-level messages after *oldest*, newest first; with None, only the newest one."""
        if oldest is None:
            body = self._call("conversations.history", channel=channel, limit="1")
            return [_message(m) for m in body.get("messages") or []]

        raw = self._pages("conversations.history", channel=channel, oldest=oldest)
        return newest_first(_message(m) for m in raw)

    def replies(self, channel: str, thread: str, oldest: str) -> list[Message]:
        """The thread's messages after *oldest*, newest first. Slack always includes the parent."""
        raw = self._pages("conversations.replies", channel=channel, ts=thread, oldest=oldest)
        return newest_first(_message(m) for m in raw)
