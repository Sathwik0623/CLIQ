#!/usr/bin/env python3
"""
typo_correction.py — Word-aware Levenshtein typo correction.

When the matcher returns match_type='none', this tries to find the closest
valid keyword for each unrecognized token — WORD BY WORD, not whole-string.
This is why "shwo verison" -> "show version" instead of garbage.

USAGE:
  python -m cliq.intelligence.typo_correction
"""

import logging
from dataclasses import dataclass, field

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("typo")


@dataclass
class TypoSuggestion:
    original_token: str
    suggested_token: str
    distance: int


@dataclass
class CorrectionResult:
    original_tokens: list
    corrected_tokens: list
    corrections: list = field(default_factory=list)   # list[TypoSuggestion]
    has_corrections: bool = False
    corrected_command: str = ""


def levenshtein(a: str, b: str) -> int:
    """Classic edit distance between two strings."""
    a, b = a.lower(), b.lower()
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            cur.append(min(prev[j] + 1,        # deletion
                           cur[j - 1] + 1,     # insertion
                           prev[j - 1] + cost))  # substitution
        prev = cur
    return prev[-1]


class TypoCorrector:
    def __init__(self, command_db, max_distance: int = 2):
        self.db = command_db
        self.max_distance = max_distance
        self._keywords_by_position = self._build_position_vocab()
        self._all_keywords = self._build_global_vocab()

    def _build_position_vocab(self) -> dict:
        vocab = {}
        for cmd in self.db.commands:
            for i, token in enumerate(cmd.tokens):
                vocab.setdefault(i, set()).add(token.lower())
        return vocab

    def _build_global_vocab(self) -> set:
        words = set()
        for cmd in self.db.commands:
            for token in cmd.tokens:
                words.add(token.lower())
        return words

    def correct_token(self, token: str, position: int):
        """Find closest keyword. Returns (corrected, distance) or (token, 0)."""
        tok = token.lower()
        candidates = self._keywords_by_position.get(position, set()) | self._all_keywords

        # Already valid?
        if tok in candidates:
            return token, 0

        # Find closest within max_distance, prefer position-specific
        best = None
        best_dist = self.max_distance + 1
        for kw in candidates:
            # Skip wildly different lengths (perf + accuracy)
            if abs(len(kw) - len(tok)) > self.max_distance:
                continue
            d = levenshtein(tok, kw)
            if d < best_dist:
                best_dist = d
                best = kw

        if best is not None and best_dist <= self.max_distance:
            return best, best_dist
        return token, -1   # -1 = no correction found

    def correct(self, tokens: list) -> CorrectionResult:
        corrected = []
        corrections = []

        for i, token in enumerate(tokens):
            # Don't try to correct tokens that look like arguments (IPs, interfaces)
            if self._looks_like_argument(token):
                corrected.append(token)
                continue

            new_tok, dist = self.correct_token(token, i)
            corrected.append(new_tok)
            if dist > 0:
                corrections.append(TypoSuggestion(token, new_tok, dist))

        return CorrectionResult(
            original_tokens=tokens,
            corrected_tokens=corrected,
            corrections=corrections,
            has_corrections=len(corrections) > 0,
            corrected_command=" ".join(corrected),
        )

    @staticmethod
    def _looks_like_argument(token: str) -> bool:
        """Heuristic: don't typo-correct things that look like args."""
        # Contains digits, dots, slashes, colons → likely IP/interface/arg
        return any(c in token for c in ".:/0123456789")


# ---------------------------------------------------------------------------
# Standalone test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    from cliq.db.query import CommandDB

    db = CommandDB()
    corrector = TypoCorrector(db)

    tests = [
        ["shwo", "verison"],          # show version
        ["sh", "ip", "rotue"],        # show ip route
        ["confgiure", "terminal"],    # configure terminal
        ["ip", "adress"],             # ip address
        ["show", "version"],          # already correct
        ["ping", "8.8.8.8"],          # arg, don't touch
        ["zzzzz"],                    # unfixable
    ]

    for tokens in tests:
        r = corrector.correct(tokens)
        print(f"\nInput:     {tokens}")
        print(f"Corrected: {r.corrected_tokens}")
        if r.has_corrections:
            for c in r.corrections:
                print(f"  '{c.original_token}' -> '{c.suggested_token}' (distance {c.distance})")
        else:
            print("  (no corrections)")