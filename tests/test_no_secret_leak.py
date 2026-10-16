"""Exercise real tool paths with fake credentials and a completely offline IMAP peer."""

import asyncio
import json
import logging
import subprocess
import traceback
from dataclasses import replace
from types import SimpleNamespace

import pytest
from test_imap import FakeMailBox, fake_msg

from imap_mcp import accounts, cli, credentials, imap, server

CANARY = "Zq9-SECRET-canary-7f"
TOOLS = [
    ("list_accounts", {}),
    ("list_emails", {}),
    ("search_emails", {"query": "Hello"}),
    ("get_email", {"account": "alpha", "id": "42"}),
    ("list_folders", {"account": "alpha"}),
    (
        "create_draft",
        {
            "account": "alpha",
            "to": "recipient@example.com",
            "subject": "Synthetic draft",
            "body": "Synthetic body",
        },
    ),
]
SCENARIOS = [
    (backend, outcome)
    for backend in ("env", "keychain")
    for outcome in ("ok", "login-error", "missing")
]
SCENARIOS += [("keychain", outcome) for outcome in ("denied", "timeout", "oserror")]


@pytest.fixture
def secret_peer(config, monkeypatch, request):
    backend, outcome = request.param
    ref = "env:CANARY_IMAP_PASSWORD" if backend == "env" else "keychain:pkma-mail-alpha/pkma"
    account = replace(
        accounts.get_account("alpha"), password_env=None, password_ref=ref, allow_drafts=True
    )
    monkeypatch.setattr(accounts, "all_accounts", lambda: [account])
    monkeypatch.setattr(accounts, "enabled_accounts", lambda: [account])
    monkeypatch.setattr(accounts, "get_account", lambda key: account)
    if backend == "env":
        if outcome == "missing":
            monkeypatch.delenv("CANARY_IMAP_PASSWORD", raising=False)
        else:
            monkeypatch.setenv("CANARY_IMAP_PASSWORD", CANARY)
    else:
        monkeypatch.setattr(credentials.sys, "platform", "darwin")

        def resolve(argv, **kwargs):
            assert argv == [
                "/usr/bin/security",
                "find-generic-password",
                "-s",
                "pkma-mail-alpha",
                "-a",
                "pkma",
                "-w",
            ]
            assert kwargs["shell"] is False and kwargs["capture_output"] is True
            if outcome == "timeout":
                raise subprocess.TimeoutExpired(argv, 5, output=CANARY, stderr=CANARY)
            if outcome == "oserror":
                raise OSError(CANARY)
            return SimpleNamespace(
                returncode={"missing": 44, "denied": 1}.get(outcome, 0),
                stdout=CANARY + "\n",
                stderr=CANARY,
            )

        monkeypatch.setattr(credentials.subprocess, "run", resolve)

    class Peer(FakeMailBox):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.client.debug = 5  # Even an inherited imaplib debug setting must be disabled.
            self.client._cmd_log = {}
            self.client.sock = SimpleNamespace(
                version=lambda: "TLSv1.3",
                cipher=lambda: ("TLS_AES_256_GCM_SHA384", "TLSv1.3", 256),
            )

        def login(self, email, password, initial_folder=None):
            assert password == CANARY
            assert self.client.debug == 0
            self.client._cmd_log[0] = password
            if outcome == "login-error":
                raise RuntimeError("peer echoed " + password)
            return self

        def logout(self):
            assert not self.client._cmd_log
            # A logout exception must also stay out of public errors and logs.
            raise RuntimeError(CANARY)

    FakeMailBox.instances = []
    FakeMailBox.messages = [fake_msg()]
    FakeMailBox.folders = [SimpleNamespace(name="Drafts", delim="/", flags=("\\Drafts",))]
    monkeypatch.setattr(imap, "MailBox", Peer)
    # TLS context creation is unrelated to these tests and need not read local certificates.
    monkeypatch.setattr(imap.ssl, "create_default_context", lambda: object())
    return account, outcome


def assert_no_leak(value, account, capsys, caplog):
    captured = capsys.readouterr()
    outputs = [
        json.dumps(value, default=str),
        captured.out,
        captured.err,
        repr(account),
        str(account),
        caplog.text,
    ]
    outputs.extend(record.getMessage() for record in caplog.records)
    assert all(CANARY not in output for output in outputs)


@pytest.mark.parametrize("secret_peer", SCENARIOS, indirect=True)
@pytest.mark.parametrize("name, arguments", TOOLS)
def test_tools_never_expose_secret(secret_peer, name, arguments, capsys, caplog):
    account, outcome = secret_peer
    caplog.set_level(logging.DEBUG)
    try:
        result = getattr(server, name)(**arguments)
    except Exception as exc:
        assert outcome != "ok"
        assert exc.__context__ is None and exc.__cause__ is None
        result = {"error": str(exc), "repr": repr(exc), "traceback": traceback.format_exc()}
    if outcome == "ok":
        assert result and "error" not in json.dumps(result)
    elif name == "list_accounts":
        expected = (
            "unreachable: RuntimeError"
            if outcome == "login-error"
            else f"no-credential: {account.credential_ref}"
        )
        assert result[0]["status"] == expected
    assert_no_leak(result, account, capsys, caplog)
    # Also pass through MCP's real serializer and exception handling/logging boundary.
    try:
        result = asyncio.run(server.mcp.call_tool(name, arguments)).model_dump(mode="json")
    except Exception as exc:
        assert outcome != "ok"
        result = {"error": str(exc), "repr": repr(exc), "traceback": traceback.format_exc()}
    assert_no_leak(result, account, capsys, caplog)


@pytest.mark.parametrize("secret_peer", SCENARIOS, indirect=True)
@pytest.mark.parametrize("target, draft", [(None, False), ("alpha", False), ("alpha", True)])
def test_doctor_never_exposes_secret(secret_peer, target, draft, capsys, caplog):
    account, outcome = secret_peer
    caplog.set_level(logging.DEBUG)
    assert cli.doctor(target, test_draft=draft) == int(outcome != "ok")
    output = capsys.readouterr()
    results = json.loads(output.out)
    assert results[0]["status"] == ("ok" if outcome == "ok" else "error")
    if outcome != "ok":
        assert account.credential_ref in results[0]["error"]
    assert CANARY not in output.out + output.err
    assert_no_leak(results, account, capsys, caplog)
