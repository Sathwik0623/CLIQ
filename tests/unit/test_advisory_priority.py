#!/usr/bin/env python3
"""Unit tests for the post-Enter advisory PRIORITY engine (advisory_router).

Covers all five reported bugs plus the supporting mode-validation fix. All
device I/O is injected via fake ``help_lookup`` / ``expand_fn`` callables so the
tests are pure and offline — the same philosophy as the rest of CLIQ.

Bug map:
  * Bug 1 — wrong-mode context CARRIED after Enter: ``int g0/1`` in EXEC advises
            the mode switch instead of dumping EXEC keywords.
  * Bug 2 — abbreviation expand-and-confirm: ``sh running-config int g0/1`` is
            confirmed as the expanded command, not a dump of interface TYPES.
  * Bug 3 — self-contradictory advice + bad fuzzy match: ``show running-config``
            in config emits ONLY the ``do`` tip and SUPPRESSES ``show`` ->
            ``flow``.
  * Bug 4 — config-subif is a valid mode for interface-level commands
            (``no ip address`` no longer wrong-mode).
  * Bug 5 — destructive-action confirmation: ``no ip address …`` surfaces a RED
            y/n confirmation naming the exact object.
"""

from types import SimpleNamespace

import pytest

from cliq.intelligence.advisory_router import (
    ANSI_BOLD_RED,
    ANSI_RESET,
    Advisory,
    TIER_ABBREVIATION,
    TIER_DESTRUCTIVE,
    TIER_FUZZY,
    TIER_INCOMPLETE,
    TIER_MODE_CONTEXT,
    build_abbreviation_advisory,
    build_destructive_confirmation,
    build_mode_context_advisory,
    describe_destructive_object,
    expand_interface_token,
    is_destructive_command,
    route_post_enter_advisory,
)
from cliq.intelligence.did_you_mean import (
    best_matches,
    is_protected_substitution,
)


# A device SYNTAX rejection (caret + "% Invalid input") so tiers 3–5 engage.
_INVALID = "                 ^\n% Invalid input detected at '^' marker."
_INCOMPLETE = "% Incomplete command."


def _pre(status="unknown", *, mode="exec", raw="", do="", matched_mode=None,
         fix_path=None):
    """Build a minimal pre-Enter Analysis stand-in."""
    matched = SimpleNamespace(mode=matched_mode) if matched_mode else None
    exp = SimpleNamespace(fix_mode_path=fix_path) if fix_path else None
    return SimpleNamespace(
        status=status, mode=mode, raw_input=raw, do_suggestion=do,
        matched_command=matched, explanation=exp,
    )


# ── Bug 1 — wrong-mode context carried after Enter ──────────────────────────
class TestBug1WrongModeCarry:
    def test_int_in_exec_advises_mode_switch(self):
        pre = _pre("wrong_mode", mode="exec", raw="int g0/1",
                   matched_mode="config")
        adv = route_post_enter_advisory(
            "int g0/1", _INVALID, "exec", pre,
            help_lookup=lambda p: "Exec\naccess-profile\nalps\n",
        )
        assert adv is not None
        assert adv.tier == TIER_MODE_CONTEXT
        assert adv.message == "Switch to config mode: configure terminal"
        # It must NOT dump the raw EXEC keyword list.
        assert "access-profile" not in adv.message
        assert "alps" not in adv.message

    def test_mode_context_uses_fix_path_enter_command(self):
        pre = _pre("wrong_mode", mode="exec", raw="int g0/1",
                   matched_mode="config-if",
                   fix_path=[("config", "configure terminal")])
        adv = build_mode_context_advisory(pre)
        assert adv.fix == "configure terminal"


# ── Bug 2 — abbreviation expand-and-confirm ─────────────────────────────────
class TestBug2AbbreviationExpansion:
    def _expand(self, c):
        return {
            "sh running-config int g0/1": "show running-config interface g0/1",
        }.get(c)

    def test_expands_and_confirms_instead_of_dumping_types(self):
        pre = _pre("unknown", mode="enable", raw="sh running-config int g0/1")
        adv = route_post_enter_advisory(
            "sh running-config int g0/1", _INVALID, "enable", pre,
            help_lookup=lambda p: "ACR\nATM-ACR\nAnalysis-Module\n",
            expand_fn=self._expand,
        )
        assert adv is not None
        assert adv.tier == TIER_ABBREVIATION
        assert adv.fix == "show running-config interface GigabitEthernet0/1"
        assert adv.message.startswith("Did you mean: show running-config")
        # No interface-type dump.
        assert "ATM-ACR" not in adv.message

    def test_interface_token_expansion(self):
        assert expand_interface_token("g0/1") == "GigabitEthernet0/1"
        assert expand_interface_token("te0/0/0.100") == "TenGigabitEthernet0/0/0.100"
        assert expand_interface_token("gi1/0/2") == "GigabitEthernet1/0/2"
        assert expand_interface_token("running-config") is None
        assert expand_interface_token("show") is None

    def test_build_abbreviation_advisory_none_when_nothing_expands(self):
        assert build_abbreviation_advisory("show version", "enable",
                                           expand_fn=lambda c: None) is None


