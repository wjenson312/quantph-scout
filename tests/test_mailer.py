"""Tests for email rendering and delivery.

Each test here corresponds to a bug that is invisible until a real inbox opens
the message, which is why they are worth having: a broken List-Unsubscribe or a
recipient list leaked into To: cannot be caught by reading the code back.
"""

from __future__ import annotations

import re
from email.message import EmailMessage
from pathlib import Path

import pytest

from scout.config import EmailConfig
from scout.digest import compose_subject
from scout.mailer import (
    _linkify,
    _normalize_password,
    markdown_to_html,
    render_html,
    send_digest,
)

CONFIGURED = EmailConfig(
    host="smtp.gmail.com",
    port=587,
    user="scout@example.com",
    password="abcd efgh ijkl mnop",
    recipients=("one@example.com", "two@example.com"),
    sender="scout@example.com",
)

SAMPLE = """# arXiv Industry Scout — week of 2026-10-02

504 scanned · 20 shortlisted · 2 featured · run cost $1.33

*1721 papers fetched.*

## 1. Gallium refining
**Paper:** [Ambient Pressure Growth of ScB2](https://arxiv.org/abs/2609.36493) — S. Kushwaha et al.
**Pipeline:** New fabrication technique -> Manufacturing process
**Readiness score:** 9/10 — weakest: performance_margin (not quantified)
**What they found:** They grew single crystals of ScB2.
**Why it matters for Gallium refining:** It could serve as a substrate.
**The catch:** The crystals were grown slowly.

## 2. Refractories
**Paper:** [Neural modeling of sintering](https://arxiv.org/abs/2609.31518) — Z. Chen et al.  *(unverified — the checking pass failed for this paper)*
**Pipeline:** Theory or model insight -> Design and simulation tools
**Readiness score:** 9/10 — weakest: reproducibility (not stated)
**What they found:** A model that predicts shrinkage.
**Why it matters for Refractories:** Fewer costly test furnaces.
**The catch:** Only three lab materials.

---

*297 papers were dropped by the free pre-filter.*

---

## Rate this week

Append to `state/feedback.yaml`. See https://example.com/feedback for context.

```yaml
  - arxiv_id: "2609.36493"
    rating:        # 1-5
```
"""


# --- password normalisation ------------------------------------------------


def test_gmail_app_password_spaces_are_stripped():
    """Google shows app passwords as 4x4; people copy the spaces."""
    assert _normalize_password("abcd efgh ijkl mnop") == "abcdefghijklmnop"


def test_already_joined_password_is_untouched():
    assert _normalize_password("abcdefghijklmnop") == "abcdefghijklmnop"


@pytest.mark.parametrize(
    "password",
    [
        "correct horse battery staple",  # real spaces, wrong shape
        "abc defg hijk lmno",            # not 4x4
        "abcd efgh ijkl",                # only three groups
        "pa55 word with spaces here",
    ],
)
def test_passwords_that_are_not_the_google_pattern_keep_their_spaces(password):
    """Mangling a provider's real password would be worse than the bug fixed."""
    assert _normalize_password(password) == password


# --- linkification ---------------------------------------------------------


def test_bare_url_becomes_a_link():
    out = _linkify("see https://arxiv.org/abs/2609.1 for more")
    assert 'href="https://arxiv.org/abs/2609.1"' in out


def test_url_inside_an_existing_href_is_not_relinkified():
    html_in = '<a href="https://arxiv.org/abs/1">https://arxiv.org/abs/1</a>'
    assert _linkify(html_in).count("<a ") == 1


def test_trailing_punctuation_is_left_outside_the_link():
    out = _linkify("see https://example.com/x.")
    assert 'href="https://example.com/x"' in out
    assert out.endswith(".")


# --- HTML rendering --------------------------------------------------------


def test_no_style_block_is_emitted():
    """Clients strip <style>; anything load-bearing must be an inline style."""
    html_out = render_html(CONFIGURED, SAMPLE)
    assert "<style" not in html_out.lower()
    assert 'style="' in html_out


def test_labels_and_titles_are_styled_differently():
    """The bold-on-bold bug: a label must not look like a paper title."""
    html_out = markdown_to_html(SAMPLE)
    assert "17.5px" in html_out        # paper title
    assert "text-transform:uppercase" in html_out  # field labels


def test_industry_is_not_repeated_in_the_why_label():
    html_out = markdown_to_html(SAMPLE)
    assert "WHY IT MATTERS FOR" not in html_out.upper()
    assert "Why it matters" in html_out


def test_every_paper_link_survives():
    html_out = markdown_to_html(SAMPLE)
    assert 'href="https://arxiv.org/abs/2609.36493"' in html_out
    assert 'href="https://arxiv.org/abs/2609.31518"' in html_out


def test_yaml_block_is_preserved_verbatim():
    """The feedback block has to be parseable after a round trip."""
    html_out = markdown_to_html(SAMPLE)
    assert "<pre" in html_out
    assert 'arxiv_id: &quot;2609.36493&quot;' in html_out


def test_unverified_marker_survives():
    assert "unverified" in markdown_to_html(SAMPLE)


