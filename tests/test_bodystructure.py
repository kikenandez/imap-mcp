"""Attachment metadata from canned IMAP BODYSTRUCTURE responses (no network, no real mail)."""

import pytest

from imap_mcp.bodystructure import (
    MAX_ATTACHMENTS,
    MAX_NAME_CHARS,
    attachments_from_fetch,
    parse_sexp,
    summarize,
)


def wrap(structure: str, uid: int = 7) -> list:
    return [f"1 (UID {uid} BODYSTRUCTURE {structure})".encode()]


PLAIN = '("TEXT" "PLAIN" ("CHARSET" "UTF-8") NIL NIL "7BIT" 120 4 NIL NIL NIL)'
HTML = '("TEXT" "HTML" ("CHARSET" "UTF-8") NIL NIL "QUOTED-PRINTABLE" 900 20 NIL NIL NIL)'
PDF = (
    '("APPLICATION" "PDF" ("NAME" "report.pdf") NIL NIL "BASE64" 4096 NIL '
    '("ATTACHMENT" ("FILENAME" "report.pdf")) NIL NIL)'
)


def test_plain_message_has_no_attachments():
    assert summarize(parse_sexp(PLAIN)) == []


def test_nested_multipart_finds_attachment():
    mixed = (
        f'(({PLAIN} {HTML} "ALTERNATIVE" ("BOUNDARY" "b2") NIL NIL NIL) {PDF} "MIXED" NIL NIL NIL)'
    )
    assert summarize(parse_sexp(mixed)) == [
        {"name": "report.pdf", "size": 4096, "content_type": "application/pdf"}
    ]


def test_deeply_nested_multipart():
    inner = f'({PDF} "MIXED" NIL NIL NIL)'
    outer = f'(({PLAIN} {inner} "RELATED" NIL NIL NIL) "MIXED" NIL NIL NIL)'
    assert [a["name"] for a in summarize(parse_sexp(outer))] == ["report.pdf"]


def test_inline_image_with_content_id_only_is_not_an_attachment():
    image = (
        '("IMAGE" "PNG" ("NAME" NIL) "<cid1@example.com>" NIL "BASE64" 300 NIL '
        '("INLINE" NIL) NIL NIL)'
    )
    # Inline, no filename: a signature or body image, not an attachment.
    assert summarize(parse_sexp(f'({PLAIN} {image} "RELATED" NIL NIL NIL)')) == []


def test_non_text_part_with_filename_and_no_disposition_is_an_attachment():
    part = '("IMAGE" "JPEG" ("NAME" "photo.jpg") NIL NIL "BASE64" 2048 NIL NIL NIL NIL)'
    assert summarize(parse_sexp(f'({PLAIN} {part} "MIXED" NIL NIL NIL)')) == [
        {"name": "photo.jpg", "size": 2048, "content_type": "image/jpeg"}
    ]


def test_text_part_with_attachment_disposition_counts():
    part = (
        '("TEXT" "CSV" ("CHARSET" "UTF-8") NIL NIL "7BIT" 55 3 NIL '
        '("ATTACHMENT" ("FILENAME" "data.csv")) NIL NIL)'
    )
    assert [a["name"] for a in summarize(parse_sexp(f'({PLAIN} {part} "MIXED" NIL NIL NIL)'))] == [
        "data.csv"
    ]


def test_text_part_with_filename_but_no_disposition_is_body_text():
    part = '("TEXT" "PLAIN" ("NAME" "body.txt") NIL NIL "7BIT" 10 1 NIL NIL NIL NIL)'
    assert summarize(parse_sexp(part)) == []


def test_attached_message_rfc822_is_one_attachment_not_recursed():
    forwarded = (
        '("MESSAGE" "RFC822" ("NAME" "fwd.eml") NIL NIL "7BIT" 5000 '
        f"(NIL NIL NIL NIL NIL NIL NIL NIL NIL NIL) ({PDF}) 80 NIL "
        '("ATTACHMENT" ("FILENAME" "fwd.eml")) NIL NIL)'
    )
    result = summarize(parse_sexp(f'({PLAIN} {forwarded} "MIXED" NIL NIL NIL)'))
    assert [a["name"] for a in result] == ["fwd.eml"]


def test_rfc2231_extended_filename():
    part = (
        '("APPLICATION" "PDF" NIL NIL NIL "BASE64" 10 NIL '
        '("ATTACHMENT" ("FILENAME*" "utf-8\'\'r%C3%A9sum%C3%A9.pdf")) NIL NIL)'
    )
    assert summarize(parse_sexp(part))[0]["name"] == "résumé.pdf"


def test_rfc2231_continuations():
    part = (
        '("APPLICATION" "PDF" NIL NIL NIL "BASE64" 10 NIL '
        '("ATTACHMENT" ("FILENAME*0*" "utf-8\'\'long%20na" "FILENAME*1*" "me%20" '
        '"FILENAME*2" "x.pdf")) NIL NIL)'
    )
    assert summarize(parse_sexp(part))[0]["name"] == "long name x.pdf"


