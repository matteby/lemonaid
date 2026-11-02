"""`watch slack` reports new channel messages and thread replies from a fake Slack client.

No test calls Slack or reads a real token.
"""

import argparse
import datetime
import io
import json
import subprocess
import urllib.error

import pytest

from lemonaid import config, slack_config
from lemonaid.brief import attached, store, waiters
from lemonaid.inbox import db
from lemonaid.watch import (
    cli,
    delivery,
    registry,
    scout_pollers,
    slack_api,
    slack_cli,
    slack_events,
    subscriptions,
)

from .wait import shared

CHANNEL = "C0123ABCDEF"
THREAD = "1712345678.123456"
TOKEN = "xoxp-not-a-real-token"


@pytest.fixture(autouse=True)
def _no_keychain(monkeypatch):
    """The token falls back to the keychain; no test reads the real one."""
    from lemonaid.slack import keychain

    monkeypatch.setattr(keychain, "read", lambda *a, **k: "")


def _m(ts, text="hi", user="U1", subtype="", thread_ts=""):
    return slack_api.Message(ts, user, text, subtype, thread_ts)


class FakeClient:
    def __init__(self, messages=(), replies=(), url="https://acme.slack.com/"):
        self.messages = list(messages)
        self.thread = list(replies)
        self.url = url
        self.calls = []
        self.fail: Exception | None = None

    def workspace_url(self):
        return self.url

    def history(self, channel, oldest):
        self.calls.append(("history", channel, oldest))
        if self.fail:
            raise self.fail
        found = slack_api.newest_first(self.messages)
        return found[:1] if oldest is None else [m for m in found if float(m.ts) > float(oldest)]

    def replies(self, channel, thread, oldest):
        self.calls.append(("replies", channel, thread, oldest))
        if self.fail:
            raise self.fail
        parent = _m(thread, "parent", thread_ts=thread)
        return slack_api.newest_first(
            [parent, *(m for m in self.thread if float(m.ts) > float(oldest))]
        )


def _poll(tmp_path, client, thread="", me=""):
    got = []
    delivered = slack_events.poll(
        slack_events.state_stem(tmp_path, CHANNEL, thread, ""),
        client,
        "my-hq",
        CHANNEL,
        thread,
        got.append,
        me,
        client.url,
    )
    return delivered, got


# The cursor


def test_the_first_poll_reads_only_the_newest_message_and_reports_nothing(tmp_path):
    client = FakeClient([_m("100.000001"), _m("200.000002")])

    assert _poll(tmp_path, client) == (False, [])
    assert client.calls == [("history", CHANNEL, None)]


def test_messages_after_the_cursor_are_reported_oldest_first_in_one_event(tmp_path):
    client = FakeClient([_m("100.000001")])
    _poll(tmp_path, client)
    client.messages += [_m("300.000003", "second"), _m("200.000002", "first")]

    delivered, [event] = _poll(tmp_path, client)

    assert delivered
    first, second = event.splitlines()
    assert '"first" (ts 200.000002)' in first and '"second" (ts 300.000003)' in second
    assert client.calls[-1] == ("history", CHANNEL, "100.000001")


def test_the_cursor_moves_so_a_message_is_reported_once(tmp_path):
    client = FakeClient([_m("100.000001")])
    _poll(tmp_path, client)
    client.messages.append(_m("200.000002"))
    _poll(tmp_path, client)

    assert _poll(tmp_path, client) == (False, [])
    assert client.calls[-1] == ("history", CHANNEL, "200.000002")


def test_a_failed_delivery_leaves_the_cursor_so_the_next_waiter_reports_it(tmp_path):
    client = FakeClient([_m("100.000001")])
    _poll(tmp_path, client)
    client.messages.append(_m("200.000002", "again"))

    def refuse(message):
        raise delivery.Failed("codex queue failed")

    with pytest.raises(delivery.Failed):
        slack_events.poll(
            slack_events.state_stem(tmp_path, CHANNEL, "", ""),
            client,
            "my-hq",
            CHANNEL,
            "",
            refuse,
        )
    _, [event] = _poll(tmp_path, client)

    assert '"again"' in event


