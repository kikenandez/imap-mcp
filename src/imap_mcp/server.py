"""Multi-account email MCP server with read tools and opt-in draft creation."""

from __future__ import annotations

import datetime as dt
from concurrent.futures import ThreadPoolExecutor

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from . import accounts as acct
from . import imap
from .credentials import MissingCredentialError

mcp = MCPServer("imap-mcp", version="0.3.0")


def _parse_since(since: str | None) -> dt.date | None:
    if not since:
        return None
    return dt.date.fromisoformat(since)  # raises ValueError on bad input


def _targets(account: str | None) -> list[acct.Account]:
    if account:
        a = acct.get_account(account)
        return [a] if a.enabled else []
    return acct.enabled_accounts()


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def list_accounts() -> list[dict]:
    """List configured mail accounts and whether each is reachable right now.

    Performs a live IMAP login per enabled account. Returns status so the caller
    knows which mailboxes are queryable.
    """

    def status(a):
        entry = {
            "account": a.key,
            "label": a.label,
            "email": a.email,
            "enabled": a.enabled,
            "allow_drafts": a.allow_drafts,
        }
        if not a.enabled:
            entry["status"] = "disabled"
        else:
            try:
                with imap.open_box(a):
                    entry["status"] = "ok"
            except MissingCredentialError:
                entry["status"] = "no-credential"
            except Exception as e:
                entry["status"] = f"unreachable: {type(e).__name__}"
        return entry

    with ThreadPoolExecutor(max_workers=4) as pool:
        return list(pool.map(status, acct.all_accounts()))


def _date_key(row):
    value = row.get("date")
    if not value:
        return dt.datetime.min.replace(tzinfo=dt.UTC)
    date = dt.datetime.fromisoformat(value)
    return date.replace(tzinfo=dt.UTC) if date.tzinfo is None else date.astimezone(dt.UTC)


def _rows(account, **kwargs):
    def fetch(a):
        try:
            return imap.fetch_rows(a, **kwargs)
        except Exception as e:
            return [{"account": a.key, "error": f"{type(e).__name__}: {e}"}]

    with ThreadPoolExecutor(max_workers=4) as pool:
        rows = [row for group in pool.map(fetch, _targets(account)) for row in group]
    return sorted(rows, key=_date_key, reverse=True)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def list_emails(
    account: str | None = None,
    since: str | None = None,
    unread_only: bool = False,
    limit: int = 25,
    folder: str | None = None,
) -> list[dict]:
    """List recent emails, newest first.

    account: account key (see list_accounts). Omit to merge ALL enabled accounts.
    since: ISO date (YYYY-MM-DD) lower bound, optional.
    unread_only: only unseen messages.
    limit: max rows, capped at 100 per account.
    folder: role or raw folder name; defaults to INBOX.

    Each row has has_attachments and attachments (name, size, content_type; at most 20),
    read from the message structure without downloading content. has_attachments is null when
    the structure could not be obtained (unknown, not 'none').
    """
    since_d = _parse_since(since)
    return _rows(account, since=since_d, unread_only=unread_only, limit=limit, folder=folder)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def search_emails(
    query: str,
    account: str | None = None,
    since: str | None = None,
    limit: int = 25,
    folder: str | None = None,
) -> list[dict]:
    """Search emails across from/subject/body text, newest first.

    query: free text; matched against sender, subject, and body (IMAP OR).
    account: omit to search ALL enabled accounts.
    since: ISO date (YYYY-MM-DD) lower bound, optional.
    limit: max rows, capped at 100 per account.
    folder: role or raw folder name; defaults to INBOX.

    Each row has has_attachments and attachments (name, size, content_type; at most 20),
    read from the message structure without downloading content. has_attachments is null when
    the structure could not be obtained (unknown, not 'none').
    """
    since_d = _parse_since(since)
    return _rows(account, query=query, since=since_d, limit=limit, folder=folder)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def get_email(account: str, id: str, folder: str | None = None) -> dict:
    """Fetch one email with a bounded plain-text body by account + numeric IMAP UID.

    id is the value returned in list_emails/search_emails rows (IMAP UID).
    Bodies are capped at 64 KiB; body_truncated reports truncation.
    Messages larger than 5 MiB are rejected. Results include stable_id and uidvalidity.
    Reading does not mark the message as read.
    """
    a = acct.get_account(account)
    msg = imap.fetch_one(a, id, folder=folder)
    if msg is None:
        return {"account": account, "id": id, "error": f"not found in {folder or 'INBOX'}"}
    return msg


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def list_folders(account: str) -> list[dict]:
    """List folder names, hierarchy delimiters, flags and resolved roles for an account."""
    return imap.list_folders(acct.get_account(account))


@mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False)
)
def create_draft(
    account: str,
    to: str | list[str],
    subject: str,
    body: str,
    cc: str | list[str] | None = None,
    html_body: str | None = None,
    reply_to_uid: str | None = None,
    reply_folder: str | None = None,
    reply_all: bool = False,
) -> dict:
    """Create a draft only on accounts with allow_drafts=true; never sends mail.

    Writes only to the resolved Drafts folder. The human reviews it in their mail
    client. For replies pass reply_to_uid and its reply_folder (default INBOX).
    Empty to derives recipients; empty subject uses the original subject.
    reply_all adds original To/Cc recipients excluding your own email address.
    Each call creates a new draft; do not automatically retry uncertain failures.
    """
    return imap.create_draft(
        acct.get_account(account),
        to,
        subject,
        body,
        cc=cc,
        html_body=html_body,
        reply_to_uid=reply_to_uid,
        reply_folder=reply_folder,
        reply_all=reply_all,
    )


def main() -> None:
    from .cli import main as run_cli

    run_cli()
