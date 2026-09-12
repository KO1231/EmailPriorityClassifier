"""Regression wall for the MIME parser.

The three defects this suite pins down were each a single-fixture bug in the
previous implementation, and each one silently degraded what the model saw:

* HTML was handed to the parser still base64-encoded, so no tag was ever removed;
* only the direct children of the root payload were examined, so the body of a
  nested message was reported as the literal string "parts could not found.";
* bodies were decoded as UTF-8 unconditionally, so legacy Japanese encodings
  raised and took the whole thread with them.
"""

from datetime import UTC, datetime
from email.header import Header
from email.message import EmailMessage as StdEmailMessage

import pytest

from epc.gmail.mime import (
    decode_body_data,
    html_to_text,
    normalise_text,
    parse_message,
    parse_thread,
    walk_parts,
)
from tests.fixtures import gmail as fx

# --------------------------------------------------------------------------
# Defect 1 — HTML was never stripped
# --------------------------------------------------------------------------

HTML_BODY = """<html>
  <head><style>.a { color: red }</style></head>
  <body>
    <!-- tracking pixel follows -->
    <p>Your invoice is ready.</p>
    <script>var tracker = 1;</script>
    <a href="https://example.com/pay">Pay now</a>
  </body>
</html>"""


def test_html_only_body_is_reduced_to_visible_text() -> None:
    parsed = parse_message(fx.message(fx.text_part(HTML_BODY, subtype="html")))

    assert parsed.body_mime_type == "text/html"
    assert "Your invoice is ready." in parsed.body
    assert "Pay now" in parsed.body
    assert "<p>" not in parsed.body
    assert "<html>" not in parsed.body


@pytest.mark.parametrize("leaked", ["var tracker = 1;", "color: red", "tracking pixel follows"])
def test_script_style_and_comments_do_not_reach_the_body(leaked: str) -> None:
    """`get_text()` keeps these, so they have to be removed explicitly."""
    parsed = parse_message(fx.message(fx.text_part(HTML_BODY, subtype="html")))
    assert leaked not in parsed.body


def test_html_to_text_handles_an_empty_document() -> None:
    assert html_to_text("") == ""


# --------------------------------------------------------------------------
# Defect 2 — nested MIME trees were not traversed
# --------------------------------------------------------------------------


def test_body_is_found_two_levels_down() -> None:
    """mixed > alternative > plain. The shape almost every mail with an
    attachment uses, and the one the old parser reported as empty."""
    payload = fx.multipart(
        "mixed",
        fx.multipart(
            "alternative",
            fx.text_part("Please review the attached contract by Friday."),
            fx.text_part("<p>Please review the attached contract by Friday.</p>", subtype="html"),
        ),
        fx.attachment_part("contract.pdf"),
    )
    parsed = parse_message(fx.message(payload))

    assert parsed.body == "Please review the attached contract by Friday."
    assert parsed.body_mime_type == "text/plain"
    assert [a.filename for a in parsed.attachments] == ["contract.pdf"]


def test_body_is_found_at_arbitrary_depth() -> None:
    payload = fx.multipart(
        "mixed",
        fx.multipart("related", fx.multipart("alternative", fx.text_part("Buried four levels down."))),
    )
    assert parse_message(fx.message(payload)).body == "Buried four levels down."


def test_walk_parts_yields_root_and_every_descendant() -> None:
    payload = fx.multipart("mixed", fx.multipart("alternative", fx.text_part("a"), fx.text_part("b")))
    assert len(list(walk_parts(payload))) == 4


def test_attachment_only_message_reports_no_body_rather_than_a_placeholder() -> None:
    payload = fx.multipart("mixed", fx.attachment_part("scan.pdf", size=91_234))
    parsed = parse_message(fx.message(payload))

    assert parsed.body == ""
    assert parsed.body_mime_type is None
    assert parsed.attachments[0].size == 91_234


# --------------------------------------------------------------------------
# Defect 3 — charsets other than UTF-8
# --------------------------------------------------------------------------

JAPANESE = "請求書の件でご連絡いたしました。本日中にご確認ください。"


@pytest.mark.parametrize("charset", ["iso-2022-jp", "shift_jis", "euc-jp", "utf-8"])
def test_legacy_japanese_charsets_round_trip(charset: str) -> None:
    parsed = parse_message(fx.message(fx.text_part(JAPANESE, charset=charset)))
    assert parsed.body == JAPANESE


def test_a_mislabelled_charset_degrades_instead_of_dropping_the_thread() -> None:
    payload = fx.mislabelled_text_part(JAPANESE, encoded_as="shift_jis", declared_as="utf-8")
    parsed = parse_message(fx.message(payload))

    assert parsed.body  # something came through
    assert "�" in parsed.body  # as replacement characters, not an exception


