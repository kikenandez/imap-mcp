from dataclasses import replace
from types import SimpleNamespace

import pytest

from imap_mcp import accounts, messages


@pytest.mark.parametrize(
    "attribute",
    [
        "hidden",
        'hidden="false"',
        'aria-hidden="true"',
        'style="display: none"',
        'style="DISPLAY: none !important"',
        'style="visibility: hidden"',
        'style="visibility: collapse"',
        'style="opacity: 0"',
        'style="font-size: 0px"',
        'style="mso-hide: all"',
        'style="display:/**/none"',
    ],
)
def test_hidden_subtrees_removed(attribute):
    msg = SimpleNamespace(
        text="",
        html=(
            f"<p>Visible</p><div {attribute}>Hidden<b>Nested</b><br>"
            '<img src="x">Hidden</div><p>After</p>'
        ),
    )
    assert messages.body_text(msg) == "Visible\nAfter"


@pytest.mark.parametrize("tag", ["head", "script", "style", "template", "noscript"])
def test_non_content_elements_removed(tag):
    msg = SimpleNamespace(text="", html=f"<{tag}>Hidden</{tag}><p>Visible &amp; readable</p>")
    assert messages.body_text(msg) == "Visible & readable"


def test_visible_content_and_self_closing_hidden_element():
    msg = SimpleNamespace(
        text="", html='<span hidden/><span aria-hidden="false" style="opacity:1">Visible</span>'
    )
    assert messages.body_text(msg) == "Visible"


@pytest.mark.parametrize("html", [False, True])
@pytest.mark.parametrize("length", [65535, 65536, 65537])
def test_body_byte_limit(html, length):
    text = "x" * length
    msg = SimpleNamespace(text="" if html else text, html=f"<p>{text}</p>" if html else "")
    result = messages.body_result(msg)
    assert len(result["body"].encode()) == min(length, 65536)
    assert result["body_truncated"] is (length > 65536)
    assert result["body_limit_bytes"] == 65536


def test_truncation_preserves_utf8_boundaries_and_plain_text_preference():
    msg = SimpleNamespace(text="€" * 22000, html="Wrong alternative")
    result = messages.body_result(msg)
    assert result["body"] == "€" * (65536 // 3)
    assert result["body_truncated"] is True


def test_stable_id_scope(config):
    account = accounts.get_account("alpha")
    baseline = messages.message_identity(account, "INBOX", "123", "42")
    assert baseline == messages.message_identity(account, "INBOX", "123", "042")
    assert baseline["stable_id"].startswith("imap:")
    alternatives = [
        (replace(account, key="other"), "INBOX", "123", "42"),
        (replace(account, host="other.example.com"), "INBOX", "123", "42"),
        (replace(account, email="other@example.com"), "INBOX", "123", "42"),
        (account, "Sent", "123", "42"),
        (account, "INBOX", "124", "42"),
        (account, "INBOX", "123", "43"),
    ]
    assert all(
        messages.message_identity(*args)["stable_id"] != baseline["stable_id"]
        for args in alternatives
    )