def test_an_empty_channel_reports_its_first_message(tmp_path):
    client = FakeClient()
    _poll(tmp_path, client)
    client.messages.append(_m("100.000001", "hello"))

    _, [event] = _poll(tmp_path, client)

    assert '"hello"' in event


# What is reported


def test_a_line_names_the_channel_author_text_ts_and_link(tmp_path):
    [line], _ = slack_events.events(
        "my-hq",
        CHANNEL,
        "",
        "1.0",
        [_m("1712345678.123456", "check  the\nUA run", user="U0ABC")],
        base="https://acme.slack.com/",
    )

    assert line.startswith("slack #my-hq: message from U0ABC at ")
    assert line.endswith(
        '"check the UA run" (ts 1712345678.123456)'
        f" https://acme.slack.com/archives/{CHANNEL}/p1712345678123456"
    )


def test_joins_and_signed_messages_are_skipped_but_the_cursor_passes_them():
    messages = [
        _m("2.0", "joined", subtype="channel_join"),
        _m("3.0", "🍋 LemonAce: done"),
        _m("4.0", ":lemon: LemonAce: also mine"),
        _m("5.0", "broadcast", subtype="thread_broadcast"),
    ]

    lines, cursor = slack_events.events("my-hq", CHANNEL, "", "1.0", messages, me="🍋 LemonAce")

    assert [line.split('"')[1] for line in lines] == ["broadcast"]
    assert cursor == "5.0"


def test_long_text_is_cut_and_entities_unescaped():
    [line], _ = slack_events.events(
        "x", CHANNEL, "", "1.0", [_m("2.0", "a &lt;b&gt; " + "z" * 400)]
    )

    excerpt = line.split('"')[1]
    assert excerpt.startswith("a <b> ") and len(excerpt) == 300 and excerpt.endswith("…")


def test_ts_values_compare_as_numbers_not_strings():
    lines, cursor = slack_events.events("x", CHANNEL, "", "999.000001", [_m("1000.000001")])

    assert len(lines) == 1 and cursor == "1000.000001"


# Threads


def test_a_thread_watch_starts_from_the_newest_reply_and_skips_the_parent(tmp_path):
    client = FakeClient(replies=[_m("1793397300.000001", "old reply", thread_ts=THREAD)])

    assert _poll(tmp_path, client, THREAD) == (False, [])
    client.thread.append(_m("1793397400.000001", "new reply", thread_ts=THREAD))
    _, [event] = _poll(tmp_path, client, THREAD)

    assert event.startswith(f"slack #my-hq thread {THREAD}: reply from U1")
    assert '"new reply"' in event and "parent" not in event
    assert event.endswith(f"?thread_ts={THREAD}&cid={CHANNEL}")
    assert client.calls[0] == ("replies", CHANNEL, THREAD, THREAD)


def test_a_thread_with_no_replies_reports_its_first_reply(tmp_path):
    client = FakeClient()
    _poll(tmp_path, client, THREAD)
    client.thread.append(_m("1793397400.000001", "first", thread_ts=THREAD))

    _, [event] = _poll(tmp_path, client, THREAD)

    assert '"first"' in event


def test_a_thread_and_its_channel_keep_separate_cursors(tmp_path):
    assert slack_events.state_stem(tmp_path, CHANNEL, "", "") != slack_events.state_stem(
        tmp_path, CHANNEL, THREAD, ""
    )


# The signature


@pytest.mark.parametrize(
    "text",
    ["🍋 LemonAce: hi", "LemonAce (Author): hi", ":lemon: LemonAce hi", "  🍋LemonAce"],
)
def test_signed_matches_with_or_without_the_lemon_marker(text):
    assert slack_events.signed(text, "🍋 LemonAce")


def test_signed_needs_the_signature_at_the_start():
    assert not slack_events.signed("ask LemonAce to look", "LemonAce")
    assert not slack_events.signed("anything", "")


