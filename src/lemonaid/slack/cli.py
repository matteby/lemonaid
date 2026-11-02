"""`lemonaid slack login` and `lemonaid slack post`.

`login` signs you in to the org's Slack app in the browser (OAuth with PKCE, no client
secret) and stores your user token in the macOS keychain, where lemonaid reads it.
`post` posts a message as you to a channel in `[slack.channels]`, signed so that
`lemonaid watch slack --me` with the same signature skips it. See docs/slack.md.
"""

import argparse
import sys
import urllib.request
import webbrowser

from .. import slack_config
from ..config import load_config
from ..watch import slack_api, slack_events
from ..watch.slack_cli import _thread
from . import keychain, oauth

# Replaced in tests: no test opens a browser, calls Slack or touches the keychain.
open_browser = webbrowser.open
opener = urllib.request.urlopen
store = keychain.write
make_client = slack_api.HttpClient
read_token = slack_config.token


def login(config: slack_config.SlackConfig, timeout: float = oauth.WAIT_SECONDS) -> int:
    if not keychain.available():
        print(
            "not signed in: lemonaid stores the Slack token only in the macOS keychain;"
            f" elsewhere, set [slack] token_command or ${config.token_env}"
        )
        return 2

    if not config.client_id:
        print(
            "not signed in: set [slack] client_id in lemonaid's config to the Slack app's"
            " client ID (see docs/slack.md)"
        )
        return 2

    pkce = oauth.new_pkce()
    try:
        listener = oauth.Listener(config.redirect_port, pkce.state)
        redirect = oauth.redirect_uri(listener.port)
        url = oauth.authorize_url(config.client_id, redirect, pkce)
        print(f"Sign in to Slack in your browser. If it does not open, visit:\n{url}", flush=True)
        if not open_browser(url):
            print("Could not open a browser; open the link above.", flush=True)
        code = listener.wait(timeout)
        grant = oauth.exchange(config.client_id, code, pkce.verifier, redirect, opener)
        store(grant.token)
    except (oauth.LoginError, keychain.KeychainError) as error:
        print(f"not signed in: {error}")
        return 2

    try:
        user, team, _ = make_client(grant.token).whoami()
    except (slack_api.Rejected, slack_api.Unavailable) as error:
        print(f"Stored the token in the keychain, but auth.test failed: {error}")
        return 1

    print(f"Signed in to Slack as {user} in {team}. The token is in the keychain.")
    return 0


def post(
    config: slack_config.SlackConfig, channel: str, text: str, me: str, thread: str = ""
) -> int:
    try:
        channel_id, label = slack_config.resolve(config, channel)
    except ValueError as error:
        print(f"not posted: {error}")
        return 2

    text = text.strip()
    if not text:
        print("not posted: the message is empty")
        return 2

    if not slack_events.signed(text, me):
        text = f"{me.strip()} {text}"
    try:
        client = make_client(read_token(config))
        ts = client.post(channel_id, text, thread)
    except (slack_config.TokenError, slack_api.Rejected) as error:
        print(f"not posted: {error}")
        return 2
    except slack_api.Unavailable as error:
        print(
            f"posting to #{label} failed ({error}); it may have gone through,"
            " so check the channel before posting again"
        )
        return 1

    try:
        base = client.workspace_url()
    except (slack_api.Rejected, slack_api.Unavailable):
        base = ""
    link = slack_events.permalink(base, channel_id, slack_api.Message(ts, "", ""), thread)
    where = f"#{label}" + (f" thread {thread}" if thread else "")
    print(f"posted to {where}: ts {ts}" + (f" {link}" if link else ""))
    return 0


def _login(a: argparse.Namespace) -> None:
    sys.exit(login(load_config().slack))


def _post(a: argparse.Namespace) -> None:
    text = a.text if a.text is not None else sys.stdin.read()
    sys.exit(post(load_config().slack, a.channel, text, a.me, a.thread or ""))


def setup_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "slack",
        help="Sign in to Slack, and post to a Slack channel",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    commands = parser.add_subparsers(dest="slack_command", required=True)

    login_parser = commands.add_parser(
        "login",
        help="Sign in to the Slack app in the browser and store your token in the keychain",
        description="Opens Slack in the browser, waits for the sign-in on localhost, and"
        " stores your user token in the macOS keychain (service lemonaid-slack). Needs"
        " [slack] client_id. Signing in again replaces the stored token.",
    )
    login_parser.set_defaults(func=_login)

    post_parser = commands.add_parser(
        "post",
        help="Post a message as you to a channel in [slack.channels]",
        description="Posts as you with chat.postMessage. Puts the --me signature at the start"
        " unless the text starts with it, so `watch slack --me` skips the post. Prints the new"
        " message's ts and link. A failed post is not retried.",
    )
    post_parser.add_argument("channel", help="a name or ID from [slack.channels]")
    post_parser.add_argument(
        "text", nargs="?", default=None, help="the message (default: standard input)"
    )
    post_parser.add_argument("--me", required=True, help='your signature, such as "🍋 LemonAce"')
    post_parser.add_argument(
        "--thread", type=_thread, default=None, metavar="TS", help="reply in this thread"
    )
    post_parser.set_defaults(func=_post)
