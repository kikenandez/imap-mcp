"""Stdio server entrypoint and explicit, human-run IMAP diagnostics."""

import argparse
import json

from . import accounts, imap
from .folders import folder_rows


def doctor(account_key: str | None = None, *, test_draft: bool = False) -> int:
    """Read-only diagnostics; a named account and flag are required for a test draft."""
    if test_draft and not account_key:
        raise ValueError("--test-draft requires an account so only one draft is created")
    try:
        targets = [accounts.get_account(account_key)] if account_key else accounts.all_accounts()
    except (OSError, KeyError, ValueError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False))
        return 1
    results = []
    for account in targets:
        result = {
            "account": account.key,
            "host": account.host,
            "port": account.port,
            "tls": "certificate and hostname verification required",
            "timeout_seconds": account.timeout_seconds,
            "allow_drafts": account.allow_drafts,
        }
        if not account.enabled:
            result["status"] = "disabled"
            results.append(result)
            continue
        try:
            with imap.open_box(account) as box:
                result["login"] = "ok"
                result["folders"] = folder_rows(box, account)
                # Probe actual UTF-8 support without the read tools' ASCII fallback.
                list(
                    box.fetch(
                        imap._criteria("café", None, False),
                        charset="UTF-8",
                        headers_only=True,
                        mark_seen=False,
                        limit=1,
                        bulk=50,
                    )
                )
                result["utf8_search"] = "ok (café; no ASCII fallback)"
            if test_draft:
                result["draft"] = imap.create_draft(
                    account,
                    account.email,
                    "imap-mcp test draft",
                    "This draft was created by imap-mcp doctor. "
                    "Review or remove it in your mail client.",
                )
            result["status"] = "ok"
        except Exception as exc:
            result["status"] = "error"
            result["error"] = f"{type(exc).__name__}: {exc}"
        results.append(result)
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return int(any(result["status"] == "error" for result in results))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Read IMAP mail and create opt-in drafts via MCP.")
    commands = parser.add_subparsers(dest="command")
    diagnostic = commands.add_parser("doctor", help="check IMAP login, folders and UTF-8 search")
    diagnostic.add_argument("account", nargs="?", help="account key; omitted checks all accounts")
    diagnostic.add_argument(
        "--test-draft",
        action="store_true",
        help="create one test draft on the named, opted-in account",
    )
    args = parser.parse_args(argv)
    if args.command == "doctor":
        if args.test_draft and not args.account:
            parser.error("--test-draft requires an account")
        raise SystemExit(doctor(args.account, test_draft=args.test_draft))
    from .server import mcp

    mcp.run()
