#!/usr/bin/env python3
"""
error_explainer.py — The heart of CLIQ: contextual error intelligence.

Replaces Cisco's "% Invalid input detected at '^'" with a human explanation:
  WHAT is wrong, WHY it's wrong, HOW to fix it, and an EXAMPLE.

Takes a MatchResult (from matcher) + the typo/mode context and produces
a structured explanation.

USAGE:
  python -m cliq.intelligence.error_explainer
"""

from dataclasses import dataclass, field


@dataclass
class ErrorExplanation:
    error_type: str            # typo | wrong_mode | missing_arg | invalid_arg | unknown
    what: str                  # what is wrong
    why: str                   # why it's wrong
    how: str                   # how to fix
    example: str = ""          # example of correct usage
    suggested_fix: str = ""    # the corrected command, if applicable
    fix_mode_path: list = field(default_factory=list)  # for wrong-mode auto-fix


class ErrorExplainer:
    def __init__(self, command_db):
        self.db = command_db

    def explain_typo(self, correction_result) -> ErrorExplanation:
        """Explain a typo-based error."""
        corrections = correction_result.corrections
        if not corrections:
            return None
        details = ", ".join(
            f"'{c.original_token}' should be '{c.suggested_token}'"
            for c in corrections
        )
        fixed = correction_result.corrected_command
        return ErrorExplanation(
            error_type="typo",
            what=f"Possible typo detected: {details}",
            why="One or more keywords don't match any valid Cisco command.",
            how=f"Did you mean: {fixed}?",
            example=fixed,
            suggested_fix=fixed,
        )

    def explain_wrong_mode(self, expanded_command: str, current_mode: str) -> ErrorExplanation:
        """
        The command exists, but not in the current mode.
        Find which mode it DOES belong to, and build the path to get there.
        """
        # Find the command in ANY mode
        target_cmd = None
        for cmd in self.db.commands:
            if expanded_command.lower().startswith(cmd.full_command.lower()):
                target_cmd = cmd
                break

        if not target_cmd:
            return None

        target_mode = target_cmd.mode
        path = self._build_mode_path(current_mode, target_mode)

        path_str = " → ".join(step[1] for step in path) if path else "(unknown path)"
        return ErrorExplanation(
            error_type="wrong_mode",
            what=f"'{target_cmd.full_command}' is not valid in {current_mode} mode.",
            why=f"This command belongs to '{target_mode}' mode, but you are in '{current_mode}' mode.",
            how=f"Enter the correct mode first: {path_str}",
            example=target_cmd.examples[0] if target_cmd.examples else target_cmd.full_command,
            fix_mode_path=path,
        )

    def explain_missing_arg(self, matched_cmd, provided_token_count: int) -> ErrorExplanation:
        """Command matched but required arguments are missing."""
        required_args = [a for a in matched_cmd.arguments if a.required]
        if not required_args:
            return None

        missing = []
        for arg in required_args:
            at = self.db.get_arg_type(arg.type)
            example = at.get("example", "<value>") if at else "<value>"
            missing.append(f"{arg.name} (e.g. {example})")

        return ErrorExplanation(
            error_type="missing_arg",
            what=f"'{matched_cmd.full_command}' is missing required argument(s).",
            why=f"This command requires: {', '.join(a.name for a in required_args)}.",
            how=f"Provide: {', '.join(missing)}",
            example=matched_cmd.examples[0] if matched_cmd.examples else "",
        )

    def explain_unknown(self, raw_input: str) -> ErrorExplanation:
        """No match, no typo correction possible."""
        return ErrorExplanation(
            error_type="unknown",
            what=f"Command '{raw_input}' is not recognized.",
            why="It doesn't match any known command, even after typo correction.",
            how="Press '?' to see available commands in this mode.",
        )

    def _build_mode_path(self, from_mode: str, to_mode: str) -> list:
        """
        Walk the mode tree from current mode to target mode.
        Returns list of (prompt_hint, command_to_enter).
        """
        # Build parent chain for target mode
        chain = []
        mode = to_mode
        visited = set()
        while mode and mode not in visited:
            visited.add(mode)
            mode_info = self.db.get_mode(mode)
            if not mode_info:
                break
            chain.append((mode, mode_info.get("enter_command")))
            mode = mode_info.get("parent")
        chain.reverse()  # root → target order

        # Only include steps we're not already past
        # (simple version: emit enter_commands from current toward target)
        path = []
        record = False
        for mode_name, enter_cmd in chain:
            if mode_name == from_mode:
                record = True
                continue
            if record and enter_cmd:
                path.append((mode_name, enter_cmd))
        # If from_mode wasn't in the chain (e.g., exec→config-if), emit full chain enters
        if not path:
            for mode_name, enter_cmd in chain:
                if enter_cmd and mode_name != from_mode:
                    path.append((mode_name, enter_cmd))
        return path


# ---------------------------------------------------------------------------
# Standalone test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    from cliq.db.query import CommandDB
    from cliq.intelligence.typo_correction import TypoCorrector

    db = CommandDB()
    explainer = ErrorExplainer(db)
    corrector = TypoCorrector(db)

    print("=" * 60)
    print("TEST 1: Typo")
    print("=" * 60)
    corr = corrector.correct(["shwo", "verison"])
    exp = explainer.explain_typo(corr)
    if exp:
        print(f"WHAT:    {exp.what}")
        print(f"WHY:     {exp.why}")
        print(f"HOW:     {exp.how}")
        print(f"EXAMPLE: {exp.example}")

    print("\n" + "=" * 60)
    print("TEST 2: Wrong mode (ip address typed in exec)")
    print("=" * 60)
    exp = explainer.explain_wrong_mode("ip address", "exec")
    if exp:
        print(f"WHAT:    {exp.what}")
        print(f"WHY:     {exp.why}")
        print(f"HOW:     {exp.how}")
        print(f"EXAMPLE: {exp.example}")
        print(f"FIX PATH:")
        for mode, cmd in exp.fix_mode_path:
            print(f"   → {cmd}   (enters {mode})")

    print("\n" + "=" * 60)
    print("TEST 3: Missing argument (ip address with no IP)")
    print("=" * 60)
    cmd = db.get_by_id("ip_address")
    exp = explainer.explain_missing_arg(cmd, provided_token_count=2)
    if exp:
        print(f"WHAT:    {exp.what}")
        print(f"WHY:     {exp.why}")
        print(f"HOW:     {exp.how}")
        print(f"EXAMPLE: {exp.example}")

    print("\n" + "=" * 60)
    print("TEST 4: Unknown command")
    print("=" * 60)
    exp = explainer.explain_unknown("flibbertigibbet")
    print(f"WHAT:    {exp.what}")
    print(f"WHY:     {exp.why}")
    print(f"HOW:     {exp.how}")