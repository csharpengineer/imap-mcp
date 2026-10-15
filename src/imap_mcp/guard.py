"""Read-only enforcement at the IMAP wire level.

Every command imaplib sends funnels through ``IMAP4._command``. The classes
here override it with an allowlist, so a state-changing command (STORE, COPY,
MOVE, EXPUNGE, DELETE, APPEND, SELECT, ...) raises before a single byte goes
to the server, no matter which code path asked for it.

FETCH is checked too: ``BODY[...]``, ``RFC822`` and ``RFC822.TEXT`` implicitly
set ``\\Seen``, so only ``BODY.PEEK[...]`` and non-mutating items get through.
"""

from __future__ import annotations

import imaplib
import re

# Commands that never change mailbox or message state. EXAMINE is the
# read-only variant of SELECT. CLOSE is deliberately excluded: on a SELECTed
# mailbox it expunges, and we never need it (LOGOUT is enough).
ALLOWED_COMMANDS = frozenset(
    {
        "CAPABILITY",
        "NOOP",
        "LOGIN",
        "AUTHENTICATE",
        "STARTTLS",
        "LOGOUT",
        "LIST",
        "LSUB",
        "STATUS",
        "EXAMINE",
        "SEARCH",
        "FETCH",
        "UID",
    }
)
ALLOWED_UID_COMMANDS = frozenset({"SEARCH", "FETCH"})

# BODY[ / BINARY[ without .PEEK, or RFC822 / RFC822.TEXT (all set \Seen).
# RFC822.HEADER and RFC822.SIZE are harmless and allowed.
_SEEN_SETTING_FETCH = re.compile(
    rb"(?<![.\w])(?:BODY|BINARY)\[|\bRFC822(?!\.(?:HEADER|SIZE))\b", re.IGNORECASE
)


class ReadOnlyViolation(imaplib.IMAP4.error):
    """Raised when code tries to send a state-changing IMAP command."""


def _as_bytes(value: object) -> bytes:
    if isinstance(value, bytes):
        return value
    return str(value).encode("utf-8", "replace")


def check_command(name: str, args: tuple) -> None:
    """Raise ReadOnlyViolation unless ``name args`` is a read-only command."""
    upper = name.upper()
    if upper not in ALLOWED_COMMANDS:
        raise ReadOnlyViolation(f"blocked IMAP command {upper}: server is read-only")
    if upper == "UID":
        if not args:
            raise ReadOnlyViolation("blocked UID command without sub-command")
        sub = _as_bytes(args[0]).decode("ascii", "replace").upper()
        if sub not in ALLOWED_UID_COMMANDS:
            raise ReadOnlyViolation(f"blocked IMAP command UID {sub}: server is read-only")
        if sub == "FETCH":
            _check_fetch_items(args[2:])
    elif upper == "FETCH":
        _check_fetch_items(args[1:])


def _check_fetch_items(items: tuple) -> None:
    for item in items:
        if item is not None and _SEEN_SETTING_FETCH.search(_as_bytes(item)):
            raise ReadOnlyViolation(
                "blocked FETCH that would set \\Seen; use BODY.PEEK[...]"
            )


class _GuardMixin:
    def _command(self, name, *args):  # noqa: D401 - imaplib internal hook
        check_command(name, args)
        return super()._command(name, *args)


class ReadOnlyIMAP4(_GuardMixin, imaplib.IMAP4):
    """Plain-TCP IMAP client that refuses state-changing commands."""


class ReadOnlyIMAP4_SSL(_GuardMixin, imaplib.IMAP4_SSL):
    """TLS IMAP client that refuses state-changing commands."""
