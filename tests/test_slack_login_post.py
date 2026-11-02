"""`lemonaid slack login` and `lemonaid slack post`, with fakes for Slack, the browser
and the keychain. No test calls Slack, opens a browser or touches the real keychain.

The login tests run the real callback listener on a free 127.0.0.1 port, and a fake
browser follows Slack's redirect to it.
"""

import argparse
import json
import subprocess
import threading
import urllib.error
import urllib.parse
import urllib.request

import pytest

from lemonaid import slack_config
from lemonaid.slack import cli, keychain, oauth
from lemonaid.watch import slack_api

TOKEN = "xoxp-1-not-a-real-token"
CHANNEL = "C0123ABCDEF"
THREAD = "1712345678.123456"


class _Response:
    def __init__(self, body: bytes):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _opener(*bodies):
    sent = []

    def open_(request, timeout):
        sent.append(request)
        body = bodies[len(sent) - 1]
        if isinstance(body, Exception):
            raise body
        return _Response(json.dumps(body).encode())

    return open_, sent


def _granted(**user):
    return {
        "ok": True,
        "team": {"id": "T1"},
        "authed_user": {
            "id": "U1",
            "scope": "channels:history,groups:history,chat:write",
            "access_token": TOKEN,
            "token_type": "user",
            **user,
        },
    }


# PKCE and the authorize URL


def test_the_code_challenge_matches_rfc_7636():
    # RFC 7636 appendix B.
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"

    assert oauth.challenge(verifier) == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


def test_each_login_gets_a_new_verifier_within_the_allowed_length():
    first, second = oauth.new_pkce(), oauth.new_pkce()

    assert 43 <= len(first.verifier) <= 128
    assert first.verifier != second.verifier and first.state != second.state
    assert first.challenge == oauth.challenge(first.verifier)


def test_the_authorize_url_asks_for_user_scopes_only():
    pkce = oauth.Pkce("v", "c", "s")
    url = urllib.parse.urlsplit(oauth.authorize_url("123.456", oauth.redirect_uri(8723), pkce))
    query = dict(urllib.parse.parse_qsl(url.query, keep_blank_values=True))

    assert url.netloc == "slack.com" and url.path == "/oauth/v2/authorize"
    assert query == {
        "client_id": "123.456",
        "scope": "",
        "user_scope": "channels:history,groups:history,chat:write",
        "redirect_uri": "http://localhost:8723/slack/oauth",
        "code_challenge": "c",
        "code_challenge_method": "S256",
        "state": "s",
    }


# The callback listener


def _local_get(url: str) -> int:
    """Follow a redirect to the listener, bypassing any proxy settings."""
    open_ = urllib.request.build_opener(urllib.request.ProxyHandler({})).open
    try:
        with open_(url.replace("localhost", "127.0.0.1"), timeout=10) as response:
            return response.status
    except urllib.error.HTTPError as error:
        return error.code


def _redirect(port: int, **query) -> str:
    return f"http://127.0.0.1:{port}{oauth.CALLBACK_PATH}?{urllib.parse.urlencode(query)}"


def test_the_listener_returns_the_code_for_the_right_state():
    listener = oauth.Listener(0, "good")

    assert _local_get(_redirect(listener.port, code="the-code", state="good")) == 200
    assert listener.wait(5) == "the-code"


@pytest.mark.parametrize(
    ("query", "match"),
    [
        ({"code": "c", "state": "forged"}, "state"),
        ({"error": "access_denied", "state": "good"}, "access_denied"),
        ({"state": "good"}, "without a code"),
    ],
)
def test_the_listener_refuses_a_wrong_state_or_a_slack_error(query, match):
    listener = oauth.Listener(0, "good")

    assert _local_get(_redirect(listener.port, **query)) == 400
    with pytest.raises(oauth.LoginError, match=match):
        listener.wait(5)


def test_the_listener_gives_up_after_its_timeout():
    listener = oauth.Listener(0, "good")

    with pytest.raises(oauth.LoginError, match="no answer"):
        listener.wait(0.05)


def test_a_port_in_use_names_the_config_key():
    first = oauth.Listener(0, "a")
    try:
        with pytest.raises(oauth.LoginError, match="redirect_port"):
            oauth.Listener(first.port, "b")
    finally:
        first.close()


# The code exchange


def test_the_exchange_sends_the_verifier_and_no_client_secret():
    open_, sent = _opener(_granted())

    grant = oauth.exchange("123.456", "the-code", "the-verifier", "http://localhost:1/x", open_)

    form = dict(urllib.parse.parse_qsl(sent[0].data.decode()))
    assert sent[0].full_url == "https://slack.com/api/oauth.v2.access"
    assert form == {
        "client_id": "123.456",
        "code": "the-code",
        "code_verifier": "the-verifier",
        "redirect_uri": "http://localhost:1/x",
        "grant_type": "authorization_code",
    }
    assert sent[0].get_header("Authorization") is None
    assert (grant.token, grant.user, grant.team) == (TOKEN, "U1", "T1")
    assert TOKEN not in repr(grant)