def test_an_unknown_charset_name_falls_back_to_utf8() -> None:
    payload = fx.part("text/plain", content=b"hello", charset="x-nonsense-9000")
    assert parse_message(fx.message(payload)).body == "hello"


def test_unpadded_base64url_decodes() -> None:
    """Gmail strips base64 padding; `urlsafe_b64decode` requires it."""
    assert decode_body_data("YWJjZGU", "utf-8") == "abcde"  # 5 bytes -> needs one '='


def test_undecodable_body_data_yields_empty_string() -> None:
    assert decode_body_data("!!!not base64!!!", "utf-8") == ""


# --------------------------------------------------------------------------
# Part selection
# --------------------------------------------------------------------------


def test_plain_text_is_preferred_over_html() -> None:
    payload = fx.multipart(
        "alternative",
        fx.text_part("plain wins"),
        fx.text_part("<p>html loses</p>", subtype="html"),
    )
    parsed = parse_message(fx.message(payload))
    assert parsed.body == "plain wins"
    assert parsed.body_mime_type == "text/plain"


def test_html_is_used_when_the_plain_part_is_empty() -> None:
    payload = fx.multipart(
        "alternative",
        fx.text_part("   \n  "),
        fx.text_part("<p>html is all there is</p>", subtype="html"),
    )
    parsed = parse_message(fx.message(payload))
    assert parsed.body == "html is all there is"
    assert parsed.body_mime_type == "text/html"


def test_an_attached_text_file_is_not_mistaken_for_the_body() -> None:
    payload = fx.multipart(
        "mixed",
        fx.text_part("the real body"),
        fx.part(
            "text/plain",
            content=b"contents of an attached log file",
            charset="utf-8",
            filename="server.log",
            headers=[("Content-Disposition", 'attachment; filename="server.log"')],
        ),
    )
    parsed = parse_message(fx.message(payload))
    assert parsed.body == "the real body"
    assert [a.filename for a in parsed.attachments] == ["server.log"]


# --------------------------------------------------------------------------
# Headers — the signal the model could not see at all before
# --------------------------------------------------------------------------


def test_sender_and_recipients_are_parsed() -> None:
    parsed = parse_message(
        fx.message(
            fx.text_part("body"),
            headers=[
                ("From", "Akira Tanaka <a.tanaka@Example.CO.JP>"),
                ("To", "me@example.com, second@example.com"),
                ("Cc", "Team <team@example.com>"),
                ("Reply-To", "noreply@example.co.jp"),
            ],
        )
    )

    assert parsed.sender == "a.tanaka@example.co.jp"
    assert parsed.sender_name == "Akira Tanaka"
    assert parsed.sender_domain == "example.co.jp"
    assert parsed.to == ["me@example.com", "second@example.com"]
    assert parsed.cc == ["team@example.com"]
    assert parsed.reply_to == "noreply@example.co.jp"
    assert parsed.recipient_count == 3


def test_rfc2047_encoded_subject_and_display_name_are_decoded() -> None:
    subject = Header("【重要】ご請求のご案内", "iso-2022-jp").encode()
    sender = f"{Header('田中 明', 'iso-2022-jp').encode()} <a.tanaka@example.co.jp>"
    assert "=?" in subject  # the fixture really is an encoded-word

    parsed = parse_message(fx.message(fx.text_part("body"), headers=[("Subject", subject), ("From", sender)]))
    assert parsed.subject == "【重要】ご請求のご案内"
    assert parsed.sender_name == "田中 明"


def test_bulk_mail_headers_are_captured() -> None:
    parsed = parse_message(
        fx.message(
            fx.text_part("This week in widgets"),
            headers=[
                ("From", "newsletter@example.com"),
                ("List-Id", "Widgets Weekly <widgets.example.com>"),
                ("List-Unsubscribe", "<https://example.com/u/1>"),
                ("Precedence", "bulk"),
                ("Auto-Submitted", "auto-generated"),
            ],
        )
    )

    assert parsed.list_id == "Widgets Weekly <widgets.example.com>"
    assert parsed.has_list_unsubscribe is True
    assert parsed.precedence == "bulk"
    assert parsed.auto_submitted == "auto-generated"
    assert parsed.looks_bulk is True


def test_a_personal_message_does_not_look_bulk() -> None:
    parsed = parse_message(fx.message(fx.text_part("Are you free Thursday?"), headers=[("From", "a@example.com")]))
    assert parsed.looks_bulk is False


def test_authentication_results_are_parsed() -> None:
    header = "mx.example.com; spf=pass smtp.mailfrom=example.com; dkim=fail header.i=@example.com; dmarc=none"
    parsed = parse_message(fx.message(fx.text_part("body"), headers=[("Authentication-Results", header)]))
    assert parsed.authentication.spf == "pass"
    assert parsed.authentication.dkim == "fail"
    assert parsed.authentication.dmarc == "none"