def test_markup_in_source_text_cannot_inject_html():
    out = markdown_to_html("**What they found:** a <script>alert(1)</script> tag")
    assert "<script>" not in out
    assert "&lt;script&gt;" in out


def test_empty_digest_renders_without_crashing():
    assert render_html(CONFIGURED, "No highly relevant papers this week.\n")


# --- subject ---------------------------------------------------------------


def test_subject_names_the_specific_paper():
    subject = compose_subject("[quant-ph Scout]", "Gallium refining", "ScB2 crystals", 7)
    assert "Gallium refining" in subject
    assert "ScB2 crystals" in subject
    assert "+ 7 more" in subject


def test_long_titles_are_trimmed_on_a_word_boundary():
    title = "Efficient simulation of millimeter-scale complex-modulated integrated Bragg gratings"
    subject = compose_subject("[quant-ph Scout]", "Mature-node chip foundry", title, 7)
    assert len(subject) <= 112
    assert subject.endswith("more")
    assert "modulat…" not in subject  # no mid-word cut


# --- delivery --------------------------------------------------------------


class _FakeSMTP:
    """Captures the message instead of sending it."""

    sent: EmailMessage | None = None
    logged_in_with: tuple[str, str] | None = None
    started_tls = False

    def __init__(self, host, port, timeout=None, context=None):
        type(self).host, type(self).port = host, port

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def ehlo(self):
        pass

    def starttls(self, context=None):
        type(self).started_tls = True

    def login(self, user, password):
        type(self).logged_in_with = (user, password)

    def send_message(self, message):
        type(self).sent = message


@pytest.fixture
def smtp(monkeypatch):
    _FakeSMTP.sent = None
    _FakeSMTP.logged_in_with = None
    _FakeSMTP.started_tls = False
    monkeypatch.setattr("smtplib.SMTP", _FakeSMTP)
    monkeypatch.setattr("smtplib.SMTP_SSL", _FakeSMTP)
    return _FakeSMTP


def _send(email=CONFIGURED, markdown=SAMPLE):
    return send_digest(email, "[quant-ph Scout] a subject", markdown, Path("2026-10-02.md"))


def test_unconfigured_email_is_a_no_op_not_an_error():
    assert send_digest(EmailConfig(), "s", SAMPLE, Path("d.md")) is False


def test_recipients_go_in_bcc_never_to_or_cc(smtp):
    _send()
    message = smtp.sent
    assert message["Bcc"] == "one@example.com, two@example.com"
    assert "two@example.com" not in (message["To"] or "")
    assert message["Cc"] is None


def test_message_is_multipart_with_plain_and_html(smtp):
    _send()
    types = {part.get_content_type() for part in smtp.sent.walk()}
    assert "text/plain" in types
    assert "text/html" in types


def test_plain_part_is_the_markdown_verbatim(smtp):
    """Spam filters read the plain part; it must not be a stub."""
    _send()
    plain = smtp.sent.get_body(preferencelist=("plain",)).get_content()
    assert "What they found" in plain
    assert len(plain) > 500


def test_list_unsubscribe_is_always_set(smtp):
    _send()
    assert smtp.sent["List-Unsubscribe"]


def test_url_unsubscribe_enables_one_click(smtp):
    email = EmailConfig(**{**CONFIGURED.__dict__, "unsubscribe": "https://x.example/u"})
    _send(email)
    assert smtp.sent["List-Unsubscribe"] == "<https://x.example/u>"
    assert smtp.sent["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"


def test_address_unsubscribe_asks_the_reader_to_reply(smtp):
    email = EmailConfig(**{**CONFIGURED.__dict__, "unsubscribe": "me@example.com"})
    _send(email)
    assert "mailto:me@example.com" in smtp.sent["List-Unsubscribe"]
    html_part = smtp.sent.get_body(preferencelist=("html",)).get_content()
    assert "reply and say so" in html_part


def test_password_is_normalised_before_login(smtp):
    _send()
    assert smtp.logged_in_with == ("scout@example.com", "abcdefghijklmnop")


def test_starttls_is_used_on_port_587(smtp):
    _send()
    assert smtp.started_tls


def test_port_465_uses_implicit_tls_without_starttls(smtp):
    _send(EmailConfig(**{**CONFIGURED.__dict__, "port": 465}))
    assert not smtp.started_tls
    assert smtp.sent is not None


def test_sender_name_appears_on_the_from_line(smtp):
    _send(EmailConfig(**{**CONFIGURED.__dict__, "sender_name": "arXiv Scout"}))
    assert smtp.sent["From"] == "arXiv Scout <scout@example.com>"


def test_markdown_is_attached_for_the_archive(smtp):
    _send()
    names = [part.get_filename() for part in smtp.sent.walk()]
    assert "2026-10-02.md" in names


def test_headers_mark_the_message_as_bulk(smtp):
    _send()
    assert smtp.sent["Precedence"] == "bulk"
    assert smtp.sent["Auto-Submitted"] == "auto-generated"
    assert re.match(r"<.+@.+>", smtp.sent["Message-ID"] or "")