@pytest.mark.parametrize(
    ("body", "match"),
    [
        (_granted(refresh_token="xoxe-1-r", expires_in=43200), "token rotation"),
        (_granted(scope="channels:history"), "groups:history, chat:write"),
        ({"ok": False, "error": "invalid_code"}, "invalid_code"),
        ({"ok": True, "access_token": "xoxb-bot", "authed_user": {"id": "U1"}}, "no user token"),
    ],
)
def test_the_exchange_refuses_rotation_missing_scopes_and_errors(body, match):
    open_, _ = _opener(body)

    with pytest.raises(oauth.LoginError, match=match) as raised:
        oauth.exchange("123.456", "c", "v", "r", open_)

    assert TOKEN not in str(raised.value)


# The keychain


def test_the_keychain_write_sends_the_token_on_standard_input(monkeypatch):
    monkeypatch.setattr(keychain, "available", lambda: True)
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, "", "")

    keychain.write(TOKEN, run, user="matt.eby")

    ((argv, kwargs),) = calls
    assert argv == ["security", "-i"]
    assert TOKEN not in " ".join(argv)
    assert kwargs["input"].startswith('add-generic-password -U -s lemonaid-slack -a "matt.eby"')
    assert f'-w "{TOKEN}"' in kwargs["input"]


@pytest.mark.parametrize(
    ("token", "user", "match"),
    [
        ('xoxp-1" -s other', "matt", "unexpected form"),
        (TOKEN, 'matt" -s x', "cannot name"),
    ],
)
def test_the_keychain_write_refuses_text_that_could_change_the_command(
    monkeypatch, token, user, match
):
    monkeypatch.setattr(keychain, "available", lambda: True)

    with pytest.raises(keychain.KeychainError, match=match) as raised:
        keychain.write(token, lambda *a, **k: pytest.fail("ran security"), user=user)

    assert TOKEN not in str(raised.value)


def test_a_failed_keychain_write_never_shows_the_token(monkeypatch):
    monkeypatch.setattr(keychain, "available", lambda: True)

    def run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, kwargs["input"], "security: error")

    with pytest.raises(keychain.KeychainError) as raised:
        keychain.write(TOKEN, run, user="matt")

    assert TOKEN not in str(raised.value)


def test_the_keychain_read_asks_security_for_the_password_only(monkeypatch):
    monkeypatch.setattr(keychain, "available", lambda: True)
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, TOKEN + "\n", "")

    assert keychain.read(run, user="matt") == TOKEN
    assert calls == [
        ["security", "find-generic-password", "-s", "lemonaid-slack", "-a", "matt", "-w"]
    ]
    assert keychain.read(lambda argv, **k: subprocess.CompletedProcess(argv, 44, "", "")) == ""


def test_nothing_is_stored_off_macos(monkeypatch):
    monkeypatch.setattr(keychain, "available", lambda: False)

    with pytest.raises(keychain.KeychainError, match="macOS"):
        keychain.write(TOKEN, lambda *a, **k: pytest.fail("ran security"), user="matt")
    assert keychain.read(lambda *a, **k: pytest.fail("ran security")) == ""


# `lemonaid slack login`


class _FakeClient:
    def __init__(self, token, posted=None, fail=None, base="https://acme.slack.com/"):
        self.token = token
        self.posted = posted if posted is not None else []
        self.fail = fail
        self.base = base

    def whoami(self):
        return "matt", "Acme", self.base

    def workspace_url(self):
        return self.base

    def post(self, channel, text, thread=""):
        if self.fail:
            raise self.fail
        self.posted.append((channel, text, thread))
        return "1793400000.000100"


@pytest.fixture
def login_fakes(monkeypatch):
    """A browser that approves at once, a fake Slack and a fake keychain."""
    monkeypatch.setattr(keychain, "available", lambda: True)
    stored = []
    seen = {}

    def browser(url):
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        seen.update(query)
        redirect = (
            query["redirect_uri"]
            + "?"
            + urllib.parse.urlencode({"code": "the-code", "state": query["state"]})
        )
        threading.Thread(target=_local_get, args=(redirect,), daemon=True).start()
        return True

    open_, sent = _opener(_granted())
    monkeypatch.setattr(cli, "open_browser", browser)
    monkeypatch.setattr(cli, "opener", open_)
    monkeypatch.setattr(cli, "store", stored.append)
    monkeypatch.setattr(cli, "make_client", _FakeClient)
    return stored, seen, sent


def _login_config(**kw):
    return slack_config.SlackConfig(client_id="123.456", redirect_port=0, **kw)


def test_login_stores_the_token_and_never_prints_it(login_fakes, capsys):
    stored, seen, sent = login_fakes

    assert cli.login(_login_config(), timeout=10) == 0

    out = capsys.readouterr().out
    assert stored == [TOKEN]
    assert TOKEN not in out
    assert "Signed in to Slack as matt in Acme" in out
    form = dict(urllib.parse.parse_qsl(sent[0].data.decode()))
    assert form["redirect_uri"] == seen["redirect_uri"]
    assert oauth.challenge(form["code_verifier"]) == seen["code_challenge"]


