"""Attachment metadata from IMAP BODYSTRUCTURE, never from message content.

BODYSTRUCTURE (RFC 3501 section 7.4.2) describes the MIME tree without transferring any part.
Fetching it does not set \\Seen. Everything here is defensive: the structure comes from a mail
server and describes mail written by strangers, so a malformed or hostile response yields no
attachments rather than an exception, and every size is bounded.
"""

from __future__ import annotations

import re
import unicodedata
from email import utils as email_utils
from email.header import decode_header, make_header

MAX_ATTACHMENTS = 20
MAX_NAME_CHARS = 200
MAX_TYPE_CHARS = 100
MAX_DEPTH = 32
MAX_RESPONSE_CHARS = 1024 * 1024
_BIDI = {*range(0x202A, 0x202F), *range(0x2066, 0x206A)}
_TOKEN = re.compile(r'\s*(?:(\()|(\))|"((?:[^"\\]|\\.)*)"|([^\s()"]+))', re.DOTALL)
_RESPONSE_START = re.compile(r"\s*\d+\s+\(")
_UID = re.compile(r"\bUID\s+(\d+)")


def parse_sexp(text: str | bytes):
    """Parse one parenthesised IMAP value. NIL becomes None. Raises ValueError if malformed."""
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")
    if len(text) > MAX_RESPONSE_CHARS:
        raise ValueError("structure too large")
    stack: list[list] = []
    root = None
    done = False
    pos = 0
    while True:
        match = _TOKEN.match(text, pos)
        if match is None:
            if text[pos:].strip():
                raise ValueError("unparseable structure")
            break
        pos = match.end()
        if done:
            break
        open_, close, quoted, atom = match.groups()
        if open_:
            if len(stack) >= MAX_DEPTH:
                raise ValueError("structure nested too deeply")
            stack.append([])
            continue
        if close:
            if not stack:
                raise ValueError("unbalanced parenthesis")
            value = stack.pop()
        elif quoted is not None:
            value = re.sub(r"\\(.)", r"\1", quoted, flags=re.DOTALL)
        else:
            value = None if atom.upper() == "NIL" else atom
        if stack:
            stack[-1].append(value)
        else:
            root, done = value, True
    if stack or not done:
        raise ValueError("incomplete structure")
    return root


def _clean(text: str, limit: int) -> str:
    kept = "".join(ch for ch in text if unicodedata.category(ch) != "Cc" and ord(ch) not in _BIDI)
    return kept.strip()[:limit]


def _params(raw) -> dict[str, str]:
    """Parameter list to {name: text}, decoding RFC 2231 continuations/charsets and RFC 2047."""
    if not isinstance(raw, list):
        return {}
    pairs = [
        (raw[i].lower(), raw[i + 1])
        for i in range(0, len(raw) - 1, 2)
        if isinstance(raw[i], str) and isinstance(raw[i + 1], str)
    ]
    try:
        decoded = email_utils.decode_params([("content-type", "x/y"), *pairs])[1:]
        values = {
            name.lower(): email_utils.unquote(email_utils.collapse_rfc2231_value(value))
            for name, value in decoded
        }
    except Exception:  # noqa: BLE001 - hostile input; fall back to the raw values
        values = dict(pairs)
    return {name: _decode_words(value) for name, value in values.items() if isinstance(value, str)}


def _decode_words(value: str) -> str:
    if "=?" not in value:
        return value
    try:
        return str(make_header(decode_header(value)))
    except Exception:  # noqa: BLE001 - unknown charset or bad encoding: keep the raw text
        return value


def _disposition(raw) -> tuple[str, dict[str, str]]:
    if isinstance(raw, list) and raw and isinstance(raw[0], str):
        return raw[0].lower(), _params(raw[1] if len(raw) > 1 else None)
    return "", {}


