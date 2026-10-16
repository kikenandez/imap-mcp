"""Read-only IMAP helpers built on imap-tools.

Every fetch uses mark_seen=False, so imap-tools issues BODY.PEEK and reading a
message never sets the \\Seen flag in the real mailbox.
"""

from __future__ import annotations

import datetime as dt
import imaplib
import ssl
import unicodedata
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from html.parser import HTMLParser

from imap_tools import AND, OR, MailBox
from imap_tools.errors import MailboxUidsError

from .accounts import Account


@contextmanager
def open_box(account: Account) -> Iterator[MailBox]:
    """Yield a logged-in, read-only mailbox for the account. Raises on failure."""
    if not account.enabled:
        raise ValueError(f"account {account.key!r} is disabled")
    pw = account.password()
    if not pw:
        raise RuntimeError(f"no password for {account.key} (env {account.password_env} unset)")
    # Google shows app passwords with spaces for readability; IMAP wants them bare.
    if account.host.endswith("gmail.com"):
        pw = pw.replace(" ", "")
    box = MailBox(
        account.host,
        port=account.port,
        ssl_context=ssl.create_default_context(),
        timeout=account.timeout_seconds,
    )
    try:
        box.login(account.email, pw, initial_folder=None)
        box.folder.set("INBOX", readonly=True)
        yield box
    finally:
        with suppress(Exception):
            box.logout()


class _HTMLText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden += 1
        if tag in {"br", "p", "div", "li", "tr", "h1", "h2", "blockquote"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)
        if tag in {"p", "div", "li", "tr", "blockquote"}:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def body_text(msg) -> str:
    if msg.text:
        return msg.text.strip()
    parser = _HTMLText()
    parser.feed(msg.html or "")
    return "\n".join(line.strip() for line in "".join(parser.parts).splitlines() if line.strip())


def _snippet(msg, length: int = 200) -> str:
    return " ".join(body_text(msg).split())[:length]


def row(msg, account_key: str) -> dict:
    """Compact metadata row for list/search results."""
    return {
        "account": account_key,
        "id": msg.uid,
        "from": msg.from_,
        "to": list(msg.to),
        "subject": msg.subject,
        "date": msg.date.isoformat() if msg.date else None,
        "unread": "\\Seen" not in msg.flags,
        "snippet": _snippet(msg),
    }


def _criteria(query: str | None, since: dt.date | None, unread_only: bool):
    parts = []
    if query:
        parts.append(OR(from_=query, subject=query, text=query))
    if since:
        parts.append(AND(date_gte=since))
    if unread_only:
        parts.append(AND(seen=False))
    if not parts:
        return AND(all=True)
    return AND(*parts) if len(parts) > 1 else parts[0]


def fetch_rows(
    account: Account,
    *,
    query: str | None = None,
    since: dt.date | None = None,
    unread_only: bool = False,
    limit: int = 25,
) -> list[dict]:
    if limit < 1:
        raise ValueError("limit must be positive")
    limit = min(limit, 100)
    with open_box(account) as box:

        def fetch(text, charset):
            return [
                row(m, account.key)
                for m in box.fetch(
                    _criteria(text, since, unread_only),
                    charset=charset,
                    limit=limit,
                    reverse=True,
                    mark_seen=False,
                    bulk=50,
                    headers_only=True,
                )
            ]

        try:
            return fetch(query, "UTF-8")
        except (MailboxUidsError, imaplib.IMAP4.error):
            folded = unicodedata.normalize("NFKD", query or "").encode("ascii", "ignore").decode()
            if query and not folded.strip():
                raise ValueError(
                    "server rejected UTF-8 search; query has no ASCII equivalent"
                ) from None
            return fetch(folded, "US-ASCII")


def fetch_one(account: Account, uid: str) -> dict | None:
    """Full message by UID (INBOX). Returns None if not found."""
    with open_box(account) as box:
        msgs = list(box.fetch(AND(uid=uid), mark_seen=False, bulk=False))
        if not msgs:
            return None
        m = msgs[0]
        return {
            "account": account.key,
            "id": m.uid,
            "from": m.from_,
            "to": list(m.to),
            "cc": list(m.cc),
            "subject": m.subject,
            "date": m.date.isoformat() if m.date else None,
            "unread": "\\Seen" not in m.flags,
            "message_id": m.headers.get("message-id", [None])[0],
            "body": body_text(m),
        }
