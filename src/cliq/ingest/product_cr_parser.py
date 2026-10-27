#!/usr/bin/env python3
"""
product_cr_parser.py — Targeted DITA-HTML extraction for Cisco product Command
References (Catalyst 9500 IOS XE 17.16.x style command_reference books).

This is a SEPARATE parser from the MCL flat-list ingester — product Command
References are static DITA-authored HTML (book TOC + per-command topic
articles), a different structure entirely from the MCL's flat page.

Two entry points:
  - :func:`discover_toc`  — parse ``<ul id="bookToc">`` into ordered
    (title, href) chapter links.
  - :func:`parse_chapter` — parse ONE fetched chapter page into structured
    :class:`RawCommand` records: title (``topictitle``), syntax (the formal
    Syntax section — ``section.refsyn`` > ``<p class="figgroup synblk">``,
    preferring the affirmative form over the ``no`` form), description (first
    ``<p class="p">``), mode (Command Mode Summary table), example
    (illustrative-only text from ``section.example`` > ``<pre
    class="codeblock">``, kept separate and NEVER used as the command itself).

    A DITA command topic's ``pre.codeblock`` almost always lives inside its
    Examples section — a multi-line config SESSION with embedded device
    prompts (``device(config)#...``), not the command's formal syntax. Using
    that codeblock as "the command" (the original bug here) produces whole
    config sessions instead of individual commands. The Syntax section
    (``refsyn``) is the authoritative source for syntax; Examples are
    illustrative only.

Pure / dependency-free: uses only the stdlib ``html.parser``. No network I/O —
fetching is the CALLER's job (``cliq.ingest.product_cr_ingest``), so this
module is trivially unit-testable against inline HTML fixtures.

Boilerplate is dropped by SCOPE, not by blacklist: extraction only ever looks
inside ``#pageContentDiv`` / ``<article class="topic">`` elements, so nav,
header, footer, the TOC itself, feedback widgets and any ad/tracker script
(doubleclick, 2o7.net, analytics, ...) living outside that scope are never
visited — and this module never fetches a URL, so such a script is never
executed or requested either.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Callable, Optional

# ---------------------------------------------------------------------------
# Minimal, dependency-free DOM: HTMLParser + a stack of _Elem builds a real
# (if lossy) tree so extraction can use text()/find()/find_all() instead of
# fragile regex-over-tags. Robust to unbalanced HTML: a stray/unmatched close
# tag is resolved by searching the open-element stack from the top.
# ---------------------------------------------------------------------------
_VOID_ELEMENTS = frozenset({
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
})
#: Elements whose text is never real content (skipped by Elem.text()).
_NON_CONTENT_TAGS = frozenset({"script", "style"})


class _Elem:
    __slots__ = ("tag", "attrs", "children", "parent")

    def __init__(self, tag: str, attrs: dict, parent: Optional["_Elem"] = None):
        self.tag = tag
        self.attrs = attrs
        self.children: list = []          # list[_Elem | str]
        self.parent = parent

    def text(self) -> str:
        parts: list[str] = []
        for c in self.children:
            if isinstance(c, str):
                parts.append(c)
            elif c.tag not in _NON_CONTENT_TAGS:
                parts.append(c.text())
        return "".join(parts)

    def find_all(self, pred: Callable[["_Elem"], bool]) -> list["_Elem"]:
        out: list[_Elem] = []
        for c in self.children:
            if isinstance(c, _Elem):
                if pred(c):
                    out.append(c)
                out.extend(c.find_all(pred))
        return out

    def find(self, pred: Callable[["_Elem"], bool]) -> Optional["_Elem"]:
        for c in self.children:
            if isinstance(c, _Elem):
                if pred(c):
                    return c
                found = c.find(pred)
                if found is not None:
                    return found
        return None

    def is_descendant_of(self, ancestor: "_Elem") -> bool:
        cur = self.parent
        while cur is not None:
            if cur is ancestor:
                return True
            cur = cur.parent
        return False


class _DomBuilder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Elem("#root", {})
        self._stack = [self.root]

    def _push(self, tag: str, attrs) -> _Elem:
        el = _Elem(tag, dict(attrs), parent=self._stack[-1])
        self._stack[-1].children.append(el)
        return el

    def handle_starttag(self, tag, attrs):
        el = self._push(tag, attrs)
        if tag not in _VOID_ELEMENTS:
            self._stack.append(el)

    def handle_startendtag(self, tag, attrs):
        self._push(tag, attrs)

    def handle_endtag(self, tag):
        for i in range(len(self._stack) - 1, 0, -1):
            if self._stack[i].tag == tag:
                del self._stack[i:]
                break

    def handle_data(self, data):
        self._stack[-1].children.append(data)


def _parse_dom(html: str) -> _Elem:
    b = _DomBuilder()
    b.feed(html)
    return b.root


def _classes(el: _Elem) -> set[str]:
    return set((el.attrs.get("class") or "").split())


def _has_class(el: _Elem, cls: str) -> bool:
    return cls in _classes(el)


def _class_contains(el: _Elem, needle: str) -> bool:
    return needle in (el.attrs.get("class") or "")


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


# ---------------------------------------------------------------------------
# TOC crawl
# ---------------------------------------------------------------------------
@dataclass
class TocEntry:
    """One book-TOC chapter link."""

    title: str
    href: str


def discover_toc(html: str) -> list[TocEntry]:
    """Parse ``<ul id="bookToc">`` into ordered, deduplicated chapter links."""
    root = _parse_dom(html)
    toc = root.find(lambda e: e.tag == "ul" and e.attrs.get("id") == "bookToc")
    if toc is None:
        return []
    entries: list[TocEntry] = []
    seen: set[str] = set()
    for a in toc.find_all(lambda e: e.tag == "a" and bool(e.attrs.get("href"))):
        href = (a.attrs.get("href") or "").strip()
        title = _clean(a.text())
        if not href or not title or href in seen:
            continue
        seen.add(href)
        entries.append(TocEntry(title=title, href=href))
    return entries


# ---------------------------------------------------------------------------
# Chapter (per-command) extraction
# ---------------------------------------------------------------------------
#: Cisco DITA -> CLIQ canonical mode vocabulary (same names doc_parser uses).
_MODE_PHRASES: tuple[tuple[str, str], ...] = (
    ("privileged exec", "enable"),
    ("user exec", "exec"),
    ("global configuration", "config"),
    ("subinterface configuration", "config-subif"),
    ("interface configuration", "config-if"),
    ("line configuration", "config-line"),
    ("vlan configuration", "config-vlan"),
    ("router configuration", "config-router"),
)


def _canonical_mode(raw_mode: str) -> str:
    norm = _clean(raw_mode).lower()
    for phrase, mode in _MODE_PHRASES:
        if phrase in norm:
            return mode
    return ""


@dataclass
class RawCommand:
    """One command extracted from a product-CR chapter, pre-tagging."""

    title: str = ""
    syntax: str = ""
    description: str = ""
    mode: str = ""
    example: str = ""   # illustrative only — never stored as the command


def _content_root(root: _Elem) -> _Elem:
    """Scope extraction to the article content — nav/header/footer/TOC/feedback
    widgets living outside this element are structurally excluded."""
    content = root.find(lambda e: e.attrs.get("id") == "pageContentDiv")
    return content if content is not None else root


#: Leading device-prompt token per line, e.g. ``device(config)#``,
#: ``Router>``, ``Switch(config-router-lisp-inst)#``. Applied only to
#: illustrative Example/bare-codeblock text — the formal Syntax section never
#: contains prompts in the first place.
_PROMPT_LINE_RE = re.compile(r"^\s*[A-Za-z][\w.\-]*(?:\([^)]*\))?[#>]\s*")


def _strip_prompts(text: str) -> str:
    return "\n".join(_PROMPT_LINE_RE.sub("", ln) for ln in text.splitlines())


def _example_section(topic: _Elem) -> Optional[_Elem]:
    return topic.find(lambda e: _has_class(e, "example"))


def _refsyn_section(topic: _Elem) -> Optional[_Elem]:
    """The Syntax section (``section.refsyn``) — authoritative source of a
    command's formal syntax, as opposed to the Examples section."""
    return topic.find(lambda e: _class_contains(e, "refsyn"))


