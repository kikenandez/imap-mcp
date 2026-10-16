"""MIME composition only; mailbox permissions and APPEND live in imap.py."""

import datetime as dt
import re
from email import policy
from email.message import EmailMessage
from email.utils import format_datetime, formataddr, getaddresses, make_msgid
from html import escape

REPLY_PREFIX = re.compile(r"^(re|aw|antw|rv|réf)\s*:", re.IGNORECASE)


def _header(value: str) -> str:
    if "\r" in value or "\n" in value:
        raise ValueError("header values must not contain newlines")
    return value


def original_header(original, name: str) -> str:
    # Unfold legitimate RFC 5322 header continuations, then reject stray newlines.
    values = original.headers.get(name, ())
    return _header(re.sub(r"\r?\n[ \t]+", " ", " ".join(values))).strip()


def _addresses(value: str | list[str] | None) -> list[str]:
    if not value:
        return []
    values = [value] if isinstance(value, str) else value
    parsed = getaddresses([_header(v) for v in values])
    if any(
        not address or "@" not in address or any(c.isspace() for c in address)
        for _, address in parsed
    ):
        raise ValueError("recipients must be email addresses")
    return [formataddr((name, address)) for name, address in parsed]


def _unique(values, seen):
    result = []
    for value in values:
        address = getaddresses([value])[0][1].casefold()
        if address not in seen:
            seen.add(address)
            result.append(value)
    return result


def build_draft(
    account,
    to,
    subject,
    body,
    cc=None,
    html_body=None,
    original=None,
    original_text="",
    reply_all=False,
):
    msg = EmailMessage(policy=policy.SMTP)
    msg["From"] = formataddr((_header(account.from_name or ""), _header(account.email)))
    msg["Date"] = format_datetime(dt.datetime.now().astimezone())
    msg["Message-ID"] = make_msgid(domain=account.email.rsplit("@", 1)[-1])
    recipients = _addresses(to)
    copies = _addresses(cc)
    subject = _header(subject)
    if original is not None:
        message_id = original_header(original, "message-id")
        if message_id:
            msg["In-Reply-To"] = message_id
            references = original_header(original, "references") or original_header(
                original, "in-reply-to"
            )
            msg["References"] = " ".join(filter(None, [references, message_id]))
        subject = subject or original.subject
        if not REPLY_PREFIX.match(subject):
            subject = f"Re: {subject}"
        if not recipients:
            recipients = _addresses(
                original_header(original, "reply-to")
                or original_header(original, "from")
                or original.from_
            )
        if reply_all:
            copies.extend(_addresses(original_header(original, "to") or list(original.to)))
            copies.extend(_addresses(original_header(original, "cc") or list(original.cc)))
        seen = {account.email.casefold()}
        recipients = _unique(recipients, seen)
        copies = _unique(copies, seen)
        quote_header = (
            f"On {original_header(original, 'date') or original.date}, "
            f"{original_header(original, 'from') or original.from_} wrote:"
        )
        quote = "\n".join(f"> {line}" for line in original_text.splitlines())
        body = f"{body.rstrip()}\n\n{quote_header}\n{quote}\n"
        if html_body is not None:
            html_body += (
                f"<br><p>{escape(quote_header)}</p>"
                f"<blockquote><pre>{escape(original_text)}</pre></blockquote>"
            )
    else:
        seen = set()
        recipients = _unique(recipients, seen)
        copies = _unique(copies, seen)
    msg["Subject"] = _header(subject)
    if recipients:
        msg["To"] = ", ".join(recipients)
    if copies:
        msg["Cc"] = ", ".join(copies)
    msg.set_content(body, charset="utf-8")
    if html_body is not None:
        msg.add_alternative(html_body, subtype="html", charset="utf-8")
    return msg, recipients, copies


def append_uid(response) -> str | None:
    if isinstance(response, (list, tuple)):
        for item in response:
            if uid := append_uid(item):
                return uid
        return None
    text = response.decode("ascii", "replace") if isinstance(response, bytes) else str(response)
    match = re.search(r"\bAPPENDUID\s+\d+\s+(\d+)\b", text, re.IGNORECASE)
    return match[1] if match else None