def _as_int(value) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _walk(part, found: list[dict], depth: int) -> None:
    if len(found) >= MAX_ATTACHMENTS:
        return
    if depth > MAX_DEPTH or not isinstance(part, list) or not part:
        raise ValueError("malformed body structure")
    if isinstance(part[0], list):  # multipart: child parts first, then subtype and extensions
        for child in part:
            if not isinstance(child, list):
                break
            _walk(child, found, depth + 1)
        return
    if (
        len(part) < 7
        or not isinstance(part[0], str)
        or not isinstance(part[1], str)
        or not (part[2] is None or isinstance(part[2], list))
        or not (isinstance(part[6], str) and part[6].isdecimal())
    ):
        raise ValueError("malformed body part")
    kind, subtype = part[0].lower(), part[1].lower()
    is_message = kind == "message" and subtype == "rfc822"
    # Extension data follows the basic fields: text adds a line count, message/rfc822 adds
    # envelope + body + line count; then md5, then the disposition.
    disposition_at = 9 if kind == "text" else 11 if is_message else 8
    dtype, dparams = _disposition(part[disposition_at] if len(part) > disposition_at else None)
    name = dparams.get("filename") or _params(part[2]).get("name") or ""
    if dtype == "attachment" or (kind != "text" and name):
        found.append(
            {
                "name": _clean(name, MAX_NAME_CHARS),
                "size": _as_int(part[6]),
                "content_type": _clean(f"{kind}/{subtype}", MAX_TYPE_CHARS),
            }
        )
    elif is_message and len(part) > 8:
        _walk(part[8], found, depth + 1)


def summarize(structure) -> list[dict] | None:
    """Attachments in a parsed BODYSTRUCTURE: name, encoded size in bytes, content type.

    An attachment is a part whose disposition is `attachment`, or a non-text part that has a
    filename. Inline images without a filename (signatures, body pictures) are not attachments.
    Returns None when the structure is malformed: unknown is not the same as "no attachments".
    """
    found: list[dict] = []
    try:
        _walk(structure, found, 0)
    except (RecursionError, ValueError, TypeError, IndexError):
        return None
    return found[:MAX_ATTACHMENTS]


def _quote(raw: bytes) -> str:
    text = raw.decode("utf-8", errors="replace").replace("\r", " ").replace("\n", " ")
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _responses(data) -> list[str]:
    """Rebuild each FETCH response from imaplib's list, folding {n} literals into quoted strings."""
    responses: list[str] = []
    after_literal = False
    for item in data if isinstance(data, list) else []:
        if isinstance(item, tuple) and len(item) == 2 and all(isinstance(x, bytes) for x in item):
            head = re.sub(rb"\{\d+\}\s*$", b"", item[0]).decode("utf-8", errors="replace")
            piece, literal = head + _quote(item[1]), True
        elif isinstance(item, bytes):
            piece, literal = item.decode("utf-8", errors="replace"), False
        else:
            after_literal = False
            continue
        if not after_literal and _RESPONSE_START.match(piece):
            responses.append(piece)
        elif after_literal and responses:
            responses[-1] += piece
        after_literal = literal
    return responses


def attachments_from_fetch(data) -> dict[str, list[dict] | None]:
    """Map UID to its attachments from a `UID FETCH (UID BODYSTRUCTURE)` response.

    A structure that is missing or cannot be read maps its UID (when readable) to None: unknown.
    An empty list means the structure was read and has no attachments.
    """
    result: dict[str, list[dict] | None] = {}
    for text in _responses(data):
        try:
            items = parse_sexp(text[_RESPONSE_START.match(text).end() - 1 :])
        except ValueError:
            head = text.split("BODYSTRUCTURE", 1)[0]
            uid = _UID.search(head)
            if uid:
                result[str(int(uid[1]))] = None
            continue
        uid = structure = None
        for i in range(0, len(items) - 1, 2) if isinstance(items, list) else ():
            key = items[i].upper() if isinstance(items[i], str) else ""
            if key == "UID" and isinstance(items[i + 1], str):
                uid = items[i + 1]
            elif key == "BODYSTRUCTURE":
                structure = items[i + 1]
        if uid and uid.isdecimal():
            result[str(int(uid))] = summarize(structure)
    return result
