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


@pytest.mark.parametrize(
    "style",
    [
        "color:white;background:white",
        "color:yellow;background:#ffff00",
        "color:#fff;background:white!important;background-color:black",
        "color:#fff;background:white;background-color:black;background:white",
        "color: #FFF; background-color: #ffffff",
        "color: WHITE; background: rgb(255, 255, 255)",
        "color:rgba(255,255,255,1);background:#fff",
        "color:rgb(100% 100% 100% / 100%);background:#fff",
        "color:transparent",
        "color:rgba(50,100,200,0)",
        "color:rgb(50 100 200 / 0%)",
        "color:#ffffff00",
        "color:#fff0",
        "max-height:0;overflow:hidden",
        "height:0px;overflow:hidden",
        "width:0;overflow:hidden",
        "position:absolute;clip:rect(0,0,0,0)",
        "position:absolute;clip:rect(1px 20px 1px 0)",
        "clip-path:inset(50%)",
        "clip-path:inset(0 0 100% 0)",
        "clip-path:circle(0px at 50% 50%)",
        "clip-path:ellipse(0 5px at center)",
        "position:absolute;left:-1000px",
        "position:absolute;top:-9999px",
        "text-indent:-1000px",
        "color:white!important;color:black;background:#fff",
    ],
)
def test_hidden_color_and_layout(style):
    msg = SimpleNamespace(
        text="", html=f'<p>Before</p><div style="{style}">Hidden<b>Nested</b></div><p>After</p>'
    )
    assert messages.body_text(msg) == "Before\nAfter"


@pytest.mark.parametrize("background", ["white", "#fff", "rgba(255, 255, 255, 1)"])
def test_nearest_ancestor_background(background):
    msg = SimpleNamespace(
        text="",
        html=f'<div style="background:{background}"><p>Before</p><section>'
        '<span style="color:#ffffff">Hidden</span></section><p>After</p></div>'
        '<div style="background:#000"><span style="color:#fff">Visible</span></div>',
    )
    assert messages.body_text(msg) == "Before\nAfter\nVisible"


@pytest.mark.parametrize(
    "style",
    [
        "color:#222;background:white",
        "color:#fff;background:#000",
        "color:rgba(255,255,255,0.5);background:#000",
        "height:0;overflow:visible",
        "height:10px;overflow:hidden",
        "clip:rect(" + " ".join(["auto"] * 4) + ")",
        "clip:rect(0 10px 10px 0)",
        "clip-path:inset(10%)",
        "clip-path:circle(10px)",
        "position:absolute;left:-999px",
        "position:relative;left:-1000px",
        "text-indent:10px",
        "color:rgb(invalid);background:white",
        "color:#fff;background:linear-gradient(black, white)",
        "color:white;color:black!important;color:white;background:#fff",
    ],
)
def test_color_layout_false_positive_guards(style):
    msg = SimpleNamespace(text="", html=f'<div style="{style}">Visible</div>')
    assert messages.body_text(msg) == "Visible"


def test_transparent_background_preserves_ancestor_and_background_scope():
    msg = SimpleNamespace(
        text="",
        html='<div style="background:#fff"><span style="background:transparent;'
        'color:white">Hidden</span></div><span style="color:white">Visible</span>',
    )
    assert messages.body_text(msg) == "Visible"
