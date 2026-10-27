#!/usr/bin/env python3
"""Unit tests for cliq.intelligence.did_you_mean (after-Enter correction).

All device I/O is injected via a fake ``help_lookup`` so these tests are pure
and offline. They assert the advisory-only behaviour: suggest the closest valid
token, drop an extraneous trailing word, or list options — never re-run.
"""

from cliq.intelligence.did_you_mean import (
    DidYouMean,
    analyze_device_error,
    best_matches,
    build_suggestion,
    locate_bad_token,
    output_has_error,
    parse_help_options,
)


# Realistic IOS error blocks (echoed command line already removed by the page
# cleaner, mirroring what reaches the server). Caret columns are built precisely
# so the '^' lands on the offending token (prompt 'R1#' is 3 chars).
# "R1#show running-config run" -> 'run' starts at command index 20 -> col 23.
_RUN_ERR = " " * 23 + "^\n% Invalid input detected at '^' marker.\n"
# "R1#show ip rout" -> 'rout' starts at command index 8 -> col 11.
_ROUT_ERR = " " * 11 + "^\n% Invalid input detected at '^' marker.\n"


class TestErrorDetection:
    def test_error_marker_detected(self):
        assert output_has_error(_RUN_ERR) is True

    def test_clean_output_not_error(self):
        assert output_has_error("Building configuration...\n") is False

    def test_empty_is_not_error(self):
        assert output_has_error("") is False


class TestHelpParsing:
    def test_parses_keywords_and_cr(self):
        page = (
            "  all          All queuing\n"
            "  interface    Show interface configuration\n"
            "  <cr>\n"
        )
        opts, has_cr = parse_help_options(page)
        assert opts == ["all", "interface"]
        assert has_cr is True

    def test_skips_non_keyword_rows(self):
        page = (
            "  A.B.C.D      IP address\n"
            "  route        Routing table\n"
        )
        opts, has_cr = parse_help_options(page)
        assert "route" in opts
        assert has_cr is False

    def test_deduplicates(self):
        page = "  route  x\n  route  y\n"
        opts, _ = parse_help_options(page)
        assert opts == ["route"]


class TestFuzzyMatch:
    def test_prefix_match_ranks_first(self):
        assert best_matches("rout", ["route", "router", "redistribute"])[0] == "route"

    def test_edit_distance_match(self):
        assert "route" in best_matches("rute", ["route", "interface"])

    def test_no_close_match_returns_empty(self):
        assert best_matches("zzzz", ["route", "interface"]) == []


class TestLocateBadToken:
    def test_caret_points_at_trailing_word(self):
        # prompt 'R1#' (len 3); caret col 23 -> char 20 into the command,
        # which is the start of 'run' (token index 2).
        cmd = "show running-config run"
        prefix, bad, idx = locate_bad_token(cmd, _RUN_ERR, prompt="R1#")
        assert bad == "run"
        assert prefix == "show running-config"
        assert idx == 2

    def test_fallback_to_last_token_without_caret(self):
        prefix, bad, idx = locate_bad_token("show ip rout", "% Invalid input\n", prompt="R1#")
        assert bad == "rout"
        assert prefix == "show ip"


class TestBuildSuggestion:
    def test_replaces_with_closest_option(self):
        s = build_suggestion("show ip", "rout", ["route", "interface"], has_cr=False)
        assert isinstance(s, DidYouMean)
        assert s.fix == "show ip route"
        assert "show ip route" in s.message

    def test_drops_trailing_word_when_prefix_complete(self):
        # No close option, but <cr> means the prefix alone is a valid command.
        s = build_suggestion("show running-config", "run", ["all", "interface"], has_cr=True)
        assert s.fix == "show running-config"

    def test_lists_options_when_no_match_and_no_cr(self):
        s = build_suggestion("show ip", "zzz", ["route", "ospf"], has_cr=False)
        assert s.fix is None
        assert "route" in s.message

    def test_returns_none_when_nothing_useful(self):
        assert build_suggestion("show ip", "zzz", [], has_cr=False) is None


class TestAnalyzeEndToEnd:
    def test_drop_trailing_run(self):
        # show running-config run -> device error -> help on prefix lists <cr>.
        def help_lookup(prefix: str) -> str:
            assert prefix == "show running-config"
            return "  all   All\n  <cr>\n"

        s = analyze_device_error(
            "show running-config run", _RUN_ERR, help_lookup, prompt="R1#"
        )
        assert s is not None
        assert s.fix == "show running-config"

    def test_correct_typo_token(self):
        def help_lookup(prefix: str) -> str:
            assert prefix == "show ip"
            return "  route    Routing table\n  ospf     OSPF info\n  <cr>\n"

        s = analyze_device_error(
            "show ip rout", _ROUT_ERR, help_lookup, prompt="R1#"
        )
        assert s is not None
        assert s.fix == "show ip route"

    def test_clean_output_yields_no_suggestion(self):
        called = False

        def help_lookup(prefix: str) -> str:
            nonlocal called
            called = True
            return ""

        s = analyze_device_error("show version", "Cisco IOS\n", help_lookup)
        assert s is None
        assert called is False

    def test_help_lookup_failure_is_silent(self):
        def help_lookup(prefix: str) -> str:
            raise RuntimeError("device busy")

        s = analyze_device_error(
            "show ip rout", _ROUT_ERR, help_lookup, prompt="R1#"
        )
        assert s is None
