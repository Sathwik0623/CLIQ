#!/usr/bin/env python3
"""
engine.py — The CLIQ intelligence orchestrator.

Ties together the full pipeline in the CORRECT order:
  1. Tokenize
  2. Expand abbreviations (sh -> show)
  3. Match against DB (mode-aware)
  4. If no match -> try typo correction on UNRESOLVED tokens only
  5. If still no match but command exists in another mode -> wrong-mode explanation
  6. If matched but missing args -> missing-arg explanation
  7. Produce a single unified Analysis result for the UI

This is the single entry point the UI calls for every keystroke/Enter.

USAGE:
  python -m cliq.intelligence.engine
"""

import hashlib
import logging
from dataclasses import dataclass, field
from typing import Optional

from cliq.parser.tokenizer import Tokenizer
from cliq.parser.matcher import CommandMatcher
from cliq.intelligence.typo_correction import TypoCorrector
from cliq.intelligence.error_explainer import ErrorExplainer, ErrorExplanation
from cliq.intelligence.arg_validator import ArgValidator, ArgValidationError

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("engine")

# Modes from which 'do <exec-cmd>' is valid.
_CONFIG_MODES = {"config", "config-if", "config-router", "config-line", "config-subif", "config-vlan", "config-ext-nacl"}
# Modes whose commands the 'do' prefix can reach.
_DO_TARGET_MODES = {"exec", "enable"}

# Bug 1 — genuine EXEC-only verbs. A 'do <cmd>' suggestion from a config mode is
# only ever correct when the command's HEAD verb is one of these (show/ping/
# write/...). Config-context commands (configure, interface, router, vlan,
# line, ...) live in enable/config and are NEVER run via 'do' — suggesting
# 'do configure terminal' from 'C8k(config)#' is wrong, so they must fall
# through to a plain wrong-mode explanation instead.
_EXEC_ONLY_HEADS = frozenset({
    "show", "ping", "traceroute", "write", "copy", "reload", "clear",
    "debug", "undebug", "telnet", "ssh", "more", "dir", "terminal",
})


def _head_is_exec_only(expanded_command: str) -> bool:
    """True when the command's first token is a genuine EXEC-only verb."""
    parts = (expanded_command or "").split()
    return bool(parts) and parts[0].lower() in _EXEC_ONLY_HEADS

# Mode-navigation commands. These move between CLI modes (or log out) and are
# universally valid regardless of the current mode. They MUST bypass typo
# correction and wrong-mode handling and be passed straight through to the
# device — otherwise 'exit'/'end' get rewritten into a suggestion or rejected
# as "Unknown command". (Bug 2 / Bug 3.)
_PASSTHROUGH_CMDS = {"exit", "end", "quit", "disable", "logout"}


@dataclass
class Analysis:
    raw_input: str
    mode: str
    status: str                       # ok | typo | wrong_mode | wrong_mode_do | missing_arg | invalid_arg | unknown | partial
    expanded_command: str = ""
    matched_command: object = None
    explanation: Optional[ErrorExplanation] = None
    suggestions: list = field(default_factory=list)
    ghost_text: str = ""              # the inline completion to show
    is_complete: bool = False
    pipe_suffix: str = ""             # everything after | (incl. leading ' | ')
    arg_error: Optional[ArgValidationError] = None
    do_suggestion: str = ""           # for wrong_mode_do: 'do <expanded cmd>'
    input_key: str = ""               # stable id of the analyzed input (mode+text)


def compute_input_key(user_input: str, mode: str) -> str:
    """Stable, compact identifier for an analyzed input line.

    The same (normalized text, mode) always yields the same key, so the
    frontend can MATCH-AND-REPLACE the warning panel for the current line
    instead of appending. A different keystroke produces a different key,
    which signals the previous warnings are now stale.
    """
    norm = " ".join(user_input.split())
    digest = hashlib.sha1(f"{mode}\x00{norm}".encode("utf-8")).hexdigest()
    return digest[:12]


