"""Email delivery of the digest. No-ops unless SMTP_* is configured.

The digest is written as Markdown; this module turns that one artifact into a
sendable email. Everything here is a lesson that cost real debugging, so the
comments say *why* rather than *what*.

What this file guarantees, and must keep guaranteeing:

* **multipart/alternative: text/plain + text/html.** The plain part is the
  Markdown verbatim. It is the fallback for text-only clients and it is also
  what spam filters read, so it is never omitted or stubbed out.
* **Inline styles, never a `<style>` block.** Some clients strip `<style>`
  entirely. A label that loses its CSS falls straight back to looking like the
  paper title, which is the exact hierarchy bug this styling exists to fix.
* **Recipients go in Bcc.** To:/Cc: leaks every subscriber's address to every
  other subscriber -- the most common small-newsletter privacy failure.
* **Bare URLs are linkified here.** Nothing else will do it; a plain
  `https://...` would render as dead text.
* **`List-Unsubscribe` is always set.** It gives the native Unsubscribe button
  in Gmail and Apple Mail, signals legitimacy, and CAN-SPAM requires a working
  opt-out regardless.
"""

from __future__ import annotations

import html
import logging
import os
import re
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path

from .config import EmailConfig

log = logging.getLogger("scout.mailer")

# ---------------------------------------------------------------------------
# Palette and type scale.
#
# Hierarchy, not weight, is what makes an item readable. An earlier version set
# the paper title and all five field labels in bold at the same size, so each
# item read as six identical bold runs with nothing to anchor the eye. The fix
# is one loud thing per item (the title) and labels that recede: 11px, uppercase,
# letter-spaced, in the accent colour.
#
# These labels mirror the lines render_digest() emits in digest.py. THE TWO
# LISTS ARE SEPARATE: if you rename a field there, rename it in ITEM_LABELS too
# or the styling silently stops applying and the item goes back to bold-on-bold.
# ---------------------------------------------------------------------------

INK = "#1a1a1a"          # body text
TITLE_INK = "#111111"    # paper titles: deliberately darker than body
MUTED = "#6b6b6b"        # bylines, metadata, footer
FAINT = "#9a9a9a"         # the quietest text in the email
ACCENT = "#9a3412"        # rust: field labels and links
RULE = "#e4e0da"          # hairlines
CARD_BG = "#faf9f7"       # the stats band and code blocks
PAGE_BG = "#f4f2ef"       # outside the content column

BODY_FONT = (
    "-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif"
)
MONO_FONT = "ui-monospace,SFMono-Regular,Menlo,Consolas,monospace"

# Field labels that get the small-uppercase accent treatment. Matched as a
# prefix, because "Why it matters" carries the industry name after it.
ITEM_LABELS = (
    "What they found",
    "Why it matters",
    "The catch",
)
# Short metadata fields, set as one quiet line rather than as labelled blocks.
META_LABELS = ("Pipeline", "Readiness score")

_LABEL_RE = re.compile(r"^\*\*([^*]+?):\*\*\s*(.*)$")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_ITEM_HEADING_RE = re.compile(r"^##\s+\d+\.\s+(.*)$")
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_EM_RE = re.compile(r"(?<!\*)\*([^*]+?)\*(?!\*)")
_CODE_RE = re.compile(r"`([^`]+)`")
_BULLET_RE = re.compile(r"^\s*-\s+(.*)$")
_BARE_URL_RE = re.compile(r"(?<![\"'=>])\bhttps?://[^\s<>\"')\]]+[^\s<>\"')\].,;:]")
_TAG_SPLIT_RE = re.compile(r"(<[^>]+>)")

# Google displays a 16-character app password as four space-separated groups of
# four. People copy the spaces, producing a 19-character value that fails auth
# with an error that explains nothing. Strip spaces ONLY when the value matches
# that exact display pattern -- some providers issue passwords with real spaces,
# and silently mangling those would be a worse bug than the one being fixed.
_APP_PASSWORD_RE = re.compile(r"^([a-z]{4})\s([a-z]{4})\s([a-z]{4})\s([a-z]{4})$", re.I)


def _normalize_password(password: str) -> str:
    if _APP_PASSWORD_RE.match(password):
        return password.replace(" ", "")
    return password


