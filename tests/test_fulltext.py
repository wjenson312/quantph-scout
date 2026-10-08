"""HTML/PDF extraction and the truncation rule (no network)."""

import pytest
import requests

from scout.fulltext import FullTextError, fetch_fulltext, html_to_text

HTML = """
<html><head><title>T</title><style>body{color:red}</style></head>
<body><nav>skip me</nav><article>
<h1>A Real Paper</h1>
<p>We measured a sensitivity of 9.8 pT.</p>
<math><mi>x</mi></math>
<script>console.log('skip')</script>
<p>Second paragraph.</p>
</article><footer>skip</footer></body></html>
"""


def test_html_to_text_keeps_prose_and_drops_markup():
    text = html_to_text(HTML)
    assert "We measured a sensitivity of 9.8 pT." in text
    assert "Second paragraph." in text
    assert "console.log" not in text
    assert "color:red" not in text
    assert "skip me" not in text  # <nav> dropped


def test_html_to_text_drops_mathml_noise():
    """LaTeXML emits the whole MathML tree; it is mostly markup by weight."""
    assert "<mi>" not in html_to_text(HTML)


class FakeResponse:
    def __init__(self, status_code=200, text="", content=b""):
        self.status_code = status_code
        self.text = text
        self.content = content

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}")


class FakeSession:
    """Returns a queued response per URL prefix and records what was requested."""

    def __init__(self, html=None, pdf=None):
        self._html = html
        self._pdf = pdf
        self.urls = []

    def get(self, url, **kwargs):
        self.urls.append(url)
        if "/html/" in url:
            if isinstance(self._html, Exception):
                raise self._html
            return self._html
        if isinstance(self._pdf, Exception):
            raise self._pdf
        return self._pdf


def _long_html(chars: int) -> str:
    return f"<article><p>{'measured data. ' * (chars // 15)}</p></article>"


def test_html_is_preferred_and_the_pdf_is_never_fetched():
    session = FakeSession(html=FakeResponse(200, _long_html(5000)))
    result = fetch_fulltext("2609.00001", session=session)

    assert result.source == "html"
    assert result.truncated is False
    assert session.urls == ["https://arxiv.org/html/2609.00001"]


def test_a_404_on_html_falls_back_to_the_pdf(tmp_path):
    pdf = _minimal_pdf()
    session = FakeSession(html=FakeResponse(404), pdf=FakeResponse(200, content=pdf))
    with pytest.raises(FullTextError):
        # The minimal PDF has too little text to be useful -- which is itself the
        # behaviour under test: a short extraction is an error, not a silent pass.
        fetch_fulltext("2609.00001", session=session)
    assert any("/pdf/" in url for url in session.urls)


def test_an_html_placeholder_page_falls_back_to_the_pdf():
    placeholder = "<article><p>No HTML for this submission.</p></article>"
    session = FakeSession(
        html=FakeResponse(200, placeholder),
        pdf=FakeResponse(200, content=b"not a pdf"),
    )
    with pytest.raises(FullTextError):
        fetch_fulltext("2609.00001", session=session)
    assert any("/pdf/" in url for url in session.urls)


def test_a_network_error_on_html_falls_back_to_the_pdf():
    session = FakeSession(
        html=requests.ConnectionError("dns"), pdf=FakeResponse(200, content=b"x")
    )
    with pytest.raises(FullTextError):
        fetch_fulltext("2609.00001", session=session)
    assert any("/pdf/" in url for url in session.urls)


def test_text_is_truncated_to_max_chars_and_flagged():
    session = FakeSession(html=FakeResponse(200, _long_html(30_000)))
    result = fetch_fulltext("2609.00001", session=session, max_chars=10_000)

    assert result.truncated is True
    assert result.chars == 10_000
    # The head is kept: title/abstract/intro/methods matter more than references.
    assert result.text.startswith("measured data.")


def test_text_under_the_cap_is_not_flagged_as_truncated():
    session = FakeSession(html=FakeResponse(200, _long_html(5_000)))
    result = fetch_fulltext("2609.00001", session=session, max_chars=100_000)
    assert result.truncated is False


def test_both_sources_failing_raises_rather_than_returning_empty_text():
    session = FakeSession(html=FakeResponse(404), pdf=FakeResponse(500))
    with pytest.raises(FullTextError):
        fetch_fulltext("2609.00001", session=session)


def test_an_explicit_pdf_url_is_used_for_the_fallback():
    session = FakeSession(html=FakeResponse(404), pdf=FakeResponse(200, content=b"x"))
    with pytest.raises(FullTextError):
        fetch_fulltext("2609.00001", "https://example.org/custom.pdf", session=session)
    assert "https://example.org/custom.pdf" in session.urls


def _minimal_pdf() -> bytes:
    return (
        b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
        b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]>>endobj\n"
        b"trailer<</Root 1 0 R>>"
    )
