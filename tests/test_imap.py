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
        self.append_calls = []
        self.fetch_calls = []
        FakeMailBox.instances.append(self)

    def set_folder(self, name, readonly=False):
        self.selections.append((name, readonly))

    def login(self, email, password, initial_folder="INBOX"):
        self.login_args = (email, password, initial_folder)
        return self

    def fetch(self, criteria, **kwargs):
        self.fetch_calls.append((criteria, kwargs))
        self.fetch_criteria = criteria
        self.fetch_kwargs = kwargs
        return iter(FakeMailBox.messages)

    def append(self, message, folder, flag_set):
        self.append_calls.append((message, folder, flag_set))
        return ("OK", [b"[APPENDUID 123 456] appended"])

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


@pytest.fixture
def draft_account(config, monkeypatch):
    from dataclasses import replace

    monkeypatch.setenv("ALPHA_PW", "test-password")
    FakeMailBox.folders = [SimpleNamespace(name="Drafts", delim="/", flags=())]
    return replace(accounts.get_account("alpha"), allow_drafts=True, from_name="Élodie")


def parsed_draft():
    from email import policy
    from email.parser import BytesParser

    raw, folder, flags = FakeMailBox.instances[-1].append_calls[0]
    return BytesParser(policy=policy.default).parsebytes(raw), raw, folder, flags


def test_drafts_require_opt_in_before_connecting(config):
    with pytest.raises(PermissionError, match="allow_drafts"):
        imap.create_draft(accounts.get_account("alpha"), "to@example.com", "Subject", "Body")
    assert not FakeMailBox.instances


def test_draft_mime_and_flags(draft_account):
    from email.utils import parsedate_to_datetime

    result = imap.create_draft(
        draft_account,
        "To <to@example.com>",
        "Café à Paris",
        "Déjà vu",
        cc=["cc@example.com"],
        html_body="<p>Déjà vu</p>",
    )
    msg, raw, folder, flags = parsed_draft()
    assert str(msg["From"]) == "Élodie <alpha@example.com>"
    assert str(msg["Subject"]) == "Café à Paris"
    assert str(msg["To"]) == "To <to@example.com>"
    assert str(msg["Cc"]) == "cc@example.com"
    assert parsedate_to_datetime(msg["Date"]).tzinfo is not None
    assert str(msg["Message-ID"]).endswith("@example.com>")
    assert msg.get_body(preferencelist=("plain",)).get_content().strip() == "Déjà vu"
    assert msg.get_body(preferencelist=("html",)).get_content().strip() == "<p>Déjà vu</p>"
    assert b"\r\n" in raw and b"\n" not in raw.replace(b"\r\n", b"")
    assert folder == "Drafts" and set(flags) == {"\\Draft", "\\Seen"}
    assert FakeMailBox.instances[-1].selections == [("INBOX", True)]
    assert result == {
        "account": "alpha",
        "folder": "Drafts",
        "uid": "456",
        "message_id": str(msg["Message-ID"]),
        "subject": "Café à Paris",
        "to": ["To <to@example.com>"],
        "cc": ["cc@example.com"],
    }


@pytest.mark.parametrize(
    "name, delim, flags",
    [
        ("Draft", "/", ()),
        ("Brouillons", "/", ()),
        ("Borradores", "/", ()),
        ("Entwürfe", "/", ()),
        ("Bozze", "/", ()),
        ("INBOX.Drafts", ".", ()),
        ("Compositions", "/", ("\\dRaFtS",)),
    ],
)
def test_draft_folder_fallbacks(draft_account, name, delim, flags):
    FakeMailBox.folders = [SimpleNamespace(name=name, delim=delim, flags=flags)]
    assert imap.create_draft(draft_account, [], "", "Body")["folder"] == name


def test_no_drafts_folder_never_appends(draft_account):
    FakeMailBox.folders = []
    with pytest.raises(ValueError, match="no selectable folder matching 'drafts'"):
        imap.create_draft(draft_account, [], "", "Body")
    assert not FakeMailBox.instances[-1].append_calls
    assert FakeMailBox.instances[-1].logged_out


