#!/usr/bin/env python3
"""
doc_parser.py — Core command-reference extractor for the CLIQ ingestion pipeline.

Pipeline:

    read_document() -> pages           (readers.py — format-specific)
        |
    normalize_pages()                  (strip headers/footers/page-nums/TOC noise)
        |
    LayoutStrategy.extract()           (format-AGNOSTIC: lines -> CommandEntry[])
        |
    post-process                       (dedup flags, confidence signals)
        |
    (entries, ParseReport)             (schema.py — the storage-layer contract)

Design goals (per spec):
  - Format-agnostic core: the extractor only ever sees a list of text lines, so
    a new file format is added by writing a reader, not by editing this file.
  - Configurable vocabulary: command verbs, section labels, mode phrases and
    noise patterns all come from parser_config.yaml.
  - Extensible layouts: alternative document layouts register a LayoutStrategy
    via register_layout(); the orchestrator selects one by name.
  - No execution, no network — this module only reads local text.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Optional, Type

import yaml

from cliq.ingest.readers import (
    ReaderError,
    UnsupportedFormatError,
    ReaderDependencyError,
    read_document,
)
from cliq.ingest.schema import (
    CommandEntry,
    ParseReport,
    CONFIDENCE_OK,
    CONFIDENCE_LOW,
)
from cliq.ingest.text_repair import desegment_text, repair_command_text

log = logging.getLogger("ingest.doc_parser")

_PROJECT_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
)
DEFAULT_CONFIG_PATH = os.path.join(_PROJECT_ROOT, "command_db", "parser_config.yaml")

# A description shorter than this many characters is flagged for human review.
_MIN_DESCRIPTION_LEN = 25
# A command-like line is only treated as a heading if it is at most this long;
# real command names are short, prose sentences are not.
_MAX_COMMAND_LINE_LEN = 80
# Prose stopwords that, if present, mean a verb-led line is a sentence (e.g.
# "Clear the counters to reset...") rather than a bare command heading.
_PROSE_MARKERS = (" the ", " to ", " a ", " an ", " is ", " are ", " use ", " that ")

# --- Real-PDF hardening (Phase 1A.1) ------------------------------------------
# Structural-heading words that mark front-matter / part / chapter dividers in
# real Cisco references. A candidate command containing these is NOT a command.
_STRUCTURAL_HEADING_RE = re.compile(
    r"\b(?:part|chapter|section|appendix|contents|index|preface|glossary)\b",
    re.IGNORECASE,
)
# Roman-numeral part labels often glued to a page number, e.g. "221P ART IV"
# (PDF text extraction frequently drops the space inside "PART").
_ROMAN_PART_RE = re.compile(r"\bp\s*art\s+[ivxlcdm]+\b", re.IGNORECASE)
# A trailing page number, optionally preceded by dot leaders, e.g.
# "prefix-list .... 56", "reload 200", "QoS 1221" -> the number is page noise.
_TRAILING_PAGENUM_RE = re.compile(r"(?:\s*\.{2,}\s*|\s+)\d{1,4}\s*$")
# Dot-leader run anywhere in the line (TOC entries: "show ip route ..... 12").
_DOT_LEADER_RE = re.compile(r"\.{4,}")
# A page number fused to a heading word with no space, e.g. "951CHAPTER 9",
# "1223CHAPTER 11", "365CHAPTER 6" — a hallmark of TOC text extraction.
_FUSED_PAGENUM_HEADING_RE = re.compile(r"\d+\s*(?:chapter|part|section)\b", re.IGNORECASE)
# Description intros that prove a real Cisco command definition body follows.
_DESCRIPTION_INTRO_RE = re.compile(
    r"^\s*(?:to\s+\w+|use\s+(?:this|the)\b|specifies?\b|enables?\b|disables?\b|"
    r"configures?\b|displays?\b|sets?\b|clears?\b|removes?\b)",
    re.IGNORECASE,
)

# --- Final noise scrub (Phase 1A.2) -------------------------------------------
# Words that name a configuration-mode CONTEXT or a section rather than a
# standalone command. These mode-entering keywords always require arguments in a
# real command (e.g. "interface type number", "router bgp"), so a BARE
# single-word candidate equal to one of these is a mode/section label — not a
# command — unless it carries a real (argument-bearing) syntax block or a
# command-style description. Extended from config (mode_context_labels:).
_DEFAULT_MODE_CONTEXT_LABELS = {
    "vlan", "interface", "line", "router", "controller", "vrf",
}
# Descriptions that describe a configuration MODE ("Use this mode to configure
# ...", "To exit to global configuration mode ...") instead of a command action.
_MODE_DESC_RE = re.compile(
    r"\buse this mode\b|\bthis mode to\b|\bto (?:exit|return) to\b|"
    r"\bconfiguration\b.*\buse this\b",
    re.IGNORECASE,
)
# A colon followed by a value/note — "minimum mask length: /0" — marks prose,
# not an imperative CLI command.
_NOTE_COLON_RE = re.compile(r"\S\s*:\s+\S")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
@dataclass
class ParserConfig:
    command_verbs: set
    section_headings: set                 # lowercased
    description_sections: set
    syntax_sections: set
    mode_phrases: list                    # (phrase_lower, mode) sorted longest-first
    noise_patterns: list                  # compiled regexes
    mode_context_labels: set = field(default_factory=set)  # bare mode/section labels

    def is_section_heading(self, line: str) -> Optional[str]:
        """Return the canonical (lowercased) heading if `line` is a section
        label, else None. Tolerates a trailing colon."""
        key = line.strip().rstrip(":").lower()
        return key if key in self.section_headings else None


_BUILTIN_CONFIG = {
    "command_verbs": ["show", "configure", "clear", "no", "interface", "router",
                      "ip", "ipv6", "vlan", "debug", "set"],
    "section_headings": ["Syntax", "Syntax Description", "Description",
                         "Command Default", "Command Modes", "Command History",
                         "Usage Guidelines", "Examples", "Related Commands"],
    "description_sections": ["description", "usage guidelines"],
    "syntax_sections": ["syntax", "syntax description"],
    "mode_phrases": {"user exec": "exec", "privileged exec": "enable",
                     "global configuration": "config",
                     "interface configuration": "config-if"},
    "noise_patterns": [r"^\s*\d+\s*$", r".*\.{4,}\s*\d+\s*$"],
    "mode_context_labels": ["vlan", "interface", "line", "router", "controller", "vrf"],
}


def load_parser_config(path: Optional[str] = None) -> ParserConfig:
    """Load parser configuration from YAML, falling back to built-in defaults
    if the file is missing or unreadable (so the module always works)."""
    raw = _BUILTIN_CONFIG
    cfg_path = path or DEFAULT_CONFIG_PATH
    try:
        if os.path.isfile(cfg_path):
            with open(cfg_path, "r", encoding="utf-8") as fh:
                loaded = yaml.safe_load(fh) or {}
            raw = {**_BUILTIN_CONFIG, **loaded}
        else:
            log.warning("Parser config not found at %s; using built-in defaults.", cfg_path)
    except Exception as exc:  # malformed YAML — degrade, don't crash
        log.warning("Failed to load parser config (%s); using defaults.", exc)

    mode_items = sorted(
        ((str(k).lower(), str(v)) for k, v in (raw.get("mode_phrases") or {}).items()),
        key=lambda kv: len(kv[0]),
        reverse=True,
    )
    compiled_noise = []
    for pat in raw.get("noise_patterns") or []:
        try:
            compiled_noise.append(re.compile(pat, re.IGNORECASE))
        except re.error as exc:
            log.warning("Skipping invalid noise pattern %r: %s", pat, exc)

    return ParserConfig(
        command_verbs={str(v).lower() for v in raw.get("command_verbs") or []},
        section_headings={str(h).lower() for h in raw.get("section_headings") or []},
        description_sections={str(s).lower() for s in raw.get("description_sections") or []},
        syntax_sections={str(s).lower() for s in raw.get("syntax_sections") or []},
        mode_phrases=mode_items,
        noise_patterns=compiled_noise,
        mode_context_labels={
            str(w).lower()
            for w in (raw.get("mode_context_labels") or _DEFAULT_MODE_CONTEXT_LABELS)
        },
    )


# ---------------------------------------------------------------------------
# Page normalisation — strip noise BEFORE extraction (layout awareness)
# ---------------------------------------------------------------------------
def normalize_pages(pages: list[str], config: ParserConfig) -> tuple[list[str], int, int, int]:
    """Flatten pages into clean lines, removing headers/footers/page-numbers/TOC.

    Returns (clean_lines, lines_total, lines_skipped, toc_lines_skipped).
    Running headers/footers are detected as identical first/last lines repeated
    across many pages — a layout-aware step that single-line noise regexes
    cannot catch. The Table-of-Contents front matter is detected as a dense run
    of dot-leader / trailing-page-number lines and excluded entirely so its
    entries are never mistaken for command definitions.
    """
    page_line_lists = [[ln.rstrip() for ln in (page or "").splitlines()] for page in pages]
    lines_total = sum(len(pl) for pl in page_line_lists)

    repeated = _detect_running_headers(page_line_lists)

    # First pass: drop running headers/footers and single-line noise.
    pre_clean: list[str] = []
    skipped = 0
    for page_lines in page_line_lists:
        for line in page_lines:
            stripped = line.strip()
            if not stripped:
                continue  # blank lines are structural, not counted as noise
            if stripped in repeated:
                skipped += 1
                continue
            if any(pat.match(stripped) for pat in config.noise_patterns):
                skipped += 1
                continue
            pre_clean.append(line)

    # Second pass: detect and remove the Table-of-Contents region (a contiguous
    # block of TOC-shaped lines near the front of the document).
    toc_span = _detect_toc_region(pre_clean)
    if toc_span is None:
        return pre_clean, lines_total, skipped, 0

    start, end = toc_span
    clean = pre_clean[:start] + pre_clean[end:]
    toc_skipped = end - start
    return clean, lines_total, skipped + toc_skipped, toc_skipped


def _is_toc_shaped(line: str) -> bool:
    """A TOC line either carries dot leaders or ends in a bare page number, and
    is not itself prose. Used to find the contiguous front-matter TOC block."""
    s = line.strip()
    if not s:
        return False
    if _DOT_LEADER_RE.search(s):
        return True
    if _FUSED_PAGENUM_HEADING_RE.search(s) or _ROMAN_PART_RE.search(s):
        return True
    # "<title text> 221" — words followed by a trailing page number, where the
    # leading text is not a sentence (TOC titles are short noun phrases).
    if _TRAILING_PAGENUM_RE.search(s):
        head = _TRAILING_PAGENUM_RE.sub("", s)
        lowered = f" {head.lower()} "
        if not any(m in lowered for m in _PROSE_MARKERS):
            return True
    return False


def _detect_toc_region(lines: list[str]) -> Optional[tuple[int, int]]:
    """Locate the contiguous Table-of-Contents block, if present.

    Scans only the front portion of the document (where a TOC lives) for a run
    in which TOC-shaped lines dominate. Returns the (start, end) half-open index
    range to drop, or None when no TOC region is confidently identified. A few
    interleaved non-TOC lines (chapter titles, blank gaps already removed) are
    tolerated so the block is not split prematurely.
    """
    if not lines:
        return None
    # The TOC is front matter: only consider roughly the first third (capped),
    # so genuine body commands with incidental trailing numbers are never eaten.
    horizon = min(len(lines), max(40, len(lines) // 3))

    first = None
    last = -1
    hits = 0
    misses_since_hit = 0
    for i in range(horizon):
        if _is_toc_shaped(lines[i]):
            if first is None:
                first = i
            last = i
            hits += 1
            misses_since_hit = 0
        elif first is not None:
            misses_since_hit += 1
            # Allow short gaps inside the TOC; abandon the run after a long gap
            # of clearly non-TOC content.
            if misses_since_hit > 5:
                break

    if first is None or hits < 3:
        return None

    span = lines[first : last + 1]
    density = hits / len(span) if span else 0.0
    # Require the block to be genuinely TOC-dominated to avoid false positives.
    if density < 0.6:
        return None
    return (first, last + 1)



def _detect_running_headers(page_line_lists: list[list[str]]) -> set:
    """Find header/footer lines that repeat across pages. Looks at the first 2
    and last 2 non-empty lines of each page; any that appear on >= 40% of pages
    (with at least 3 pages total) are treated as running headers/footers."""
    if len(page_line_lists) < 3:
        return set()
    from collections import Counter

    edge_counts: Counter = Counter()
    for page_lines in page_line_lists:
        non_empty = [ln.strip() for ln in page_lines if ln.strip()]
        edges = non_empty[:2] + non_empty[-2:]
        for edge in set(edges):
            edge_counts[edge] += 1
    threshold = max(2, int(len(page_line_lists) * 0.4))
    return {line for line, count in edge_counts.items() if count >= threshold}


# ---------------------------------------------------------------------------
# Layout strategies — pluggable extraction cores
# ---------------------------------------------------------------------------
class LayoutStrategy:
    """Base class for layout-specific extractors. Subclass and register to
    support a new document layout without touching the orchestrator."""

    name = "base"

    def __init__(self, config: ParserConfig):
        self.config = config

    def extract(self, lines: list[str], platform: str, doc_source: str) -> list[CommandEntry]:
        raise NotImplementedError


class CiscoReferenceLayout(LayoutStrategy):
    """Extractor for the common Cisco command-reference layout:

        <command name>                       <- heading (bare verb-led line)
        To <do x>, use the <command> ... in <mode> mode.   <- intro/description
        <command> [optional args] {choice}   <- syntax line(s)
        Syntax Description                    <- labelled sections follow
        ...
        Command Modes
        Privileged EXEC (#)
    """

    name = "cisco_reference"

    def extract(self, lines: list[str], platform: str, doc_source: str) -> list[CommandEntry]:
        entries, _ = self.extract_with_stats(lines, platform, doc_source)
        return entries

    def extract_with_stats(
        self, lines: list[str], platform: str, doc_source: str
    ) -> tuple[list[CommandEntry], dict]:
        """Extract entries and also report how many heading-like candidates were
        rejected, broken down by reason (deferred-accept: a candidate is only
        kept once it shows evidence of being a real command AND does not match a
        section-label / descriptive-note / empty-heading noise pattern).

        Returns (entries, reasons) where reasons maps a reject-reason string
        ('no_evidence', 'section_label', 'descriptive_note', 'empty_heading') to
        its count.
        """
        entries: list[CommandEntry] = []
        reasons: dict[str, int] = {}
        current: Optional[_Builder] = None

        def commit(builder: Optional["_Builder"]) -> None:
            if builder is None:
                return
            if not builder.has_command_evidence():
                reason = "no_evidence"
            else:
                reason = builder.rejection_reason()
            if reason is None:
                entries.append(builder.finalize())
            else:
                reasons[reason] = reasons.get(reason, 0) + 1

        for idx, line in enumerate(lines):
            stripped = line.strip()
            if not stripped:
                continue

            heading = self.config.is_section_heading(stripped)
            if heading is not None:
                if current is not None:
                    current.section = heading
                    current.saw_section = True
                continue

            if self._is_command_heading(stripped, current):
                commit(current)
                current = _Builder(
                    command=self._normalize_command(stripped),
                    platform=platform,
                    doc_source=doc_source,
                    source_line=idx + 1,
                    config=self.config,
                )
                # A heading that already carries optional args is also a variant.
                if self._has_arg_markers(stripped):
                    current.add_syntax(stripped)
                continue

            if current is None:
                continue  # preamble / TOC text before the first command

            self._route_body_line(current, stripped)

        commit(current)
        return entries, reasons

    # ---- line classification helpers ----
    def _is_command_heading(self, line: str, current: Optional["_Builder"]) -> bool:
        """A heading is a short, verb-led line that is NOT prose, NOT a TOC /
        structural heading, and NOT just a syntax variant of the command already
        being built."""
        if len(line) > _MAX_COMMAND_LINE_LEN:
            return False
        # Reject structural dividers and TOC-shaped lines outright — these are
        # the page-number / PART / CHAPTER artefacts seen in real Cisco PDFs.
        if _STRUCTURAL_HEADING_RE.search(line) or _ROMAN_PART_RE.search(line):
            return False
        if _FUSED_PAGENUM_HEADING_RE.search(line) or _DOT_LEADER_RE.search(line):
            return False
        cleaned = repair_command_text(self._strip_page_number(line))
        tokens = cleaned.split()
        if not tokens:
            return False
        first = tokens[0]
        if first.lower() not in self.config.command_verbs:
            return False
        # Real Cisco CLI command headings are lowercase. A capitalised first
        # token (e.g. "VLAN", "Interface", "Line", "IPv6 ...") is a mode-table
        # label or show-output line lifted from front matter, not a command.
        if first != first.lower():
            return False
        lowered = f" {cleaned.lower()} "
        if any(marker in lowered for marker in _PROSE_MARKERS):
            return False
        if cleaned.rstrip().endswith((".", ":", ",", ";")):
            return False
        # A bracketed/braced line that shares the current command's prefix is a
        # syntax variant, not a new entry.
        if self._has_arg_markers(line) and current is not None:
            base = current.command.split()
            if base and tokens[: len(base)] == base:
                return False
        return True

    @staticmethod
    def _has_arg_markers(line: str) -> bool:
        return any(ch in line for ch in "[]{}<>|")

    @staticmethod
    def _strip_page_number(line: str) -> str:
        """Remove dot leaders and a trailing page number from a TOC-style line,
        e.g. 'prefix-list .... 56' -> 'prefix-list', 'reload 200' -> 'reload'.

        A number is only treated as a page number when it sits at the very end
        of the line. Numbers that are genuine command arguments (e.g. the ACL
        number in 'access-list 10 permit ...') are followed by more tokens and
        are therefore preserved.
        """
        s = _DOT_LEADER_RE.sub(" ", line).rstrip()
        s = _TRAILING_PAGENUM_RE.sub("", s)
        return " ".join(s.split())

    @classmethod
    def _normalize_command(cls, line: str) -> str:
        """The canonical command is the repaired text before any optional-arg marker."""
        line = repair_command_text(cls._strip_page_number(line))
        cut = len(line)
        for ch in "[{<|":
            pos = line.find(ch)
            if pos != -1:
                cut = min(cut, pos)
        return " ".join(line[:cut].split())

    def _route_body_line(self, current: "_Builder", line: str) -> None:
        """Send a non-heading body line to the right field based on the current
        section and the line's shape."""
        section = current.section
        # Syntax variant: explicitly under a syntax section, or a line that
        # repeats the command with optional-arg markers anywhere.
        if section in self.config.syntax_sections or self._is_syntax_variant(current, line):
            current.add_syntax(line)
            return
        if section is None or section in self.config.description_sections:
            current.add_description(line)
            return
        # Other sections (Command Modes, Command History, Examples, ...) are not
        # copied verbatim, but their text is still scanned for a mode phrase.
        current.note_block(line)

    def _is_syntax_variant(self, current: "_Builder", line: str) -> bool:
        if not self._has_arg_markers(line):
            return False
        base = current.command.split()
        tokens = line.split()
        return bool(base) and tokens[: len(base)] == base


# Registry of layout strategies (extensible).
_LAYOUTS: dict[str, Type[LayoutStrategy]] = {
    CiscoReferenceLayout.name: CiscoReferenceLayout,
}


def register_layout(name: str, strategy: Type[LayoutStrategy]) -> None:
    """Register a new layout strategy so it can be selected by name."""
    _LAYOUTS[name] = strategy


def available_layouts() -> list[str]:
    return sorted(_LAYOUTS.keys())


# ---------------------------------------------------------------------------
# Per-entry builder
# ---------------------------------------------------------------------------
@dataclass
class _Builder:
    command: str
    platform: str
    doc_source: str
    source_line: int
    config: ParserConfig
    section: Optional[str] = None
    saw_section: bool = False                    # a labelled Cisco section was seen
    _desc: list = field(default_factory=list)
    _syntax: list = field(default_factory=list)
    _block: list = field(default_factory=list)   # all body text, for mode scan

    def add_description(self, line: str) -> None:
        self._desc.append(line.strip())
        self._block.append(line.strip())

    def add_syntax(self, line: str) -> None:
        variant = repair_command_text(line)
        if variant and variant not in self._syntax:
            self._syntax.append(variant)
        self._block.append(line.strip())

    def note_block(self, line: str) -> None:
        self._block.append(line.strip())

    def has_command_evidence(self) -> bool:
        """True when this candidate shows proof of being a real command
        definition rather than a stray TOC/heading line. Evidence is any of:
        a captured syntax variant, a labelled Cisco section, a description that
        opens with a recognizable intro ("To display...", "Use this command..."),
        or a description of reasonable length.
        """
        if self._syntax or self.saw_section:
            return True
        description = " ".join(part for part in self._desc if part).strip()
        if not description:
            return False
        if _DESCRIPTION_INTRO_RE.search(description):
            return True
        return len(description) >= _MIN_DESCRIPTION_LEN

    def _is_mode_context_label(self) -> bool:
        """True when the command is a BARE single word naming a config-mode
        context / section (vlan, interface, line, router, ...). Multi-word
        commands that merely start with such a word (e.g. 'interface range')
        are real commands and are not labels."""
        tokens = self.command.split()
        return len(tokens) == 1 and tokens[0].lower() in self.config.mode_context_labels

    def _has_real_syntax(self) -> bool:
        """True when a captured syntax variant carries arguments beyond the bare
        command name — the signature of a genuine command definition."""
        base = len(self.command.split())
        return any(len(v.split()) > base for v in self._syntax)

    def rejection_reason(self) -> Optional[str]:
        """Second-stage scrub applied to candidates that already passed the
        evidence gate. Returns a reason string to REJECT prose/labels that are
        not real commands, or None to ACCEPT. Legitimate short commands and
        'no ' negation forms are deliberately preserved.
        """
        command = self.command.strip()
        description = " ".join(part for part in self._desc if part).strip()
        has_syntax = bool(self._syntax)
        has_valid_desc = bool(description) and (
            len(description) >= _MIN_DESCRIPTION_LEN
            or bool(_DESCRIPTION_INTRO_RE.search(description))
        )

        # (1) Bare mode/section-context label (VLAN, Interface, Line, Router...).
        # A real command page for such a word carries an argument-bearing syntax
        # line or a command-style description; a mode table describes the MODE.
        if self._is_mode_context_label():
            if _MODE_DESC_RE.search(description):
                return "section_label"
            if not self._has_real_syntax() and not has_valid_desc:
                return "section_label"

        # (2) Descriptive note / prose: a colon followed by a value/note and no
        # real syntax block to vouch for it being a command.
        if _NOTE_COLON_RE.search(command) and not self._has_real_syntax():
            return "descriptive_note"

        # (3) Empty description + no syntax + looks like a heading/label/note.
        if not description and not has_syntax:
            if (
                self._is_mode_context_label()
                or _NOTE_COLON_RE.search(command)
                or _STRUCTURAL_HEADING_RE.search(command)
            ):
                return "empty_heading"

        return None

    def _detect_mode(self) -> str:
        haystack = " ".join(self._block).lower()
        # PDF text extraction frequently collapses spaces ("globalconfiguration
        # mode"), so also match against a whitespace-stripped haystack.
        squashed = re.sub(r"\s+", "", haystack)
        for phrase, mode in self.config.mode_phrases:
            if phrase in haystack or phrase.replace(" ", "") in squashed:
                return mode
        return "exec"  # default when the document does not say

    def finalize(self) -> CommandEntry:
        description = " ".join(part for part in self._desc if part).strip()
        description = re.sub(r"\s+", " ", description)
        # PDF prose often arrives with word boundaries collapsed
        # ("IPaddressofthemulticastgroup"); rebuild them. Command lines are not
        # routed through here, so command text is never altered.
        description = desegment_text(description, readable=True)
        description = re.sub(r"\s+", " ", description).strip()
        entry = CommandEntry(
            command=self.command,
            description=description,
            syntax_variants=list(self._syntax),
            mode=self._detect_mode(),
            platform=self.platform,
            doc_source=self.doc_source,
            source_line=self.source_line,
        )
        _apply_confidence(entry)
        return entry


def _apply_confidence(entry: CommandEntry) -> None:
    """Attach confidence signals so a human knows which entries to review."""
    desc = entry.description.strip()
    if not desc:
        entry.flags.append("empty_description")
        entry.confidence = CONFIDENCE_LOW
    elif len(desc) < _MIN_DESCRIPTION_LEN:
        entry.flags.append("short_description")
        entry.confidence = CONFIDENCE_LOW
    if _is_suspicious_command(entry.command):
        entry.flags.append("suspicious_command")
        entry.confidence = CONFIDENCE_LOW


def _is_suspicious_command(command: str) -> bool:
    """True when the command text still looks like TOC/heading residue: it ends
    in a bare page number, contains a PART/CHAPTER divider word, or carries a
    page number fused to a heading word (e.g. '951CHAPTER 9')."""
    s = command.strip()
    if not s:
        return False
    if _STRUCTURAL_HEADING_RE.search(s) or _ROMAN_PART_RE.search(s):
        return True
    if _FUSED_PAGENUM_HEADING_RE.search(s):
        return True
    return bool(re.search(r"\s\d{1,4}$", s))


def _flag_duplicates(entries: list[CommandEntry]) -> int:
    """Flag (do NOT drop) repeated commands within the same document. Returns
    the number of entries flagged as duplicates."""
    seen: dict[str, int] = {}
    flagged = 0
    for i, entry in enumerate(entries):
        key = " ".join(entry.command.lower().split())
        if key in seen:
            entry.flags.append("duplicate")
            entry.duplicate_of = seen[key]
            flagged += 1
        else:
            seen[key] = i
    return flagged


# ---------------------------------------------------------------------------
# Orchestrator — the public entry point
# ---------------------------------------------------------------------------
def parse_document(
    path: str,
    platform: str,
    layout: str = CiscoReferenceLayout.name,
    config: Optional[ParserConfig] = None,
) -> tuple[list[CommandEntry], ParseReport]:
    """Parse one command-reference document into structured entries.

    Args:
        path: local file path (.pdf/.docx/.txt).
        platform: platform tag (e.g. 'iosxe', 'iosxr') — the source of truth.
        layout: name of the registered LayoutStrategy to use.
        config: optional pre-loaded ParserConfig (else loaded from YAML).

    Returns (entries, report). Never raises on an unreadable/corrupt file —
    such failures are captured as warnings in the report so the surrounding
    pipeline keeps running.
    """
    doc_source = os.path.basename(path)
    report = ParseReport(doc_source=doc_source, platform=platform)
    cfg = config or load_parser_config()

    strategy_cls = _LAYOUTS.get(layout)
    if strategy_cls is None:
        report.warnings.append(
            f"Unknown layout '{layout}'; valid: {', '.join(available_layouts())}"
        )
        return [], report

    try:
        pages = read_document(path)
    except (UnsupportedFormatError, ReaderDependencyError, ReaderError) as exc:
        log.warning("Skipping '%s': %s", path, exc)
        report.warnings.append(str(exc))
        return [], report
    except Exception as exc:  # last-resort guard — never crash the pipeline
        log.warning("Unexpected error reading '%s': %s", path, exc)
        report.warnings.append(f"Unexpected read error: {exc}")
        return [], report

    if not any((p or "").strip() for p in pages):
        report.warnings.append("Document contained no extractable text (image-only or empty).")
        return [], report

    clean_lines, lines_total, skipped, toc_skipped = normalize_pages(pages, cfg)
    report.lines_total = lines_total
    report.lines_skipped_noise = skipped
    report.toc_lines_skipped = toc_skipped

    strategy = strategy_cls(cfg)
    if isinstance(strategy, CiscoReferenceLayout):
        entries, reasons = strategy.extract_with_stats(clean_lines, platform, doc_source)
    else:
        entries = strategy.extract(clean_lines, platform, doc_source)
        reasons = {}
    report.candidates_rejected = sum(reasons.values())
    report.rejected_no_evidence = reasons.get("no_evidence", 0)
    report.rejected_section_label = reasons.get("section_label", 0)
    report.rejected_descriptive_note = reasons.get("descriptive_note", 0)
    report.rejected_empty_heading = reasons.get("empty_heading", 0)

    report.duplicates_flagged = _flag_duplicates(entries)
    report.entries_extracted = len(entries)
    report.low_confidence = sum(1 for e in entries if e.confidence == CONFIDENCE_LOW)
    report.suspicious_commands = sum(
        1 for e in entries if "suspicious_command" in e.flags
    )
    if not entries:
        report.warnings.append("No command entries were extracted — check layout/verbs config.")
    return entries, report


# ---------------------------------------------------------------------------
# Preview / spot-check
# ---------------------------------------------------------------------------
def format_preview(entries: list[CommandEntry], limit: int = 20) -> str:
    """Render the first `limit` entries as readable text for a quality
    spot-check before bulk ingestion."""
    if not entries:
        return "(no entries to preview)"
    lines: list[str] = []
    shown = entries[:limit]
    for i, e in enumerate(shown, 1):
        flag_str = f"  [{', '.join(e.flags)}]" if e.flags else ""
        lines.append(f"{i:>3}. {e.command}    ({e.mode} | {e.platform}){flag_str}")
        desc = e.description if e.description else "(no description)"
        if len(desc) > 160:
            desc = desc[:157] + "..."
        lines.append(f"     {desc}")
        if e.syntax_variants:
            lines.append(f"     syntax: {e.syntax_variants[0]}")
    if len(entries) > limit:
        lines.append(f"... and {len(entries) - limit} more (showing first {limit}).")
    return "\n".join(lines)
