import datetime as dt
from types import SimpleNamespace

import pytest

from imap_mcp import accounts, imap


class FakeMailBox:
    """Stands in for imap_tools.MailBox; records login/fetch args, no network."""

    instances = []
    messages = []
    folders = []

    def __init__(self, host, port=993, ssl_context=None, timeout=None):
        self.ssl_context = ssl_context
        self.timeout = timeout
        self.folder = SimpleNamespace(
            set=self.set_folder,
            get=lambda: self.selections[-1][0],
            list=lambda: FakeMailBox.folders,
        )
        self.selections = []
        self.host = host
        self.port = port
        self.login_args = None
        self.fetch_kwargs = None
        self.logged_out = False
        FakeMailBox.instances.append(self)

    def set_folder(self, name, readonly=False):
        self.selections.append((name, readonly))

    def login(self, email, password, initial_folder="INBOX"):
        self.login_args = (email, password, initial_folder)
        return self

    def fetch(self, criteria, **kwargs):
        self.fetch_criteria = criteria
        self.fetch_kwargs = kwargs
        return iter(FakeMailBox.messages)

    def logout(self):
        self.logged_out = True


@pytest.fixture(autouse=True)
def fake_mailbox(monkeypatch):
    FakeMailBox.instances = []
    FakeMailBox.messages = []
    FakeMailBox.folders = []
    monkeypatch.setattr(imap, "MailBox", FakeMailBox)


