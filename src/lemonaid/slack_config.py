"""The `[slack]` table: where the Slack token comes from, and the channels lemons may watch.

The channel allowlist limits lemons, not the token. Slack scopes cover a type of
conversation, so a token can read every channel its user can; `[slack.channels]` only
keeps a lemon from watching one that is not listed. It also maps names to IDs, so a
command can say `my-hq` without a lookup call.
"""

import os
import re
import subprocess
import sys
from collections import abc
from dataclasses import dataclass, field

_CHANNEL_ID = re.compile(r"[CGD][A-Z0-9]{6,}")


class TokenError(RuntimeError):
    """The token could not be read. The message never contains the token."""


@dataclass(frozen=True)
class SlackConfig:
    token_command: tuple[str, ...] = ()
    token_env: str = "SLACK_USER_TOKEN"
    channels: dict[str, str] = field(default_factory=dict)  # name -> channel ID


def _warn(key: str, expected: str) -> None:
    print(f"Warning: [slack] {key} must be {expected}", file=sys.stderr)


def _channels(raw: object) -> dict[str, str]:
    if not isinstance(raw, dict):
        _warn("channels", "a table of name = channel ID")
        return {}

    found = {}
    for name, value in raw.items():
        if isinstance(value, str) and _CHANNEL_ID.fullmatch(value):
            found[str(name).lstrip("#")] = value
        else:
            print(
                f"Warning: [slack.channels] {name} is ignored; give it a channel ID such as C0123ABCD",
                file=sys.stderr,
            )
    return found


def parse(data: dict) -> SlackConfig:
    command = data.get("token_command", [])
    if not isinstance(command, list) or not all(isinstance(w, str) for w in command):
        _warn(
            "token_command", 'a list of strings, such as ["security", "find-generic-password", ...]'
        )
        command = []
    env = data.get("token_env", SlackConfig.token_env)
    if not isinstance(env, str) or not env:
        _warn("token_env", "the name of an environment variable")
        env = SlackConfig.token_env
    return SlackConfig(tuple(command), env, _channels(data.get("channels", {})))


def resolve(config: SlackConfig, channel: str) -> tuple[str, str]:
    """The channel's ID and the name to show for it; ValueError for one not in the allowlist."""
    wanted = channel.strip().lstrip("#")
    if wanted in config.channels:
        return config.channels[wanted], wanted

    for name, channel_id in config.channels.items():
        if channel_id == wanted:
            return channel_id, name

    listed = ", ".join(sorted(config.channels)) or "none"
    raise ValueError(
        f"{channel} is not in [slack.channels] in lemonaid's config (listed: {listed});"
        ' add it there as name = "<channel ID>"'
    )


def token(
    config: SlackConfig,
    environ: abc.Mapping[str, str] = os.environ,
    run: abc.Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> str:
    """The token from `token_command`, else from the `token_env` variable."""
    if config.token_command:
        try:
            done = run(list(config.token_command), capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError) as error:
            raise TokenError(
                f"[slack] token_command could not run: {type(error).__name__}"
            ) from None
        if done.returncode != 0 or not done.stdout.strip():
            raise TokenError(f"[slack] token_command exited {done.returncode} without a token")

        return done.stdout.strip()

    if value := environ.get(config.token_env, "").strip():
        return value

    raise TokenError(
        f"no Slack token: set [slack] token_command in lemonaid's config, or ${config.token_env}"
    )