# The config


def _write_config(tmp_path, text):
    (tmp_path / "config.toml").write_text(text)


def test_the_slack_table_is_read_from_config(tmp_path):
    _write_config(
        tmp_path,
        """
[slack]
token_command = ["security", "find-generic-password", "-s", "lemonaid-slack", "-w"]
[slack.channels]
my-hq = "C0123ABCDEF"
"#team-alerts" = "C0456GHIJKL"
bad = "general"
""",
    )

    slack = config.load_config(tmp_path / "config.toml").slack

    assert slack.token_command[0] == "security"
    assert slack.channels == {"my-hq": CHANNEL, "team-alerts": "C0456GHIJKL"}


@pytest.mark.parametrize("given", ["my-hq", "#my-hq", CHANNEL])
def test_a_listed_channel_resolves_by_name_or_id(given):
    allowed = slack_config.SlackConfig(channels={"my-hq": CHANNEL})

    assert slack_config.resolve(allowed, given) == (CHANNEL, "my-hq")


def test_an_unlisted_channel_is_refused_even_by_id():
    allowed = slack_config.SlackConfig(channels={"my-hq": CHANNEL})

    with pytest.raises(ValueError, match=r"not in \[slack.channels\]"):
        slack_config.resolve(allowed, "C0456GHIJKL")
    with pytest.raises(ValueError):
        slack_config.resolve(slack_config.SlackConfig(), CHANNEL)


def test_the_token_command_comes_before_the_environment():
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, TOKEN + "\n", "")

    found = slack_config.token(
        slack_config.SlackConfig(token_command=("get-token",)),
        {"SLACK_USER_TOKEN": "xoxp-from-env"},
        run,
    )

    assert found == TOKEN and calls == [["get-token"]]


def test_the_token_falls_back_to_the_named_variable():
    cfg = slack_config.SlackConfig(token_env="MY_SLACK")

    assert slack_config.token(cfg, {"MY_SLACK": TOKEN}) == TOKEN
    with pytest.raises(slack_config.TokenError, match=r"\$MY_SLACK"):
        slack_config.token(cfg, {})


def test_the_token_falls_back_to_the_keychain_item_login_wrote():
    cfg = slack_config.SlackConfig()

    assert slack_config.token(cfg, {}, stored=lambda: TOKEN) == TOKEN
    assert slack_config.token(cfg, {"SLACK_USER_TOKEN": "xoxp-env"}, stored=lambda: TOKEN) == (
        "xoxp-env"
    )
    with pytest.raises(slack_config.TokenError, match="slack login"):
        slack_config.token(cfg, {}, stored=lambda: "")


def test_a_failing_token_command_never_shows_its_output():
    def run(command, **kwargs):
        return subprocess.CompletedProcess(command, 1, TOKEN, TOKEN)

    with pytest.raises(slack_config.TokenError) as error:
        slack_config.token(slack_config.SlackConfig(token_command=("x",)), {}, run)

    assert TOKEN not in str(error.value)


# The HTTP client


class _Response(io.BytesIO):
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


def test_history_pages_through_cursors_and_sends_the_token_as_a_header():
    open_, sent = _opener(
        {
            "ok": True,
            "messages": [{"ts": "3.0", "user": "U1", "text": "c"}],
            "response_metadata": {"next_cursor": "abc"},
        },
        {"ok": True, "messages": [{"ts": "2.0", "bot_id": "B1", "text": "b"}]},
    )

    found = slack_api.HttpClient(TOKEN, open_).history(CHANNEL, "1.0")

    assert [(m.ts, m.user) for m in found] == [("3.0", "U1"), ("2.0", "B1")]
    assert sent[0].get_header("Authorization") == f"Bearer {TOKEN}"
    assert b"oldest=1.0" in sent[0].data and b"cursor=abc" in sent[1].data
    assert TOKEN.encode() not in sent[0].data