def _bare_syntax_codeblock(topic: _Elem, example: Optional[_Elem]) -> Optional[_Elem]:
    """A ``<pre class="codeblock">`` used directly AS the syntax — only
    relevant for the rare topic with no ``refsyn`` section at all. Codeblocks
    living inside the Examples section are illustrative sessions, never
    syntax, and are excluded here."""
    for pre in topic.find_all(lambda e: e.tag == "pre" and _class_contains(e, "codeblock")):
        if example is not None and pre.is_descendant_of(example):
            continue
        return pre
    return None


def _command_topics(content_root: _Elem) -> list[_Elem]:
    """Each per-command DITA topic: an ``<article class="topic ...">`` that
    defines an actual command — identified by a Syntax section (``refsyn``)
    or, failing that, a codeblock that is not inside an Examples section. A
    codeblock ALONE is not sufficient: that also matches topics whose only
    content is an illustrative Examples session."""
    articles = content_root.find_all(
        lambda e: e.tag == "article" and _class_contains(e, "topic")
    )
    out = []
    for a in articles:
        example = _example_section(a)
        if _refsyn_section(a) is not None or _bare_syntax_codeblock(a, example) is not None:
            out.append(a)
    return out


def _extract_title(topic: _Elem) -> str:
    el = topic.find(lambda e: _class_contains(e, "topictitle"))
    return _clean(el.text()) if el is not None else ""