class IntelligenceEngine:
    def __init__(self, command_db):
        self.db = command_db
        self.matcher = CommandMatcher(command_db)
        self.corrector = TypoCorrector(command_db)
        self.explainer = ErrorExplainer(command_db)
        self.arg_validator = ArgValidator(command_db)

    def analyze(self, user_input: str, mode: str = "exec", is_executing: bool = False,
               device_type: str = "") -> Analysis:
        """Public entry point. Runs the analysis pipeline and stamps a stable
        `input_key` on the result so each emitted warning event is tied to the
        exact input that produced it (enabling match-and-replace on the UI)."""
        result = self._analyze(user_input, mode, is_executing, device_type)
        result.input_key = compute_input_key(user_input, mode)
        return result

    def _analyze(self, user_input: str, mode: str = "exec", is_executing: bool = False,
               device_type: str = "") -> Analysis:
        # ---- Pipe pass-through: analyze the part before '|', append the rest ----
        pipe_suffix = ""
        pipe_tail_raw = ""        # original text after '|' (preserves user's spacing)
        had_pipe = "|" in user_input
        if had_pipe:
            head, _, tail = user_input.partition("|")
            pipe_tail_raw = tail
            pipe_suffix = " |" + (" " + tail.strip() if tail.strip() else "")
            user_input = head.rstrip()

        # ---- 'do <cmd>' prefix from a config mode targets exec/enable ----
        stripped = user_input.lstrip()
        if mode in _CONFIG_MODES and (stripped == "do" or stripped.startswith("do ")):
            inner = stripped[3:].lstrip() if stripped.startswith("do ") else ""
            if not inner:
                return Analysis(
                    raw_input=user_input + pipe_suffix, mode=mode, status="partial",
                    expanded_command="do", ghost_text=" ",
                )
            # Try exec first, then enable; pick the better outcome.
            best = None
            for try_mode in ("exec", "enable"):
                cand = self._analyze_one(inner, try_mode, is_executing)
                if cand.status == "ok":
                    best = cand
                    break
                if best is None or self._status_priority(cand.status) > self._status_priority(best.status):
                    best = cand
            inner_analysis = best
            inner_analysis.raw_input = user_input + pipe_suffix
            inner_analysis.mode = mode
            if inner_analysis.expanded_command:
                inner_analysis.expanded_command = "do " + inner_analysis.expanded_command
            inner_analysis.pipe_suffix = pipe_suffix
            if had_pipe:
                inner_analysis.ghost_text = self._compute_pipe_ghost(pipe_tail_raw)
            # Soft capability-aware suggestion ranking on the 'do' path too.
            # This never blocks — the device decides what it accepts.
            self._apply_platform_check(inner_analysis, is_executing, device_type)
            return inner_analysis

        # ---- 'do <cmd>' typed in exec/enable mode: 'do' is NOT valid here ----
        # Instead of failing with "Unknown command", suggest dropping 'do' and
        # running the bare command directly (it is already an exec command).
        if mode in _DO_TARGET_MODES and stripped.startswith("do "):
            inner = stripped[3:].lstrip()
            if inner:
                inner_analysis = self._analyze_one(inner, mode, is_executing)
                # Only suggest the drop when the remainder is a real command.
                if inner_analysis.status in ("ok", "partial", "missing_arg"):
                    best_cmd = (
                        inner_analysis.suggestions[0].full_command
                        if inner_analysis.suggestions
                        else (inner_analysis.expanded_command or inner)
                    )
                    return Analysis(
                        raw_input=user_input + pipe_suffix, mode=mode, status="typo",
                        expanded_command=inner_analysis.expanded_command or inner,
                        matched_command=inner_analysis.matched_command,
                        suggestions=inner_analysis.suggestions or [],
                        pipe_suffix=pipe_suffix,
                        explanation=ErrorExplanation(
                            error_type="typo",
                            what="'do' is only used inside configuration mode.",
                            why=f"You are in {mode} mode, where commands run directly.",
                            how=f"In {mode} mode, type '{best_cmd}' directly (no 'do' prefix).",
                        ),
                    )

        result = self._analyze_one(user_input, mode, is_executing)
        result.pipe_suffix = pipe_suffix
        if had_pipe:
            result.ghost_text = self._compute_pipe_ghost(pipe_tail_raw)

        # ---- Soft capability-aware suggestion ranking (never blocks) ----
        self._apply_platform_check(result, is_executing, device_type)
        return result

    def _apply_platform_check(self, result: "Analysis", is_executing: bool,
                              device_type: str) -> None:
        """Apply SOFT, device-capability-aware ranking — never a hard block.

        The device is the source of truth for what is valid: CLIQ must NEVER
        reject a command the device actually accepts. A Cat8000v (IOS XE) both
        routes AND switches, so 'show vlan brief', 'switchport ...' and
        'ip ospf ...' are all valid on it. We therefore no longer set
        status='wrong_device' from a hardcoded platform table.

        Instead, when we have a device capability, we gently re-order the
        suggestion list so commands matching the device's detected capability
        (routing / switching) surface first. This is purely advisory and never
        removes or invalidates any suggestion. Truly invalid input is surfaced
        by the device's own error ('% Invalid input' / '% Unrecognized'),
        optionally enriched with a 'Did you mean?' hint.
        """
        if not device_type or not result.suggestions or len(result.suggestions) < 2:
            return
        try:
            from cliq.validation.platform_rules import command_suits_platform
        except Exception:
            return
        # Stable sort: capability-matching commands first, original order kept
        # within each group. 'both'/neutral commands always count as matching.
        result.suggestions.sort(
            key=lambda c: 0 if command_suits_platform(
                getattr(c, "full_command", "") or "", device_type
            ) else 1
        )

    @staticmethod
    def _status_priority(status: str) -> int:
        # Higher is "more useful" when picking between two attempts.
        return {
            "ok": 100, "partial": 80, "missing_arg": 70, "invalid_arg": 60,
            "typo": 50, "wrong_mode": 40, "wrong_mode_do": 30, "unknown": 0,
        }.get(status, 0)

    def _analyze_one(self, user_input: str, mode: str, is_executing: bool) -> Analysis:
        # Mode-navigation passthrough (exit/end/quit/disable/logout). These are
        # valid in every mode and must NOT be typo-corrected or flagged as
        # wrong-mode — pass them straight through as a complete, ok command.
        nav = user_input.strip().lower()
        if nav in _PASSTHROUGH_CMDS:
            return Analysis(
                raw_input=user_input, mode=mode, status="ok",
                expanded_command=nav, is_complete=True,
            )

        match = self.matcher.match(user_input, mode)

        # CASE A: Exact match in current mode
        if match.match_type == "exact" and match.is_complete:
            cmd = match.matched_command
            tok = Tokenizer.tokenize(user_input)
            expanded_tokens = self.matcher.expander.expand(tok.tokens).expanded_tokens

            missing_exp = self._check_missing_args(cmd, tok.word_count)
            if missing_exp:
                # Still compute ghost text for the command-word portion so the
                # user can see the full command name complete (e.g. "ip a" → "ip address")
                ghost = self._compute_ghost(user_input, cmd.full_command)
                return Analysis(
                    raw_input=user_input, mode=mode, status="missing_arg",
                    expanded_command=match.expanded_command,
                    matched_command=cmd, explanation=missing_exp,
                    ghost_text=ghost,
                )

            # Value-level validation only at execute time (avoids flicker mid-typing).
            if is_executing:
                arg_err = self.arg_validator.validate(cmd, expanded_tokens)
                if arg_err:
                    # Before reporting invalid_arg, check whether the bad token
                    # looks like a keyword typo (e.g. "bq" ≈ "brief").
                    # If so, escalate to typo status with the full corrected command.
                    if arg_err.provided:
                        is_kw_typo, suggested_kw = self._is_keyword_typo(
                            arg_err.provided, len(cmd.tokens), mode
                        )
                        if is_kw_typo:
                            corrected = cmd.full_command + " " + suggested_kw
                            # Build a synthetic CorrectionResult for the explainer
                            from cliq.intelligence.typo_correction import CorrectionResult, TypoSuggestion
                            correction = CorrectionResult(
                                original_tokens=expanded_tokens,
                                corrected_tokens=cmd.tokens + [suggested_kw],
                                corrections=[TypoSuggestion(
                                    original_token=arg_err.provided,
                                    suggested_token=suggested_kw,
                                    distance=1,
                                )],
                                has_corrections=True,
                                corrected_command=corrected,
                            )
                            typo_exp = self.explainer.explain_typo(correction)
                            recheck = self.matcher.match(corrected, mode)
                            suggestions = recheck.suggestions if recheck.match_type in ("exact", "prefix") else [cmd]
                            return Analysis(
                                raw_input=user_input, mode=mode, status="typo",
                                expanded_command=corrected,
                                explanation=typo_exp,
                                suggestions=suggestions,
                            )
                    return Analysis(
                        raw_input=user_input, mode=mode, status="invalid_arg",
                        expanded_command=match.expanded_command,
                        matched_command=cmd, arg_error=arg_err,
                        explanation=ErrorExplanation(
                            error_type="invalid_arg",
                            what=f"Invalid value for '{arg_err.arg_name}': '{arg_err.provided}'",
                            why=arg_err.reason,
                            how=f"Example: {arg_err.example}" if arg_err.example else f"Provide a valid {arg_err.arg_type}",
                            example=arg_err.example,
                        ),
                    )

            return Analysis(
                raw_input=user_input, mode=mode, status="ok",
                expanded_command=match.expanded_command,
                matched_command=cmd, is_complete=True,
                suggestions=[cmd],
                ghost_text=self._compute_ghost(user_input, cmd.full_command),
            )

        # CASE B: Partial/prefix match → still typing, offer ghost text
        if match.match_type == "prefix" and match.suggestions:
            best = match.suggestions[0]
            # Only show ghost text if there is a SINGLE possible next token
            # (avoids misleading the user when many subcommands are available).
            ghost = self._compute_unambiguous_ghost(user_input, match.suggestions)
            return Analysis(
                raw_input=user_input, mode=mode, status="partial",
                expanded_command=match.expanded_command,
                suggestions=match.suggestions,
                ghost_text=ghost,
            )

        # CASE C: command exists in another mode
        wrong_mode_exp = self.explainer.explain_wrong_mode(match.expanded_command, mode)
        if wrong_mode_exp:
            target_cmd = self._find_cmd_anywhere(match.expanded_command)
            target_mode = target_cmd.mode if target_cmd else None
            # Special case: from a config mode -> exec/enable command. Suggest
            # 'do' ONLY for genuine EXEC-only verbs (show/ping/write/...). Bug 1:
            # config-context commands that happen to live in enable/config
            # (e.g. 'configure terminal') must NOT be offered as 'do configure
            # terminal' — they fall through to the plain wrong-mode message.
            if (
                mode in _CONFIG_MODES
                and target_mode in _DO_TARGET_MODES
                and _head_is_exec_only(match.expanded_command)
            ):
                return Analysis(
                    raw_input=user_input, mode=mode, status="wrong_mode_do",
                    expanded_command=match.expanded_command,
                    matched_command=target_cmd,
                    explanation=wrong_mode_exp,
                    do_suggestion="do " + match.expanded_command,
                )
            return Analysis(
                raw_input=user_input, mode=mode, status="wrong_mode",
                expanded_command=match.expanded_command,
                matched_command=target_cmd,
                explanation=wrong_mode_exp,
            )

        # CASE D: typo correction
        tok = Tokenizer.tokenize(user_input)
        exp = self.matcher.expander.expand(tok.tokens)
        correction = self.corrector.correct(exp.expanded_tokens)
        if correction.has_corrections:
            recheck = self.matcher.match(correction.corrected_command, mode)
            if recheck.match_type in ("exact", "prefix"):
                typo_exp = self.explainer.explain_typo(correction)
                return Analysis(
                    raw_input=user_input, mode=mode, status="typo",
                    expanded_command=correction.corrected_command,
                    explanation=typo_exp,
                    suggestions=recheck.suggestions,
                )

        return Analysis(
            raw_input=user_input, mode=mode, status="unknown",
            explanation=self.explainer.explain_unknown(user_input),
        )

    def check_platform_compatibility(self, command_id: str, device_type: str) -> tuple:
        """Return (compatible: bool, reason: str)."""
        cmd = self.db.get_by_id(command_id)
        if not cmd:
            return True, ""
        platforms = getattr(cmd, "platforms", [])
        if not platforms:
            return True, ""  # no restriction
        dt = device_type.lower()
        if any(p.lower() == dt or dt.startswith(p.lower()) for p in platforms):
            return True, ""
        supported = ", ".join(platforms)
        return False, (
            f"Supported on: {supported}. "
            f"Current device type: {device_type or 'unknown'}"
        )

    def _find_cmd_anywhere(self, expanded: str):
        if not expanded:
            return None
        low = expanded.lower()
        for c in self.db.commands:
            if low.startswith(c.full_command.lower()):
                return c
        return None

    def _is_keyword_typo(self, token: str, position: int, mode: str) -> tuple:
        """
        Check if an unrecognised token is a typo of a known keyword at this position.
        Only matches single keyword-style tokens (no dots/colons/digits-heavy strings).
        Returns (is_typo: bool, suggested_keyword: str).

        Also checks same-length prefixes of keywords so that e.g. 'bq' (typo of
        'br') is caught as a near-miss for 'brief' (since 'br' is a prefix of 'brief'
        with distance 1 from 'bq').
        """
        from cliq.intelligence.typo_correction import levenshtein
        # Skip tokens that look like IP addresses, numbers, or long strings
        if any(c in token for c in (".", ":")):
            return False, ""
        if token.isdigit() or (len(token) > 12):
            return False, ""

        tlen = len(token)
        candidates: set[str] = set()
        for cmd in self.db.commands_in_mode(mode):
            if position < len(cmd.tokens):
                candidates.add(cmd.tokens[position].lower())

        # Track (prefix_dist, full_dist, keyword) — lower is better.
        # Using full_dist as tiebreaker ensures 'brief' beats 'binding' for 'bq'
        # (both have prefix_dist 1, but full_dist: 'brief'=4 < 'binding'=6).
        best: str | None = None
        best_eff_d = 3
        best_full_d = 999
        for kw in candidates:
            # Effective distance: min of (prefix match, full match)
            prefix_d = 999
            if len(kw) >= tlen:
                prefix_d = levenshtein(token.lower(), kw[:tlen])
            full_d = levenshtein(token.lower(), kw.lower()) if abs(len(kw) - tlen) <= 4 else 999
            eff_d = min(prefix_d, full_d)
            if eff_d > 2:
                continue
            if eff_d < best_eff_d or (eff_d == best_eff_d and full_d < best_full_d):
                best_eff_d = eff_d
                best_full_d = full_d
                best = kw

        if best and best_eff_d <= 2:
            return True, best
        return False, ""

    @staticmethod
    def _compute_unambiguous_ghost(typed: str, suggestions: list) -> str:
        """
        Ghost text for a PARTIAL match with multiple suggestions.

        Rules:
        - If there is only one suggestion → show full ghost (complete it).
        - If the current (last) word is still being typed (no trailing space):
            show the completion of the CURRENT word only if ALL suggestions
            agree on that word (unambiguous current-word completion).
        - If the user just finished a word (trailing space):
            show the next word only if ALL suggestions agree on the next word.
        - Otherwise return "" (ambiguous — don't mislead).

        This prevents "show ip " from locking ghost into "interface brief"
        when "show ip route" is equally valid.
        """
        if not suggestions:
            return ""

        # Single match — show full completion
        if len(suggestions) == 1:
            return IntelligenceEngine._compute_ghost(typed, suggestions[0].full_command)

        trailing_space = typed.endswith(" ")
        typed_words = [w for w in typed.split(" ") if w]
        n_typed = len(typed_words)

        # Collect the relevant next word from each suggestion
        next_words: list[str] = []
        for cmd in suggestions:
            fw = cmd.full_command.split()
            if trailing_space:
                # User finished a word — look at word at position n_typed
                idx = n_typed
            else:
                # User is mid-word — look at current position (n_typed - 1)
                idx = n_typed - 1
            if idx < len(fw):
                next_words.append(fw[idx].lower())

        if not next_words:
            return ""

        # Unambiguous if all suggestions share the same next word
        if len(set(next_words)) == 1:
            if trailing_space:
                return next_words[0]
            else:
                # Complete the current partial word
                current_partial = typed_words[-1] if typed_words else ""
                target = next_words[0]
                if target.startswith(current_partial.lower()):
                    completion = target[len(current_partial):]
                    return completion
                return ""

        # Ambiguous — compute longest common prefix of all next words
        # Show ghost only up to (but not including) where they diverge
        if not trailing_space:
            current_partial = typed_words[-1] if typed_words else ""
            # Find longest common extension of current_partial across all next words
            candidates = [w for w in next_words if w.startswith(current_partial.lower())]
            if len(candidates) == len(next_words):
                # All agree the current partial belongs here — find longest common prefix
                lcp = candidates[0]
                for c in candidates[1:]:
                    while not c.startswith(lcp):
                        lcp = lcp[:-1]
                    if not lcp:
                        break
                if len(lcp) > len(current_partial):
                    return lcp[len(current_partial):]
        return ""

    @staticmethod
    def _compute_ghost(typed: str, full: str) -> str:
        """
        Compute inline ghost text relative to what the user LITERALLY typed.

        Suggests ONE word at a time (VS Code-style), never the whole command:
          typed='sh'     -> ghost 'ow'          (completes the current word)
          typed='show '  -> ghost 'ip'          (next single word)
          typed='sh ip'  -> ghost ' interface'  (current word done -> next word)

        The ghost, when appended to `typed`, must produce a sensible line.
        """
        typed_words = typed.split(" ")
        full_words = full.split(" ")

        # Track trailing space (user finished a word, starting next)
        trailing_space = typed.endswith(" ")
        if trailing_space:
            # User typed e.g. "show " — ghost ONLY the next single word.
            n_typed = len([w for w in typed_words if w])
            if n_typed < len(full_words):
                return full_words[n_typed]
            return ""

        # No trailing space — complete the CURRENT (last) word only.
        n_typed = len(typed_words)
        if n_typed == 0:
            return full_words[0] if full_words else ""
        if n_typed > len(full_words):
            return ""

        current_word = typed_words[-1]                  # e.g. "i"
        target_word = full_words[n_typed - 1]           # e.g. "ip"

        # The current word must be a prefix of the target word (abbreviation)
        if not target_word.lower().startswith(current_word.lower()):
            return ""

        # Still completing the current word — stop here (word-by-word).
        completion = target_word[len(current_word):]    # "p" for i->ip
        if completion:
            return completion

        # Current word already fully typed — suggest the NEXT single word.
        remaining_words = full_words[n_typed:]          # ["interface", "brief"]
        if remaining_words:
            return " " + remaining_words[0]
        return ""

    # IOS pipe filter keywords. `redirect/append/tee` write output elsewhere;
    # `count` summarizes; `include/exclude/section/begin` filter visible lines.
    _PIPE_KEYWORDS = (
        "include", "exclude", "section", "begin", "count",
        "redirect", "append", "tee",
    )

    def _compute_pipe_ghost(self, tail_raw: str) -> str:
        """
        Compute ghost text for the part typed after '|'.
        `tail_raw` preserves the user's literal spacing after the pipe character.
        Returns the suffix to append to the user's CURRENT typed input so the
        rendered line stays correct visually.
        """
        if not tail_raw:
            # Just '|' typed, nothing after — suggest ' include' as the most
            # common filter (still single keystroke to override).
            return " include"

        # Whether the user is mid-word (no trailing space) vs. between tokens.
        trailing_space = tail_raw.endswith(" ")
        toks = tail_raw.split()

        if not toks:
            # Only whitespace after '|' (e.g. '| '). Suggest 'include'.
            return "include"

        # Only attempt to ghost the FIRST pipe keyword. Whatever follows is a
        # user-supplied pattern/regex that we shouldn't try to autocomplete.
        if len(toks) > 1 or trailing_space:
            return ""

        partial = toks[0].lower()
        matches = [k for k in self._PIPE_KEYWORDS if k.startswith(partial)]
        if len(matches) != 1:
            return ""
        full = matches[0]
        if partial == full:
            return ""
        return full[len(partial):]

    def _check_missing_args(self, cmd, provided_word_count):
        """Check if a matched command is missing required arguments."""
        required = [a for a in cmd.arguments if a.required]
        if not required:
            return None
        # word_count includes the command tokens themselves
        expected = len(cmd.tokens) + len(required)
        if provided_word_count < expected:
            return self.explainer.explain_missing_arg(cmd, provided_word_count)
        return None


