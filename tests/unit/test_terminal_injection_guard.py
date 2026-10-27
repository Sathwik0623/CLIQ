"""Guard against advisory content leaking into the device terminal.

The frontend has no JS test runner (see frontend/package.json — no
jest/vitest configured), so this asserts directly against the TypeScript
source: the `intel_answer` WS handler in useTerminalSessions.ts must dispatch
ONLY to the advisory panel (setIntelItems) and must never call
`term.write`/`session.term.write`, and the `⚡ CLIQ:` marker (the reported
injection signature) must not appear anywhere in that handler's block.
"""
from __future__ import annotations

from pathlib import Path

_SRC = (
    Path(__file__).resolve().parents[2]
    / "frontend" / "src" / "lib" / "useTerminalSessions.ts"
)


def _extract_handler_block(source: str, marker: str) -> str:
    """Return the brace-balanced `if (msg.type === '<marker>') { ... }` block."""
    needle = f"if (msg.type === '{marker}') {{"
    start = source.index(needle)
    brace_start = source.index("{", start)
    depth = 0
    i = brace_start
    for i in range(brace_start, len(source)):
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
            if depth == 0:
                return source[brace_start:i + 1]
    raise AssertionError(f"unbalanced braces while extracting {marker!r} handler")


class TestIntelAnswerNeverWritesToTerminal:
    def setup_method(self):
        self.source = _SRC.read_text(encoding="utf-8")
        self.block = _extract_handler_block(self.source, "intel_answer")

    def test_handler_exists_and_dispatches_to_panel(self):
        assert "setIntelItems" in self.block

    def test_handler_never_calls_term_write(self):
        assert "term.write" not in self.block

    def test_handler_never_emits_the_cliq_marker(self):
        assert "CLIQ:" not in self.block
        assert "\u26a1" not in self.block

    def test_handler_holds_no_terminal_reference(self):
        # No `session`/`term` variable at all — the write is structurally
        # impossible to reintroduce without first wiring in a new terminal
        # access point (which this test would then also need updating to see).
        assert "session.term" not in self.block
        assert "const term" not in self.block
