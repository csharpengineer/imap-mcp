"""Per-account connection pool: lazy connect, reconnect on drop, isolation."""

from __future__ import annotations

import imaplib
import os
import threading
from typing import Callable, Mapping, TypeVar

from .client import Mailbox, MailError
from .config import AccountConfig
from .guard import ReadOnlyViolation

T = TypeVar("T")

# Signs that the session is gone (server closed it, idle timeout, network).
# Every operation is read-only, so retrying once on a fresh session is safe.
_DROPPED = (imaplib.IMAP4.abort, OSError, EOFError)


class _Slot:
    def __init__(self) -> None:
        # imaplib is not thread-safe: one in-flight operation per account.
        self.lock = threading.Lock()
        self.box: Mailbox | None = None


class AccountPool:
    def __init__(self, accounts: Mapping[str, AccountConfig], env: Mapping[str, str] | None = None):
        if not accounts:
            raise ValueError("at least one account is required")
        self.accounts = dict(accounts)
        self._env = os.environ if env is None else env
        self._slots = {name: _Slot() for name in self.accounts}

    @property
    def names(self) -> list[str]:
        return sorted(self.accounts)

    def resolve(self, name: str | None) -> AccountConfig:
        if not name:
            if len(self.accounts) == 1:
                return next(iter(self.accounts.values()))
            raise MailError(f"account is required; choose one of: {', '.join(self.names)}")
        try:
            return self.accounts[name]
        except KeyError:
            raise MailError(f"unknown account {name!r}; valid accounts: {', '.join(self.names)}") from None

    def describe(self) -> list[dict]:
        return [self.accounts[n].describe(self._env) for n in self.names]

    def run(self, name: str | None, op: Callable[[Mailbox], T]) -> T:
        account = self.resolve(name)
        slot = self._slots[account.name]
        with slot.lock:
            for attempt in (1, 2):
                if slot.box is None:
                    slot.box = self._connect(account)
                try:
                    return op(slot.box)
                except (MailError, ReadOnlyViolation):
                    raise  # session is still healthy
                except _DROPPED as exc:
                    self._drop(slot)
                    if attempt == 2:
                        raise MailError(f"account {account.name!r}: connection lost: {exc}") from None
                except imaplib.IMAP4.error as exc:
                    # Protocol error: session state is unknown, start fresh next time.
                    self._drop(slot)
                    raise MailError(f"account {account.name!r}: IMAP error: {exc}") from None
        raise AssertionError("unreachable")

    def close_all(self) -> None:
        for slot in self._slots.values():
            with slot.lock:
                self._drop(slot)

    def _connect(self, account: AccountConfig) -> Mailbox:
        password = account.password(self._env)
        if password is None:
            raise MailError(
                f"account {account.name!r}: password env var {account.password_env} is not set"
            )
        return Mailbox(account, password).connect()

    @staticmethod
    def _drop(slot: _Slot) -> None:
        if slot.box is not None:
            slot.box.close()
            slot.box = None