def test_login_needs_a_client_id(login_fakes, capsys):
    assert cli.login(slack_config.SlackConfig(), timeout=1) == 2
    assert "[slack] client_id" in capsys.readouterr().out


def test_login_stops_off_macos(monkeypatch, capsys):
    monkeypatch.setattr(keychain, "available", lambda: False)

    assert cli.login(_login_config(), timeout=1) == 2
    assert "token_command" in capsys.readouterr().out


def test_login_stores_nothing_when_slack_refuses(login_fakes, monkeypatch, capsys):
    stored, _, _ = login_fakes
    open_, _ = _opener({"ok": False, "error": "invalid_code"})
    monkeypatch.setattr(cli, "opener", open_)

    assert cli.login(_login_config(), timeout=10) == 2
    assert stored == []
    assert "invalid_code" in capsys.readouterr().out


# `lemonaid slack post`


@pytest.fixture
def posting(monkeypatch):
    posted = []
    clients = []

    def make(token, **kw):
        clients.append(_FakeClient(token, posted, **kw))
        return clients[-1]

    monkeypatch.setattr(cli, "make_client", make)
    monkeypatch.setattr(cli, "read_token", lambda config: TOKEN)
    return posted, monkeypatch


ALLOWED = slack_config.SlackConfig(channels={"my-hq": CHANNEL})


def test_post_signs_the_message_and_prints_its_ts_and_link(posting, capsys):
    posted, _ = posting

    assert cli.post(ALLOWED, "#my-hq", "  The run finished.\n", "🍋 LemonAce") == 0

    assert posted == [(CHANNEL, "🍋 LemonAce The run finished.", "")]
    out = capsys.readouterr().out
    assert out.strip() == (
        "posted to #my-hq: ts 1793400000.000100"
        " https://acme.slack.com/archives/C0123ABCDEF/p1793400000000100"
    )
    assert TOKEN not in out


def test_post_does_not_sign_twice_and_replies_in_a_thread(posting, capsys):
    posted, _ = posting

    assert cli.post(ALLOWED, CHANNEL, "🍋 LemonAce: done", "🍋 LemonAce", THREAD) == 0
    assert cli.post(ALLOWED, CHANNEL, "LemonAce: done", "🍋 LemonAce", THREAD) == 0

    assert posted == [
        (CHANNEL, "🍋 LemonAce: done", THREAD),
        (CHANNEL, "LemonAce: done", THREAD),
    ]
    assert f"thread_ts={THREAD}" in capsys.readouterr().out


def test_post_refuses_an_unlisted_channel_and_an_empty_message(posting, capsys):
    posted, _ = posting

    assert cli.post(ALLOWED, "general", "hi", "🍋 A") == 2
    assert cli.post(ALLOWED, "my-hq", "  \n", "🍋 A") == 2
    assert posted == []
    out = capsys.readouterr().out
    assert "not in [slack.channels]" in out and "empty" in out


def test_a_post_that_may_have_gone_through_is_not_retried(posting, capsys):
    _, monkeypatch = posting
    monkeypatch.setattr(
        cli, "make_client", lambda token: _FakeClient(token, fail=slack_api.Unavailable("timeout"))
    )

    assert cli.post(ALLOWED, "my-hq", "hi", "🍋 A") == 1
    assert "check the channel before posting again" in capsys.readouterr().out


def test_a_refused_post_exits_2(posting, capsys):
    _, monkeypatch = posting
    monkeypatch.setattr(
        cli,
        "make_client",
        lambda token: _FakeClient(token, fail=slack_api.Rejected("Slack refused: not_in_channel")),
    )

    assert cli.post(ALLOWED, "my-hq", "hi", "🍋 A") == 2
    assert "not_in_channel" in capsys.readouterr().out


def test_the_client_posts_with_chat_post_message():
    open_, sent = _opener({"ok": True, "channel": CHANNEL, "ts": "5.0"})

    assert slack_api.HttpClient(TOKEN, open_).post(CHANNEL, "hi", THREAD) == "5.0"

    form = dict(urllib.parse.parse_qsl(sent[0].data.decode()))
    assert sent[0].full_url.endswith("/chat.postMessage")
    assert form == {"channel": CHANNEL, "text": "hi", "thread_ts": THREAD}
    assert sent[0].get_header("Authorization") == f"Bearer {TOKEN}"


def test_the_post_command_parses(monkeypatch):
    from lemonaid.slack import cli as slack_cli

    parser = argparse.ArgumentParser()
    slack_cli.setup_parser(parser.add_subparsers())

    a = parser.parse_args(["slack", "post", "my-hq", "hi", "--me", "🍋 A", "--thread", THREAD])
    assert (a.channel, a.text, a.me, a.thread) == ("my-hq", "hi", "🍋 A", THREAD)
    with pytest.raises(SystemExit):
        parser.parse_args(["slack", "post", "my-hq", "hi"])  # no --me
