"""Bounded plain-text rendering and mailbox-scoped message identity."""

import hashlib
import json
import re
from html.parser import HTMLParser

BODY_LIMIT_BYTES = 64 * 1024
MESSAGE_LIMIT_BYTES = 5 * 1024 * 1024
_VOID = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}
_BLOCK = {"br", "p", "div", "li", "tr", "h1", "h2", "blockquote"}


def _is_hidden(tag, attrs):
    attrs = dict(attrs)
    if tag in {"head", "script", "style", "template", "noscript"} or "hidden" in attrs:
        return True
    if (attrs.get("aria-hidden") or "").strip().lower() == "true":
        return True
    style = re.sub(r"/\*.*?\*/", "", attrs.get("style") or "", flags=re.DOTALL)
    for declaration in style.split(";"):
        key, _, value = declaration.partition(":")
        key = key.strip().lower()
        value = re.sub(r"\s*!\s*important\s*$", "", value.lower()).strip()
        if (
            (key == "display" and value == "none")
            or (key == "visibility" and value in {"hidden", "collapse"})
            or (key == "mso-hide" and value == "all")
        ):
            return True
        if key in {"opacity", "font-size"} and re.fullmatch(
            r"0+(?:\.0+)?(?:px|pt|em|rem|%)?", value
        ):
            return True
    return False


class _HTMLText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.stack = []

    def handle_starttag(self, tag, attrs):
        hidden = bool(self.stack and self.stack[-1][1]) or _is_hidden(tag, attrs)
        if tag not in _VOID:
            self.stack.append((tag, hidden))
        if tag in _BLOCK and not hidden:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        hidden = bool(self.stack and self.stack[-1][1])
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break
        if tag in _BLOCK and not hidden:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.stack or not self.stack[-1][1]:
            self.parts.append(data)


def body_result(msg) -> dict:
    if msg.text:
        text = msg.text.replace("\r\n", "\n").replace("\r", "\n").strip()
    else:
        parser = _HTMLText()
        parser.feed(msg.html or "")
        parser.close()
        text = "\n".join(
            line.strip() for line in "".join(parser.parts).splitlines() if line.strip()
        )
    encoded = text.encode("utf-8")
    return {
        "body": encoded[:BODY_LIMIT_BYTES].decode("utf-8", errors="ignore"),
        "body_truncated": len(encoded) > BODY_LIMIT_BYTES,
        "body_limit_bytes": BODY_LIMIT_BYTES,
    }


def body_text(msg) -> str:
    return body_result(msg)["body"]


def message_identity(account, folder: str, uidvalidity: str, uid: str) -> dict:
    # RFC 9051 section 2.3.1.1: folder + UIDVALIDITY + UID scopes persistent UIDs.
    # Include account/server identity so equal UIDs from different accounts never collide.
    scope = [
        account.key,
        account.host.lower(),
        account.port,
        account.email,
        folder,
        uidvalidity,
        str(int(uid)),
    ]
    digest = hashlib.sha256(
        json.dumps(scope, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()
    return {"stable_id": f"imap:{digest}", "uidvalidity": uidvalidity}