# ── Bug 3 — self-contradictory advice + bad fuzzy match ─────────────────────
class TestBug3DoTipAndFuzzySuppression:
    def test_only_do_tip_fires_and_fuzzy_suppressed(self):
        pre = _pre("wrong_mode_do", mode="config", raw="show running-config",
                   do="do show running-config")
        # help_lookup returns 'flow' — a config keyword one edit from 'show'.
        adv = route_post_enter_advisory(
            "show running-config", _INVALID, "config", pre,
            help_lookup=lambda p: "flow  Flow\nflow-monitor  Monitor\n",
        )
        assert adv is not None
        assert adv.tier == TIER_MODE_CONTEXT
        assert adv.message == (
            "'show' isn't available directly in config mode "
            "\u2014 use: do show running-config"
        )
        # The contradictory 'Did you mean: flow' line must NOT appear.
        assert "flow" not in adv.message.lower()

    def test_fuzzy_matcher_rejects_show_to_flow(self):
        # Even called directly, best_matches must never turn show into flow.
        assert best_matches("show", ["flow", "flow-monitor", "frame-relay"]) == []

    def test_protected_substitution_flags_show_flow_but_allows_sh_show(self):
        assert is_protected_substitution("show", "flow") is True
        assert is_protected_substitution("sh", "show") is False
        assert is_protected_substitution("route", "router") is False  # not global

    def test_fuzzy_still_corrects_a_genuine_typo(self):
        # A non-protected token still gets corrected (no over-suppression).
        assert best_matches("rotue", ["route", "router"])[0] == "route"


# ── Bug 4 — config-subif valid for interface-level commands ─────────────────
class TestBug4ConfigSubifInheritsInterfaceCommands:
    def test_no_ip_address_valid_in_config_subif(self):
        from cliq.db.query import CommandDB

        db = CommandDB()
        pool = {c.full_command for c in db.commands_in_mode("config-subif")}
        assert "no ip address" in pool
        assert "ip address" in pool

    def test_engine_does_not_flag_wrong_mode_on_subinterface(self):
        from cliq.db.query import CommandDB
        from cliq.intelligence.engine import IntelligenceEngine

        engine = IntelligenceEngine(CommandDB())
        a = engine.analyze(
            "no ip address 192.168.10.1 255.255.255.0", "config-subif",
            is_executing=True,
        )
        assert a.status != "wrong_mode"
        assert a.status != "wrong_mode_do"

    def test_validator_accepts_config_subif_mode(self):
        from cliq.validation.validator import SafetyValidator

        assert "config-subif" in SafetyValidator.VALID_MODES
        assert "config-subif" in SafetyValidator.DO_PREFIX_MODES


# ── Bug 5 — destructive-action confirmation (red y/n, dynamic object) ────────
class TestBug5DestructiveConfirmation:
    def test_no_ip_address_is_destructive(self):
        assert is_destructive_command("no ip address 192.168.10.1 255.255.255.0")
        assert is_destructive_command("shutdown")
        assert is_destructive_command("clear counters")
        assert is_destructive_command("write erase")
        assert is_destructive_command("reload")
        assert is_destructive_command("erase startup-config")
        assert not is_destructive_command("show running-config")
        assert not is_destructive_command("interface g0/1")

    def test_confirmation_names_the_exact_object_and_is_red(self):
        adv = build_destructive_confirmation(
            "no ip address 192.168.10.1 255.255.255.0", "config-subif",
        )
        assert adv is not None
        assert adv.tier == TIER_DESTRUCTIVE
        assert adv.danger is True
        assert adv.message == (
            "You are about to DELETE IP 192.168.10.1 255.255.255.0 "
            "on this subinterface \u2014 confirm? (y/n)"
        )
        # Terminal ANSI red wraps the confirmation.
        assert adv.ansi_message.startswith(ANSI_BOLD_RED)
        assert adv.ansi_message.endswith(ANSI_RESET)

    def test_object_naming_variants(self):
        assert describe_destructive_object(
            "no ip address 10.0.0.1 255.0.0.0", "config-if") == (
            "DELETE", "IP 10.0.0.1 255.0.0.0 on this interface")
        assert describe_destructive_object("reload") == (
            "RELOAD", "the entire device")
        assert describe_destructive_object("write erase") == (
            "ERASE", "the startup configuration")
        verb, _ = describe_destructive_object("no description")
        assert verb == "REMOVE"

    def test_destructive_wins_priority_over_everything(self):
        # Even with a wrong-mode pre-Enter verdict, the destructive confirmation
        # (tier 1) pre-empts the mode advisory (tier 2).
        pre = _pre("wrong_mode", mode="config-if", raw="no ip address 1.1.1.1",
                   matched_mode="config")
        adv = route_post_enter_advisory(
            "no ip address 1.1.1.1 255.255.255.0", "", "config-subif", pre,
            help_lookup=lambda p: "",
        )
        assert adv.tier == TIER_DESTRUCTIVE
        assert adv.danger is True

    def test_advisory_only_no_auto_fix(self):
        adv = build_destructive_confirmation("no shutdown", "config-if")
        # Advisory-only: a destructive confirmation never carries an auto-run fix.
        assert adv.fix is None


