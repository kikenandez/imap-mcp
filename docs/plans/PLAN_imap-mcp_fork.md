# Fork plan — imap-mcp (from LeeFlannery/imap-mcp)

Instructions for the coding agent. Work phase by phase, one PR per phase, CI green before moving on.
Upstream: https://github.com/LeeFlannery/imap-mcp (MIT, Python ≥3.12, uv, imap-tools 1.14.0, mcp 2.0.0, ~300 LOC, 31 tests).
Fork: `kikenandez/imap-mcp`. Keep the upstream MIT LICENSE and copyright line.

## Target
A local stdio MCP server that reads mail across several IMAP accounts (AOL, Yahoo, OVH MX Plan, OVH Zimbra), can create **drafts only** on accounts that opt in, never sends, never deletes, never changes flags on stored messages. Microsoft 365 via OAuth2 later (phase 4).

## Ground rules
- Every change keeps `uv run --locked pytest`, `ruff check`, `ruff format --check` green. Add tests for every behaviour you add or fix. No real network in tests: extend the existing fakes (`tests/test_imap.py` `FakeMailBox`, `tests/test_server.py` monkeypatches).
- No secrets in the repo or in tests. `accounts.toml` stays gitignored.
- Read path must never mutate a mailbox. Write path = `APPEND` to the Drafts folder only.
- Conventional Commits. Update README for each user-visible change.

---

## Phase 0 — Security and robustness fixes (do first)

Audited issues in upstream, file refs are upstream `src/imap_mcp/`:

1. **TLS certificates not verified** — `imap.py:27` `MailBox(host, port=port)` → imaplib falls back to an unverified context (CERT_NONE, no hostname check).
   Fix: `MailBox(host, port=port, ssl_context=ssl.create_default_context(), timeout=30)`. Add optional per-account `tls_verify = true` (default true) only if strictly needed; default must remain verified. Test that `MailBox` receives a verifying context.
2. **No socket timeout** — add `timeout` (default 30 s, per-account override `timeout_seconds`). `list_accounts` logs in serially; run the per-account work with a small `ThreadPoolExecutor` so one dead host doesn't stall the rest. Same for `list_emails`/`search_emails` merges.
3. **Non-ASCII search crashes** (`UnicodeEncodeError`, e.g. "café") — pass `charset="UTF-8"` to `box.fetch(...)` in `fetch_rows`. If a server rejects UTF-8 SEARCH, catch and retry with ASCII-folded query; return a per-account error row rather than raising.
4. **Read path uses SELECT** (`initial_folder="INBOX"` → read-write select). Switch to `login(..., initial_folder=None)` then `box.folder.set(folder, readonly=True)` (EXAMINE). Update `test_open_box_logs_in_and_out`.
5. **List rows fetch full RFC822 bodies** (`fetch_rows`, `bulk=True`, `headers_only=False`). Use `headers_only=True` for list/search rows (snippet may become empty — acceptable, or fetch `BODY.PEEK[TEXT]<0.2048>` partial if cheap), `bulk=50` chunks, and cap `limit` at 100.
6. **Wrong cross-account ordering** — server.py:74/99 sorts ISO strings with mixed offsets. Sort on `datetime` converted to UTC.
7. **HTML not converted** — when no text/plain part, strip HTML to text (stdlib `html.parser` based converter, no heavy deps). Update `test_snippet_falls_back_to_html`.
8. Mark existing tools `annotations=ToolAnnotations(readOnlyHint=True)`.

## Phase 1 — Multi-folder read

- Add optional `folder: str | None = None` to `list_emails`, `search_emails`, `get_email` (default INBOX).
- Add tool `list_folders(account)` → name, delimiter, flags, resolved role (`inbox|sent|drafts|junk|trash|archive|None`).
- Role resolution: port `folder_role()` + `NAME_PATTERNS` + `_fold` from bgalmes/rubit-mcp-mail `src/rubit_mcp_mail/backends/imap.py` (MIT, keep attribution in a header comment). Order: SPECIAL-USE flag (`\Drafts`, `\Sent`, …, case-insensitive) → delimiter-aware leaf-name match (covers `INBOX.Drafts`, `INBOX/Drafts`) → per-account override in TOML (`[account.folders] drafts = "..."`).
- Accept `folder` as a role (`"drafts"`) or raw name.
- Fix `get_email` error text ("not found in INBOX" → folder-aware).

## Phase 2 — Opt-in `create_draft`

Config (`accounts.py`): add `allow_drafts: bool = False`, `from_name: str | None = None`, optional folder overrides. Document in `accounts.example.toml`.