def test_reply_threading_recipients_and_quote(draft_account):
    FakeMailBox.folders.append(SimpleNamespace(name="Sent", delim="/", flags=("\\Sent",)))
    FakeMailBox.messages = [
        fake_msg(
            headers={
                "message-id": ["<original@example.com>"],
                "references": ["<root@example.com>"],
                "in-reply-to": ["<ignored@example.com>"],
                "reply-to": ["Support <support@example.com>"],
                "from": ["Sender <sender@example.com>"],
                "to": ["ALPHA@example.com, peer@example.com"],
                "cc": ["peer@example.com, Support <support@example.com>, third@example.com"],
                "date": ["Thu, 30 Jul 2026 12:00:00 +0000"],
            },
            text="Original café\nSecond line",
        )
    ]
    result = imap.create_draft(
        draft_account,
        [],
        "",
        "Ma réponse",
        reply_to_uid="42",
        reply_folder="sent",
        reply_all=True,
        html_body="<p>Ma réponse</p>",
    )
    msg, _, _, _ = parsed_draft()
    assert str(msg["In-Reply-To"]) == "<original@example.com>"
    assert str(msg["References"]) == "<root@example.com> <original@example.com>"
    assert result["to"] == ["Support <support@example.com>"]
    assert result["cc"] == ["peer@example.com", "third@example.com"]
    assert result["subject"] == "Re: Hello"
    plain = msg.get_body(preferencelist=("plain",)).get_content()
    assert "On Thu, 30 Jul 2026 12:00:00 +0000, Sender <sender@example.com> wrote:" in plain
    assert "> Original café\r\n> Second line" in plain
    assert "<blockquote>" in msg.get_body(preferencelist=("html",)).get_content()
    box = FakeMailBox.instances[-1]
    assert box.selections == [("INBOX", True), ("Sent", True), ("INBOX", True)]
    assert [kw["headers_only"] for _, kw in box.fetch_calls] == [True, False]
    assert all(kw["mark_seen"] is False for _, kw in box.fetch_calls)


@pytest.mark.parametrize("subject", ["Re: Hi", "aw : Hallo", "ANTW: Hi", "rv: Hola", "Réf : Salut"])
def test_reply_subject_prefix_preserved(draft_account, subject):
    FakeMailBox.messages = [fake_msg()]
    result = imap.create_draft(draft_account, "to@example.com", subject, "Body", reply_to_uid="42")
    assert result["subject"] == subject


def test_reply_uses_in_reply_to_fallback_and_from(draft_account):
    FakeMailBox.messages = [
        fake_msg(
            headers={"message-id": ["<id@example.com>"], "in-reply-to": ["<parent@example.com>"]}
        )
    ]
    result = imap.create_draft(draft_account, "", "", "Reply", reply_to_uid="42")
    assert result["to"] == ["sender@example.com"]
    assert str(parsed_draft()[0]["References"]) == "<parent@example.com> <id@example.com>"


def test_reply_missing_original_does_not_append(draft_account):
    with pytest.raises(ValueError, match="not found in INBOX"):
        imap.create_draft(draft_account, [], "", "Reply", reply_to_uid="42")
    assert not FakeMailBox.instances[-1].append_calls


@pytest.mark.parametrize("field", ["to", "cc", "subject"])
def test_header_injection_never_appends(draft_account, field):
    args = dict(to="to@example.com", subject="Hello", body="Body")
    args[field] = "to@example.com\r\nBcc: injected@example.com"
    with pytest.raises(ValueError, match="newlines"):
        imap.create_draft(draft_account, **args)
    assert not FakeMailBox.instances[-1].append_calls


def test_append_without_uid_and_no_retries(draft_account, monkeypatch):
    from imap_tools.errors import MailboxAppendError

    monkeypatch.setattr(FakeMailBox, "append", lambda *a, **kw: ("OK", [b"appended"]))
    assert imap.create_draft(draft_account, [], "", "Body")["uid"] is None
    calls = []

    def fail(*args, **kwargs):
        calls.append(1)
        raise MailboxAppendError(("NO", [b"quota exceeded"]), "OK")

    monkeypatch.setattr(FakeMailBox, "append", fail)
    with pytest.raises(MailboxAppendError):
        imap.create_draft(draft_account, [], "", "Body")
    assert len(calls) == 1


def test_reply_options_require_uid(draft_account):
    with pytest.raises(ValueError, match="require reply_to_uid"):
        imap.create_draft(draft_account, [], "", "Body", reply_all=True)
    assert not FakeMailBox.instances


def test_disabled_opted_in_account_never_connects(draft_account):
    from dataclasses import replace

    with pytest.raises(ValueError, match="disabled"):
        imap.create_draft(replace(draft_account, enabled=False), [], "", "Body")
    assert not FakeMailBox.instances


def test_draft_config_folder_fallback(draft_account):
    from dataclasses import replace

    FakeMailBox.folders = [SimpleNamespace(name="Compositions", delim="/", flags=())]
    account = replace(draft_account, folders={"drafts": "Compositions"})
    assert imap.create_draft(account, [], "", "Body")["folder"] == "Compositions"


def test_reply_without_thread_headers_and_html_only_original(draft_account):
    FakeMailBox.messages = [fake_msg(headers={}, text="", html="<p>A &amp; B</p>")]
    result = imap.create_draft(
        draft_account, "explicit@example.com", "Custom", "Reply", reply_to_uid="42"
    )
    msg = parsed_draft()[0]
    assert "In-Reply-To" not in msg and "References" not in msg
    assert "> A & B" in msg.get_content()
    assert result["to"] == ["explicit@example.com"]
    assert result["subject"] == "Re: Custom"
