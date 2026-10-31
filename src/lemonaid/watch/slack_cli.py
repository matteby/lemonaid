"""`lemonaid watch slack`: a blocking waiter on a Slack channel or one thread in it.

Without `--thread`, reports new top-level messages in the channel, including replies also
sent to the channel. With `--thread TS`, reports new replies in the thread whose parent
message has that `ts`. Joins, leaves and other notices are skipped, and so is a message
whose text starts with the `--me` signature (after an optional 🍋), so a lemon's own posts
don't wake it. Each event gives one line per message: author, time, an excerpt, its `ts`
and a link. Makes one Slack call per interval.

The channel must be listed in `[slack.channels]` in lemonaid's config, by name or ID; that
allowlist limits lemons, not the token. The token comes from `[slack] token_command`, or the
`[slack] token_env` variable (`SLACK_USER_TOKEN` by default). See docs/watch.md.

By default prints one line per message and never exits on its own. With `--once`, prints
the first event and exits, so a Claude background Bash task wakes its session once on
completion. With `--codex-thread`, queues the first event into that Codex thread and exits.
The newest `ts` reported is saved as a cursor under the lemon's Lemon-ID (or `--me` without
a brief), so a first waiter takes the messages there now as seen and a later one reports
what arrived since. One waiter per (channel, thread, lemon) may run.

    lemonaid watch slack --wait my-hq --me "🍋 LemonAce" --once
    lemonaid watch slack --wait C0123ABCDEF --thread 1712345678.123456 --codex-thread
    lemonaid watch slack --status my-hq
"""

import argparse
import pathlib
import random
import re
import sys
import time

from .. import slack_config
from ..config import load_config
from ..log import get_logger
from . import cursors, delivery, registry, slack_api, slack_events, waiter_lock

_log = get_logger("watch.slack")
_MAX_DELAY = 600.0

make_client = slack_api.HttpClient  # replaced in tests: no test calls Slack
read_token = slack_config.token


def _thread(value: str) -> str:
    if not re.fullmatch(r"\d+\.\d+", value):
        raise argparse.ArgumentTypeError(
            f"not a Slack message ts such as 1712345678.123456: {value!r}"
        )

    return value


def targets(channel: str, label: str, thread: str) -> tuple[str, ...]:
    """What `watch stop` matches: the channel's ID and name, and the thread's `channel/ts`."""
    if thread:
        return (f"{channel}/{thread}", f"{label}/{thread}", thread)

    return (channel, label) if label != channel else (channel,)


def open_client(config: slack_config.SlackConfig) -> tuple[slack_api.Client, str]:
    """A client and the workspace URL for links. Raises TokenError, or Rejected for a bad token."""
    client = make_client(read_token(config))
    try:
        return client, client.workspace_url()
    except slack_api.Unavailable as error:
        _log.warning("auth.test failed (%s); events will have no links", error)
        return client, ""


def delay(attempts: int, interval: float, retry_after: float) -> float:
    ceiling = min(_MAX_DELAY, interval * 2 ** min(attempts, 20))
    return max(retry_after, random.uniform(ceiling / 2, ceiling))


def run(a: argparse.Namespace) -> int:
    a.codex_thread, problem = delivery.codex_thread(a.codex_thread)
    if problem:
        print(f"not started: {problem}")
        return 2

    a.thread = a.thread or ""
    config = load_config().slack
    try:
        channel, label = slack_config.resolve(config, a.wait or a.status)
    except ValueError as error:
        print(f"not started: {error}")
        return 2

    state_dir = a.state_dir or cursors.directory("slack", a.me, a.codex_thread)
    state_dir.mkdir(parents=True, exist_ok=True)
    stem = slack_events.state_stem(state_dir, channel, a.thread, a.me if a.state_dir else "")
    lock_path = stem.with_suffix(".lock")
    if a.status:
        running = waiter_lock.held(lock_path)
        print(
            f"waiter running: {waiter_lock.lock_holder(lock_path)}"
            if running
            else "no waiter running"
        )
        return 0 if running else 1

    lock = waiter_lock.acquire(lock_path)
    where = f"#{label}" + (f" thread {a.thread}" if a.thread else "")
    if lock is None:
        print(
            f"not started: a waiter for {where} as {a.me or '(no --me)'} is already running ({waiter_lock.lock_holder(lock_path)})"
        )
        return 3

    if a.codex_thread:
        problem = delivery.codex_setup_problem()
        deliver = delivery.to_codex(
            a.codex_thread, "lemonaid watch slack", "Handle it, then rearm the waiter."
        )
    else:
        problem, deliver = "", delivery.to_stdout
    if problem:
        print(f"not started: {problem}")
        return 2

    try:
        client, base = open_client(config)
    except (slack_config.TokenError, slack_api.Rejected) as error:
        print(f"not started: {error}")
        return 2

    once = a.once or bool(a.codex_thread)
    attempts = 0
    try:
        with registry.watching(a, "slack", targets(channel, label, a.thread), a.codex_thread):
            while True:
                wait = a.interval
                try:
                    if (
                        slack_events.poll(
                            stem, client, label, channel, a.thread, deliver, a.me, base
                        )
                        and once
                    ):
                        return 0

                    attempts = 0
                except slack_api.Unavailable as error:
                    _log.warning("Cannot read Slack for %s: %s", where, error)
                    wait = delay(attempts, a.interval, error.retry_after)
                    attempts += 1
                time.sleep(wait)
    except KeyboardInterrupt:
        pass
    except slack_api.Rejected as error:
        print(f"stopped watching {where}: {error}")
        return 2
    except delivery.Failed as e:
        print(f"{e}; the message was not recorded and will be reported to the next waiter")
        return 1
    return 0


def _cmd(a: argparse.Namespace) -> None:
    sys.exit(run(a))


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    ap = subparsers.add_parser(
        "slack",
        help="Wait for new messages in a Slack channel or thread",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--wait", metavar="CHANNEL", help="channel to watch: a name or ID from [slack.channels]"
    )
    mode.add_argument(
        "--status", metavar="CHANNEL", help="report whether a waiter for this channel is running"
    )
    ap.add_argument(
        "--thread",
        type=_thread,
        default=None,
        metavar="TS",
        help="watch the replies in this thread (its parent message's ts) instead of the channel",
    )
    ap.add_argument(
        "--me",
        default="",
        help="your signature: messages starting with it are yours and skipped; also keeps two lemons' waiters apart",
    )
    ap.add_argument("--interval", type=float, default=60.0, help="seconds between Slack calls")
    ap.add_argument("--once", action="store_true", help="print the first event and exit")
    delivery.add_codex_thread_argument(ap)
    ap.add_argument(
        "--state-dir",
        type=pathlib.Path,
        default=None,
        help="where the cursors and waiter lock live (default: this lemon's cursor directory)",
    )
    ap.set_defaults(func=_cmd)
