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


def _style(attrs):
    raw = re.sub(r"/\*.*?\*/", "", dict(attrs).get("style") or "", flags=re.DOTALL)
    style = {}
    important = set()
    for declaration in raw.split(";"):
        key, _, value = declaration.partition(":")
        key, value = key.strip().lower(), value.strip().lower()
        # For this limited renderer both background declarations set the same color slot.
        if key == "background-color":
            key = "background"
        priority = bool(re.search(r"!\s*important\s*$", value))
        if key not in important or priority:
            style[key] = re.sub(r"\s*!\s*important\s*$", "", value).strip()
        if priority:
            important.add(key)
    return style


def _color(value):
    """Normalize common inline sRGB colors; unknown CSS stays unknown."""
    value = value.strip().lower()
    names = {
        "white": "#ffffff",
        "black": "#000000",
        "red": "#ff0000",
        "green": "#008000",
        "blue": "#0000ff",
        "yellow": "#ffff00",
        "aqua": "#00ffff",
        "cyan": "#00ffff",
        "fuchsia": "#ff00ff",
        "magenta": "#ff00ff",
        "gray": "#808080",
        "grey": "#808080",
        "silver": "#c0c0c0",
        "maroon": "#800000",
        "purple": "#800080",
        "lime": "#00ff00",
        "olive": "#808000",
        "navy": "#000080",
        "teal": "#008080",
        "transparent": "#00000000",
    }
    value = names.get(value, value)
    if re.fullmatch(r"#[0-9a-f]{3,4}|#[0-9a-f]{6}(?:[0-9a-f]{2})?", value):
        digits = value[1:]
        if len(digits) in {3, 4}:
            digits = "".join(c * 2 for c in digits)
        channels = tuple(int(digits[i : i + 2], 16) / 255 for i in range(0, len(digits), 2))
        return channels if len(channels) == 4 else (*channels, 1.0)
    match = re.fullmatch(r"rgba?\(([^()]*)\)", value)
    if match:
        parts = re.split(r"\s*[,/]\s*|\s+", match[1].strip())
        if len(parts) in {3, 4}:
            try:
                channels = [
                    float(c.rstrip("%")) / (100 if c.endswith("%") else 255) for c in parts[:3]
                ]
                alpha = parts[3] if len(parts) == 4 else "1"
                channels.append(float(alpha.rstrip("%")) / (100 if alpha.endswith("%") else 1))
                if all(0 <= c <= 1 for c in channels):
                    return tuple(channels)
            except ValueError:
                pass
    return None


def _background(style, inherited):
    background = inherited
    # Declaration order respects the shorthand resetting background-color.
    for key, value in style.items():
        if key in {"background", "background-color"}:
            color = _color(value)
            # Unknown images/gradients do not imply a solid color.
            background = inherited if color and color[3] == 0 else color
    return background


def _zero(value):
    return bool(re.fullmatch(r"[+-]?0+(?:\.0+)?(?:px|pt|em|rem|%)?", value))


def _offscreen(value):
    match = re.fullmatch(r"(-\d+(?:\.\d+)?)px", value)
    return bool(match and float(match[1]) <= -1000)


def _collapsed_clip(style):
    rect = re.fullmatch(r"rect\(([^()]*)\)", style.get("clip", ""))
    if rect:
        sides = re.split(r"\s*,\s*|\s+", rect[1].strip())
        if len(sides) == 4:
            sides = ["0" if _zero(side) else side for side in sides]
            if sides[0] == sides[2] != "auto" or sides[1] == sides[3] != "auto":
                return True
    clip = style.get("clip-path", "")
    shape = re.fullmatch(r"(circle|ellipse)\((.*?)\)", clip)
    if shape:
        radii = shape[2].split(" at ")[0].split()
        if len(radii) == (1 if shape[1] == "circle" else 2) and any(map(_zero, radii)):
            return True
    inset = re.fullmatch(r"inset\(([^()]*)\)", clip)
    if inset:
        sides = inset[1].split(" round ")[0].split()
        if 1 <= len(sides) <= 4 and all(
            re.fullmatch(r"[+-]?\d+(?:\.\d+)?%", s) or _zero(s) for s in sides
        ):
            values = [float(s.rstrip("%")) for s in sides]
            top, right, bottom, left = (
                values * 4
                if len(values) == 1
                else values * 2
                if len(values) == 2
                else [*values, values[1]]
                if len(values) == 3
                else values
            )
            return top + bottom >= 100 or left + right >= 100
    return False


def _is_hidden(tag, attrs, style, background):
    attrs = dict(attrs)
    if tag in {"head", "script", "style", "template", "noscript"} or "hidden" in attrs:
        return True
    if (attrs.get("aria-hidden") or "").strip().lower() == "true":
        return True
    if (
        style.get("display") == "none"
        or style.get("visibility") in {"hidden", "collapse"}
        or style.get("mso-hide") == "all"
    ):
        return True
    if any(_zero(style.get(key, "")) for key in ("opacity", "font-size")):
        return True
    foreground = _color(style.get("color", ""))
    if foreground and (foreground[3] == 0 or foreground == background):
        return True
    if style.get("overflow") == "hidden" and any(
        _zero(style.get(key, "")) for key in ("height", "max-height", "width")
    ):
        return True
    if _collapsed_clip(style) or _offscreen(style.get("text-indent", "")):
        return True
    return style.get("position") == "absolute" and any(
        _offscreen(style.get(key, "")) for key in ("left", "top")
    )


class _HTMLText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.stack = []

    def handle_starttag(self, tag, attrs):
        style = _style(attrs)
        background = _background(style, self.stack[-1][2] if self.stack else None)
        hidden = bool(self.stack and self.stack[-1][1]) or _is_hidden(tag, attrs, style, background)
        if tag not in _VOID:
            self.stack.append((tag, hidden, background))
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