def _ssl_context() -> ssl.SSLContext:
    """A TLS context with a trust store that is actually populated.

    python.org builds of Python on macOS do not read the system keychain. Unless
    `Install Certificates.command` has been run, `ssl.create_default_context()`
    returns a context with ZERO certificate authorities and every TLS connection
    dies with CERTIFICATE_VERIFY_FAILED -- which reads like a wrong password or a
    dead server and is neither. Prefer certifi's bundle, which is a real
    dependency of `requests` and therefore always present.

    GitHub Actions runners have a fine trust store; this costs nothing there.
    """
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:  # pragma: no cover - certifi is a hard dependency of requests
        log.warning("certifi unavailable; falling back to the system trust store")
        return ssl.create_default_context()


# ---------------------------------------------------------------------------
# Inline Markdown
# ---------------------------------------------------------------------------


def _link(url: str, text: str, *, color: str = ACCENT, bold: bool = False) -> str:
    weight = "600" if bold else "inherit"
    return (
        f'<a href="{html.escape(url, quote=True)}" '
        f'style="color:{color};text-decoration:underline;'
        f'text-decoration-color:{RULE};font-weight:{weight};">{text}</a>'
    )


def _linkify(fragment: str) -> str:
    """Wrap bare URLs in anchors, leaving existing markup alone.

    Splits on tags so a URL inside an `href=` is never touched, and tracks
    anchor depth so a bare URL used as the *text* of an existing link does not
    get a second anchor nested inside the first.
    """
    out: list[str] = []
    depth = 0
    for part in _TAG_SPLIT_RE.split(fragment):
        if part.startswith("<"):
            lowered = part.lower()
            if lowered.startswith("<a"):
                depth += 1
            elif lowered.startswith("</a"):
                depth = max(0, depth - 1)
            out.append(part)
        elif depth:
            out.append(part)
        else:
            out.append(_BARE_URL_RE.sub(lambda m: _link(m.group(0), m.group(0)), part))
    return "".join(out)


def _inline(text: str, *, linkify: bool = True) -> str:
    """Escape, then apply inline Markdown. Order is load-bearing.

    Escaping first means no source text can inject markup. Code spans go next so
    their contents are never re-parsed as emphasis; explicit links before bare
    URLs so `[text](url)` wins.
    """
    out = html.escape(text)
    out = _CODE_RE.sub(
        lambda m: (
            f'<code style="font-family:{MONO_FONT};font-size:0.9em;'
            f'background:{CARD_BG};padding:1px 4px;border-radius:3px;">'
            f"{m.group(1)}</code>"
        ),
        out,
    )
    out = _LINK_RE.sub(lambda m: _link(html.unescape(m.group(2)), m.group(1)), out)
    out = _BOLD_RE.sub(
        lambda m: f'<strong style="font-weight:600;color:{TITLE_INK};">{m.group(1)}</strong>',
        out,
    )
    out = _EM_RE.sub(lambda m: f'<em style="color:{MUTED};">{m.group(1)}</em>', out)
    return _linkify(out) if linkify else out


# ---------------------------------------------------------------------------
# Block rendering
# ---------------------------------------------------------------------------


def _short_label(name: str) -> str:
    for label in ITEM_LABELS:
        if name.startswith(label):
            return label
    return name


def _meta_value(name: str, body: str) -> str:
    """One metadata field, rendered for the quiet line under the title.

    `Readiness score: 9/10` loses its meaning once the label is dropped, so the
    word is folded into the value. The pipeline arrow is the Markdown's ASCII
    `->`; an email can afford the real one.
    """
    body = body.replace(" -> ", " \u2192 ")
    if name == "Readiness score":
        body = f"readiness {body}"
    return _inline(body, linkify=False)


def _para(text: str, *, size: str = "15px", color: str = INK, top: str = "0") -> str:
    return (
        f'<p style="margin:{top} 0 12px;font-size:{size};line-height:1.62;'
        f'color:{color};">{text}</p>'
    )


def _label(text: str) -> str:
    return (
        f'<div style="margin:18px 0 5px;font-size:11px;font-weight:700;'
        f"letter-spacing:0.09em;text-transform:uppercase;color:{ACCENT};"
        f'">{html.escape(text)}</div>'
    )


def _hr() -> str:
    return (
        f'<div style="border-top:1px solid {RULE};margin:30px 0;'
        f'font-size:0;line-height:0;">&nbsp;</div>'
    )


