"""Configuration from environment variables only."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Config:
    host: str
    user: str
    # repr=False keeps the password out of tracebacks and debug logs.
    password: str = field(repr=False)
    port: int = 993
    ssl: bool = True
    timeout: float = 30.0

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Config":
        env = os.environ if env is None else env
        missing = [k for k in ("IMAP_HOST", "IMAP_USER", "IMAP_PASS") if not env.get(k)]
        if missing:
            raise ConfigError(f"missing required env vars: {', '.join(missing)}")
        ssl = _parse_bool(env.get("IMAP_SSL", "true"), "IMAP_SSL")
        try:
            port = int(env.get("IMAP_PORT") or (993 if ssl else 143))
            timeout = float(env.get("IMAP_TIMEOUT") or 30)
        except ValueError as exc:
            raise ConfigError(f"IMAP_PORT/IMAP_TIMEOUT must be numeric: {exc}") from None
        return cls(
            host=env["IMAP_HOST"],
            user=env["IMAP_USER"],
            password=env["IMAP_PASS"],
            port=port,
            ssl=ssl,
            timeout=timeout,
        )


def _parse_bool(value: str, name: str) -> bool:
    v = value.strip().lower()
    if v in _TRUE:
        return True
    if v in _FALSE:
        return False
    raise ConfigError(f"{name} must be true/false, got {value!r}")
