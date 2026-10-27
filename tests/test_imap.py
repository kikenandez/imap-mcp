import datetime as dt
from types import SimpleNamespace

import pytest

from imap_mcp import accounts, imap


class FakeMailBox:
    """Stands in for imap_tools.MailBox; records login/fetch args, no network."""

    instances = []
    messages = []
    bodystructures = {}  # uid -> raw BODYSTRUCTURE text, or an Exception to raise
    bodystructure_status = "OK"

    def __init__(self, host, port=993):
        self.client = SimpleNamespace(uid=self.uid_fetch)
        self.uid_calls = []
        self.host = host
        self.port = port
        self.login_args = None
        self.fetch_kwargs = None
        self.logged_out = False
        FakeMailBox.instances.append(self)

    def login(self, email, password, initial_folder="INBOX"):
        self.login_args = (email, password, initial_folder)
        return self

    def fetch(self, criteria, **kwargs):
        self.fetch_criteria = criteria
        self.fetch_kwargs = kwargs
        return iter(FakeMailBox.messages)

    def uid_fetch(self, command, uids, parts):
        self.uid_calls.append((command, uids, parts))
        assert command == "FETCH"
        # Only metadata may be requested: no body part, nothing that could set \\Seen.
        assert parts == "(UID BODYSTRUCTURE)"
        return self.bodystructure_fetch(uids)

    def bodystructure_fetch(self, uids):
        if FakeMailBox.bodystructure_status != "OK":
            return FakeMailBox.bodystructure_status, [b"fetch refused"]
        data = []
        for i, uid in enumerate(uids.split(","), 1):
            structure = FakeMailBox.bodystructures.get(uid)
            if isinstance(structure, Exception):
                raise structure
            if structure is not None:
                data.append(f"{i} (UID {uid} BODYSTRUCTURE {structure})".encode())
        return "OK", data or [None]

    def logout(self):
        self.logged_out = True


@pytest.fixture(autouse=True)
def fake_mailbox(monkeypatch):
    FakeMailBox.instances = []
    FakeMailBox.messages = []
    FakeMailBox.bodystructures = {}
    FakeMailBox.bodystructure_status = "OK"
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
        assert box.login_args == ("alpha@example.com", "s3cret", "INBOX")
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
    assert imap._snippet(msg) == "<p>hi</p>"


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


PDF_PART = (
    '("APPLICATION" "PDF" ("NAME" "q3.pdf") NIL NIL "BASE64" 2048 NIL '
    '("ATTACHMENT" ("FILENAME" "q3.pdf")) NIL NIL)'
)
TEXT_PART = '("TEXT" "PLAIN" ("CHARSET" "UTF-8") NIL NIL "7BIT" 12 1 NIL NIL NIL)'


def test_rows_report_attachments_from_bodystructure(config, monkeypatch):
    monkeypatch.setenv("ALPHA_PW", "pw")
    FakeMailBox.messages = [fake_msg(uid="42"), fake_msg(uid="43"), fake_msg(uid="44")]
    FakeMailBox.bodystructures = {
        "42": f'({TEXT_PART} {PDF_PART} "MIXED" NIL NIL NIL)',
        "43": TEXT_PART,
        # "44": the server returned nothing for this UID
    }
    rows = {r["id"]: r for r in imap.fetch_rows(accounts.get_account("alpha"))}
    assert rows["42"]["has_attachments"] is True
    assert rows["42"]["attachments"] == [
        {"name": "q3.pdf", "size": 2048, "content_type": "application/pdf"}
    ]
    assert rows["43"]["has_attachments"] is False and rows["43"]["attachments"] == []
    # Absent from the reply: unknown (null), never "no attachments".
    assert rows["44"]["has_attachments"] is None and rows["44"]["attachments"] == []


def test_attachment_lookup_is_one_metadata_fetch_and_reads_no_content(config, monkeypatch):
    monkeypatch.setenv("ALPHA_PW", "pw")
    FakeMailBox.messages = [fake_msg(uid="42"), fake_msg(uid="43")]
    imap.fetch_rows(accounts.get_account("alpha"))
    box = FakeMailBox.instances[-1]
    assert box.uid_calls == [("FETCH", "42,43", "(UID BODYSTRUCTURE)")]
    assert box.fetch_kwargs["mark_seen"] is False