class _Renderer:
    """Markdown -> email HTML for the shape render_digest() emits.

    Written against that format rather than generically, because the hierarchy
    the email needs is not the hierarchy the Markdown has: `## 1. <industry>` is
    a *category label*, and the paper title -- which Markdown buries inside a
    `**Paper:**` line -- is the real heading. So the two are swapped here.

    Anything unrecognised falls through to plain paragraph rendering, so adding a
    section to the digest degrades to readable text instead of vanishing.
    """

    def __init__(self) -> None:
        self.out: list[str] = []
        self.in_code = False
        self.code: list[str] = []
        self.in_list = False
        self.item_open = False
        self.pending_meta: list[str] = []

    # -- item cards ---------------------------------------------------------

    def _close_item(self) -> None:
        self._flush_meta()
        if self.item_open:
            self.out.append("</div>")
            self.item_open = False

    def _open_item(self, industry: str) -> None:
        self._close_list()
        self._close_item()
        self.out.append(
            f'<div style="margin:34px 0 0;padding:22px 0 4px;'
            f'border-top:1px solid {RULE};">'
        )
        self.item_open = True
        # The industry is the eyebrow: it says which lens the paper is read
        # through, and it must not compete with the title underneath it.
        self.out.append(
            f'<div style="font-size:11px;font-weight:700;letter-spacing:0.1em;'
            f"text-transform:uppercase;color:{ACCENT};margin:0 0 7px;"
            f'">{_inline(industry, linkify=False)}</div>'
        )

    def _flush_meta(self) -> None:
        if not self.pending_meta:
            return
        joined = '<span style="color:%s;"> &middot; </span>' % FAINT
        self.out.append(
            f'<p style="margin:0 0 4px;font-size:12.5px;line-height:1.55;'
            f'color:{MUTED};">{joined.join(self.pending_meta)}</p>'
        )
        self.pending_meta = []

    def _paper_line(self, body: str) -> None:
        """`**Paper:** [title](url) — byline  *(unverified ...)*`

        Promoted to the item's headline: title large and dark, byline and the
        unverified marker demoted to a quiet line beneath it.
        """
        match = _LINK_RE.search(body)
        if match:
            title = _inline(match.group(1), linkify=False)
            url = html.unescape(match.group(2))
            rest = body[: match.start()] + body[match.end() :]
            headline = _link(url, title, color=TITLE_INK, bold=True)
        else:
            headline, rest = _inline(body, linkify=False), ""
        self.out.append(
            f'<div style="margin:0 0 6px;font-size:17.5px;line-height:1.32;'
            f'font-weight:600;color:{TITLE_INK};">{headline}</div>'
        )
        rest = rest.strip().lstrip("—-").strip()
        if rest:
            self.out.append(
                f'<p style="margin:0 0 10px;font-size:13px;line-height:1.5;'
                f'color:{MUTED};">{_inline(rest, linkify=False)}</p>'
            )

    # -- generic blocks -----------------------------------------------------

    def _close_list(self) -> None:
        if self.in_list:
            self.out.append("</ul>")
            self.in_list = False

    def _bullet(self, text: str) -> None:
        self._flush_meta()
        if not self.in_list:
            self.out.append(
                '<ul style="margin:0 0 14px;padding-left:20px;">'
            )
            self.in_list = True
        self.out.append(
            f'<li style="margin:0 0 7px;font-size:14.5px;line-height:1.6;'
            f'color:{INK};">{_inline(text)}</li>'
        )

    def _code_block(self) -> None:
        """A `<pre>` the reader can copy out of.

        The feedback block is YAML that the next run has to parse, so it must
        survive the trip intact: no smart quotes, no wrapping, no linkifying.
        """
        body = html.escape("\n".join(self.code))
        self.out.append(
            f'<pre style="margin:0 0 16px;padding:14px 16px;background:{CARD_BG};'
            f"border:1px solid {RULE};border-radius:6px;font-family:{MONO_FONT};"
            f"font-size:12px;line-height:1.5;color:{INK};white-space:pre;"
            f'overflow-x:auto;">{body}</pre>'
        )
        self.code = []

    def _heading(self, level: int, text: str) -> None:
        self._close_list()
        self._close_item()
        size = {1: "25px", 2: "19px"}.get(level, "16px")
        space = "0 0 10px" if level == 1 else "30px 0 10px"
        self.out.append(
            f'<h{min(level, 3)} style="margin:{space};font-size:{size};'
            f"line-height:1.28;font-weight:700;color:{TITLE_INK};"
            f'">{_inline(text, linkify=False)}</h{min(level, 3)}>'
        )

    # -- driver -------------------------------------------------------------

    def render(self, markdown: str) -> str:
        lines = markdown.replace("\r\n", "\n").split("\n")
        first_para_done = False

        for raw in lines:
            line = raw.rstrip()

            if line.strip().startswith("```"):
                if self.in_code:
                    self._code_block()
                    self.in_code = False
                else:
                    self._close_list()
                    self._flush_meta()
                    self.in_code = True
                continue
            if self.in_code:
                self.code.append(raw)
                continue

            if not line.strip():
                self._close_list()
                continue

            if line.strip() in {"---", "***", "___"}:
                self._close_list()
                self._close_item()
                self.out.append(_hr())
                continue

            item = _ITEM_HEADING_RE.match(line)
            if item:
                self._open_item(item.group(1))
                continue

            heading = _HEADING_RE.match(line)
            if heading:
                self._heading(len(heading.group(1)), heading.group(2))
                continue

            bullet = _BULLET_RE.match(line)
            if bullet:
                self._bullet(bullet.group(1))
                continue
            self._close_list()

            label = _LABEL_RE.match(line)
            if label:
                name, body = label.group(1), label.group(2)
                if name == "Paper":
                    self._paper_line(body)
                    continue
                if name in META_LABELS:
                    self.pending_meta.append(_meta_value(name, body))
                    continue
                if name.startswith(ITEM_LABELS):
                    self._flush_meta()
                    # "Why it matters for <industry>" is trimmed back to "Why it
                    # matters": the eyebrow directly above already names the
                    # industry, and repeating it makes the label wrap.
                    self.out.append(_label(_short_label(name)))
                    self.out.append(_para(_inline(body)))
                    continue
                # An unrecognised label still reads correctly -- just without
                # the accent treatment. Add it to ITEM_LABELS to style it.
                self._flush_meta()
                self.out.append(_para(_inline(line)))
                continue

            self._flush_meta()
            if not first_para_done and not self.item_open:
                # The funnel line: scanned / tagged / shortlisted / featured /
                # cost. Set as a band so the numbers read as provenance rather
                # than as the first sentence of the newsletter.
                self.out.append(
                    f'<p style="margin:0 0 18px;padding:11px 14px;'
                    f"background:{CARD_BG};border:1px solid {RULE};"
                    f"border-radius:6px;font-size:12.5px;line-height:1.6;"
                    f'color:{MUTED};">{_inline(line)}</p>'
                )
                first_para_done = True
                continue
            size = "15px" if self.item_open else "14.5px"
            self.out.append(_para(_inline(line), size=size, color=INK))

        if self.in_code:  # unterminated fence: still show the content
            self._code_block()
        self._close_list()
        self._close_item()
        return "\n".join(self.out)


