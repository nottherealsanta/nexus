"""Bounded, offline conversion of untrusted HTML into Markdown.

This module only converts already-fetched text. It performs no network access
and does not interpret converted text as instructions; callers should label its
result as untrusted source content.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import quote, urljoin, urlsplit

__all__ = ["html_to_markdown"]

MAX_INPUT_CHARS = 2_000_000
MAX_NODES = 20_000
MAX_DEPTH = 80
MAX_OUTPUT_CHARS = 250_000
_TRUNCATION_MARKER = "\n\n[Content truncated]"
_VOID_TAGS = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
)
_DROP_TAGS = frozenset(
    {"script", "style", "noscript", "iframe", "form", "nav", "header", "footer", "aside", "svg", "canvas", "template"}
)
_CHROME_ROLES = frozenset({"banner", "navigation", "contentinfo", "complementary"})
_CHROME_WORDS = re.compile(
    r"(?:^|[-_\s])(nav|navigation|navbar|breadcrumb|breadcrumbs|cookie|cookie-banner|header|footer|sidebar|hidden)(?:$|[-_\s])",
    re.IGNORECASE,
)
_BLOCK_TAGS = frozenset(
    {"address", "article", "blockquote", "dd", "div", "dl", "dt", "figcaption", "figure", "h1", "h2", "h3", "h4", "h5", "h6", "hr", "li", "main", "ol", "p", "pre", "section", "table", "ul"}
)
_WS = re.compile(r"\s+")


@dataclass
class _Node:
    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    children: list[_Node | str] = field(default_factory=list)


def _clean_text(value: str) -> str:
    """Remove NUL/control characters while retaining normal Unicode text."""
    return "".join(
        char
        for char in value
        if char in "\t\n\r" or ord(char) >= 32 and not 0x7F <= ord(char) <= 0x9F
    ).replace("\x00", "")


def _is_hidden(attrs: dict[str, str]) -> bool:
    if "hidden" in attrs or attrs.get("aria-hidden", "").strip().lower() == "true":
        return True
    style = re.sub(r"\s+", "", attrs.get("style", "").lower())
    if re.search(r"(?:^|;)display:none(?:!important)?(?:;|$)", style):
        return True
    if re.search(r"(?:^|;)visibility:hidden(?:!important)?(?:;|$)", style):
        return True
    if re.search(r"(?:^|;)visibility:collapse(?:!important)?(?:;|$)", style):
        return True
    if re.search(r"(?:^|;)opacity:0(?:\.0+)?(?:!important)?(?:;|$)", style):
        return True
    if attrs.get("role", "").strip().lower() in _CHROME_ROLES:
        return True
    identifiers = f"{attrs.get('id', '')} {attrs.get('class', '')}"
    return bool(_CHROME_WORDS.search(identifiers))


class _HTMLTreeParser(HTMLParser):
    """Build a tiny bounded tree while discarding unsafe and chrome subtrees."""

    def __init__(self, *, node_limit: int, depth_limit: int) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("root")
        self.stack = [self.root]
        self.node_limit = node_limit
        self.depth_limit = depth_limit
        self.nodes = 1
        self.truncated = False
        self._drop_stack: list[str] = []
        self._depth_skip_stack: list[str] = []
        self._title_depth = 0
        self.title_parts: list[str] = []
        self._title_chars = 0
        self.body_seen = False

    def _add(self, node: _Node | str) -> bool:
        if self.nodes >= self.node_limit:
            self.truncated = True
            return False
        self.nodes += 1
        self.stack[-1].children.append(node)
        return True

    @staticmethod
    def _attributes(attrs: list[tuple[str, str | None]]) -> dict[str, str]:
        # Event handlers and all unrelated attributes are intentionally ignored.
        accepted = {"href", "role", "hidden", "aria-hidden", "style", "id", "class", "start"}
        result: dict[str, str] = {}
        for key, value in attrs:
            key = key.lower()
            if key in accepted and key not in result:
                result[key] = _clean_text(value or "")[:2000]
        return result

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        values = self._attributes(attrs)
        if tag == "body":
            self.body_seen = True
        if self._title_depth:
            if tag not in _VOID_TAGS:
                self._title_depth += 1
            return
        if self._drop_stack:
            if tag not in _VOID_TAGS:
                self._drop_stack.append(tag)
            return
        if self._depth_skip_stack:
            if tag not in _VOID_TAGS:
                self._depth_skip_stack.append(tag)
            return
        if tag == "title":
            self._title_depth = 1
            return
        if tag in _DROP_TAGS or _is_hidden(values):
            if tag not in _VOID_TAGS:
                self._drop_stack.append(tag)
            return
        if tag in {"html", "head", "body", "meta", "link", "base"}:
            return
        if len(self.stack) >= self.depth_limit:
            self.truncated = True
            if tag not in _VOID_TAGS:
                self._depth_skip_stack.append(tag)
            return
        # Retain only fields used by the Markdown writer.
        attrs_kept = {key: value for key, value in values.items() if key in {"href", "start"}}
        node = _Node(tag, attrs_kept)
        if not self._add(node):
            return
        if tag not in _VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag.lower() not in _VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self._title_depth:
            self._title_depth -= 1
            return
        if self._drop_stack:
            if tag in self._drop_stack:
                del self._drop_stack[self._drop_stack.index(tag) :]
            return
        if self._depth_skip_stack:
            if tag in self._depth_skip_stack:
                del self._depth_skip_stack[self._depth_skip_stack.index(tag) :]
            return
        if tag in {"html", "head", "body"}:
            return
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                break

    def handle_data(self, data: str) -> None:
        data = _clean_text(data)
        if not data:
            return
        if self._title_depth:
            remaining = 4000 - self._title_chars
            if remaining > 0:
                part = data[:remaining]
                self.title_parts.append(part)
                self._title_chars += len(part)
            return
        if self._drop_stack or self._depth_skip_stack:
            return
        if self.nodes >= self.node_limit:
            self.truncated = True
            return
        # Coalesce parser chunks to keep both node count and tree size bounded.
        if self.stack[-1].children and isinstance(self.stack[-1].children[-1], str):
            self.stack[-1].children[-1] += data
        else:
            self._add(data)


def _safe_href(href: str, base_url: str) -> str | None:
    href = _clean_text(href).strip()
    if not href or any(ord(char) < 32 for char in href):
        return None
    try:
        resolved = urljoin(base_url, href)
        parsed = urlsplit(resolved)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
            return None
        # Quote spaces and Markdown delimiters without changing URL semantics.
        return quote(resolved, safe=":/?#@!$&'*,;=%+~.-_")
    except (ValueError, UnicodeError):
        return None


def _inline(children: list[_Node | str], base_url: str, *, preserve_ws: bool = False) -> str:
    pieces: list[str] = []
    for child in children:
        if isinstance(child, str):
            text = child if preserve_ws else _WS.sub(" ", child)
            pieces.append(text)
            continue
        tag = child.tag
        content = _render_inline_node(child, base_url, preserve_ws=preserve_ws)
        if tag in {"strong", "b"} and content.strip():
            content = f"**{content.strip()}**"
        elif tag in {"em", "i"} and content.strip():
            content = f"*{content.strip()}*"
        elif tag == "del" and content.strip():
            content = f"~~{content.strip()}~~"
        elif tag == "code" and content:
            fence = "`" * max(1, max((len(m.group()) for m in re.finditer(r"`+", content)), default=0) + 1)
            pad = " " if content.startswith("`") or content.endswith("`") else ""
            content = f"{fence}{pad}{content}{pad}{fence}"
        elif tag == "a":
            href = _safe_href(child.attrs.get("href", ""), base_url)
            if href and content.strip():
                content = f"[{content.strip()}]({href})"
        elif tag == "br":
            content = "  \n"
        pieces.append(content)
    return "".join(pieces)


def _render_inline_node(node: _Node, base_url: str, *, preserve_ws: bool = False) -> str:
    if node.tag == "img":
        return ""
    if node.tag in _DROP_TAGS:
        return ""
    return _inline(node.children, base_url, preserve_ws=preserve_ws)


def _plain(children: list[_Node | str], base_url: str) -> str:
    return _inline(children, base_url).strip()


def _render(node: _Node, base_url: str, *, indent: int = 0) -> str:
    tag = node.tag
    if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
        level = int(tag[1])
        text = _plain(node.children, base_url)
        return f"{'#' * level} {text}\n\n" if text else ""
    if tag == "p":
        text = _plain(node.children, base_url)
        return f"{text}\n\n" if text else ""
    if tag == "pre":
        code = _text_content(node.children).strip("\n")
        if not code:
            return ""
        longest = max((len(match.group()) for match in re.finditer(r"`+", code)), default=0)
        fence = "`" * max(3, longest + 1)
        return f"{fence}\n{code}\n{fence}\n\n"
    if tag in {"ul", "ol"}:
        ordered = tag == "ol"
        try:
            number = max(1, int(node.attrs.get("start", "1")))
        except ValueError:
            number = 1
        output: list[str] = []
        for child in node.children:
            if isinstance(child, _Node) and child.tag == "li":
                marker = f"{number}. " if ordered else "- "
                output.append(_render_list_item(child, base_url, indent, marker))
                number += 1
            elif isinstance(child, _Node):
                output.append(_render(child, base_url, indent=indent))
        return "".join(output) + ("\n" if output else "")
    if tag == "li":
        return _render_list_item(node, base_url, indent, "- ")
    if tag == "blockquote":
        text = _render_children(node.children, base_url).strip()
        return "".join(f"> {line}\n" for line in text.splitlines()) + "\n" if text else ""
    if tag == "hr":
        return "---\n\n"
    if tag == "table":
        return _render_table(node, base_url)
    if tag in _DROP_TAGS:
        return ""
    if tag in {"a", "b", "strong", "i", "em", "del", "s", "code", "br", "img"}:
        return _inline([node], base_url)
    return _render_children(node.children, base_url, indent=indent)


def _text_content(children: list[_Node | str]) -> str:
    """Return literal descendant text for a preformatted code block."""
    return "".join(
        child if isinstance(child, str) else _text_content(child.children)
        for child in children
    )


def _render_list_item(node: _Node, base_url: str, indent: int, marker: str) -> str:
    pieces: list[str] = []
    nested: list[str] = []
    for child in node.children:
        if isinstance(child, _Node) and child.tag in {"ul", "ol"}:
            nested.append(_render(child, base_url, indent=indent + len(marker)))
        elif isinstance(child, _Node) and child.tag in _BLOCK_TAGS:
            pieces.append(_render(child, base_url).strip())
        elif isinstance(child, str):
            pieces.append(_WS.sub(" ", child))
        else:
            pieces.append(_render_inline_node(child, base_url))
    text = _WS.sub(" ", "".join(pieces)).strip()
    prefix = " " * indent + marker
    result = prefix + text + "\n"
    for nested_text in nested:
        result += nested_text
    return result


def _render_children(children: list[_Node | str], base_url: str, *, indent: int = 0) -> str:
    parts: list[str] = []
    for child in children:
        if isinstance(child, str):
            text = _WS.sub(" ", child).strip()
            if text:
                parts.append(text + "\n\n")
        else:
            parts.append(_render(child, base_url, indent=indent))
    return "".join(parts)


def _table_rows(node: _Node, base_url: str) -> list[tuple[bool, list[str]]]:
    rows: list[tuple[bool, list[str]]] = []
    stack = list(reversed(node.children))
    while stack and len(rows) < 500:
        child = stack.pop()
        if not isinstance(child, _Node):
            continue
        if child.tag == "tr":
            cells: list[str] = []
            header = False
            for cell in child.children:
                if isinstance(cell, _Node) and cell.tag in {"td", "th"}:
                    header |= cell.tag == "th"
                    cells.append(_plain(cell.children, base_url).replace("|", r"\|").replace("\n", " "))
            if cells:
                rows.append((header, cells))
        else:
            stack.extend(reversed(child.children))
    return rows


def _render_table(node: _Node, base_url: str) -> str:
    rows = _table_rows(node, base_url)
    if not rows:
        return ""
    width = max(len(cells) for _, cells in rows)
    normalized = [cells + [""] * (width - len(cells)) for _, cells in rows]
    header_index = next((i for i, (is_header, _) in enumerate(rows) if is_header), None)
    if header_index is None:
        header_index = 0
        body = normalized[1:]
    else:
        body = normalized[:header_index] + normalized[header_index + 1 :]
    header = normalized[header_index]
    lines = ["| " + " | ".join(header) + " |", "| " + " | ".join("---" for _ in range(width)) + " |"]
    lines.extend("| " + " | ".join(cells) + " |" for cells in body)
    return "\n".join(lines) + "\n\n"


def _bounded_output(markdown: str, *, truncated: bool) -> str:
    limit = max(0, MAX_OUTPUT_CHARS)
    if len(markdown) <= limit and not truncated:
        return markdown
    marker = _TRUNCATION_MARKER
    if limit <= len(marker):
        return marker[:limit]
    return markdown[: limit - len(marker)].rstrip() + marker


def html_to_markdown(html: str, base_url: str) -> tuple[str, str]:
    """Convert bounded HTML text to ``(markdown, title)`` without network I/O.

    ``base_url`` must be the final HTTP(S) URL after redirects. The input is
    truncated before parsing at :data:`MAX_INPUT_CHARS`; tree depth, node count,
    and final output are also capped. Links with non-HTTP(S) or malformed
    resolved URLs are emitted as their visible text only.

    The returned Markdown and title are untrusted page content, not tool
    instructions. A calling tool should preserve that distinction in its wrapper.
    """
    if not isinstance(html, str):
        raise TypeError("html must be text")
    if not isinstance(base_url, str):
        raise TypeError("base_url must be text")
    try:
        base = urlsplit(base_url)
    except ValueError as exc:
        raise ValueError("base_url must be an absolute HTTP(S) URL") from exc
    if base.scheme.lower() not in {"http", "https"} or not base.netloc:
        raise ValueError("base_url must be an absolute HTTP(S) URL")

    input_truncated = len(html) > MAX_INPUT_CHARS
    parser = _HTMLTreeParser(node_limit=MAX_NODES, depth_limit=MAX_DEPTH)
    parser.feed(html[:MAX_INPUT_CHARS])
    parser.close()
    title = _WS.sub(" ", _clean_text("".join(parser.title_parts))).strip()[:1000]
    markdown = _render_children(parser.root.children, base_url)
    markdown = re.sub(r"\n{3,}", "\n\n", markdown).strip()
    return _bounded_output(markdown, truncated=input_truncated or parser.truncated), title
