"""Slack's OAuth v2 flow with PKCE, for a user token and no client secret.

PKCE (RFC 7636): lemonaid makes a random code verifier and sends its SHA-256 hash, the
code challenge, with the authorize request. It sends the verifier itself with the code
exchange, so Slack knows the same program asked both times. The app must have PKCE
enabled, and must not have token rotation enabled (see docs/slack.md).
"""

import base64
import hashlib
import html
import http.server
import json
import secrets
import threading
import typing as ty
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

AUTHORIZE = "https://slack.com/oauth/v2/authorize"
EXCHANGE = "https://slack.com/api/oauth.v2.access"
SCOPES = ("channels:history", "groups:history", "chat:write")
CALLBACK_PATH = "/slack/oauth"
WAIT_SECONDS = 300.0

Opener = ty.Callable[..., ty.Any]


class LoginError(RuntimeError):
    """The login failed. The message never contains a token or a code."""


@dataclass(frozen=True)
class Pkce:
    verifier: str
    challenge: str
    state: str


@dataclass(frozen=True)
class Grant:
    """What the code exchange gave. `repr` hides the token."""

    token: str
    user: str
    team: str
    scopes: tuple[str, ...]

    def __repr__(self) -> str:
        return f"Grant(<token>, user={self.user!r}, team={self.team!r}, scopes={self.scopes!r})"


def challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def new_pkce() -> Pkce:
    verifier = secrets.token_urlsafe(64)  # 86 characters, within RFC 7636's 43 to 128
    return Pkce(verifier, challenge(verifier), secrets.token_urlsafe(24))


def redirect_uri(port: int) -> str:
    return f"http://localhost:{port}{CALLBACK_PATH}"


def authorize_url(client_id: str, redirect: str, pkce: Pkce) -> str:
    return (
        AUTHORIZE
        + "?"
        + urllib.parse.urlencode(
            {
                "client_id": client_id,
                "scope": "",
                "user_scope": ",".join(SCOPES),
                "redirect_uri": redirect,
                "code_challenge": pkce.challenge,
                "code_challenge_method": "S256",
                "state": pkce.state,
            }
        )
    )


_PAGE = """<!doctype html><meta charset="utf-8"><title>lemonaid</title>
<body style="font-family: sans-serif; margin: 3em"><p>{}</p></body>"""


class _Callback(http.server.BaseHTTPRequestHandler):
    server: "_Server"

    def do_GET(self) -> None:
        url = urllib.parse.urlsplit(self.path)
        if url.path != CALLBACK_PATH:
            self.send_error(404)
            return

        query = urllib.parse.parse_qs(url.query)
        first = {key: values[0] for key, values in query.items() if values}
        if first.get("state") != self.server.state:
            message, ok = (
                "This sign-in link is not the one lemonaid started. Run `lemonaid slack login` again.",
                False,
            )
            self.server.result = LoginError("the browser returned a state that does not match")
        elif "error" in first:
            message, ok = f"Slack did not sign you in: {first['error']}.", False
            self.server.result = LoginError(f"Slack did not sign you in: {first['error']}")
        elif first.get("code"):
            message, ok = "lemonaid is signed in to Slack. You can close this tab.", True
            self.server.result = first["code"]
        else:
            message, ok = "Slack sent no code. Run `lemonaid slack login` again.", False
            self.server.result = LoginError("Slack redirected without a code")

        body = _PAGE.format(html.escape(message)).encode()
        self.send_response(200 if ok else 400)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        self.server.done.set()

    def log_message(self, format: str, *args: ty.Any) -> None:
        pass  # the request line carries the code; keep it off the terminal


class _Server(http.server.HTTPServer):
    def __init__(self, port: int, state: str):
        super().__init__(("127.0.0.1", port), _Callback)
        self.state = state
        self.result: str | LoginError | None = None
        self.done = threading.Event()


class Listener:
    """Listens on 127.0.0.1 for Slack's redirect. Bind first, then open the browser."""

    def __init__(self, port: int, state: str):
        try:
            self._server = _Server(port, state)
        except OSError as error:
            raise LoginError(
                f"cannot listen on port {port} for Slack's redirect ({error.strerror});"
                " close what uses it, or change [slack] redirect_port and the app's redirect URL"
            ) from None
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def wait(self, seconds: float = WAIT_SECONDS) -> str:
        """The code Slack sent, or LoginError."""
        try:
            if not self._server.done.wait(seconds):
                raise LoginError(f"no answer from Slack within {seconds:.0f} seconds")
        finally:
            self.close()
        result = self._server.result
        if isinstance(result, LoginError):
            raise result
        assert isinstance(result, str)
        return result

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


def exchange(
    client_id: str,
    code: str,
    verifier: str,
    redirect: str,
    opener: Opener = urllib.request.urlopen,
) -> Grant:
    """Trade the code for a user token, with the verifier and no client secret."""
    request = urllib.request.Request(
        EXCHANGE,
        data=urllib.parse.urlencode(
            {
                "client_id": client_id,
                "code": code,
                "code_verifier": verifier,
                "redirect_uri": redirect,
                "grant_type": "authorization_code",
            }
        ).encode(),
    )
    try:
        with opener(request, timeout=30) as response:
            body = json.loads(response.read())
    except urllib.error.HTTPError as error:
        raise LoginError(f"oauth.v2.access returned HTTP {error.code}") from None
    except (urllib.error.URLError, OSError, ValueError) as error:
        raise LoginError(f"oauth.v2.access failed: {type(error).__name__}") from None

    if not body.get("ok"):
        raise LoginError(f"Slack refused the sign-in: {body.get('error', 'unknown_error')}")

    user = body.get("authed_user") or {}
    if user.get("refresh_token") or body.get("refresh_token"):
        raise LoginError(
            "the Slack app has token rotation on, and lemonaid does not refresh tokens;"
            " the app needs token_rotation_enabled: false (see docs/slack.md)"
        )
    token = str(user.get("access_token") or "")
    if not token.startswith("xoxp-"):
        raise LoginError("Slack returned no user token; check the app's user scopes")

    scopes = tuple(s for s in str(user.get("scope", "")).split(",") if s)
    missing = [s for s in SCOPES if s not in scopes]
    if missing:
        raise LoginError(f"the token lacks {', '.join(missing)}; add them to the app's user scopes")
    return Grant(
        token, str(user.get("id", "")), str((body.get("team") or {}).get("id", "")), scopes
    )
