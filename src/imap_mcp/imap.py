"""Read-only IMAP access and opt-in draft APPEND, built on imap-tools.

Header fetches use mark_seen=False; bounded body fetches explicitly use BODY.PEEK.
Reading a message never sets the \\Seen flag in the real mailbox.
"""

from __future__ import annotations

import datetime as dt
import imaplib
import re
import ssl
import unicodedata
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from email import policy

from imap_tools import AND, OR, MailBox, MailMessage, MailMessageFlags
from imap_tools.errors import MailboxFetchError, MailboxUidsError

from .accounts import Account
from .credentials import MissingCredentialError
from .drafts import append_uid, build_draft
from .folders import folder_rows, resolve_folder
from .messages import MESSAGE_LIMIT_BYTES, body_result, body_text, message_identity


@contextmanager
def open_box(account: Account, folder: str | None = None) -> Iterator[MailBox]:
    """Yield a logged-in, read-only mailbox for the account. Raises on failure."""
    if not account.enabled:
        raise ValueError(f"account {account.key!r} is disabled")
    pw = account.password()
    if not pw:
        raise MissingCredentialError(f"no-credential: {account.credential_ref}")
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
        # imaplib debug output and its rolling command log can contain LOGIN credentials.
        box.client.debug = 0
        authenticated = False
        try:
            box.login(account.email, pw, initial_folder=None)
            authenticated = True
        except Exception:
            pass  # Authentication responses may echo credentials; never propagate them.
        finally:
            del pw
            if hasattr(box.client, "_cmd_log"):
                box.client._cmd_log.clear()
        if not authenticated:
            raise RuntimeError(f"IMAP login failed ({account.credential_ref})")
        box.folder.set(resolve_folder(box, account, folder), readonly=True)
        yield box
    finally:
        with suppress(Exception):
            box.logout()


def _snippet(msg, length: int = 200) -> str:
    return " ".join(body_text(msg).split())[:length]


