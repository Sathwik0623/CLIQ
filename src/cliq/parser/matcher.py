#!/usr/bin/env python3
"""
matcher.py — The command matcher: ties tokenizer + abbreviation + CommandDB.

Given raw user input and the current CLI mode, determines:
  - What command (if any) the user is typing/has typed
  - Whether it's complete, partial, or invalid
  - The best-fit command for suggestions/ghost text

This is the single entry point the UI/intelligence layers call.

USAGE (standalone test):
  python matcher.py   (requires command_db.sqlite + query.py)
"""

import logging
from dataclasses import dataclass, field
from typing import Optional

from cliq.parser.tokenizer import Tokenizer
from cliq.parser.abbreviation import AbbreviationExpander



logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("matcher")


@dataclass
class MatchResult:
    raw_input: str
    mode: str
    expanded_command: str               # tokens expanded to full keywords
    matched_command: object = None      # Command object if a match was found
    match_type: str = "none"            # exact | partial | prefix | none
    suggestions: list = field(default_factory=list)  # candidate Command objects
    expansion_note: list = field(default_factory=list)  # [(orig, expanded), ...]
    is_complete: bool = False           # is this a complete, valid command?


class CommandMatcher:
    def __init__(self, command_db):
        self.db = command_db
        self.expander = AbbreviationExpander(command_db)

    def match(self, user_input: str, mode: str = "exec") -> MatchResult:
        tok = Tokenizer.tokenize(user_input)

        # Empty input → suggest everything valid in this mode
        if not tok.tokens:
            return MatchResult(
                raw_input=user_input,
                mode=mode,
                expanded_command="",
                suggestions=self.db.commands_in_mode(mode),
                match_type="none",
            )

        # Step 1: expand abbreviations word-by-word
        exp = self.expander.expand(tok.tokens)
        expanded_str = " ".join(exp.expanded_tokens)

        # Step 1a: explicit abbreviation lookup. The YAML lets each command
        # declare hand-crafted shortcuts (e.g. 'sh ip dhcp serv stats') that
        # may include non-prefix tokens. If the user's input starts with one,
        # use it directly — this beats word-by-word expansion for shorthands.
        explicit = self._match_explicit_abbrev(user_input, mode)
        if explicit is not None:
            cmd, full_replacement = explicit
            # Re-derive expanded form (replacement + any trailing args).
            return MatchResult(
                raw_input=user_input,
                mode=mode,
                expanded_command=full_replacement,
                matched_command=cmd,
                match_type="exact",
                expansion_note=[],
                is_complete=True,
                suggestions=[cmd],
            )

        # Step 1b: also keep a "soft" expansion where the LAST token (the one
        # the user is still typing) is left as-is. This rescues prefix matching
        # when the expander aggressively picked a non-target keyword for a
        # short prefix, e.g. 'sh r' → 'show route' (no command), where the user
        # actually means 'show running-config'.
        soft_str = expanded_str
        last_user_typed_partial = not user_input.endswith(" ") and len(tok.tokens) > 0
        if last_user_typed_partial and exp.expanded_tokens:
            raw_last = tok.tokens[-1]
            if exp.expanded_tokens[-1].lower() != raw_last.lower():
                soft_str = " ".join(exp.expanded_tokens[:-1] + [raw_last]).strip()

        # Step 2: try exact full-command match (mode-aware)
        exact = None
        for cmd in self.db.commands_in_mode(mode):
            if cmd.full_command.lower() == expanded_str.lower():
                exact = cmd
                break

        if exact:
            return MatchResult(
                raw_input=user_input,
                mode=mode,
                expanded_command=expanded_str,
                matched_command=exact,
                match_type="exact",
                expansion_note=exp.expansions,
                is_complete=True,
                suggestions=[exact],
            )

        # Step 3: prefix matches (still typing) — mode-aware
        prefix_matches = self.db.find_prefix_matches(expanded_str, mode=mode)

        # Step 3b: if the strict expansion found nothing but the user is still
        # typing the last token, retry against the soft expansion. This makes
        # ghost text reactive to every keystroke during abbreviation entry.
        if not prefix_matches and soft_str and soft_str != expanded_str:
            soft_matches = self.db.find_prefix_matches(soft_str, mode=mode)
            if soft_matches:
                prefix_matches = soft_matches
                expanded_str = soft_str

        if prefix_matches:
            # If exactly one prefix match AND input ends mid-word, it's a strong hint
            match_type = "prefix" if len(prefix_matches) >= 1 else "none"
            return MatchResult(
                raw_input=user_input,
                mode=mode,
                expanded_command=expanded_str,
                matched_command=prefix_matches[0] if len(prefix_matches) == 1 else None,
                match_type=match_type,
                suggestions=prefix_matches,
                expansion_note=exp.expansions,
                is_complete=False,
            )

        # Step 4: no prefix match — could be a command WITH arguments
        # Check if expanded_str STARTS WITH a known full command (e.g. "ping 8.8.8.8")
        for cmd in self.db.commands_in_mode(mode):
            cmd_lower = cmd.full_command.lower()
            if expanded_str.lower().startswith(cmd_lower + " ") or \
               expanded_str.lower() == cmd_lower:
                # It's that command plus arguments
                return MatchResult(
                    raw_input=user_input,
                    mode=mode,
                    expanded_command=expanded_str,
                    matched_command=cmd,
                    match_type="exact",
                    expansion_note=exp.expansions,
                    is_complete=True,
                    suggestions=[cmd],
                )

        # Nothing matched
        return MatchResult(
            raw_input=user_input,
            mode=mode,
            expanded_command=expanded_str,
            match_type="none",
            suggestions=[],
            expansion_note=exp.expansions,
        )

    def _match_explicit_abbrev(self, user_input: str, mode: str):
        """
        Try to match the input against the hand-crafted abbreviations declared
        on each command. If the input starts with an exact abbreviation
        (followed by end-of-string or whitespace), return (command, full_form)
        where full_form is `cmd.full_command` plus the user's trailing args.
        """
        text = user_input.strip()
        if not text:
            return None
        text_low = text.lower()
        best = None  # (cmd, full_replacement, abbrev_len)
        for cmd in self.db.commands_in_mode(mode):
            for ab in cmd.abbreviations:
                a = ab.lower().strip()
                if not a:
                    continue
                if text_low == a:
                    if best is None or len(a) > best[2]:
                        best = (cmd, cmd.full_command, len(a))
                elif text_low.startswith(a + " "):
                    trailing = text[len(a):].strip()
                    full = cmd.full_command + (" " + trailing if trailing else "")
                    if best is None or len(a) > best[2]:
                        best = (cmd, full, len(a))
        if best is None:
            return None
        return best[0], best[1]


# ---------------------------------------------------------------------------
# Standalone test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    from cliq.db.query import CommandDB

    db = CommandDB()
    matcher = CommandMatcher(db)

    tests = [
        ("sh ip int br", "exec"),
        ("sh ip", "exec"),
        ("ping 8.8.8.8", "exec"),
        ("conf t", "enable"),
        ("ip add 192.168.1.1 255.255.255.0", "config-if"),
        ("no shut", "config-if"),
        ("int Gig0/0", "config"),
        ("show ip route", "exec"),
        ("ip address", "exec"),       # right command, WRONG mode (should not match in exec)
        ("garbage cmd", "exec"),
    ]

    for inp, mode in tests:
        r = matcher.match(inp, mode)
        print(f"\nInput: {inp!r}  (mode={mode})")
        print(f"  expanded:   {r.expanded_command!r}")
        print(f"  match_type: {r.match_type}")
        print(f"  complete:   {r.is_complete}")
        if r.matched_command:
            print(f"  matched:    {r.matched_command.full_command} "
                  f"[{r.matched_command.risk_class}]")
        if r.expansion_note:
            print(f"  expansions: {r.expansion_note}")
        if len(r.suggestions) > 1:
            print(f"  suggestions: {[c.full_command for c in r.suggestions]}")