def test_a_429_is_unavailable_with_its_retry_after():
    error = urllib.error.HTTPError(slack_api._API, 429, "Too Many", {"Retry-After": "30"}, None)
    open_, _ = _opener(error)

    with pytest.raises(slack_api.Unavailable) as raised:
        slack_api.HttpClient(TOKEN, open_).history(CHANNEL, None)

    assert raised.value.retry_after == 30.0


def test_a_bad_token_is_rejected_and_other_errors_are_transient():
    open_, _ = _opener(
        {"ok": False, "error": "invalid_auth"}, {"ok": False, "error": "fatal_error"}
    )
    client = slack_api.HttpClient(TOKEN, open_)

    with pytest.raises(slack_api.Rejected, match="invalid_auth"):
        client.workspace_url()
    with pytest.raises(slack_api.Unavailable):
        client.workspace_url()


def test_the_client_never_shows_its_token():
    assert TOKEN not in repr(slack_api.HttpClient(TOKEN))


# The command


def _args(*argv):
    parser = argparse.ArgumentParser()
    cli.setup_parser(parser.add_subparsers())
    a = parser.parse_args(["watch", "slack", *argv])
    a.invocation = ["lemonaid", "watch", "slack", *argv]
    return a


@pytest.fixture
def allowed(tmp_path, monkeypatch):
    _write_config(tmp_path, f'[slack.channels]\nmy-hq = "{CHANNEL}"\n')
    monkeypatch.setattr(slack_cli, "read_token", lambda config: TOKEN)


def test_once_prints_the_first_new_message_and_exits(allowed, monkeypatch, capsys):
    client = FakeClient([_m("100.000001")])
    monkeypatch.setattr(slack_cli, "make_client", lambda token: client)
    monkeypatch.setattr(
        slack_cli.time, "sleep", lambda s: client.messages.append(_m("200.0", "yo"))
    )

    assert slack_cli.run(_args("--wait", "#my-hq", "--once")) == 0

    out = capsys.readouterr().out
    assert '"yo"' in out and TOKEN not in out


def test_an_unlisted_channel_does_not_start(allowed, capsys):
    assert slack_cli.run(_args("--wait", "C0456GHIJKL", "--once")) == 2
    assert "not in [slack.channels]" in capsys.readouterr().out


def test_a_rejected_token_does_not_start(allowed, monkeypatch, capsys):
    client = FakeClient()
    client.workspace_url = lambda: (_ for _ in ()).throw(
        slack_api.Rejected("Slack refused auth.test: invalid_auth")
    )
    monkeypatch.setattr(slack_cli, "make_client", lambda token: client)

    assert slack_cli.run(_args("--wait", "my-hq", "--once")) == 2
    assert "not started: Slack refused auth.test" in capsys.readouterr().out


def test_a_transient_failure_backs_off_and_keeps_waiting(allowed, monkeypatch, capsys):
    client = FakeClient([_m("100.000001")])
    monkeypatch.setattr(slack_cli, "make_client", lambda token: client)
    _poll_once = slack_events.poll
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 1:
            client.fail = slack_api.Unavailable("rate limited", 90)
        else:
            client.fail = None
            client.messages.append(_m("200.0", "after"))

    monkeypatch.setattr(slack_cli.time, "sleep", sleep)
    monkeypatch.setattr(slack_events, "poll", _poll_once)

    assert slack_cli.run(_args("--wait", "my-hq", "--interval", "1", "--once")) == 0
    assert sleeps[0] == 1 and sleeps[1] >= 90
    assert '"after"' in capsys.readouterr().out


def test_a_channel_removed_while_watching_stops_the_waiter(allowed, monkeypatch, capsys):
    client = FakeClient([_m("100.000001")])
    monkeypatch.setattr(slack_cli, "make_client", lambda token: client)

    def sleep(seconds):
        client.fail = slack_api.Rejected("Slack refused conversations.history: channel_not_found")

    monkeypatch.setattr(slack_cli.time, "sleep", sleep)

    assert slack_cli.run(_args("--wait", "my-hq", "--once")) == 2
    assert "stopped watching #my-hq: " in capsys.readouterr().out