def markdown_to_html(markdown: str) -> str:
    return _Renderer().render(markdown)


def _unsubscribe_blocks(email: EmailConfig) -> tuple[str, str, str]:
    """Return (List-Unsubscribe header value, one-click header, footer HTML).

    The wording adapts to what the value *is*. Printing a bare address and
    expecting people to copy it into a new message is how a list ends up with
    no working opt-out at all.
    """
    value = (email.unsubscribe or "").strip()
    if value.startswith(("http://", "https://")):
        header = f"<{value}>"
        # One-click is only honoured for an HTTPS endpoint that accepts POST.
        one_click = "List-Unsubscribe=One-Click" if value.startswith("https://") else ""
        footer = f'{_link(value, "Unsubscribe")} from this digest.'
        return header, one_click, footer
    if "@" in value:
        return (
            f"<mailto:{value}?subject=unsubscribe>",
            "",
            "Want off the list? Just reply and say so.",
        )
    reply_to = email.sender or email.user or ""
    header = f"<mailto:{reply_to}?subject=unsubscribe>" if reply_to else ""
    return header, "", "Want off the list? Just reply and say so."


def render_html(email: EmailConfig, markdown: str) -> str:
    """Wrap the rendered digest in the email shell."""
    body = markdown_to_html(markdown)
    _, _, unsubscribe = _unsubscribe_blocks(email)
    footer_parts = [
        "Assembled automatically from new arXiv preprints. "
        "Every claim traces to the linked paper, and the readiness scores and "
        "industry fits are model judgements, not peer review.",
        unsubscribe,
    ]
    footer = "<br>".join(part for part in footer_parts if part)
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(email.sender_name or "arXiv Industry Scout")}</title>
</head>
<body style="margin:0;padding:0;background:{PAGE_BG};">
<div style="display:none;max-height:0;overflow:hidden;opacity:0;">
{html.escape(_preheader(markdown))}
</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"
 style="background:{PAGE_BG};">
<tr><td align="center" style="padding:24px 12px;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"
 style="max-width:640px;background:#ffffff;border:1px solid {RULE};border-radius:8px;">
