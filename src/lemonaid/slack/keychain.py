"""The macOS keychain item that holds the Slack user token `lemonaid slack login` gets.

lemonaid writes the item with `security -i`, which reads its command from standard
input, so the token never appears in a process's arguments. Reading uses
`security find-generic-password -w`, which prints only the password. On other systems
there is no item: `write` raises and `read` returns "".
"""

import getpass
import re
import subprocess
import sys
from collections import abc

SERVICE = "lemonaid-slack"
_TOKEN = re.compile(r"xox[a-z]-[A-Za-z0-9-]+")
_NAME = re.compile(r"[A-Za-z0-9._@+-]+")

Run = abc.Callable[..., subprocess.CompletedProcess]


class KeychainError(RuntimeError):
    """The token could not be stored. The message never contains the token."""


def available(platform: str = sys.platform) -> bool:
    return platform == "darwin"


def account() -> str:
    return getpass.getuser()


def read_command(user: str = "") -> list[str]:
    return ["security", "find-generic-password", "-s", SERVICE, "-a", user or account(), "-w"]


def write(token: str, run: Run = subprocess.run, user: str = "") -> None:
    """Store *token*, replacing an earlier one."""
    if not available():
        raise KeychainError("lemonaid stores the Slack token only in the macOS keychain")

    user = user or account()
    if not _TOKEN.fullmatch(token):
        raise KeychainError("Slack returned a token in an unexpected form; nothing was stored")
    if not _NAME.fullmatch(user):
        raise KeychainError(f"cannot name a keychain item after the user {user!r}")

    command = (
        f'add-generic-password -U -s {SERVICE} -a "{user}" -l "lemonaid Slack token" -w "{token}"\n'
    )
    try:
        done = run(["security", "-i"], input=command, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as error:
        raise KeychainError(f"security could not run: {type(error).__name__}") from None
    if done.returncode != 0 or "error" in (done.stdout + done.stderr).lower():
        raise KeychainError(f"security could not store the token (exit {done.returncode})")


def read(run: Run = subprocess.run, user: str = "") -> str:
    """The stored token, or "" when there is none."""
    if not available():
        return ""

    try:
        done = run(read_command(user), capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout.strip() if done.returncode == 0 else ""