def test_missing_authentication_results_leaves_every_verdict_unset() -> None:
    parsed = parse_message(fx.message(fx.text_part("body")))
    assert parsed.authentication.spf is None


def test_raw_label_ids_are_preserved() -> None:
    """Translating these to display names is what made tab membership unknowable."""
    labels = ["INBOX", "UNREAD", "CATEGORY_PROMOTIONS", "Label_123", "IMPORTANT"]
    parsed = parse_message(fx.message(fx.text_part("body"), label_ids=labels))
    assert parsed.label_ids == labels


# --------------------------------------------------------------------------
# Text normalisation
# --------------------------------------------------------------------------


def test_line_structure_survives_normalisation() -> None:
    """Quoted-reply stripping keys off line starts, so lines must not be flattened."""
    normalised = normalise_text("Hi there\r\n\r\n> quoted line one\r\n>   quoted line two\t\t\n\n\n\nBye")
    assert normalised == "Hi there\n\n> quoted line one\n> quoted line two\n\nBye"


def test_horizontal_whitespace_is_collapsed() -> None:
    assert normalise_text("a  \t  b") == "a b"


# --------------------------------------------------------------------------
# Malformed input must not take a thread down
# --------------------------------------------------------------------------


def test_a_message_with_no_payload_parses_to_an_empty_body() -> None:
    parsed = parse_message({"id": "m", "threadId": "t"})
    assert parsed.body == ""
    assert parsed.body_mime_type is None
    assert parsed.sender == ""


def test_a_malformed_internal_date_falls_back_to_the_epoch() -> None:
    raw = fx.message(fx.text_part("body"))
    raw["internalDate"] = "not-a-number"
    assert parse_message(raw).date == datetime.fromtimestamp(0, tz=UTC)


def test_internal_date_is_converted_from_milliseconds() -> None:
    raw = fx.message(fx.text_part("body"), internal_date_ms=1_767_225_600_000)
    assert parse_message(raw).date == datetime(2026, 1, 1, tzinfo=UTC)


# --------------------------------------------------------------------------
# Threads
# --------------------------------------------------------------------------


def test_thread_keeps_gmail_order_and_exposes_the_latest_message() -> None:
    raw = fx.thread(
        fx.message(fx.text_part("first"), message_id="m1", internal_date_ms=1_000),
        fx.message(fx.text_part("second"), message_id="m2", internal_date_ms=2_000),
        fx.message(fx.text_part("third"), message_id="m3", internal_date_ms=3_000),
        thread_id="t-42",
    )
    parsed = parse_thread(raw)

    assert parsed.thread_id == "t-42"
    assert parsed.message_ids == ["m1", "m2", "m3"]
    assert parsed.subject == parsed.messages[0].subject
    assert parsed.latest is not None
    assert parsed.latest.body == "third"


def test_thread_unions_labels_and_reports_the_tabs_to_leave() -> None:
    raw = fx.thread(
        fx.message(fx.text_part("a"), message_id="m1", label_ids=["INBOX", "CATEGORY_PROMOTIONS"]),
        fx.message(fx.text_part("b"), message_id="m2", label_ids=["INBOX", "UNREAD"]),
    )
    parsed = parse_thread(raw)

    assert parsed.label_ids == {"INBOX", "UNREAD", "CATEGORY_PROMOTIONS"}
    assert parsed.non_primary_categories == {"CATEGORY_PROMOTIONS"}


def test_an_empty_thread_is_harmless() -> None:
    parsed = parse_thread({"id": "t"})
    assert parsed.messages == []
    assert parsed.latest is None
    assert parsed.subject == ""


# --------------------------------------------------------------------------
# A realistic message assembled with the stdlib, rather than by hand
# --------------------------------------------------------------------------


def test_a_stdlib_built_message_parses_end_to_end() -> None:
    msg = StdEmailMessage()
    msg["From"] = "Support <support@example.com>"
    msg["To"] = "me@example.com"
    msg["Subject"] = "Ticket #4821 updated"
    msg.set_content("The engineer has replied.\n\nSee the portal for details.")
    msg.add_alternative("<p>The engineer has replied.</p>", subtype="html")
    msg.add_attachment(b"%PDF-1.4 fake", maintype="application", subtype="pdf", filename="report.pdf")

    parsed = parse_message(fx.message(fx.from_std_message(msg), label_ids=["INBOX"]))

    assert parsed.subject == "Ticket #4821 updated"
    assert parsed.sender == "support@example.com"
    assert parsed.body_mime_type == "text/plain"
    assert parsed.body.startswith("The engineer has replied.")
    assert [a.filename for a in parsed.attachments] == ["report.pdf"]
