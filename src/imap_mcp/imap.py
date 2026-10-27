"""Read-only IMAP helpers built on imap-tools.

Every fetch uses mark_seen=False, so imap-tools issues BODY.PEEK and reading a
message never sets the \\Seen flag in the real mailbox.
"""

from __future__ import annotations

import datetime as dt
import imaplib
from collections.abc import Iterator
from contextlib import contextmanager, suppress

from imap_tools import AND, OR, MailBox

from .accounts import Account
from .bodystructure import attachments_from_fetch


@contextmanager
def open_box(account: Account) -> Iterator[MailBox]:
    """Yield a logged-in, read-only mailbox for the account. Raises on failure."""
    pw = account.password()
    if not pw:
        raise RuntimeError(f"no password for {account.key} (env {account.password_env} unset)")
    # Google shows app passwords with spaces for readability; IMAP wants them bare.
    if account.host.endswith("gmail.com"):
        pw = pw.replace(" ", "")
    box = MailBox(account.host, port=account.port)
    box.login(account.email, pw, initial_folder="INBOX")
    try:
        yield box
    finally:
        with suppress(Exception):
            box.logout()


def _snippet(msg, length: int = 200) -> str:
    body = (msg.text or msg.html or "").strip().replace("\r", " ").replace("\n", " ")
    return body[:length]


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


ATTACHMENT_FETCH_CHUNK = 50


def _attachments_by_uid(box, uids: list[str]) -> dict[str, list[dict] | None]:
    """Attachment names/sizes from BODYSTRUCTURE only: metadata, no part content, no \\Seen.

    A refused or failed lookup leaves those messages' attachments unknown (None, reported as
    null); it never fails the listing.
    """
    found: dict[str, list[dict] | None] = {}
    for start in range(0, len(uids), ATTACHMENT_FETCH_CHUNK):
        chunk = ",".join(uids[start : start + ATTACHMENT_FETCH_CHUNK])
        try:
            status, data = box.client.uid("FETCH", chunk, "(UID BODYSTRUCTURE)")
        except imaplib.IMAP4.error:
            continue
        if status == "OK":
            found.update(attachments_from_fetch(data))
    return found


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
    crit = _criteria(query, since, unread_only)
    with open_box(account) as box:
        msgs = box.fetch(
            crit,
            limit=limit,
            reverse=True,  # newest first
            mark_seen=False,  # BODY.PEEK -- never touch \Seen
            bulk=True,
            headers_only=False,
        )
        msgs = list(msgs)
        attachments = _attachments_by_uid(box, [m.uid for m in msgs if m.uid])
        return [
            {
                **row(m, account.key),
                # None = structure not obtained (null): never read as "no attachments".
                "has_attachments": None
                if attachments.get(m.uid) is None
                else bool(attachments[m.uid]),
                "attachments": attachments.get(m.uid) or [],
            }
            for m in msgs
        ]


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
            "body": (m.text or m.html or "").strip(),
        }
