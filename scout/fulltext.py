"""Fetch a paper's full text: arXiv's LaTeXML HTML if it exists, else the PDF.

arXiv has only rendered HTML for papers submitted since late 2023, and not for
all of those, so the PDF path is a real fallback rather than a safety net.
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass

import requests
from bs4 import BeautifulSoup
from pypdf import PdfReader

log = logging.getLogger("scout.fulltext")

# Elements that carry no prose. Dropping <math> matters: LaTeXML emits the full
# MathML tree for every equation, which is mostly markup by weight.
_DROP_TAGS = ("script", "style", "math", "nav", "footer", "figure")

# arXiv serves a 200 page for papers with no HTML render; it is short and says so.
_NO_HTML_MARKERS = (
    "no html for",
    "html is not available",
    "unavailable for this submission",
)
_MIN_USEFUL_CHARS = 2000


@dataclass
class FullText:
    text: str
    source: str  # 'html' | 'pdf'
    truncated: bool

    @property
    def chars(self) -> int:
        return len(self.text)


class FullTextError(RuntimeError):
    """Neither the HTML nor the PDF could be turned into usable text."""


def html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all(_DROP_TAGS):
        tag.decompose()
    body = soup.find("article") or soup.find("main") or soup.body or soup
    text = body.get_text(separator="\n")
    lines = (line.strip() for line in text.splitlines())
    return "\n".join(line for line in lines if line)


def pdf_to_text(data: bytes) -> str:
    reader = PdfReader(io.BytesIO(data))
    pages = []
    for page in reader.pages:
        try:
            pages.append(page.extract_text() or "")
        except Exception as exc:  # a single unreadable page shouldn't lose the paper
            log.warning("pdf page extraction failed: %s", exc)
    text = "\n".join(pages)
    lines = (line.strip() for line in text.splitlines())
    return "\n".join(line for line in lines if line)


def _looks_like_placeholder(text: str) -> bool:
    if len(text) >= _MIN_USEFUL_CHARS:
        return False
    lowered = text.lower()
    return any(marker in lowered for marker in _NO_HTML_MARKERS) or not text.strip()


def fetch_fulltext(
    arxiv_id: str,
    pdf_url: str | None = None,
    *,
    max_chars: int = 120_000,
    timeout: int = 60,
    user_agent: str = "quantph-scout/0.1",
    session: requests.Session | None = None,
) -> FullText:
    """Return the paper's text, preferring HTML. Raises FullTextError if both fail."""
    session = session or requests.Session()
    headers = {"User-Agent": user_agent}

    text = ""
    source = ""
    html_url = f"https://arxiv.org/html/{arxiv_id}"
    try:
        response = session.get(html_url, headers=headers, timeout=timeout)
        if response.status_code == 200:
            candidate = html_to_text(response.text)
            if _looks_like_placeholder(candidate):
                log.info("%s: HTML page is a placeholder, falling back to PDF", arxiv_id)
            else:
                text, source = candidate, "html"
        else:
            log.info("%s: no HTML (%s), falling back to PDF", arxiv_id, response.status_code)
    except requests.RequestException as exc:
        log.warning("%s: HTML fetch failed (%s), falling back to PDF", arxiv_id, exc)

    if not text:
        url = pdf_url or f"https://arxiv.org/pdf/{arxiv_id}"
        try:
            response = session.get(url, headers=headers, timeout=timeout)
            response.raise_for_status()
            text, source = pdf_to_text(response.content), "pdf"
        except (requests.RequestException, Exception) as exc:
            raise FullTextError(f"{arxiv_id}: PDF fetch/parse failed: {exc}") from exc

    if len(text.strip()) < _MIN_USEFUL_CHARS:
        raise FullTextError(
            f"{arxiv_id}: extracted only {len(text.strip())} chars from {source or 'nothing'}"
        )

    truncated = len(text) > max_chars
    if truncated:
        # Keep the head: title, abstract, intro, methods and most results live
        # there, and the references tail is the least useful part to send.
        text = text[:max_chars]
        log.info("%s: truncated full text to %d chars", arxiv_id, max_chars)
    return FullText(text=text, source=source, truncated=truncated)
