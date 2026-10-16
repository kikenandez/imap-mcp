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
    email = "beta@gmail.com"
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
    """Fail immediately if an offline test accidentally attempts a real connection."""
    import socket

    def blocked(*args, **kwargs):
        raise AssertionError("network access is forbidden in tests; use the IMAP fakes")

    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)
