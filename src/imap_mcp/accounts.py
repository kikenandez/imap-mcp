"""Non-secret account registry; credentials are resolved at each connection."""

from __future__ import annotations

import math
import os
import tomllib
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from .credentials import resolve_password, validate_reference

# Config resolution order: $IMAP_MCP_ACCOUNTS if set, else accounts.toml at the
# project root (src/imap_mcp/accounts.py -> two parents up).
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "accounts.toml"


def config_path() -> Path:
    override = os.environ.get("IMAP_MCP_ACCOUNTS")
    return Path(override) if override else DEFAULT_CONFIG_PATH


@dataclass(frozen=True)
class Account:
    key: str
    label: str
    email: str
    host: str
    port: int
    password_env: str | None = None
    enabled: bool = True
    password_ref: str | None = None
    timeout_seconds: float = 30
    folders: dict[str, str] = field(default_factory=dict)
    allow_drafts: bool = False
    from_name: str | None = None

    def __post_init__(self):
        if self.password_ref is not None and self.password_env is not None:
            raise ValueError("use password_ref or legacy password_env, not both")
        if self.password_env is not None and not isinstance(self.password_env, str):
            raise ValueError("password_env must be an environment variable name")
        validate_reference(self.credential_ref, self.key)
        if type(self.allow_drafts) is not bool or type(self.enabled) is not bool:
            raise ValueError("allow_drafts and enabled must be TOML booleans")
        if self.from_name is not None and not isinstance(self.from_name, str):
            raise ValueError("from_name must be a string")
        if not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be a finite positive number")
        roles = {"inbox", "sent", "drafts", "junk", "trash", "archive"}
        if any(
            k not in roles or not isinstance(v, str) or not v.strip()
            for k, v in self.folders.items()
        ):
            raise ValueError("folders must map known roles to nonempty folder names")

    @property
    def credential_ref(self) -> str:
        if self.password_ref is not None:
            return self.password_ref
        if self.password_env:
            return f"env:{self.password_env}"
        raise ValueError("an account requires password_ref (or legacy password_env)")

    def password(self) -> str | None:
        """Resolve on every call, including after secret rotation; never cache values."""
        return resolve_password(self.credential_ref, self.key)


@lru_cache(maxsize=1)
def _load() -> dict[str, Account]:
    path = config_path()
    if not path.exists():
        raise FileNotFoundError(
            f"no account config at {path}; copy accounts.example.toml to "
            "accounts.toml and fill in your accounts (or point "
            "IMAP_MCP_ACCOUNTS at a config file)"
        )
    with path.open("rb") as fh:
        raw = tomllib.load(fh)
    if "password" in raw:
        raise ValueError("password fields are forbidden; use password_ref")
    out: dict[str, Account] = {}
    for a in raw.get("account", []):
        if "password" in a:
            raise ValueError("password fields are forbidden; use password_ref")
        acct = Account(
            key=a["key"],
            label=a.get("label", a["email"]),
            email=a["email"],
            host=a["host"],
            port=int(a.get("port", 993)),
            password_env=a.get("password_env"),
            password_ref=a.get("password_ref"),
            enabled=a.get("enabled", True),
            timeout_seconds=float(a.get("timeout_seconds", 30)),
            folders=a.get("folders", {}),
            allow_drafts=a.get("allow_drafts", False),
            from_name=a.get("from_name"),
        )
        out[acct.key] = acct
    return out


def all_accounts() -> list[Account]:
    return list(_load().values())


def enabled_accounts() -> list[Account]:
    return [a for a in all_accounts() if a.enabled]


def get_account(key: str) -> Account:
    accts = _load()
    if key not in accts:
        raise KeyError(f"unknown account {key!r}; known: {', '.join(accts)}")
    return accts[key]