# ── Priority ordering + incomplete tier ─────────────────────────────────────
class TestPriorityOrdering:
    def test_incomplete_tier_when_no_higher_tier(self):
        pre = _pre("missing_arg", mode="enable", raw="ping")
        adv = route_post_enter_advisory(
            "ping", _INCOMPLETE, "enable", pre,
            help_lookup=lambda p: "  WORD  Ping destination address\n",
        )
        assert adv is not None
        assert adv.tier == TIER_INCOMPLETE
        assert "WORD" in adv.message

    def test_silent_when_output_is_accepted(self):
        pre = _pre("ok", mode="enable", raw="show version")
        adv = route_post_enter_advisory(
            "show version", "Cisco IOS Software ...", "enable", pre,
            help_lookup=lambda p: "",
        )
        assert adv is None

    def test_silent_on_operational_message(self):
        # A bare operational '%' (not a syntax error) stays silent.
        pre = _pre("ok", mode="enable", raw="clear ip ospf process")
        # 'clear' is destructive, so it WOULD fire tier 1 — use a non-destructive
        # operational case instead.
        pre2 = _pre("ok", mode="config-router", raw="network 10.0.0.0")
        adv = route_post_enter_advisory(
            "network 10.0.0.0 0.0.0.255 area 0",
            "%OSPF: No router process 1", "config-router", pre2,
            help_lookup=lambda p: "",
        )
        assert adv is None

    def test_fuzzy_tier_fires_last(self):
        pre = _pre("unknown", mode="enable", raw="shw version")
        adv = route_post_enter_advisory(
            "shw version", _INVALID, "enable", pre,
            help_lookup=lambda p: "show  Show running system information\n",
            expand_fn=lambda c: None,  # 'shw' is a typo, not an abbreviation
        )
        assert adv is not None
        assert adv.tier == TIER_FUZZY


# ── Server wiring — pre-Enter destructive hint + abbreviation helper ─────────
class TestServerWiring:
    def test_pre_enter_build_live_hint_destructive_is_red(self):
        from cliq.web.server import _build_live_hint

        analysis = SimpleNamespace(
            status="ok", mode="config-subif",
            raw_input="no ip address 192.168.10.1 255.255.255.0",
            explanation=None, matched_command=None, do_suggestion="",
            expanded_command="no ip address 192.168.10.1 255.255.255.0",
        )
        hint = _build_live_hint(analysis)
        assert hint is not None
        assert hint["danger"] is True
        assert "DELETE IP 192.168.10.1 255.255.255.0" in hint["message"]
        assert "on this subinterface" in hint["message"]
        assert hint["ansiMessage"].startswith("\x1b[1;31m")

    def test_pre_enter_destructive_pre_empts_do_hint(self):
        from cliq.web.server import _build_live_hint

        # 'no shutdown' in config-if is destructive (negation) → red confirm
        # pre-empts any other hint.
        analysis = SimpleNamespace(
            status="ok", mode="config-if", raw_input="no shutdown",
            explanation=None, matched_command=None, do_suggestion="",
            expanded_command="no shutdown",
        )
        hint = _build_live_hint(analysis)
        assert hint is not None
        assert hint.get("danger") is True

    def test_expand_abbrev_line(self):
        from cliq.web.server import _expand_abbrev_line

        assert _expand_abbrev_line("sh running-config int g0/1") == (
            "show running-config interface GigabitEthernet0/1")
        assert _expand_abbrev_line("sh ip int br") == "show ip interface brief"
        # A genuine typo does NOT expand (defers to the fuzzy tier).
        assert _expand_abbrev_line("sh ip rotue") is None
        # A fully-typed command has nothing to expand.
        assert _expand_abbrev_line("show version") is None