<tr><td style="padding:30px 30px 34px;font-family:{BODY_FONT};color:{INK};
 -webkit-text-size-adjust:100%;">
{body}
<div style="border-top:1px solid {RULE};margin:34px 0 0;padding:16px 0 0;
 font-size:11.5px;line-height:1.6;color:{FAINT};">
{footer}
</div>
</td></tr>
</table>
</td></tr>
</table>
</body>
</html>
"""


def _preheader(markdown: str) -> str:
    """The grey line Gmail shows next to the subject.

    Left to itself it becomes the funnel statistics, which is the least
    interesting sentence in the email. Use the first paper's title instead.
    """
    for line in markdown.split("\n"):
        match = _LINK_RE.search(line)
        if line.startswith("**Paper:**") and match:
            return match.group(1)[:140]
    return ""


def send_digest(
    email: EmailConfig, subject: str, markdown: str, path: Path
) -> bool:
    """Send the digest. Returns False (without raising) if email isn't configured."""
    if not email.enabled:
        log.info("email not configured (SMTP_* absent); skipping")
        return False

    sender = email.sender or email.user or ""
    unsub_header, one_click, _ = _unsubscribe_blocks(email)

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = (
        f"{email.sender_name} <{sender}>" if email.sender_name else sender
    )
    # Addressing, and why it changes with the size of the list.
    #
    # For several recipients the addresses MUST go in Bcc: To:/Cc: would hand
    # every subscriber's address to every other subscriber.
    #
    # But "To: myself, Bcc: everyone" is also the exact shape of a bulk blast,
    # and for a one-person list it additionally makes From and To the same
    # address -- which is what a spoofed phishing message looks like, and is a
    # reliable way to land in Junk. With a single recipient there is nothing to
    # protect, so address them directly and skip Bcc entirely.
    if len(email.recipients) == 1:
        message["To"] = email.recipients[0]
    else:
        message["To"] = sender
        message["Bcc"] = ", ".join(email.recipients)
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = make_msgid(domain=sender.split("@")[-1] or None)
    # List-Unsubscribe is a POSITIVE signal: it is what Gmail and Apple Mail
    # read to show a native Unsubscribe button, and its presence is part of
    # Google's own sender guidance.
    if unsub_header:
        message["List-Unsubscribe"] = unsub_header
    if one_click:
        message["List-Unsubscribe-Post"] = one_click
    # List-Id identifies this as a real mailing list, which is the modern,
    # well-regarded way to say what `Precedence: bulk` used to say.
    #
    # `Precedence: bulk` and `Auto-Submitted` are deliberately NOT set. They are
    # legacy self-demotion: they tell a filter "this is machine-generated bulk
    # mail" and buy nothing in return, and Google's sender guidance explicitly
    # advises against Precedence. Setting them put this digest in Junk.
    if email.list_id:
        message["List-Id"] = email.list_id

    # Plain part first: multipart/alternative means last-wins, so HTML is the
    # preferred alternative and the Markdown is the fallback.
    message.set_content(markdown)
    message.add_alternative(render_html(email, markdown), subtype="html")
    # The Markdown file can ride along, but it is off by default: `text/markdown`
    # is a subtype many filters do not recognise, and an unrecognised attachment
    # on automated mail is a junk signal. The archive copy already exists in the
    # repository and as a workflow artifact, so the attachment is redundant for
    # the normal setup. EMAIL_ATTACH_MARKDOWN=1 puts it back.
    if email.attach_markdown:
        message.add_attachment(
            markdown.encode("utf-8"),
            maintype="text",
            subtype="markdown",
            filename=path.name,
        )

    password = _normalize_password(email.password or "")
    context = _ssl_context()
    log.info(
        "emailing digest to %d recipient(s) via %s:%d",
        len(email.recipients),
        email.host,
        email.port,
    )
    if email.port == 465:
        with smtplib.SMTP_SSL(
            email.host or "", email.port, timeout=60, context=context
        ) as server:
            server.login(email.user or "", password)
            server.send_message(message)
    else:
        with smtplib.SMTP(email.host or "", email.port, timeout=60) as server:
            server.ehlo()
            server.starttls(context=context)
            server.ehlo()
            server.login(email.user or "", password)
            server.send_message(message)
    log.info("digest emailed")
    return True


def preview_path() -> Path | None:
    """Where `--email-preview` should write, if the environment asks for it."""
    value = os.getenv("EMAIL_PREVIEW_PATH")
    return Path(value) if value else None
