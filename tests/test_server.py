import datetime as dt

import pytest

from imap_mcp import imap, server


def test_parse_since():
    assert server._parse_since(None) is None
    assert server._parse_since("2026-07-01") == dt.date(2026, 7, 1)
    with pytest.raises(ValueError):
        server._parse_since("July 1st")


def test_targets(config):
    assert [a.key for a in server._targets(None)] == ["alpha", "gmailish"]
    assert [a.key for a in server._targets("alpha")] == ["alpha"]
    assert server._targets("off") == []  # disabled account resolves to nothing
    with pytest.raises(KeyError):
        server._targets("nope")


def test_list_accounts_statuses(config, monkeypatch):
    class Box:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def open_box(account):
        if account.key == "gmailish":
            raise ConnectionRefusedError("nope")
        return Box()

    monkeypatch.setenv("ALPHA_PW", "pw")
    monkeypatch.setenv("BETA_PW", "pw")
    monkeypatch.setattr(imap, "open_box", open_box)

    status = {e["account"]: e["status"] for e in server.list_accounts()}
    assert status == {
        "alpha": "ok",
        "gmailish": "unreachable: ConnectionRefusedError",
        "off": "disabled",
    }

    monkeypatch.delenv("ALPHA_PW")
    status = {e["account"]: e["status"] for e in server.list_accounts()}
    assert status["alpha"] == "no-credential"


def test_list_emails_merges_and_sorts(config, monkeypatch):
    def fetch_rows(account, **kwargs):
        return {
            "alpha": [
                {"account": "alpha", "id": "1", "date": "2026-07-01T00:00:00+00:00"},
                {"account": "alpha", "id": "2", "date": "2026-07-03T00:00:00+00:00"},
            ],
            "gmailish": [
                {"account": "gmailish", "id": "9", "date": "2026-07-02T00:00:00+00:00"},
            ],
        }[account.key]

    monkeypatch.setattr(imap, "fetch_rows", fetch_rows)
    rows = server.list_emails()
    assert [(r["account"], r["id"]) for r in rows] == [
        ("alpha", "2"),
        ("gmailish", "9"),
        ("alpha", "1"),
    ]


def test_list_emails_single_account_passes_filters(config, monkeypatch):
    seen = {}

    def fetch_rows(account, **kwargs):
        seen[account.key] = kwargs
        return []

    monkeypatch.setattr(imap, "fetch_rows", fetch_rows)
    server.list_emails(account="alpha", since="2026-07-01", unread_only=True, limit=5)
    assert seen == {
        "alpha": {"since": dt.date(2026, 7, 1), "unread_only": True, "limit": 5, "folder": None},
    }


def test_list_emails_account_failure_is_isolated(config, monkeypatch):
    def fetch_rows(account, **kwargs):
        if account.key == "alpha":
            raise TimeoutError("slow server")
        return [{"account": account.key, "id": "1", "date": "2026-07-01T00:00:00+00:00"}]

    monkeypatch.setattr(imap, "fetch_rows", fetch_rows)
    rows = server.list_emails()
    errors = [r for r in rows if "error" in r]
    assert errors == [{"account": "alpha", "error": "TimeoutError: slow server"}]
    assert [r["account"] for r in rows if "error" not in r] == ["gmailish"]


def test_search_emails_passes_query(config, monkeypatch):
    seen = {}

    def fetch_rows(account, **kwargs):
        seen[account.key] = kwargs
        return []

    monkeypatch.setattr(imap, "fetch_rows", fetch_rows)
    server.search_emails("invoice", since="2026-07-01", limit=10)
    assert set(seen) == {"alpha", "gmailish"}
    assert seen["alpha"] == {
        "query": "invoice",
        "since": dt.date(2026, 7, 1),
        "limit": 10,
        "folder": None,
    }


def test_get_email_found(config, monkeypatch):
    monkeypatch.setattr(imap, "fetch_one", lambda a, uid, **kwargs: {"account": a.key, "id": uid})
    assert server.get_email("alpha", "42") == {"account": "alpha", "id": "42"}


def test_get_email_not_found(config, monkeypatch):
    monkeypatch.setattr(imap, "fetch_one", lambda a, uid, **kwargs: None)
    out = server.get_email("alpha", "42")
    assert out["error"] == "not found in INBOX"


def test_all_tools_registered():
    import asyncio

    tools = {t.name for t in asyncio.run(server.mcp.list_tools())}
    assert tools == {
        "list_accounts",
        "list_emails",
        "search_emails",
        "get_email",
        "list_folders",
        "create_draft",
    }


def test_dates_sort_by_instant(config, monkeypatch):
    monkeypatch.setattr(
        imap,
        "fetch_rows",
        lambda a, **kw: [
            {"date": "2026-07-01T12:00:00+04:00", "id": "earlier"},
            {"date": "2026-07-01T10:00:00+00:00", "id": "later"},
        ],
    )
    assert [r["id"] for r in server.list_emails(account="alpha")] == ["later", "earlier"]


@pytest.mark.parametrize("operation", ["list", "search", "accounts"])
def test_accounts_run_concurrently(config, monkeypatch, operation):
    from contextlib import contextmanager
    from threading import Barrier

    barrier = Barrier(2, timeout=3)

    def fetch(*args, **kwargs):
        barrier.wait()
        return []

    @contextmanager
    def open_box(a):
        barrier.wait()
        yield

    monkeypatch.setenv("ALPHA_PW", "test-password")
    monkeypatch.setenv("BETA_PW", "test-password")
    monkeypatch.setattr(imap, "fetch_rows", fetch)
    monkeypatch.setattr(imap, "open_box", open_box)
    if operation == "accounts":
        assert [r["status"] for r in server.list_accounts()] == ["ok", "ok", "disabled"]
    elif operation == "search":
        assert server.search_emails("café") == []
    else:
        assert server.list_emails() == []


def test_read_only_annotations():
    import asyncio

    assert all(
        t.annotations.model_dump(by_alias=True)["readOnlyHint"]
        for t in asyncio.run(server.mcp.list_tools())
        if t.name != "create_draft"
    )


def test_folder_tools_forward_arguments(config, monkeypatch):
    monkeypatch.setattr(imap, "fetch_rows", lambda a, **kw: [{"folder": kw["folder"]}])
    assert server.list_emails(account="alpha", folder="sent") == [{"folder": "sent"}]
    assert server.search_emails("x", account="alpha", folder="sent") == [{"folder": "sent"}]
    monkeypatch.setattr(imap, "fetch_one", lambda a, uid, folder: None)
    assert server.get_email("alpha", "42", folder="sent")["error"] == "not found in sent"
    monkeypatch.setattr(imap, "list_folders", lambda a: [{"account": a.key}])
    assert server.list_folders("alpha") == [{"account": "alpha"}]


def test_draft_tool_and_annotations(config, monkeypatch):
    import asyncio

    seen = {}

    def create(account, to, subject, body, **kwargs):
        seen.update(account=account.key, to=to, subject=subject, body=body, **kwargs)
        return {"uid": "123"}

    monkeypatch.setattr(imap, "create_draft", create)
    assert server.create_draft("alpha", [], "Hi", "Reply", reply_to_uid="42", reply_all=True) == {
        "uid": "123"
    }
    assert seen["account"] == "alpha" and seen["reply_to_uid"] == "42" and seen["reply_all"]
    tool = next(t for t in asyncio.run(server.mcp.list_tools()) if t.name == "create_draft")
    annotations = tool.annotations.model_dump(by_alias=True)
    assert annotations["readOnlyHint"] is False
    assert annotations["destructiveHint"] is False
    assert annotations["idempotentHint"] is False