def fake_msg(**overrides):
    base = dict(
        uid="42",
        from_="sender@example.com",
        to=("rcpt@example.com",),
        cc=(),
        subject="Hello",
        date=dt.datetime(2026, 7, 30, 12, 0, tzinfo=dt.UTC),
        flags=("\\Seen",),
        text="line one\nline two",
        html="",
        headers={"message-id": ["<abc@example.com>"]},
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_open_box_requires_password(config):
    alpha = accounts.get_account("alpha")
    with pytest.raises(RuntimeError, match="ALPHA_PW"), imap.open_box(alpha):
        pass


def test_open_box_logs_in_and_out(config, monkeypatch):
    monkeypatch.setenv("ALPHA_PW", "s3cret")
    with imap.open_box(accounts.get_account("alpha")) as box:
        assert box.login_args == ("alpha@example.com", "s3cret", None)
        assert (box.host, box.port) == ("imap.example.com", 1993)
        assert not box.logged_out
    assert box.logged_out


def test_open_box_strips_spaces_for_gmail_only(config, monkeypatch):
    monkeypatch.setenv("BETA_PW", "abcd efgh ijkl mnop")
    with imap.open_box(accounts.get_account("gmailish")) as box:
        assert box.login_args[1] == "abcdefghijklmnop"

    monkeypatch.setenv("ALPHA_PW", "pass with spaces")
    with imap.open_box(accounts.get_account("alpha")) as box:
        assert box.login_args[1] == "pass with spaces"


def test_snippet_collapses_newlines_and_truncates():
    msg = fake_msg(text="a\nb\nc" + "x" * 500)
    snip = imap._snippet(msg)
    assert snip.startswith("a b c")
    assert len(snip) == 200


def test_snippet_falls_back_to_html():
    msg = fake_msg(text="", html="<p>hi</p>")
    assert imap._snippet(msg) == "hi"


def test_row_shape():
    msg = fake_msg(flags=())
    assert imap.row(msg, "alpha") == {
        "account": "alpha",
        "id": "42",
        "from": "sender@example.com",
        "to": ["rcpt@example.com"],
        "subject": "Hello",
        "date": "2026-07-30T12:00:00+00:00",
        "unread": True,
        "snippet": "line one line two",
    }


def test_row_seen_flag_means_read():
    assert imap.row(fake_msg(), "alpha")["unread"] is False


@pytest.mark.parametrize(
    "kwargs, expected",
    [
        (dict(query=None, since=None, unread_only=False), "(ALL)"),
        (dict(query=None, since=dt.date(2026, 7, 1), unread_only=False), "(SINCE 1-Jul-2026)"),
        (dict(query=None, since=None, unread_only=True), "(UNSEEN)"),
    ],
)
def test_criteria_simple(kwargs, expected):
    assert str(imap._criteria(**kwargs)) == expected


def test_criteria_query_matches_from_subject_body():
    crit = str(imap._criteria("invoice", None, False))
    for token in ("FROM", "SUBJECT", "TEXT", "invoice"):
        assert token in crit


def test_criteria_combines_with_and():
    crit = str(imap._criteria("invoice", dt.date(2026, 7, 1), True))
    for token in ("invoice", "SINCE 1-Jul-2026", "UNSEEN"):
        assert token in crit


def test_fetch_rows_never_marks_seen(config, monkeypatch):
    monkeypatch.setenv("ALPHA_PW", "pw")
    FakeMailBox.messages = [fake_msg()]
    rows = imap.fetch_rows(accounts.get_account("alpha"), limit=7)

    assert [r["id"] for r in rows] == ["42"]
    assert rows[0]["account"] == "alpha"
    kwargs = FakeMailBox.instances[-1].fetch_kwargs
    assert kwargs["mark_seen"] is False
    assert kwargs["reverse"] is True
    assert kwargs["limit"] == 7


def test_fetch_one_full_message(config, monkeypatch):
    monkeypatch.setenv("ALPHA_PW", "pw")
    FakeMailBox.messages = [fake_msg()]
    msg = imap.fetch_one(accounts.get_account("alpha"), "42")

    assert msg == {
        "account": "alpha",
        "id": "42",
        "folder": "INBOX",
        "from": "sender@example.com",
        "to": ["rcpt@example.com"],
        "cc": [],
        "subject": "Hello",
        "date": "2026-07-30T12:00:00+00:00",
        "unread": False,
        "message_id": "<abc@example.com>",
        "body": "line one\nline two",
    }
    assert FakeMailBox.instances[-1].fetch_kwargs["mark_seen"] is False


def test_fetch_one_not_found(config, monkeypatch):
    monkeypatch.setenv("ALPHA_PW", "pw")
    FakeMailBox.messages = []
    assert imap.fetch_one(accounts.get_account("alpha"), "42") is None


def test_verified_tls_timeout_and_examine(config, monkeypatch):
    import ssl
    from dataclasses import replace

    monkeypatch.setenv("ALPHA_PW", "test-password")
    account = replace(accounts.get_account("alpha"), timeout_seconds=7)
    with imap.open_box(account) as box:
        assert box.ssl_context.verify_mode == ssl.CERT_REQUIRED
        assert box.ssl_context.check_hostname
        assert box.timeout == 7
        assert box.selections == [("INBOX", True)]


def test_login_failure_closes_connection(config, monkeypatch):
    monkeypatch.setenv("ALPHA_PW", "test-password")

    def fail(*args, **kwargs):
        raise RuntimeError("login failed")

    monkeypatch.setattr(FakeMailBox, "login", fail)
    with (
        pytest.raises(RuntimeError, match="login failed"),
        imap.open_box(accounts.get_account("alpha")),
    ):
        pass
    assert FakeMailBox.instances[-1].logged_out


def test_disabled_never_connects(config):
    with pytest.raises(ValueError, match="disabled"), imap.open_box(accounts.get_account("off")):
        pass
    assert not FakeMailBox.instances


def test_header_fetch_limit(config, monkeypatch):
    monkeypatch.setenv("ALPHA_PW", "test-password")
    imap.fetch_rows(accounts.get_account("alpha"), limit=1000)
    args = FakeMailBox.instances[-1].fetch_kwargs
    assert args["limit"] == 100
    assert args["headers_only"] is True
    assert args["bulk"] == 50
    assert args["charset"] == "UTF-8"
    with pytest.raises(ValueError, match="positive"):
        imap.fetch_rows(accounts.get_account("alpha"), limit=0)


@pytest.mark.parametrize("query", ["café", "invoice", "東京"])
def test_search_charset_fallback(config, monkeypatch, query):
    import imaplib

    monkeypatch.setenv("ALPHA_PW", "test-password")
    calls = []

    def fetch(self, criteria, **kwargs):
        calls.append((str(criteria), kwargs))
        if kwargs["charset"] == "UTF-8":
            raise imaplib.IMAP4.error("BADCHARSET")
        yield fake_msg()

    monkeypatch.setattr(FakeMailBox, "fetch", fetch)
    if query == "東京":
        with pytest.raises(ValueError, match="no ASCII equivalent"):
            imap.fetch_rows(accounts.get_account("alpha"), query=query)
        assert len(calls) == 1
    else:
        assert imap.fetch_rows(accounts.get_account("alpha"), query=query)
        assert len(calls) == 2
        assert calls[1][1]["charset"] == "US-ASCII"
        assert ("cafe" if query == "café" else query) in calls[1][0]


def test_html_body_strips_hidden_content_and_decodes_entities():
    assert (
        imap.body_text(
            fake_msg(text="", html="<style>x</style><p>A &amp; B</p><p>C</p><script>x</script>")
        )
        == "A & B\nC"
    )


def test_folder_reads_use_examine_and_return_folder(config, monkeypatch):
    monkeypatch.setenv("ALPHA_PW", "test-password")
    FakeMailBox.folders = [SimpleNamespace(name="INBOX.Drafts", delim=".", flags=())]
    FakeMailBox.messages = [fake_msg()]
    account = accounts.get_account("alpha")
    for result in (
        imap.fetch_rows(account, folder="drafts"),
        [imap.fetch_one(account, "42", folder="drafts")],
    ):
        assert result[0]["folder"] == "INBOX.Drafts"
    assert all(box.selections == [("INBOX.Drafts", True)] for box in FakeMailBox.instances)
    assert imap.list_folders(account) == [
        {"name": "INBOX.Drafts", "delimiter": ".", "flags": [], "role": "drafts"}
    ]
