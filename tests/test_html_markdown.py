"""Offline coverage for bounded HTML-to-Markdown conversion."""
from __future__ import annotations

import pytest

from nexus.tools.builtin import _html_markdown as converter


def test_converts_title_headings_nested_lists_table_code_and_relative_links():
    html = """<!doctype html>
    <html><head><title> A &amp; B </title></head><body>
      <h1>Guide</h1><p>See <a href="../start?q=1&amp;x=2">the start</a>.</p>
      <ul><li>first<ul><li>nested</li></ul></li><li>second</li></ul>
      <table><thead><tr><th>Name</th><th>Value</th></tr></thead>
        <tbody><tr><td>one</td><td>two</td></tr></tbody></table>
      <pre><code>if (x) {\n  y();\n}</code></pre>
    </body></html>"""

    markdown, title = converter.html_to_markdown(html, "https://example.test/a/b/page.html")

    assert title == "A & B"
    assert "# Guide" in markdown
    assert "[the start](https://example.test/a/start?q=1&x=2)" in markdown
    assert "- first\n  - nested" in markdown
    assert "| Name | Value |\n| --- | --- |\n| one | two |" in markdown
    assert "```\nif (x) {\n  y();\n}\n```" in markdown


def test_discards_scripts_comments_chrome_hidden_and_event_attributes():
    html = """<!-- comment text -->
    <nav>navigation secret</nav><header>site chrome</header>
    <aside class="sidebar">side secret</aside><footer>footer secret</footer>
    <div hidden>hidden secret</div><p style="display: none">also secret</p>
    <p aria-hidden="true">aria secret</p><form><p>form secret</p></form>
    <script>alert('script secret')</script><style>.x{}</style>
    <noscript>noscript secret</noscript><iframe>frame secret</iframe>
    <p onclick="alert(1)" onmouseover="bad()">Visible</p>"""

    markdown, _ = converter.html_to_markdown(html, "https://example.test/")

    assert markdown == "Visible"
    for secret in ("secret", "chrome", "alert", "comment", "bad()"):
        assert secret not in markdown


@pytest.mark.parametrize(
    "href",
    ["javascript:alert(1)", "data:text/html,evil", "file:///etc/passwd"],
)
def test_unsafe_or_cross_scheme_links_are_not_emitted(href):
    markdown, _ = converter.html_to_markdown(
        f'<p><a href="{href}">safe label</a></p>', "https://example.test/page"
    )

    assert markdown == "safe label"
    assert "[safe label]" not in markdown


def test_resolves_absolute_http_links_and_sanitizes_controls():
    markdown, _ = converter.html_to_markdown(
        '<p>A\x00B\x01C <a href="https://other.test/a b">link</a> &amp; &#x1f642;</p>',
        "https://example.test/page",
    )

    assert markdown == "ABC [link](https://other.test/a%20b) & 🙂"
    assert "\x00" not in markdown and "\x01" not in markdown


def test_resolves_protocol_relative_http_link_against_final_scheme():
    markdown, _ = converter.html_to_markdown(
        '<a href="//cdn.example.test/item">item</a>', "https://example.test/final"
    )

    assert markdown == "[item](https://cdn.example.test/item)"


def test_malformed_html_is_recovered_and_deep_nesting_is_bounded(monkeypatch):
    monkeypatch.setattr(converter, "MAX_DEPTH", 12)
    markdown, _ = converter.html_to_markdown(
        "<h2>Recovered</h2><p>before <b>bold<p>after"
        + "<div>" * 30
        + "too deep"
        + "</div>" * 30,
        "https://example.test/",
    )

    assert "## Recovered" in markdown
    assert "bold" in markdown and "after" in markdown
    assert "too deep" not in markdown
    assert "[Content truncated]" in markdown


def test_node_input_and_output_limits_are_reported(monkeypatch):
    monkeypatch.setattr(converter, "MAX_NODES", 5)
    monkeypatch.setattr(converter, "MAX_INPUT_CHARS", 100)
    monkeypatch.setattr(converter, "MAX_OUTPUT_CHARS", 24)

    markdown, _ = converter.html_to_markdown(
        "<p>one</p><p>two</p><p>three</p>" + "x" * 200,
        "https://example.test/",
    )

    assert len(markdown) <= 24
    assert markdown.endswith("[Content truncated]")


@pytest.mark.parametrize("base_url", ["javascript:alert(1)", "file:///tmp/a", "/relative"])
def test_requires_absolute_http_base_url(base_url):
    with pytest.raises(ValueError, match=r"absolute HTTP\(S\)"):
        converter.html_to_markdown("<p>text</p>", base_url)


def test_plain_text_and_empty_html_are_supported():
    assert converter.html_to_markdown("plain &amp; simple", "https://example.test/") == (
        "plain & simple",
        "",
    )
    assert converter.html_to_markdown("", "https://example.test/") == ("", "")
