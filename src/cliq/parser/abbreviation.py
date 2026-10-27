#!/usr/bin/env python3
"""
abbreviation.py — Word-aware Cisco abbreviation expansion.

The KEY differentiator: expands each token independently against the set of
valid keywords for the current position, rather than fuzzy-matching the whole
string. This is why "sh in bq" correctly maps toward "show ip ..." not
"show version".

Cisco allows abbreviating any keyword to its shortest unambiguous prefix:
  sh   -> show
  int  -> interface
  br   -> brief
  conf -> configure

USAGE (standalone test):
  python abbreviation.py   (requires command_db.sqlite + query.py)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from cliq.db.query import CommandDB

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("abbreviation")


@dataclass
class ExpansionResult:
    original_tokens: list
    expanded_tokens: list
    expansions: list          # [(original, expanded), ...] for tokens that changed
    ambiguous: list           # tokens that matched >1 keyword (couldn't resolve)
    unresolved: list          # tokens that matched NO keyword


class AbbreviationExpander:
    """
    Expands abbreviated tokens using the keyword vocabulary from the CommandDB.
    Builds a position-aware keyword set so expansion is contextual.
    """

    def __init__(self, command_db):
        self.db = command_db
        # Build the global keyword vocabulary from all command tokens
        self._keywords_by_position = self._build_position_vocab()
        self._all_keywords = self._build_global_vocab()

    def _build_position_vocab(self) -> dict:
        """
        Map position -> set of valid keywords at that position.
        e.g. position 0 -> {show, ping, configure, interface, ...}
             position 1 -> {ip, version, terminal, ...}
        """
        vocab = {}
        for cmd in self.db.commands:
            for i, token in enumerate(cmd.tokens):
                vocab.setdefault(i, set()).add(token.lower())
        return vocab

    def _build_global_vocab(self) -> set:
        """All keywords anywhere (fallback when position-specific fails)."""
        words = set()
        for cmd in self.db.commands:
            for token in cmd.tokens:
                words.add(token.lower())
        return words

    def _candidates_for_position(self, resolved_prefix: list, position: int) -> set:
        """
        Context-aware candidate set for the keyword at `position`, given the
        already-resolved prefix tokens. Mirrors real Cisco IOS behavior:
        after `configure`, only `terminal` is valid at position 1, so `t`
        unambiguously expands to `terminal` (not `trunk`).

        Falls back to the global position-i vocab when no command matches the
        resolved prefix (e.g. user is mid-typo).
        """
        if position == 0:
            return self._keywords_by_position.get(0, set())

        prefix_lower = [t.lower() for t in resolved_prefix[:position]]
        kws = set()
        for cmd in self.db.commands:
            if len(cmd.tokens) <= position:
                continue
            if [t.lower() for t in cmd.tokens[:position]] != prefix_lower:
                continue
            kws.add(cmd.tokens[position].lower())

        if kws:
            return kws
        return self._keywords_by_position.get(position, set())

    def expand_token(self, token: str, position: int, candidates: set | None = None) -> tuple:
        """
        Expand a single token. Returns (expanded_token, status).
        status ∈ {"exact", "expanded", "ambiguous", "unresolved"}

        If `candidates` is provided, restrict prefix matching to that set
        (context-aware). Otherwise fall back to position-vocab + global.
        """
        tok = token.lower()

        if candidates is None:
            candidates = self._keywords_by_position.get(position, set()) | self._all_keywords

        # 1) Exact match — keep as-is
        if tok in candidates:
            return token, "exact"

        # 2) Prefix match (the Cisco abbreviation rule)
        prefix_matches = [kw for kw in candidates if kw.startswith(tok)]

        if len(prefix_matches) == 1:
            return prefix_matches[0], "expanded"
        elif len(prefix_matches) > 1:
            # Ambiguous — only auto-resolve if there's a clearly shortest unique option
            shortest = min(prefix_matches, key=len)
            same_len = [m for m in prefix_matches if len(m) == len(shortest)]
            if len(same_len) == 1:
                return shortest, "expanded"
            return token, "ambiguous"
        else:
            return token, "unresolved"

    def expand(self, tokens: list) -> ExpansionResult:
        """Expand a full list of tokens, word by word, with context awareness."""
        expanded = []
        expansions = []
        ambiguous = []
        unresolved = []

        for i, token in enumerate(tokens):
            candidates = self._candidates_for_position(expanded, i)
            new_tok, status = self.expand_token(token, i, candidates)
            expanded.append(new_tok)
            if status == "expanded":
                expansions.append((token, new_tok))
            elif status == "ambiguous":
                ambiguous.append(token)
            elif status == "unresolved":
                unresolved.append(token)

        return ExpansionResult(
            original_tokens=tokens,
            expanded_tokens=expanded,
            expansions=expansions,
            ambiguous=ambiguous,
            unresolved=unresolved,
        )


# ---------------------------------------------------------------------------
# Standalone test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    from cliq.db.query import CommandDB

    db = CommandDB()
    expander = AbbreviationExpander(db)

    tests = [
        ["sh", "ver"],
        ["sh", "ip", "int", "br"],
        ["conf", "t"],
        ["int", "GigabitEthernet0/0"],
        ["ip", "add"],
        ["no", "shut"],
        ["show", "ip", "route"],     # already full
        ["xyz", "abc"],              # unresolved
    ]

    for tokens in tests:
        result = expander.expand(tokens)
        print(f"\nInput:    {tokens}")
        print(f"Expanded: {result.expanded_tokens}")
        if result.expansions:
            print(f"  Changes: {result.expansions}")
        if result.ambiguous:
            print(f"  ⚠️ Ambiguous: {result.ambiguous}")
        if result.unresolved:
            print(f"  ❌ Unresolved: {result.unresolved}")