def test_attachment_lookup_is_chunked(config, monkeypatch):
    monkeypatch.setenv("ALPHA_PW", "pw")
    FakeMailBox.messages = [fake_msg(uid=str(n)) for n in range(1, 101)]
    imap.fetch_rows(accounts.get_account("alpha"), limit=100)
    calls = FakeMailBox.instances[-1].uid_calls
    assert [len(c[1].split(",")) for c in calls] == [50, 50]


def test_no_messages_means_no_attachment_fetch(config, monkeypatch):
    monkeypatch.setenv("ALPHA_PW", "pw")
    assert imap.fetch_rows(accounts.get_account("alpha")) == []
    assert FakeMailBox.instances[-1].uid_calls == []


def test_malformed_bodystructure_does_not_break_the_list(config, monkeypatch):
    monkeypatch.setenv("ALPHA_PW", "pw")
    FakeMailBox.messages = [fake_msg(uid="42")]
    FakeMailBox.bodystructures = {"42": '(("TEXT" "PLAIN"'}
    row = imap.fetch_rows(accounts.get_account("alpha"))[0]
    assert row["has_attachments"] is None and row["attachments"] == []
    assert row["subject"] == "Hello"


@pytest.mark.parametrize("failure", [("NO", "refused"), ("BAD", "unsupported")])
def test_refused_bodystructure_fetch_does_not_break_the_list(config, monkeypatch, failure):
    monkeypatch.setenv("ALPHA_PW", "pw")
    FakeMailBox.messages = [fake_msg(uid="42")]
    FakeMailBox.bodystructure_status = failure[0]
    row = imap.fetch_rows(accounts.get_account("alpha"))[0]
    assert row["has_attachments"] is None and row["attachments"] == []
    assert row["subject"] == "Hello"


def test_bodystructure_protocol_error_does_not_break_the_list(config, monkeypatch):
    import imaplib

    monkeypatch.setenv("ALPHA_PW", "pw")
    FakeMailBox.messages = [fake_msg(uid="42")]
    FakeMailBox.bodystructures = {"42": imaplib.IMAP4.error("server hiccup")}
    row = imap.fetch_rows(accounts.get_account("alpha"))[0]
    assert row["has_attachments"] is None and row["attachments"] == []


def test_failed_chunk_is_unknown_only_for_its_own_uids(config, monkeypatch):
    monkeypatch.setenv("ALPHA_PW", "pw")
    FakeMailBox.messages = [fake_msg(uid=str(n)) for n in range(1, 101)]
    FakeMailBox.bodystructures = {
        str(n): f'({TEXT_PART} {PDF_PART} "MIXED" NIL NIL NIL)' for n in range(1, 101)
    }
    original = FakeMailBox.bodystructure_fetch

    first_uid_of_first_chunk = None

    def record_then_fail(self, uids):
        nonlocal first_uid_of_first_chunk
        if first_uid_of_first_chunk is None:
            first_uid_of_first_chunk = uids
            return original(self, uids)
        return "NO", [b"refused"]

    monkeypatch.setattr(FakeMailBox, "bodystructure_fetch", record_then_fail)
    rows = imap.fetch_rows(accounts.get_account("alpha"), limit=100)
    first_chunk = set(first_uid_of_first_chunk.split(","))
    assert len(first_chunk) == 50
    for r in rows:
        if r["id"] in first_chunk:
            assert r["has_attachments"] is True
        else:
            assert r["has_attachments"] is None and r["attachments"] == []


def test_read_structure_without_attachments_is_false_not_null(config, monkeypatch):
    monkeypatch.setenv("ALPHA_PW", "pw")
    FakeMailBox.messages = [fake_msg(uid="42")]
    FakeMailBox.bodystructures = {"42": TEXT_PART}
    row = imap.fetch_rows(accounts.get_account("alpha"))[0]
    assert row["has_attachments"] is False and row["attachments"] == []


def test_search_rows_carry_attachments_too(config, monkeypatch):
    monkeypatch.setenv("ALPHA_PW", "pw")
    FakeMailBox.messages = [fake_msg(uid="42")]
    FakeMailBox.bodystructures = {"42": f'({TEXT_PART} {PDF_PART} "MIXED" NIL NIL NIL)'}
    row = imap.fetch_rows(accounts.get_account("alpha"), query="invoice")[0]
    assert row["has_attachments"] is True
