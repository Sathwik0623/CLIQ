#!/usr/bin/env python3
"""
tokenizer.py — Splits raw CLI input into clean tokens.

Handles:
  - Whitespace splitting
  - Pipe (|) separation for output modifiers
  - Quoted strings (e.g. descriptions)
  - Trailing-space detection (signals "ready for next arg")

USAGE (standalone test):
  python tokenizer.py
"""

import re
from dataclasses import dataclass


@dataclass
class TokenizedInput:
    raw: str                    # original input
    tokens: list                # command tokens (before any pipe)
    pipe_tokens: list           # tokens after a | (output modifiers)
    has_pipe: bool              # was there a | ?
    trailing_space: bool        # did input end with a space? (ready for next word)
    word_count: int             # number of command tokens


class Tokenizer:
    # Matches quoted strings OR non-whitespace runs
    _TOKEN_RE = re.compile(r'"[^"]*"|\'[^\']*\'|\S+')

    @staticmethod
    def tokenize(user_input: str) -> TokenizedInput:
        raw = user_input
        # Detect trailing space BEFORE stripping (important for ghost text)
        trailing_space = len(user_input) > 0 and user_input[-1] == " "

        stripped = user_input.strip()

        # Split on the FIRST pipe — everything after is output modifiers
        has_pipe = "|" in stripped
        if has_pipe:
            cmd_part, _, pipe_part = stripped.partition("|")
        else:
            cmd_part, pipe_part = stripped, ""

        tokens = Tokenizer._split(cmd_part)
        pipe_tokens = Tokenizer._split(pipe_part)

        return TokenizedInput(
            raw=raw,
            tokens=tokens,
            pipe_tokens=pipe_tokens,
            has_pipe=has_pipe,
            trailing_space=trailing_space,
            word_count=len(tokens),
        )

    @staticmethod
    def _split(text: str) -> list:
        """Split text into tokens, preserving quoted strings."""
        text = text.strip()
        if not text:
            return []
        raw_tokens = Tokenizer._TOKEN_RE.findall(text)
        # Strip surrounding quotes from quoted tokens
        cleaned = []
        for t in raw_tokens:
            if (t.startswith('"') and t.endswith('"')) or \
               (t.startswith("'") and t.endswith("'")):
                cleaned.append(t[1:-1])
            else:
                cleaned.append(t)
        return cleaned


# ---------------------------------------------------------------------------
# Standalone test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    tests = [
        "show ip interface brief",
        "show ip int br ",                       # trailing space
        "show ip route | include 10.0.0.0",      # pipe
        'description "WAN link to HQ"',          # quoted string
        "ping 8.8.8.8",
        "",                                      # empty
        "   ",                                   # whitespace only
        "interface GigabitEthernet0/0",
    ]
    for t in tests:
        result = Tokenizer.tokenize(t)
        print(f"\nInput: {t!r}")
        print(f"  tokens:        {result.tokens}")
        print(f"  pipe_tokens:   {result.pipe_tokens}")
        print(f"  trailing_space:{result.trailing_space}")
        print(f"  word_count:    {result.word_count}")