#!/usr/bin/env python3
"""Unit tests for faithful device-error passthrough on '?'-help paths.

When the device answers an invalid '?'-help path with a '%'-prefixed error
(e.g. '% Unrecognized command') instead of an options list, CLIQ must surface
that error verbatim (like CML) — never a blank prompt — and never feed it to
the ghost engine. Valid help (options list) must still be cleaned as before.
"""

from cliq.connection.live_device import _extract_context_help, is_help_error


class TestIsHelpError:
    def test_unrecognized_command(self):
        assert is_help_error("% Unrecognized command\n") is True

    def test_invalid_input(self):
        assert is_help_error("% Invalid input detected at '^' marker.\n") is True

    def test_incomplete_command(self):
        assert is_help_error("% Incomplete command.\n") is True

    def test_ambiguous_command(self):
        assert is_help_error("% Ambiguous command: 'sh c'\n") is True

    def test_options_list_is_not_error(self):
        assert is_help_error("  all   All\n  <cr>\n") is False

    def test_empty_is_not_error(self):
        assert is_help_error("") is False
        assert is_help_error("   \n  \n") is False


class TestErrorPassthrough:
    def test_unrecognized_command_passes_through(self):
        # IOS XE 17.18 (C8k) shape: echoed query, error, redisplayed prompt.
        raw = (
            "C8k#show controller ethernet ?\n"
            "% Unrecognized command\n"
            "C8k#"
        )
        out = _extract_context_help(raw, "show controller ethernet ")
        assert out == "% Unrecognized command"

    def test_invalid_input_passes_through(self):
        # IOSv 15.9 shape with a caret marker line.
        raw = (
            "R1#show ip foo ?\n"
            "                 ^\n"
            "% Invalid input detected at '^' marker.\n"
            "R1#"
        )
        out = _extract_context_help(raw, "show ip foo ")
        assert "% Invalid input detected at '^' marker." in out
        # Only the error survives — no prompt echo, no caret-only line.
        assert "R1#" not in out

    def test_incomplete_command_passes_through(self):
        raw = "R1#ping ?\n% Incomplete command.\nR1#"
        out = _extract_context_help(raw, "ping ")
        assert out == "% Incomplete command."


class TestValidHelpUnaffected:
    def test_options_list_still_cleaned(self):
        raw = (
            "C8k#show controller ?\n"
            "  Ethernet   Ethernet IEEE 802.3\n"
            "  <cr>\n"
            "C8k#show controller "
        )
        out = _extract_context_help(raw, "show controller ")
        assert "Ethernet" in out
        assert "C8k#" not in out
        # A valid options body is NOT an error.
        assert is_help_error(out) is False

    def test_stray_error_dropped_when_options_present(self):
        # A mis-paged valid help can leak a stray '% ...'; with real options
        # present, the stray error is still dropped (no regression).
        raw = (
            "  Ethernet   Ethernet IEEE 802.3\n"
            "% Unrecognized command\n"
            "  <cr>\n"
        )
        out = _extract_context_help(raw, "show controller ")
        assert "Ethernet" in out
        assert "% Unrecognized command" not in out