# ---------------------------------------------------------------------------
# Standalone test — the FULL pipeline, correct order
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    from cliq.db.query import CommandDB

    db = CommandDB()
    engine = IntelligenceEngine(db)

    tests = [
        ("sh ip int br", "exec"),                      # abbrev → ok
        ("sh ip rotue", "exec"),                       # 'sh' ok, 'rotue' typo → route
        ("shwo verison", "exec"),                      # both typos → show version
        ("sh ip", "exec"),                             # partial → ghost text
        ("ip address", "exec"),                        # wrong mode
        ("ip add 192.168.1.1 255.255.255.0", "config-if"),  # ok with args
        ("ip address", "config-if"),                   # missing args
        ("flibbertigibbet", "exec"),                   # unknown
    ]

    for inp, mode in tests:
        a = engine.analyze(inp, mode)
        print(f"\n{'='*55}")
        print(f"Input: {inp!r}  (mode={mode})")
        print(f"  STATUS: {a.status}")
        if a.expanded_command and a.expanded_command != inp:
            print(f"  expanded: {a.expanded_command}")
        if a.ghost_text:
            print(f"  ghost: {inp}[{a.ghost_text}]")
        if a.explanation:
            e = a.explanation
            print(f"  WHAT: {e.what}")
            print(f"  HOW:  {e.how}")