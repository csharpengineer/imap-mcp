"""Account configuration.

Two sources, usable together:

* ``IMAP_MCP_CONFIG`` -> JSON file mapping account name to non-secret settings
  plus ``password_env``, the name of the env var holding that account's
  password. Passwords are never read from the file.
* Legacy single-account env vars ``IMAP_HOST`` / ``IMAP_USER`` / ``IMAP_PASS``
  (+ optional ``IMAP_PORT`` / ``IMAP_SSL`` / ``IMAP_TIMEOUT``), exposed as the
  account named ``default``.

Passwords are resolved from the environment only at connect time, so a
missing password breaks that one account, not the server.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

DEFAULT_ACCOUNT = "default"
LEGACY_PASSWORD_ENV = "IMAP_PASS"

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}
_ALLOWED_KEYS = {"host", "port", "user", "ssl", "timeout", "password_env"}
_SECRET_KEYS = {"password", "pass", "passwd", "secret", "app_password", "token"}


class ConfigError(ValueError):
    """Invalid configuration. Messages never include secret values."""


@dataclass(frozen=True)
class AccountConfig:
    name: str
    host: str
    user: str
    password_env: str
    port: int = 993
    ssl: bool = True
    timeout: float = 30.0

    def password(self, env: Mapping[str, str] | None = None) -> str | None:
        env = os.environ if env is None else env
        return env.get(self.password_env) or None

    def describe(self, env: Mapping[str, str] | None = None) -> dict:
        """Non-secret summary; reports whether the password is set, never its value."""
        return {
            "name": self.name,
            "host": self.host,
            "port": self.port,
            "user": self.user,
            "ssl": self.ssl,
            "password_env": self.password_env,
            "password_set": self.password(env) is not None,
        }


def load_accounts(env: Mapping[str, str] | None = None) -> dict[str, AccountConfig]:
    """Build all accounts from the environment (and config file, if any)."""
    env = os.environ if env is None else env
    accounts: dict[str, AccountConfig] = {}
    path = env.get("IMAP_MCP_CONFIG")
    if path:
        accounts.update(_from_file(Path(path).expanduser()))
    legacy = _legacy_account(env)
    if legacy is not None:
        if DEFAULT_ACCOUNT in accounts:
            raise ConfigError(
                f"account {DEFAULT_ACCOUNT!r} is defined both in IMAP_MCP_CONFIG and by "
                "IMAP_HOST/IMAP_USER env vars; remove one"
            )
        accounts[DEFAULT_ACCOUNT] = legacy
    if not accounts:
        raise ConfigError(
            "no IMAP accounts configured: set IMAP_MCP_CONFIG to a JSON account file, "
            "or set IMAP_HOST, IMAP_USER and IMAP_PASS for a single account"
        )
    return accounts


def _legacy_account(env: Mapping[str, str]) -> AccountConfig | None:
    host, user = env.get("IMAP_HOST"), env.get("IMAP_USER")
    if not host and not user:
        return None
    if not host or not user:
        missing = "IMAP_HOST" if not host else "IMAP_USER"
        raise ConfigError(f"single-account setup is missing {missing}")
    ssl = _parse_bool(env.get("IMAP_SSL", "true"), "IMAP_SSL")
    return AccountConfig(
        name=DEFAULT_ACCOUNT,
        host=host,
        user=user,
        password_env=LEGACY_PASSWORD_ENV,
        port=_parse_int(env.get("IMAP_PORT") or (993 if ssl else 143), "IMAP_PORT"),
        ssl=ssl,
        timeout=_parse_float(env.get("IMAP_TIMEOUT") or 30, "IMAP_TIMEOUT"),
    )


def _from_file(path: Path) -> dict[str, AccountConfig]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"IMAP_MCP_CONFIG file not found: {path}") from None
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(f"cannot read IMAP_MCP_CONFIG file {path}: {exc}") from None
    except json.JSONDecodeError as exc:
        # Position only: echoing the offending text could leak a pasted secret.
        raise ConfigError(f"IMAP_MCP_CONFIG is not valid JSON (line {exc.lineno}, column {exc.colno})") from None
    if not isinstance(data, dict) or not isinstance(data.get("accounts"), dict) or not data["accounts"]:
        raise ConfigError('IMAP_MCP_CONFIG must be an object like {"accounts": {"name": {...}}}')
    return {name: _account_from_dict(name, raw) for name, raw in data["accounts"].items()}


def _account_from_dict(name: str, raw: Any) -> AccountConfig:
    where = f"account {name!r}"
    if not _NAME.match(name):
        raise ConfigError(f"{where}: names may use letters, digits, '_', '.', '-' (max 64)")
    if not isinstance(raw, dict):
        raise ConfigError(f"{where}: must be an object")
    secret = sorted(k for k in raw if k.lower() in _SECRET_KEYS)
    if secret:
        raise ConfigError(
            f"{where}: remove {', '.join(secret)} from the config file; passwords must come "
            "from an env var named by password_env"
        )
    unknown = sorted(set(raw) - _ALLOWED_KEYS)
    if unknown:
        raise ConfigError(f"{where}: unknown keys {', '.join(unknown)}; allowed: {', '.join(sorted(_ALLOWED_KEYS))}")
    for key in ("host", "user", "password_env"):
        if not isinstance(raw.get(key), str) or not raw[key].strip():
            raise ConfigError(f"{where}: {key} is required and must be a non-empty string")
    if not _ENV_NAME.match(raw["password_env"]):
        raise ConfigError(f"{where}: password_env must be an env var name like WORK_IMAP_PASS")
    ssl = raw.get("ssl", True)
    if not isinstance(ssl, bool):
        raise ConfigError(f"{where}: ssl must be true or false")
    return AccountConfig(
        name=name,
        host=raw["host"].strip(),
        user=raw["user"].strip(),
        password_env=raw["password_env"],
        port=_parse_int(raw.get("port", 993 if ssl else 143), f"{where}: port"),
        ssl=ssl,
        timeout=_parse_float(raw.get("timeout", 30), f"{where}: timeout"),
    )


def _parse_bool(value: str, name: str) -> bool:
    v = value.strip().lower()
    if v in _TRUE:
        return True
    if v in _FALSE:
        return False
    raise ConfigError(f"{name} must be true/false")


def _parse_int(value: Any, name: str) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError):
        raise ConfigError(f"{name} must be an integer") from None
    if isinstance(value, bool) or not 0 < port < 65536:
        raise ConfigError(f"{name} must be between 1 and 65535")
    return port


def _parse_float(value: Any, name: str) -> float:
    try:
        timeout = float(value)
    except (TypeError, ValueError):
        raise ConfigError(f"{name} must be a number") from None
    if isinstance(value, bool) or timeout <= 0:
        raise ConfigError(f"{name} must be positive")
    return timeout
