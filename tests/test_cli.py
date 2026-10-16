import json
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from imap_mcp import cli, imap, server


@pytest.fixture
def diagnostics(config, monkeypatch):
    calls = []

    class Box:
        folder = SimpleNamespace(
            list=lambda: [
                SimpleNamespace(name="Draft", delim="/", flags=("\\Drafts",)),
            ]
        )

        def fetch(self, criteria, **kwargs):
            calls.append(("fetch", str(criteria), kwargs))
            return iter(())

    @contextmanager
    def open_box(account):
        calls.append(("login", account.key))
        yield Box()

    def draft(account, to, subject, body):
        calls.append(("draft", account.key, to, subject))
        if not account.allow_drafts:
            raise PermissionError("draft creation is disabled")
        return {"folder": "Draft", "uid": "42"}

    monkeypatch.setattr(imap, "open_box", open_box)
    monkeypatch.setattr(imap, "create_draft", draft)
    return calls


def test_doctor_reads_only_and_shows_status(diagnostics, capsys):
    assert cli.doctor() == 0
    results = json.loads(capsys.readouterr().out)
    assert [r["status"] for r in results] == ["ok", "ok", "disabled"]
    assert results[0]["folders"][0]["role"] == "drafts"
    assert "verification required" in results[0]["tls"]
    assert results[0]["timeout_seconds"] == 30
    assert "no ASCII fallback" in results[0]["utf8_search"]
    assert [call for call in diagnostics if call[0] == "login"] == [
        ("login", "alpha"),
        ("login", "gmailish"),
    ]
    assert not any(call[0] == "draft" for call in diagnostics)
    for _, criteria, kwargs in (call for call in diagnostics if call[0] == "fetch"):
        assert "café" in criteria
        assert kwargs == {
            "charset": "UTF-8",
            "headers_only": True,
            "mark_seen": False,
            "limit": 1,
            "bulk": 50,
        }


def test_doctor_test_draft_is_explicit(config, diagnostics, capsys):
    config.write_text(config.read_text().replace("port = 1993", "port = 1993\nallow_drafts = true"))
    assert cli.doctor("alpha", test_draft=True) == 0
    result = json.loads(capsys.readouterr().out)[0]
    assert result["draft"] == {"folder": "Draft", "uid": "42"}
    assert [call for call in diagnostics if call[0] == "draft"] == [
        ("draft", "alpha", "alpha@example.com", "imap-mcp test draft"),
    ]


def test_doctor_non_opt_in_draft_fails(diagnostics, capsys):
    assert cli.doctor("alpha", test_draft=True) == 1
    assert "disabled" in json.loads(capsys.readouterr().out)[0]["error"]


def test_doctor_error_isolation(diagnostics, monkeypatch, capsys):
    original = imap.open_box

    def open_box(account):
        if account.key == "alpha":
            raise TimeoutError("timed out")
        return original(account)

    monkeypatch.setattr(imap, "open_box", open_box)
    assert cli.doctor() == 1
    assert [r["status"] for r in json.loads(capsys.readouterr().out)] == ["error", "ok", "disabled"]


def test_doctor_utf8_rejection_is_visible(diagnostics, monkeypatch, capsys):
    import imaplib

    def fail(*args, **kwargs):
        raise imaplib.IMAP4.error("BADCHARSET")

    @contextmanager
    def open_box(account):
        yield SimpleNamespace(folder=SimpleNamespace(list=lambda: []), fetch=fail)

    monkeypatch.setattr(imap, "open_box", open_box)
    assert cli.doctor("alpha") == 1
    result = json.loads(capsys.readouterr().out)[0]
    assert result["login"] == "ok" and "BADCHARSET" in result["error"]
    assert "utf8_search" not in result


def test_unknown_account(config, capsys):
    assert cli.doctor("missing") == 1
    assert "unknown account" in json.loads(capsys.readouterr().out)["error"]


def test_missing_configuration(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("IMAP_MCP_ACCOUNTS", str(tmp_path / "missing.toml"))
    assert cli.doctor() == 1
    assert "accounts.example.toml" in json.loads(capsys.readouterr().out)["error"]


def test_default_entrypoint_runs_stdio(monkeypatch):
    calls = []
    monkeypatch.setattr(server.mcp, "run", lambda: calls.append("stdio"))
    cli.main([])
    assert calls == ["stdio"]


def test_cli_dispatches_doctor(diagnostics, capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["doctor", "alpha"])
    assert exc.value.code == 0
    assert len(json.loads(capsys.readouterr().out)) == 1


def test_test_draft_requires_single_named_account(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["doctor", "--test-draft"])
    assert exc.value.code == 2
    assert "requires an account" in capsys.readouterr().err
    with pytest.raises(ValueError, match="requires an account"):
        cli.doctor(test_draft=True)


def test_real_stdio_handshake_and_tool_discovery(tmp_path):
    import asyncio
    import sys

    from mcp import ClientSession, StdioServerParameters, stdio_client

    async def check():
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-c", "from imap_mcp import main; main()"],
            env={"IMAP_MCP_ACCOUNTS": str(tmp_path / "intentionally-missing.toml")},
        )
        async with asyncio.timeout(15):
            async with stdio_client(parameters) as (reader, writer):
                async with ClientSession(reader, writer) as session:
                    initialized = await session.initialize()
                    assert initialized.model_dump(by_alias=True)["serverInfo"]["version"] == "0.2.0"
                    result = await session.list_tools()
                    assert {tool.name for tool in result.tools} == {
                        "list_accounts",
                        "list_folders",
                        "list_emails",
                        "search_emails",
                        "get_email",
                        "create_draft",
                    }

    asyncio.run(check())
