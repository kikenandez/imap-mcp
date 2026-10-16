import textwrap

import pytest

from imap_mcp import accounts

CONFIG = textwrap.dedent("""\
    [[account]]
    key = "alpha"
    label = "alpha@example.com (Test)"
    email = "alpha@example.com"
    host = "imap.example.com"
    port = 1993
    password_env = "ALPHA_PW"
    enabled = true

    [[account]]
    key = "gmailish"
    email = "beta@example.com"
    host = "imap.gmail.com"
    password_env = "BETA_PW"

    [[account]]
    key = "off"
    label = "off@example.com"
    email = "off@example.com"
    host = "imap.example.com"
    password_env = "OFF_PW"
    enabled = false
""")


@pytest.fixture
def config(tmp_path, monkeypatch):
    """Point the registry at a three-account test config (one disabled)."""
    path = tmp_path / "accounts.toml"
    path.write_text(CONFIG)
    monkeypatch.setenv("IMAP_MCP_ACCOUNTS", str(path))
    accounts._load.cache_clear()
    yield path
    accounts._load.cache_clear()


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Reject any real connection attempt during offline tests."""
    import socket

    def blocked(*args, **kwargs):
        raise AssertionError("network access is forbidden in tests; use the IMAP fakes")

    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)


@pytest.fixture(autouse=True)
def no_real_keychain(monkeypatch):
    """A forgotten fake must fail before accessing the user's credential store."""
    import subprocess
    from pathlib import Path

    original = subprocess.run

    def guarded(args, *positional, **kwargs):
        command = args if isinstance(args, str) else str(args[0])
        if "security" in command or Path(command).name == "secret-tool":
            raise AssertionError("real credential backends are forbidden in tests")
        return original(args, *positional, **kwargs)

    monkeypatch.setattr(subprocess, "run", guarded)