Tool `create_draft(account, to, subject, body, cc=None, html_body=None, reply_to_uid=None, reply_folder=None, reply_all=False)`:
- **Guard in the IMAP layer**, not only the tool: `imap.create_draft()` raises/returns error unless `account.allow_drafts`. Test: non-opt-in account → `append` never called.
- Build MIME with `email.message.EmailMessage`:
  - `From` = `formataddr((from_name, email))`; `Date` = `email.utils.format_datetime(now local tz)`; `Message-ID` = `make_msgid(domain=<email domain>)`.
  - `set_content(body)` (UTF-8); if `html_body`, `add_alternative(html_body, subtype="html")`.
  - Serialize `msg.as_bytes(policy=email.policy.SMTP)` (CRLF).
- Reply mode (`reply_to_uid` set): fetch original headers with `headers_only=True` (`message-id`, `references`, `in-reply-to`, `reply-to`, `from`, `to`, `cc`, `subject`, `date`):
  - `In-Reply-To` = original Message-ID; `References` = (orig References or orig In-Reply-To) + " " + orig Message-ID.
  - Subject: keep if it already matches `^(re|aw|antw|rv|réf)\s*:` (case-insensitive), else prefix `Re: `.
  - If `to` empty: Reply-To else From; `reply_all` adds original To+Cc minus own address.
  - Quote body of the original mail below with `> ` and a header line `On <date>, <from> wrote:`.
  - Logic reference (TypeScript, read only): unikinCK/mcp-imap `src/reply.ts`, `src/tools/write.ts:160-180, 488-517, 612-732`.
- Drafts folder: role `drafts` via phase-1 resolver; fallback names `Drafts, Draft, Brouillons, Borradores, Entwürfe, Bozze`. If none found → clear error. **Never create folders.**
- `box.append(raw_bytes, folder, flag_set=[MailMessageFlags.DRAFT, MailMessageFlags.SEEN])`. INBOX stays EXAMINEd; APPEND doesn't need a selected folder. Parse `APPENDUID <validity> <uid>` from the response when present.
- Return `{account, folder, uid|None, message_id, subject, to, cc}`.
- Annotations: `readOnlyHint=False, destructiveHint=False, idempotentHint=False`.
- Register the tool always, but description must say it only writes drafts and only on opt-in accounts. Update `test_all_tools_registered`.
- Tests: MIME headers present and well-formed, UTF-8 subject/body (accents), reply threading headers, subject prefixing, reply-all excludes own address, folder fallback by name and by `\Drafts` flag, `INBOX.Drafts` with "." delimiter, error when no Drafts folder, flags passed to append.
- Prompt-injection note in README: mail content is untrusted; the server never sends, the human reviews drafts in their mail client.

## Phase 3 — Provider docs + manual test script

- README cheat sheet rows: AOL `imap.aol.com:993` (app password), Yahoo `imap.mail.yahoo.com:993` (app password, Drafts folder may be named `Draft`), OVH MX Plan (host from OVH manager, often `ssl0.ovh.net`; dovecot may use `INBOX.` prefix), OVH Zimbra (host from OVH manager).
- Add `imap-mcp doctor [account]` CLI: login, list folders with resolved roles, run a UTF-8 search, print TLS/timeout status. No writes unless `--test-draft` is passed (then creates one draft "imap-mcp test draft" and prints its folder/uid).
- Mark unverified provider facts as such in docs.

## Phase 4 — Microsoft 365 / Outlook (later)

- Config: `auth = "password" | "oauth_microsoft"` (default password), `client_id` (optional; fallback Thunderbird public client `9e5f94bc-e8a4-4e73-b8be-63364c29d753`, document the caveat), host default `outlook.office365.com:993`.
- Port from rubit-mcp-mail (MIT, keep attribution): `auth/oauth_microsoft.py` (MSAL device-code, silent refresh, `SerializableTokenCache`), `secrets.py` (keyring with 0600 file fallback), constants from `providers.py` (authority `https://login.microsoftonline.com/common`, scope `https://outlook.office.com/IMAP.AccessAsUser.All`).
- In `open_box`: branch to `box.xoauth2(email, access_token, initial_folder=None)`.
- CLI `imap-mcp auth <account>` runs the device-code flow (the stdio server can't prompt).
- Deps: `msal`, `keyring` as an optional extra `[m365]`.
- README: token cache lives in OS keyring / 0600 file (this relaxes "no secrets on disk" for OAuth accounts only); tenant may need "Allow public client flows" and IMAP enabled.
- Tests: fake MSAL app + fake `xoauth2`, no network.

## Definition of done (per phase)
- CI green on 3.12 and 3.13.
- New/changed behaviour covered by tests.
- README updated.
- PR description lists what changed, what is untested against real servers.