def row(msg, account_key: str) -> dict:
    """Compact metadata row for list/search results."""
    return {
        "account": account_key,
        "id": msg.uid,
        "message_id": msg.headers.get("message-id", [None])[0],
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


def _uidvalidity(box) -> str:
    _, values = box.client.response("UIDVALIDITY")
    value = values[0] if values else None
    if isinstance(value, bytes):
        value = value.decode("ascii", errors="replace")
    if not isinstance(value, str) or not value.isascii() or not value.isdecimal() or int(value) < 1:
        raise RuntimeError("server did not provide a valid UIDVALIDITY")
    return str(int(value))


def _validate_uid(uid: str) -> str:
    if (
        not isinstance(uid, str)
        or not uid.isascii()
        or not uid.isdecimal()
        or not 0 < int(uid) < 2**32
    ):
        raise ValueError("uid must be a positive numeric IMAP UID (32-bit)")
    return str(int(uid))


def _fetch_message(box, uid: str):
    """Bound the wire fetch before MIME decoding, including attachments."""
    uid = _validate_uid(uid)
    result = box.client.uid(
        "FETCH", uid, f"(UID FLAGS RFC822.SIZE BODY.PEEK[]<0.{MESSAGE_LIMIT_BYTES}>)"
    )
    if result[0] != "OK":
        raise MailboxFetchError(result, "OK")
    data = result[1] or []
    literals = [item for item in data if isinstance(item, tuple)]
    if not literals:
        return None
    if (
        len(literals) != 1
        or len(literals[0]) != 2
        or not isinstance(literals[0][0], bytes)
        or not isinstance(literals[0][1], bytes)
    ):
        raise RuntimeError("unexpected single-message FETCH response")
    raw = literals[0][1]
    if len(raw) > MESSAGE_LIMIT_BYTES:
        raise ValueError("message exceeds the 5 MiB download limit")
    metadata = b" ".join(
        item[0] if isinstance(item, tuple) else item
        for item in data
        if isinstance(item, (bytes, tuple))
    )
    size_match = re.search(rb"\bRFC822\.SIZE\s+(\d+)\b", metadata)
    if size_match is None:
        raise RuntimeError("missing RFC822.SIZE; refusing partial MIME")
    size = int(size_match[1])
    if size > MESSAGE_LIMIT_BYTES:
        raise ValueError("message exceeds the 5 MiB download limit")
    if size != len(raw):
        raise RuntimeError("incomplete message; refusing partial MIME")
    message = MailMessage(data)
    if message.uid != uid:
        raise RuntimeError("server returned a different message UID")
    return message


def fetch_rows(
    account: Account,
    *,
    query: str | None = None,
    since: dt.date | None = None,
    unread_only: bool = False,
    limit: int = 25,
    folder: str | None = None,
) -> list[dict]:
    if limit < 1:
        raise ValueError("limit must be positive")
    limit = min(limit, 100)
    with open_box(account, folder) as box:
        validity = _uidvalidity(box)

        def fetch(text, charset):
            return [
                {
                    **row(m, account.key),
                    "folder": box.folder.get(),
                    **message_identity(account, box.folder.get(), validity, m.uid),
                }
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
            folded = "".join(
                char
                for char in unicodedata.normalize("NFD", query or "")
                if not unicodedata.combining(char)
            )
            # Retry only if removing accents produces a nonempty ASCII query.
            # Never hide the original error or drop other Unicode characters.
            if not query or folded == query or not folded.isascii() or not folded.strip():
                raise
            return fetch(folded, "US-ASCII")


def fetch_one(account: Account, uid: str, folder: str | None = None) -> dict | None:
    """Bounded message body by UID in a folder (default INBOX); None if not found."""
    uid = _validate_uid(uid)
    with open_box(account, folder) as box:
        validity = _uidvalidity(box)
        m = _fetch_message(box, uid)
        if m is None:
            return None
        return {
            "account": account.key,
            "id": m.uid,
            "folder": box.folder.get(),
            "from": m.from_,
            "to": list(m.to),
            "cc": list(m.cc),
            "subject": m.subject,
            "date": m.date.isoformat() if m.date else None,
            "unread": "\\Seen" not in m.flags,
            "message_id": m.headers.get("message-id", [None])[0],
            **message_identity(account, box.folder.get(), validity, m.uid),
            **body_result(m),
        }


def list_folders(account: Account) -> list[dict]:
    with open_box(account) as box:
        return folder_rows(box, account)


def create_draft(
    account: Account,
    to: str | list[str],
    subject: str,
    body: str,
    cc: str | list[str] | None = None,
    html_body: str | None = None,
    reply_to_uid: str | None = None,
    reply_folder: str | None = None,
    reply_all: bool = False,
) -> dict:
    """APPEND a draft only when opted in. Never send or alter existing messages."""
    if not account.allow_drafts:
        raise PermissionError(
            f"draft creation is disabled for {account.key}; set allow_drafts = true"
        )
    if (reply_folder is not None or reply_all) and not reply_to_uid:
        raise ValueError("reply_folder and reply_all require reply_to_uid")
    if reply_to_uid is not None:
        reply_to_uid = _validate_uid(reply_to_uid)
    with open_box(account) as box:
        folder = resolve_folder(box, account, "drafts")
        original = None
        original_text = ""
        if reply_to_uid:
            source_folder = resolve_folder(box, account, reply_folder)
            box.folder.set(source_folder, readonly=True)
            original = next(
                box.fetch(
                    AND(uid=reply_to_uid), headers_only=True, mark_seen=False, limit=1, bulk=False
                ),
                None,
            )
            if original is None:
                raise ValueError(f"message {reply_to_uid} not found in {source_folder}")
            # Headers alone cannot supply the quoted body. Read it with BODY.PEEK.
            full = _fetch_message(box, reply_to_uid)
            if full is None:
                raise ValueError(f"message {reply_to_uid} disappeared from {source_folder}")
            rendered = body_result(full)
            original_text = rendered["body"]
            if rendered["body_truncated"]:
                original_text += "\n[Original message truncated]"
            box.folder.set("INBOX", readonly=True)
        message, recipients, copies = build_draft(
            account, to, subject, body, cc, html_body, original, original_text, reply_all
        )
        result = box.append(
            message.as_bytes(policy=policy.SMTP),
            folder,
            flag_set=[MailMessageFlags.DRAFT, MailMessageFlags.SEEN],
        )
        return {
            "account": account.key,
            "folder": folder,
            "uid": append_uid(result),
            "message_id": str(message["Message-ID"]),
            "subject": str(message["Subject"]),
            "to": recipients,
            "cc": copies,
        }