def test_status_reports_no_waiter(allowed, capsys):
    assert slack_cli.run(_args("--status", "my-hq", "--thread", THREAD)) == 1
    assert capsys.readouterr().out.strip() == "no waiter running"


def test_a_bad_thread_ts_is_refused():
    with pytest.raises(SystemExit):
        _args("--wait", "my-hq", "--thread", "yesterday")


def test_stop_matches_a_channel_by_name_or_id_and_a_thread_by_ts():
    assert slack_cli.targets(CHANNEL, "my-hq", "") == (CHANNEL, "my-hq")
    assert THREAD in slack_cli.targets(CHANNEL, "my-hq", THREAD)
    assert "slack" in registry.KINDS


# In a brief


def test_a_watching_line_is_read_as_a_slack_subscription(tmp_path):
    spec = subscriptions._parse(
        f'lemonaid watch slack --wait "#my-hq" --thread {THREAD} --me "🍋 LemonAce" --once',
        "p.Wordy",
        "Author (Wordy)",
        tmp_path,
    )

    assert spec.kind == "slack" and spec.target == ("my-hq", THREAD)
    assert spec.me == "🍋 LemonAce" and spec.interval == 60


def test_a_watching_bullet_is_a_slack_subscription_without_problems(tmp_path):
    text = shared.BRIEF + "- `lemonaid watch slack --wait my-hq`\n"

    reading = subscriptions.report(text, tmp_path)

    assert not reading.problems
    assert [s.target for s in reading.specs if s.kind == "slack"] == [("my-hq",)]


def test_the_scout_polls_a_slack_subscription(allowed, tmp_path, monkeypatch):
    client = FakeClient([_m("100.000001")])
    monkeypatch.setattr(slack_cli, "make_client", lambda token: client)
    spec = subscriptions.Subscription("slack", ("my-hq",), "🍋 LemonAce", interval=60)
    got = []
    poll = scout_pollers.open_poller(spec, tmp_path / "cursors", "", got.append, ())

    poll()
    client.messages += [_m("200.0", "🍋 LemonAce: mine"), _m("300.0", "yours")]
    poll()

    [event] = got
    assert '"yours"' in event and "mine" not in event


def test_the_scout_backs_off_after_a_failed_read(allowed, tmp_path, monkeypatch):
    client = FakeClient([_m("100.000001")])
    monkeypatch.setattr(slack_cli, "make_client", lambda token: client)
    spec = subscriptions.Subscription("slack", ("my-hq",), interval=60)
    poll = scout_pollers.open_poller(spec, tmp_path / "cursors", "", lambda m: True, ())
    client.fail = slack_api.Unavailable("rate limited", 120)

    poll()
    poll()

    assert len(client.calls) == 1


def test_the_scout_refuses_an_unlisted_channel(allowed, tmp_path):
    spec = subscriptions.Subscription("slack", ("C0456GHIJKL",), interval=60)

    with pytest.raises(ValueError, match=r"slack\.channels"):
        scout_pollers.open_poller(spec, tmp_path / "cursors", "", lambda m: True, ())


def test_a_started_waiter_records_its_command_in_the_brief(allowed, monkeypatch):
    path = store.create("watching", datetime.date(2026, 10, 30))
    with db.connect() as conn:
        attached.attach(conn, "codex:owner", path)
    monkeypatch.setenv("LEMONAID_CHANNEL", "codex:owner")
    client = FakeClient([_m("100.000001")])
    monkeypatch.setattr(slack_cli, "make_client", lambda token: client)
    monkeypatch.setattr(
        slack_cli.time, "sleep", lambda s: client.messages.append(_m(f"{len(client.calls)}00.0"))
    )
    a = _args("--wait", "my-hq", "--me", "🍋 LemonAce", "--once")

    assert slack_cli.run(a) == 0
    assert slack_cli.run(a) == 0
    assert waiters.commands(path.read_text()) == [
        "lemonaid watch slack --wait my-hq --me '🍋 LemonAce' --once"
    ]
