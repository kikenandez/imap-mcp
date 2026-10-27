# Upstream PR draft: attachment metadata in list_emails / search_emails

Not opened. Waiting for the fork owner's go-ahead.

## Summary

Rows returned by `list_emails` and `search_emails` now carry `has_attachments` (bool) and
`attachments` (a list of `{name, size, content_type}`), so an assistant can tell which mail
has files without downloading any message.

## How

- The list fetch stays headers-only (`BODY.PEEK[HEADER]`). After it, one extra
  `UID FETCH <uids> (UID BODYSTRUCTURE)` per 50 messages asks the server for the MIME tree.
  BODYSTRUCTURE is metadata: no part content is transferred and `\Seen` is never set. The
  mailbox is still SELECTed read-only.
- New `imap_mcp/bodystructure.py`: a small defensive parser for the response (quoted strings,
  NIL, nested lists, `{n}` literals folded in from imaplib's tuples) and the attachment rules.
- Attachment = disposition `attachment`, or a non-`text/*` part that has a filename.
  Inline images without a filename (signature pictures) do not count. `message/rfc822`
  with an attachment disposition or a filename is one attachment and is not opened.
- Names: RFC 2231 (charsets, continuations) and RFC 2047 decoded; control characters and bidi
  overrides removed; cut to 200 characters. At most 20 entries per message. `size` is the
  encoded size from the server.
- Failure policy: a malformed structure, an unknown UID, a refused (`NO`/`BAD`) or failed
  FETCH gives `has_attachments: false`, `attachments: []`. The listing itself never fails
  because of this lookup. Nesting depth and response size are bounded.

## Tests

`tests/test_bodystructure.py` (32 tests, canned responses): nested multipart, inline vs
attachment, text parts, attached message/rfc822, RFC 2231 and 2047 names, bad charsets,
truncation, the 20 cap, literals, UID ordering, malformed and hostile input.
`tests/test_imap.py` (+10): rows, one metadata-only fetch (no BODY part), chunking, empty
list, refused/failed fetch, search path, ASCII-fallback retry. The fake mailbox asserts the
only extra command is exactly `(UID BODYSTRUCTURE)`. Locked pytest and Ruff pass.

## Compatibility

Rows gain two keys; nothing is renamed or removed. One extra IMAP round trip per 50 rows.