def _extract_syntax(topic: _Elem) -> str:
    """Formal syntax from the Syntax section (``refsyn`` ->
    ``<p class="figgroup synblk">``), preferring the affirmative form over
    the ``no`` form. Falls back to a bare (non-Example) codeblock only when
    no Syntax section exists."""
    refsyn = _refsyn_section(topic)
    if refsyn is not None:
        forms = refsyn.find_all(lambda e: e.tag == "p" and _has_class(e, "figgroup"))
        affirmative = next(
            (f for f in forms if not _clean(f.text()).lower().startswith("no ")), None
        )
        form = affirmative or (forms[0] if forms else None)
        if form is not None:
            return _clean(_strip_prompts(form.text()))
    pre = _bare_syntax_codeblock(topic, _example_section(topic))
    return _clean(_strip_prompts(pre.text())) if pre is not None else ""


def _extract_description(topic: _Elem, exclude: list) -> str:
    """First body ``<p class="p">``, skipping any paragraph that lives inside
    the Syntax or Examples sections (arg tables / example intros)."""
    for p in topic.find_all(lambda e: e.tag == "p" and _has_class(e, "p")):
        if any(root is not None and p.is_descendant_of(root) for root in exclude):
            continue
        text = _clean(p.text())
        if text:
            return text
    return ""


def _extract_example(topic: _Elem, example: Optional[_Elem]) -> str:
    """Illustrative-only text from the Examples section — never the command."""
    if example is None:
        return ""
    pre = example.find(lambda e: e.tag == "pre" and _class_contains(e, "codeblock"))
    return _clean(_strip_prompts(pre.text())) if pre is not None else ""


def _extract_mode(topic: _Elem) -> str:
    """Mode from the "Command Mode Summary" table, if present.

    Cisco DITA tables vary (caption vs. preceding heading vs. thead text), so
    this matches loosely: any table whose own text mentions "mode" is treated
    as the mode-summary table, and the mode name is read from the first data
    row's first cell (conventionally "Mode" is column 1).
    """
    for table in topic.find_all(lambda e: e.tag == "table"):
        if "mode" not in table.text().lower():
            continue
        rows = table.find_all(lambda e: e.tag == "tr")
        for row in rows:
            cells = row.find_all(lambda e: e.tag in ("td", "th"))
            if not cells or cells[0].tag == "th":
                continue
            mode = _canonical_mode(cells[0].text())
            if mode:
                return mode
    return ""


def parse_chapter(html: str) -> list[RawCommand]:
    """Extract every command topic from one fetched chapter page.

    Only ``#pageContentDiv`` is scanned — nav/header/footer/TOC/feedback
    widgets and any script content are never visited.
    """
    root = _parse_dom(html)
    content = _content_root(root)
    out: list[RawCommand] = []
    for topic in _command_topics(content):
        syntax = _extract_syntax(topic)
        if not syntax:
            continue
        example_section = _example_section(topic)
        refsyn_section = _refsyn_section(topic)
        out.append(RawCommand(
            title=_extract_title(topic),
            syntax=syntax,
            description=_extract_description(topic, [example_section, refsyn_section]),
            mode=_extract_mode(topic),
            example=_extract_example(topic, example_section),
        ))
    return out