def test_rfc2047_encoded_filename():
    part = (
        '("APPLICATION" "PDF" NIL NIL NIL "BASE64" 10 NIL '
        '("ATTACHMENT" ("FILENAME" "=?UTF-8?B?w6l0w6kucGRm?=")) NIL NIL)'
    )
    assert summarize(parse_sexp(part))[0]["name"] == "été.pdf"


def test_rfc2047_with_bad_charset_does_not_crash():
    part = (
        '("APPLICATION" "PDF" NIL NIL NIL "BASE64" 10 NIL '
        '("ATTACHMENT" ("FILENAME" "=?no-such-charset?Q?abc?=")) NIL NIL)'
    )
    assert len(summarize(parse_sexp(part))) == 1


def test_name_falls_back_to_content_type_name_param():
    part = (
        '("APPLICATION" "ZIP" ("NAME" "a.zip") NIL NIL "BASE64" 9 NIL ("ATTACHMENT" NIL) NIL NIL)'
    )
    assert summarize(parse_sexp(part))[0]["name"] == "a.zip"


def test_unnamed_attachment_has_empty_name():
    part = '("APPLICATION" "ZIP" NIL NIL NIL "BASE64" 9 NIL ("ATTACHMENT" NIL) NIL NIL)'
    assert summarize(parse_sexp(part)) == [
        {"name": "", "size": 9, "content_type": "application/zip"}
    ]


def test_names_truncated_and_control_characters_removed():
    name = "a" * 500
    part = (
        '("APPLICATION" "PDF" NIL NIL NIL "BASE64" 10 NIL '
        f'("ATTACHMENT" ("FILENAME" "{name}")) NIL NIL)'
    )
    assert len(summarize(parse_sexp(part))[0]["name"]) == MAX_NAME_CHARS
    evil = (
        '("APPLICATION" "PDF" NIL NIL NIL "BASE64" 10 NIL '
        '("ATTACHMENT" ("FILENAME" "a\x07b\u202ec")) NIL NIL)'
    )
    assert "\x07" not in summarize(parse_sexp(evil))[0]["name"]


def test_attachment_list_is_capped():
    parts = " ".join(PDF for _ in range(MAX_ATTACHMENTS + 15))
    assert len(summarize(parse_sexp(f'({parts} "MIXED" NIL NIL NIL)'))) == MAX_ATTACHMENTS


def test_quoted_string_escapes():
    part = (
        '("APPLICATION" "PDF" NIL NIL NIL "BASE64" 10 NIL '
        '("ATTACHMENT" ("FILENAME" "say \\"hi\\".pdf")) NIL NIL)'
    )
    assert summarize(parse_sexp(part))[0]["name"] == 'say "hi".pdf'


def test_fetch_response_maps_uid_to_attachments():
    data = [
        f"1 (UID 7 BODYSTRUCTURE {PLAIN})".encode(),
        f'2 (UID 8 BODYSTRUCTURE ({PLAIN} {PDF} "MIXED" NIL NIL NIL))'.encode(),
    ]
    result = attachments_from_fetch(data)
    assert result["7"] == []
    assert [a["name"] for a in result["8"]] == ["report.pdf"]


def test_fetch_response_with_imaplib_literal_split():
    # imaplib hands a {n} literal as a (head, literal) tuple followed by the remainder.
    data = [
        (
            b'1 (UID 9 BODYSTRUCTURE (("APPLICATION" "PDF" ("NAME" {7}',
            b"lit.pdf",
        ),
        b') NIL NIL "BASE64" 10 NIL ("ATTACHMENT" ("FILENAME" "lit.pdf")) NIL NIL) "MIXED"))',
    ]
    assert attachments_from_fetch(data)["9"][0]["name"] == "lit.pdf"


def test_fetch_response_uid_after_bodystructure_and_flags():
    data = [f"1 (FLAGS (\\Seen) BODYSTRUCTURE {PDF} UID 11)".encode()]
    assert attachments_from_fetch(data)["11"][0]["name"] == "report.pdf"


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "(",
        "((((",
        '("TEXT" "PLAIN"',
        "NIL",
        '("APPLICATION")',
        '("APPLICATION" "PDF" "notalist" NIL NIL "BASE64" "notanint" NIL "notalist" NIL NIL)',
        '(("APPLICATION" "PDF") "MIXED")',
        "{99}",
        '"unterminated',
        "(" * 5000 + ")" * 5000,
    ],
)
def test_malformed_structures_give_no_attachments_and_do_not_raise(bad):
    data = [f"1 (UID 5 BODYSTRUCTURE {bad})".encode()]
    assert attachments_from_fetch(data).get("5", []) == []


def test_garbage_items_are_skipped():
    assert attachments_from_fetch([None, b")", b"garbage", 42]) == {}
    assert attachments_from_fetch(None) == {}
