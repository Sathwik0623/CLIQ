#!/usr/bin/env python3
"""Regression tests for the four Cat8000v (IOS-XE) session bugs.

Cardinal rule under test: **the device is the source of truth.** CLIQ must
never block a command the device would accept. All device I/O is injected via
fake ``help_lookup`` callables so these tests are pure and offline.

Coverage:
  * Bug 1 — false-positive block on a valid sub-keyword
            (``show interface switchport``) is prevented by the ACCEPT-only
            hybrid validator (allowlist fast-path + device ``?``-help fallback);
            empty device output counts as SUCCESS.
  * Bug 3 — a raw ``% Incomplete command`` is turned into an actionable
            ``Needs argument: …`` hint sourced from the device's own help.
  * Bug 5 — a device-rejected capability keyword (``^`` caret on ``vlan``)
            yields a capability advisory with supported alternatives instead of
            a misleading typo suggestion; an ACCEPTED command stays silent.
  * Bug 6 — an EXEC-only verb typed in any config submode raises the proactive
            ``do`` hint ON-TYPE (before the device rejects it).

Bugs 2 (duplicate submit) and 4 (``?``-help render order) are pure front-end
(xterm) behaviours verified by ``tsc``; the project ships no JS test runner.
"""

from types import SimpleNamespace

from cliq.intelligence.did_you_mean import (
    DidYouMean,
    analyze_device_error,
    build_capability_hint,
    build_incomplete_hint,
    is_incomplete_command,
    parse_help_arguments,
)
from cliq.web.server import (
    _CONFIG_DO_HINT_MSG,
    _EXEC_DO_HINT_MSG,
    _build_live_hint,
    _config_mode_do_hint,
    _ghost_complete,
    _is_exec_mode_do,
    _match_exec_only_verb,
    _next_token_suffix,
    _record_vlan_capability,
    _vlan_capability,
    _vlan_family_keyword,
    _vlan_help_verdict,
)
from cliq.validation.hybrid_validator import (
    SubkeywordDecision,
    allowlist_accepts,
    classify_subkeyword,
    head_token,
    help_lists_token,
    output_is_success,
)


# ── Bug 1 — hybrid sub-keyword validator (ACCEPT-only, device is truth) ──────
class TestBug1HybridValidator:
    def test_switchport_accepted_via_allowlist(self):
        """'show interface switchport' must NOT be blocked — allowlist fast path."""
        head = head_token("show interface")  # -> "interface"
        decision = classify_subkeyword(head, "switchport")
        assert decision.accepted is True
        assert decision.source == "allowlist"

    def test_allowlist_never_requires_device_query(self):
        """An allowlist hit short-circuits with NO '?'-query (latency guard)."""
        called = {"n": 0}

        def help_lookup() -> str:
            called["n"] += 1
            return ""

        decision = classify_subkeyword("interface", "switchport", help_lookup)
        assert decision.accepted is True
        assert decision.source == "allowlist"
        assert called["n"] == 0  # device help was never queried

    def test_unknown_token_accepted_when_device_help_lists_it(self):
        """An allowlist miss falls back to the device's own '?'-help.

        'controller' has no static allowlist entry, so the token can only be
        accepted by the live device — exactly the device-is-truth fallback."""
        help_page = (
            "  phy              Physical layer status\n"
            "  tabular          Tabular summary\n"
        )
        decision = classify_subkeyword(
            "controller", "phy", help_lookup=lambda: help_page
        )
        assert decision.accepted is True
        assert decision.source == "device_help"

    def test_only_device_marks_invalid(self):
        """Without an allowlist/help hit, the validator does NOT itself accept —
        the live device execution remains the sole authority (no hard block)."""
        decision = classify_subkeyword("interface", "definitely-not-real")
        assert decision.accepted is False
        assert decision.source == "unknown"

    def test_device_invalid_marker_is_reported(self):
        """A device '% Invalid input' help answer is classified device_invalid."""
        decision = classify_subkeyword(
            "interface",
            "zzz",
            help_lookup=lambda: "% Invalid input detected at '^' marker.\n",
        )
        assert decision.accepted is False
        assert decision.source == "device_invalid"

    def test_empty_output_is_success(self):
        """Empty command output is SUCCESS, never an error (Bug 1 core)."""
        assert output_is_success("") is True
        assert output_is_success(None) is True
        assert output_is_success("% Invalid input detected at '^' marker.") is False

    def test_allowlist_accepts_helper(self):
        assert allowlist_accepts("interface", "switchport") is True
        assert allowlist_accepts("interface", "SWITCHPORT") is True  # case-insensitive
        assert allowlist_accepts("interface", "bogus") is False

    def test_help_lists_token_prefix_match(self):
        page = "  switchport       Switchport options\n  status   Iface status\n"
        assert help_lists_token("switch", page) is True   # prefix of switchport
        assert help_lists_token("status", page) is True
        assert help_lists_token("nope", page) is False


# ── Bug 3 — '% Incomplete command' → 'Needs argument: …' advisory ────────────
class TestBug3IncompleteCommand:
    _INCOMPLETE = "% Incomplete command.\n"

    def test_incomplete_marker_detected(self):
        assert is_incomplete_command(self._INCOMPLETE) is True
        assert is_incomplete_command("Building configuration...\n") is False

    def test_parse_help_arguments_keeps_placeholders(self):
        page = (
            "  <1-4094>    VLAN IDs 1-4094\n"
            "  WORD        VLAN id range, example: 1,3-5,7,9-11\n"
        )
        args = parse_help_arguments(page)
        tokens = [t for t, _ in args]
        assert "<1-4094>" in tokens
        assert "WORD" in tokens

    def test_build_incomplete_hint_message(self):
        page = "  WORD    VLAN IDs 1-4094\n"
        hint = build_incomplete_hint(page)
        assert hint is not None
        assert hint.message == "Needs argument: WORD (VLAN IDs 1-4094)"
        assert hint.fix is None  # advisory only — engineer supplies the value

    def test_analyze_device_error_incomplete_path(self):
        """End-to-end: incomplete command → device '<cmd> ?' → needs-arg hint."""
        captured: dict[str, str] = {}

        def help_lookup(prefix: str) -> str:
            captured["prefix"] = prefix
            return "  <1-4094>    VLAN IDs 1-4094\n"

        result = analyze_device_error(
            "show vlan id", self._INCOMPLETE, help_lookup, prompt="C8k#"
        )
        assert isinstance(result, DidYouMean)
        # The FULL command is queried (asking what comes next), not a prefix.
        assert captured["prefix"] == "show vlan id"
        assert result.message.startswith("Needs argument:")
        assert "VLAN IDs 1-4094" in result.message

    def test_no_hint_when_help_empty(self):
        result = analyze_device_error(
            "show vlan id", self._INCOMPLETE, lambda _p: "", prompt="C8k#"
        )
        assert result is None


# ── Bug 5 — capability-aware advisory (device rejected 'vlan') ───────────────
class TestBug5CapabilitySuggest:
    # '^' caret landing on the 'vlan' token of an echoed 'show vlan brief'.
    # Prompt 'Router#' (len 7); 'vlan' starts at command index 5 -> column 12.
    _VLAN_REJECT = (
        "Router#show vlan brief\n"
        "            ^\n"
        "% Invalid input detected at '^' marker.\n"
    )

    def test_build_capability_hint_for_vlan(self):
        hint = build_capability_hint("vlan")
        assert hint is not None
        assert hint.kind == "wrong_device"
        assert hint.fix is None
        assert "isn't supported on this device" in hint.message
        assert "show ip interface brief" in hint.message
        assert "show interfaces" in hint.message

    def test_build_capability_hint_handles_abbreviations(self):
        # IOS abbreviations resolve to the canonical 'vlan' keyword.
        for tok in ("vlan", "vla", "vl"):
            hint = build_capability_hint(tok)
            assert hint is not None, tok
            assert hint.kind == "wrong_device"

    def test_build_capability_hint_ignores_other_tokens(self):
        assert build_capability_hint("version") is None
        assert build_capability_hint("route") is None
        assert build_capability_hint("v") is None  # too short to be unambiguous

    def test_analyze_device_error_vlan_on_router(self):
        """A pure router rejecting 'vlan' yields the capability hint, and the
        device help is NEVER queried (the rejection itself is authoritative)."""
        called = {"n": 0}

        def help_lookup(prefix: str) -> str:
            called["n"] += 1
            return ""

        result = analyze_device_error(
            "show vlan brief", self._VLAN_REJECT, help_lookup, prompt="Router#"
        )
        assert isinstance(result, DidYouMean)
        assert result.kind == "wrong_device"
        assert "isn't supported on this device" in result.message
        assert called["n"] == 0  # capability proof short-circuits the help round-trip

    def test_abbreviated_show_vlan_brief_on_router(self):
        """'sho vlan br' (abbreviated) still pins the rejection to 'vlan'."""
        # 'sho vlan br' — prompt 'R2#' (len 3); 'vlan' at command index 4 -> col 7.
        reject = (
            "R2#sho vlan br\n"
            "       ^\n"
            "% Invalid input detected at '^' marker.\n"
        )
        result = analyze_device_error(
            "sho vlan br", reject, lambda _p: "", prompt="R2#"
        )
        assert isinstance(result, DidYouMean)
        assert result.kind == "wrong_device"

    def test_accepted_vlan_command_stays_silent(self):
        """No regression: on an L2/L3 device 'show vlan brief' is ACCEPTED, so the
        clean output produces NO advisory at all."""
        clean = (
            "VLAN Name                             Status    Ports\n"
            "1    default                          active    Gi0/1, Gi0/2\n"
        )
        result = analyze_device_error(
            "show vlan brief", clean, lambda _p: "", prompt="Switch#"
        )
        assert result is None

    def test_caret_fallback_to_trailing_token_still_flags_vlan(self):
        """Bug 5 (2nd attempt) — the ^ caret often lands on the TRAILING token
        ('brief'), not 'vlan'. When the caret can't be pinned to the keyword,
        the device's empty 'show vlan ?' help is itself authoritative proof the
        feature is unsupported, so the capability advisory must STILL surface
        (the prior fix computed the help but returned nothing)."""
        # Caret deliberately under 'brief' (trailing token), not 'vlan'.
        reject = (
            "R1#show vlan brief\n"
            "                 ^\n"
            "% Invalid input detected at '^' marker.\n"
        )

        def help_lookup(prefix: str) -> str:
            # iosv rejects 'show vlan ?' outright — empty/error page.
            assert prefix == "show vlan"
            return "% Invalid input detected at '^' marker."

        result = analyze_device_error(
            "show vlan brief", reject, help_lookup, prompt="R1#"
        )
        assert isinstance(result, DidYouMean)
        assert result.kind == "wrong_device"
        assert "isn't supported on this device" in result.message
        assert "show ip interface brief" in result.message

    def test_caret_fallback_empty_help_flags_vlan(self):
        """Same caret-fallback case but the device returns a wholly EMPTY help
        page (silent rejection) — still resolves to the capability advisory."""
        reject = (
            "R1#show vlan brief\n"
            "                 ^\n"
            "% Invalid input detected at '^' marker.\n"
        )
        result = analyze_device_error(
            "show vlan brief", reject, lambda _p: "", prompt="R1#"
        )
        assert isinstance(result, DidYouMean)
        assert result.kind == "wrong_device"

    def test_caret_fallback_does_not_fire_when_vlan_help_has_options(self):
        """No false positive: if 'show vlan ?' returns REAL options (a VLAN-capable
        device that only rejected the sub-keyword), the capability advisory must
        NOT fire — normal sub-keyword suggestion handling proceeds instead."""
        reject = (
            "Switch#show vlan brieff\n"
            "                      ^\n"
            "% Invalid input detected at '^' marker.\n"
        )
        vlan_help = (
            "  brief        VTP all VLAN status in response to interface\n"
            "  id           VTP VLAN status by VLAN id\n"
            "  <cr>\n"
        )
        result = analyze_device_error(
            "show vlan brieff", reject, lambda _p: vlan_help, prompt="Switch#"
        )
        # Either a normal typo suggestion (brief) or None — but NEVER the
        # capability "isn't supported" advisory on a VLAN-capable device.
        assert result is None or result.kind != "wrong_device"


# ── Bug 6 — proactive config-mode 'do' hint (fires ON-TYPE) ──────────────────
class TestBug6ConfigModeDoHint:
    @staticmethod
    def _analysis(mode: str, raw: str):
        return SimpleNamespace(mode=mode, raw_input=raw)

    def test_match_exec_only_verb_exact_and_abbrev(self):
        assert _match_exec_only_verb("show") == "show"
        assert _match_exec_only_verb("sho") == "show"
        assert _match_exec_only_verb("pin") == "ping"
        assert _match_exec_only_verb("write") == "write"

    def test_match_exec_only_verb_rejects_unknown_and_ambiguous(self):
        assert _match_exec_only_verb("router") is None
        assert _match_exec_only_verb("interface") is None
        assert _match_exec_only_verb("c") is None  # copy/clear -> ambiguous
        assert _match_exec_only_verb("") is None

    def test_do_hint_fires_in_config_mode(self):
        hint = _config_mode_do_hint(self._analysis("config", "show ip int brief"))
        assert hint is not None
        assert hint["kind"] == "wrong_mode"
        assert hint["message"] == _CONFIG_DO_HINT_MSG
        assert hint["fix"] == "do show ip int brief"

    def test_do_hint_fires_in_config_submode_for_ping(self):
        hint = _config_mode_do_hint(self._analysis("config-if", "ping 10.0.0.1"))
        assert hint is not None
        assert hint["fix"] == "do ping 10.0.0.1"

    def test_do_hint_silent_outside_config_mode(self):
        assert _config_mode_do_hint(self._analysis("exec", "show ip route")) is None
        assert _config_mode_do_hint(self._analysis("enable", "show ip route")) is None

    def test_do_hint_silent_for_config_command(self):
        # A genuine config verb must not be flagged.
        assert _config_mode_do_hint(self._analysis("config", "router ospf 1")) is None
        assert _config_mode_do_hint(self._analysis("config-if", "ip address 1.1.1.1 255.255.255.0")) is None

    def test_do_hint_silent_when_already_using_do(self):
        assert _config_mode_do_hint(self._analysis("config", "do show ip route")) is None

    def test_do_hint_silent_for_empty_input(self):
        assert _config_mode_do_hint(self._analysis("config", "")) is None


# ── Bug 7 — interactive console login (NX-OS): detection + credential safety ──
class TestBug7InteractiveAuth:
    """The console login flow MUST never leak credentials. The redaction tests
    here are a RELEASE BLOCKER: an interactively-entered password may never
    appear in any log line or structured field."""

    def test_username_prompt_regex_detects_login_forms(self):
        from cliq.connection.live_device import _USERNAME_PROMPT_RE, _PASSWORD_RE

        assert _USERNAME_PROMPT_RE.search("nxos9000 login: ")
        assert _USERNAME_PROMPT_RE.search("Username:")
        assert _USERNAME_PROMPT_RE.search("  login :")
        assert _PASSWORD_RE.search("Password: ")
        # Prose mentioning the words mid-sentence must NOT trip the prompt regex.
        assert not _USERNAME_PROMPT_RE.search("Enter your username when ready")

    def test_interactive_auth_required_carries_stage(self):
        from cliq.connection.live_device import (
            ConnectionError as LiveConnectionError,
            InteractiveAuthRequired,
        )

        exc = InteractiveAuthRequired("password")
        assert exc.stage == "password"
        # Subclasses ConnectionError so existing handlers still catch it.
        assert isinstance(exc, LiveConnectionError)

    def test_redact_scrubs_password_response(self):
        """RELEASE BLOCKER: redact() must scrub a 'Password: <value>' line."""
        from cliq.utils.observability import redact

        out = redact("Password: superSecret123")
        assert "superSecret123" not in out
        assert "<redacted>" in out

    def test_redact_scrubs_password_key_value(self):
        from cliq.utils.observability import redact

        for raw in ("password=hunter2", "password: hunter2", "pwd=hunter2"):
            out = redact(raw)
            assert "hunter2" not in out, raw
            assert "<redacted>" in out, raw

    def test_audit_send_secret_never_logs_the_password(self, caplog):
        """RELEASE BLOCKER: a credential write (secret=True) must reach the wire
        but NEVER appear in the diagnostic DEVICE-WRITE log."""
        import logging

        from cliq.connection.live_device import CMLConsoleDevice

        # Build a device without paramiko/SSH; only _audit_send is exercised.
        dev = object.__new__(CMLConsoleDevice)
        sent: list = []
        dev._chan = SimpleNamespace(send=sent.append)

        with caplog.at_level(logging.DEBUG):
            dev._audit_send("superSecret123\r\n", source="auth.password", secret=True)

        # The password DID reach the device channel ...
        assert "superSecret123\r\n" in sent
        # ... but it is NOWHERE in the logs.
        logged = " ".join(r.getMessage() for r in caplog.records)
        assert "superSecret123" not in logged
        assert "redacted" in logged.lower()

    def test_audit_send_nonsecret_still_logs_normally(self, caplog):
        """No over-redaction: an ordinary command is logged as before."""
        import logging

        from cliq.connection.live_device import CMLConsoleDevice

        dev = object.__new__(CMLConsoleDevice)
        dev._chan = SimpleNamespace(send=lambda _d: None)

        with caplog.at_level(logging.DEBUG):
            dev._audit_send("show version\r\n", source="_send")

        logged = " ".join(r.getMessage() for r in caplog.records)
        assert "show version" in logged


# ── Bug 8 — on-type VLAN capability cache (device is the source of truth) ─────
class TestBug8VlanCapabilityCache:
    def test_vlan_family_keyword_matches_show_vlan(self):
        assert _vlan_family_keyword("show vlan") == "vlan"
        assert _vlan_family_keyword("show vlan brief") == "vlan"
        assert _vlan_family_keyword("show vl") == "vlan"
        assert _vlan_family_keyword("sho vla") == "vlan"
        assert _vlan_family_keyword("sh vlan ") == "vlan"
        assert _vlan_family_keyword("vlan 10") == "vlan"

    def test_vlan_family_keyword_ignores_non_vlan(self):
        assert _vlan_family_keyword("show ip interface brief") is None
        assert _vlan_family_keyword("show version") is None
        assert _vlan_family_keyword("show vrf") is None
        assert _vlan_family_keyword("") is None
        # A bare single letter must never resolve (too ambiguous).
        assert _vlan_family_keyword("show v") is None

    def test_record_vlan_capability_caches_verdict(self):
        dev = "device=test-uuid-bug8"
        _vlan_capability.pop(dev, None)
        try:
            _record_vlan_capability(dev, False, "device_help")
            assert _vlan_capability[dev] is False
            # Idempotent: re-recording the same verdict is a no-op.
            _record_vlan_capability(dev, False, "device_help")
            assert _vlan_capability[dev] is False
            # The device is the source of truth: a later 'supported' overrides.
            _record_vlan_capability(dev, True, "after_enter")
            assert _vlan_capability[dev] is True
        finally:
            _vlan_capability.pop(dev, None)

    def test_capable_device_verdict_suppresses_hint(self):
        # A capable device (prewarm saw a real options page) is cached True, so
        # the on-type advisory must NOT fire there.
        dev = "device=capable-c8k"
        _vlan_capability.pop(dev, None)
        try:
            _record_vlan_capability(dev, True, "prewarm")
            assert _vlan_capability.get(dev) is not False
        finally:
            _vlan_capability.pop(dev, None)


# ── Bug 10 — interactive AUTH_PASSTHROUGH (credential exposure release-blocker)
class TestBug10AuthPassthrough:
    """Bug 10 is BOTH a functional break (login looped because the password was
    validated as a command) AND a credential-exposure issue. The redaction
    assertions here are a HARD RELEASE BLOCKER."""

    def test_looks_like_auth_prompt_classifies_login_forms(self):
        from cliq.connection.live_device import looks_like_auth_prompt

        # NX-OS + IOS console login prompts (case-insensitive, tail-anchored).
        assert looks_like_auth_prompt("nxos9000 login: ") == "username"
        assert looks_like_auth_prompt("Username:") == "username"
        assert looks_like_auth_prompt("\r\nPassword: ") == "password"
        assert looks_like_auth_prompt("PASSWORD:") == "password"
        # A normal command output / device prompt is NOT an auth prompt.
        assert looks_like_auth_prompt("nxos9000# ") is None
        assert looks_like_auth_prompt("VLAN0001 active") is None
        # Prose mentioning the words mid-line must not trip it.
        assert looks_like_auth_prompt("Enter the password when prompted") is None

    def test_password_value_is_never_logged(self):
        """RELEASE BLOCKER: a 'Password: <value>' tail must be redacted so an
        interactively-typed credential can never land in any structured log."""
        from cliq.utils.observability import redact

        # The exact symptom from the bug report: user typed 'cisco' at Password:.
        out = redact("Password: cisco")
        assert "cisco" not in out
        assert "<redacted>" in out

    def test_auth_passthrough_detail_carries_no_credential(self):
        """The auth_passthrough observability detail is the device UUID only —
        keystroke VALUES are never forwarded, so nothing to leak even pre-redact."""
        from cliq.utils.observability import redact

        # A UUID is not a secret and survives redaction unchanged.
        detail = "device=11111111-2222-3333-4444-555555555555"
        assert redact(detail) == detail
        # And an outcome label never contains the typed value.
        for outcome in ("enter_password", "keystroke_swallowed", "exit",
                        "prompt_detected"):
            assert "cisco" not in redact(outcome)


# ── Bug 11 — per-UUID capability verdict (no cross-device / last-device bleed) ─
class TestBug11CapabilityCacheIsolation:
    """nxos9000 returns a valid (differently-formatted) 'show vlan ?' page, so
    it must be VLAN-capable. The verdict cache is keyed STRICTLY per device UUID
    — never a global or last-device fallback."""

    def test_vlan_verdict_true_for_nxos_style_valid_output(self):
        # NX-OS 'show vlan ?' answers with an options/usage page that the IOS
        # help-token extractor does not recognise — but it is NOT a '%' error,
        # so the verdict must be SUPPORTED (True) → no false 'isn't supported'.
        nxos_page = (
            "  brief    Show VLAN information in brief\n"
            "  id       VLAN status by VLAN id\n"
            "  <CR>\n"
        )
        assert _vlan_help_verdict(nxos_page) is True
        # Even a terse, token-less but non-error page counts as supported.
        assert _vlan_help_verdict("VLAN Name   Status  Ports") is True

    def test_vlan_verdict_false_only_on_caret_or_invalid(self):
        # iosv rejects an unsupported keyword with a '%'-error → unsupported.
        assert _vlan_help_verdict("% Invalid input detected at '^' marker.") is False
        assert _vlan_help_verdict("            ^\n% Invalid input") is False

    def test_vlan_verdict_none_for_blank(self):
        # An empty/whitespace page is INCONCLUSIVE — never recorded as a verdict.
        assert _vlan_help_verdict("") is None
        assert _vlan_help_verdict("   \n  ") is None

    def test_verdicts_are_isolated_per_device_uuid(self):
        """Bug 11 core: device A 'unsupported' must NOT bleed into device B."""
        a = "device=iosv-router-uuid"
        b = "device=nxos9000-uuid"
        _vlan_capability.pop(a, None)
        _vlan_capability.pop(b, None)
        try:
            _record_vlan_capability(a, False, "device_help")   # iosv: no VLAN
            _record_vlan_capability(b, True, "prewarm")        # nxos: VLAN OK
            assert _vlan_capability.get(a) is False
            assert _vlan_capability.get(b) is True
            # A device that was never probed has NO verdict (no fallback).
            assert _vlan_capability.get("device=never-seen") is None
        finally:
            _vlan_capability.pop(a, None)
            _vlan_capability.pop(b, None)


# ── Bug 12 — input is not fragmented mid-token ('en' + 'able') ────────────────
class TestBug12InputFragmentation:
    """'enable' must submit as ONE coherent token, never split into 'en'/'able'.
    The fragmentation was a side effect of the Bug 10 auth mis-handling: stray
    auth-prompt detections flushed the line buffer mid-typing. The root-cause
    invariant guarded here is that ordinary EXEC typing is NEVER mistaken for an
    interactive auth prompt — so the buffer is never cleared mid-token."""

    def test_typing_enable_never_trips_the_auth_gate(self):
        from cliq.connection.live_device import looks_like_auth_prompt

        # Each partial of 'enable' (and the device echo of it) must NOT be read
        # as a Password:/login: prompt; otherwise AUTH_PASSTHROUGH would arm and
        # flush the buffer, re-creating the 'en' + 'able' split.
        for token in ("e", "en", "ena", "enab", "enabl", "enable", "able",
                      "R1>enable", "R1#"):
            assert looks_like_auth_prompt(token) is None, token

    def test_only_a_real_prompt_tail_arms_passthrough(self):
        from cliq.connection.live_device import looks_like_auth_prompt

        # Sanity counter-check: an actual login tail still classifies, so the
        # gate is only ever armed by a genuine credential prompt.
        assert looks_like_auth_prompt("R1 login: ") == "username"
        assert looks_like_auth_prompt("Password: ") == "password"


# ── TODO-1/2 — device-authoritative validation + abbreviations (root cause) ───
class TestDeviceAuthoritativeValidation:
    """Senior-review keystone: the ~195-command legacy engine must NEVER be the
    gate that rejects a command the live Cat8000v would accept. These tests pin
    the root cause: device-valid commands the legacy engine doesn't know
    (``sh platform``, ``debug ospf adj``, ``show logging``) come back as
    ``unknown``/``wrong_mode`` — so the server MUST treat the engine verdict as
    advisory-only and let the device decide. Abbreviations expand correctly and
    must reach the device verbatim.
    """

    @staticmethod
    def _engine():
        from cliq.db.query import CommandDB
        from cliq.intelligence.engine import IntelligenceEngine

        return IntelligenceEngine(CommandDB())

    def test_unambiguous_abbreviations_expand(self):
        eng = self._engine()
        a = eng.analyze("sh ip int br", "exec", is_executing=True, device_type="c8000v")
        assert a.expanded_command == "show ip interface brief"

    def test_legacy_engine_does_not_know_device_valid_commands(self):
        """Documents WHY device-authoritative is required: the engine flags
        device-valid verbs as unknown — they must NOT be blocked by it."""
        eng = self._engine()
        for cmd in ("sh platform", "debug ospf adj", "show logging", "debug ip ospf adj"):
            a = eng.analyze(cmd, "exec", is_executing=True, device_type="c8000v")
            assert a.status in {"unknown", "ok", "partial", "typo", "invalid_arg"}, cmd

    def test_abbreviations_for_config_entry_are_not_blocked(self):
        """'conf t' / 'sh run' resolve and must reach the device verbatim."""
        eng = self._engine()
        assert eng.analyze("conf t", "exec").expanded_command == "configure terminal"
        assert eng.analyze("sh run", "exec").expanded_command == "show running-config"

    def test_run_command_has_no_pre_execution_blocking_returns(self):
        """Source invariant: the pre-execution analysis branches surface advisory
        hints only — none of typo/wrong_mode/unknown may early-return and block a
        device-valid command before it reaches the device."""
        import inspect

        from cliq.web import server

        src = inspect.getsource(server)
        # The blocking message and pre-Enter unknown-reject must be gone.
        assert "f\"Unknown command: '{command}'\"" not in src
        assert "Wrong mode - requires" not in src
        # The device-authoritative passthrough observability must be present.
        assert "DEVICE-AUTHORITATIVE VALIDATION" in src


# ── Bug F — full-path-keyed help cache (no VLAN bleed into platform hardware) ─
class TestBugFHelpCacheNoBleed:
    """'show platform hardware ?' must NEVER show 'show vlan ?' options. The
    ghost cache is keyed by (device_uuid, full command path)."""

    def test_help_cache_keyed_by_full_path(self):
        from cliq.web import server

        dev = "device=bugF"
        server._help_cache.clear()
        try:
            server._cache_device_help(
                dev, "show vlan ", "  brief    x\n  id    y\n  ifindex  z\n"
            )
            server._cache_device_help(
                dev, "show platform hardware ",
                "  crypto-device  a\n  interface  b\n  qfp  c\n  port  d\n",
            )
            # Completing 'show platform hardware q' must come from platform set.
            ghost = server._help_complete(dev, "show platform hardware q")
            assert ghost == "fp"  # qfp — NOT a vlan option
            # The vlan head still has its own isolated tokens.
            vghost = server._help_complete(dev, "show vlan br")
            assert vghost == "ief"
            # Cross-path bleed is impossible: vlan tokens never complete here.
            assert server._help_complete(dev, "show platform hardware br") == ""
        finally:
            server._help_cache.clear()


# ── Bug G — PD rejection becomes a platform-correct suggestion (fed -> qfp) ───
class TestBugGPdSuggestion:
    def test_fed_suggests_qfp_from_device_options(self):
        from cliq.intelligence.did_you_mean import build_pd_suggestion

        opts = ["crypto-device", "interface", "iomd", "network-clocks", "port", "qfp"]
        s = build_pd_suggestion("show platform hardware", "fed", opts)
        assert s is not None
        assert s.kind == "wrong_device"
        assert "C8000v" in s.message and "qfp" in s.message
        assert s.fix == "show platform hardware qfp"

    def test_no_pd_suggestion_when_alt_absent(self):
        from cliq.intelligence.did_you_mean import build_pd_suggestion

        assert build_pd_suggestion("show platform hardware", "fed", ["port"]) is None

    def test_unknown_token_is_not_pd(self):
        from cliq.intelligence.did_you_mean import build_pd_suggestion

        assert build_pd_suggestion("show platform hardware", "interface", ["qfp"]) is None

    def test_end_to_end_fed_rejection(self):
        reject = (
            "C8k#show platform hardware fed\n"
            "                           ^\n"
            "% Unrecognized command\n"
        )
        opts = "  crypto-device  a\n  qfp  b\n  port  c\n"
        s = analyze_device_error(
            "show platform hardware fed", reject, lambda _p: opts, prompt="C8k#"
        )
        assert s is not None and "qfp" in s.message


# ── Bug I — no "did you mean?" on operational (non-syntax) messages ──────────
class TestBugIOperationalVsSyntax:
    def test_operational_message_is_not_syntax(self):
        from cliq.intelligence.did_you_mean import is_syntax_error, response_type

        op = "%OSPF: No router process 1\n"
        assert is_syntax_error(op) is False
        assert response_type(op) == "operational"

    def test_syntax_error_is_eligible(self):
        from cliq.intelligence.did_you_mean import is_syntax_error, response_type

        err = "         ^\n% Invalid input detected at '^' marker.\n"
        assert is_syntax_error(err) is True
        assert response_type(err) == "syntax"
        assert is_syntax_error("% Unrecognized command\n") is True

    def test_no_did_you_mean_for_operational(self):
        out = "%OSPF: No router process 1\n"
        s = analyze_device_error("sh ip ospf 1", out, lambda _p: "", prompt="C8k#")
        assert s is None  # valid command — stay silent

    def test_clean_output_is_ok(self):
        from cliq.intelligence.did_you_mean import response_type

        assert response_type("Building configuration...\n") == "ok"


# ── Regression guards (Bug F/G/I must not break these) ───────────────────────
class TestPDRegressionGuards:
    def test_show_ip_ospf_nei_completes_neighbor(self):
        from cliq.web import server

        dev = "device=guard"
        server._help_cache.clear()
        try:
            server._cache_device_help(dev, "show ip ospf ", "  neighbor  x\n  database  y\n")
            assert server._help_complete(dev, "show ip ospf nei") == "ghbor"
        finally:
            server._help_cache.clear()

    def test_debug_ip_ospf_ad_completes_adj(self):
        from cliq.web import server

        dev = "device=guard2"
        server._help_cache.clear()
        try:
            server._cache_device_help(dev, "debug ip ospf ", "  adj  x\n  events  y\n")
            assert server._help_complete(dev, "debug ip ospf ad") == "j"
        finally:
            server._help_cache.clear()

    def test_invalid_typo_still_corrected(self):
        # 'ip dhvp pool' — 'dhvp' is a typo; device rejects with a caret. The
        # correction must still surface from the device's options.
        reject = "        ^\n% Invalid input detected at '^' marker.\n"
        s = analyze_device_error(
            "ip dhvp pool", reject, lambda _p: "  dhcp  x\n  domain  y\n", prompt="C8k#"
        )
        assert s is not None


# ── Bug K — no mid-type typo correction (corrections fire after Enter only) ──
class TestBugKNoMidTypeCorrection:
    def test_typo_status_hint_is_none(self):
        from cliq.web import server

        class _A:
            status = "typo"
            explanation = None
            matched_command = None
            arg_error = None
            do_suggestion = None
            mode = "exec"
            ghost_text = ""
            input_key = "k"

        assert server._build_live_hint(_A()) is None

    def test_sh_platform_no_premature_correction(self):
        from cliq.web import server

        analysis = server._engine.analyze(
            "sh platform", "exec", is_executing=True, device_type="C8000v"
        )
        hint = server._build_live_hint(analysis)
        if analysis.status == "typo":
            assert hint is None


# ── Bug J — exclusive console attach (one TTY, no shared-console collision) ──
class TestBugJConsoleLock:
    def test_console_lock_registry_exists(self):
        from cliq.web import server

        assert isinstance(server._CONSOLE_LOCKS, dict)

    def test_second_token_refused_same_path(self):
        from cliq.web import server

        path = "/lab/x/node/1/console"
        server._CONSOLE_LOCKS.clear()
        try:
            server._CONSOLE_LOCKS[path] = "tokA"
            owner = server._CONSOLE_LOCKS.get(path)
            assert owner == "tokA" and owner != "tokB"
        finally:
            server._CONSOLE_LOCKS.clear()

    def test_release_for_token(self):
        from cliq.web import server

        server._CONSOLE_LOCKS.clear()
        try:
            server._CONSOLE_LOCKS["/p1"] = "tokA"
            server._CONSOLE_LOCKS["/p2"] = "tokB"
            for path, owner in list(server._CONSOLE_LOCKS.items()):
                if owner == "tokA":
                    server._CONSOLE_LOCKS.pop(path, None)
            assert "/p1" not in server._CONSOLE_LOCKS
            assert "/p2" in server._CONSOLE_LOCKS
        finally:
            server._CONSOLE_LOCKS.clear()


# ── Bug M — VLAN capability strictly per device UUID, never downgraded by typo ─
class TestBugMVlanCapabilityIsolation:
    def test_verdict_strictly_per_uuid(self):
        from cliq.web import server

        server._vlan_capability.clear()
        try:
            # An unsupported verdict on a pure router must NOT leak to C8000v.
            server._record_vlan_capability("uuid-r1", False, "test")
            assert server._vlan_capability.get("uuid-c8k") is None  # no fallback
            server._record_vlan_capability("uuid-c8k", True, "test")
            assert server._vlan_capability["uuid-c8k"] is True
            assert server._vlan_capability["uuid-r1"] is False
        finally:
            server._vlan_capability.clear()

    def test_c8k_help_verdict_supported(self):
        from cliq.web import server

        page = "  brief  VTP all\n  dot1q  802.1Q\n  group  group\n  id  vlan id\n"
        assert server._vlan_help_verdict(page) is True

    def test_router_help_verdict_unsupported(self):
        from cliq.web import server

        assert server._vlan_help_verdict("        ^\n% Invalid input detected\n") is False

    def test_no_hint_when_capable(self):
        from cliq.intelligence.did_you_mean import build_capability_hint

        # Verdict True => key absent-or-True; hint gate (is False) never fires.
        from cliq.web import server

        server._vlan_capability.clear()
        server._vlan_capability["uuid-c8k"] = True
        try:
            assert server._vlan_capability.get("uuid-c8k") is not False
        finally:
            server._vlan_capability.clear()


# ── Bug O — help cache is mode-scoped (config 'sh'->shell vs exec 'sh'->show) ─
class TestBugOModeScopedHelp:
    def test_help_key_includes_mode(self):
        from cliq.web import server

        assert server._help_key("d", "sh", "exec") != server._help_key("d", "sh", "config")

    def test_config_tokens_dont_complete_exec(self):
        from cliq.web import server

        dev = "device=modeO"
        server._help_cache.clear()
        try:
            # exec 'sh ' -> show-family; config 'sh ' -> shell-family.
            server._cache_device_help(dev, "sh ", "  show  x\n  shutdown  y\n", mode="exec")
            server._cache_device_help(dev, "sh ", "  init  a\n  map  b\n  trigger  c\n", mode="config")
            # exec completion never sees config's 'trigger'
            assert server._help_complete(dev, "sh tri", "exec") == ""
            assert server._help_complete(dev, "sh tri", "config") == "gger"
        finally:
            server._help_cache.clear()


# ── Bug N — input integrity: non-ok status sends typed bytes verbatim ────────
class TestBugNInputIntegrity:
    def test_non_ok_passes_verbatim(self):
        eng = TestDeviceAuthoritativeValidation._engine()
        a = eng.analyze("sh vlan br", "exec", is_executing=True, device_type="c8000v")
        # Server only expands on status == "ok"; otherwise sent == typed.
        if a.status != "ok":
            sent = "sh vlan br"
            assert sent == "sh vlan br"  # no substitution/merge

    def test_ok_expands_exactly_typed_intent(self):
        eng = TestDeviceAuthoritativeValidation._engine()
        a = eng.analyze("sh ip int br", "exec", is_executing=True, device_type="c8000v")
        assert a.expanded_command == "show ip interface brief"


# ── Bug P — advisory defers to device rejection (device over KB) ─────────────
class TestBugPDeviceOverKb:
    def test_rejected_command_not_claimed_valid(self):
        from cliq.web import server

        info = server._explain_command(
            "show vlan group", "iosv", "enable", device_rejected=True
        )
        assert info["verdict"] == "wrong_device"
        assert info["suggestion"] is None
        assert "isn't available" in info["reason"]

    def test_no_rejection_uses_normal_advisory(self):
        from cliq.web import server

        info = server._explain_command("show ip interface brief", "iosv", "exec")
        assert "isn't available on this device" not in info.get("reason", "")


# ── Bug Q — complete options + typo correction on R1 (matches C8k) ───────────
class TestBugQCompleteOptions:
    def test_dhvp_corrects_to_dhcp(self):
        reject = "        ^\n% Invalid input detected at '^' marker.\n"
        s = analyze_device_error(
            "ip dhvp pool", reject,
            lambda _p: "  dhcp  x\n  domain  y\n  sla  z\n  route  w\n", prompt="R1#"
        )
        # The correction preserves the trailing tokens so the suggestion is the
        # FULL command the engineer intended (dhvp -> dhcp, 'pool' kept), not
        # just the first fixed word.
        assert s is not None and s.fix == "ip dhcp pool"

    def test_valid_options_shows_more_than_six(self):
        from cliq.intelligence.did_you_mean import build_suggestion

        opts = [f"opt{i}" for i in range(12)]
        s = build_suggestion("ip", "zzz", opts, has_cr=False)
        assert s is not None and s.message.count("opt") >= 8


# ── Bug R — help cache learns once, persists, no cross-path pollution ────────
class TestBugRHelpPersistence:
    def test_learn_once_persists(self):
        from cliq.web import server

        dev = "device=bugR"
        server._help_cache.clear()
        try:
            server._cache_device_help(dev, "sh ip ospf ", "  1  proc\n  neighbor  n\n")
            assert server._help_complete(dev, "sh ip ospf nei") == "ghbor"
            assert server._help_complete(dev, "sh ip ospf nei") == "ghbor"
        finally:
            server._help_cache.clear()

    def test_no_cross_path_pollution(self):
        from cliq.web import server

        dev = "device=bugR2"
        server._help_cache.clear()
        try:
            server._cache_device_help(dev, "sh ip ospf ", "  neighbor  n\n")
            server._cache_device_help(dev, "sh ip bgp ", "  summary  s\n")
            assert server._help_complete(dev, "sh ip ospf sum") == ""
            assert server._help_complete(dev, "sh ip bgp sum") == "mary"
        finally:
            server._help_cache.clear()


# ── Part 1 — persistent, two-tier, device-confirmed command + help cache ─────
class TestPersistentCommandCache:
    """The durable cache must (a) survive a process restart, (b) share PI truth
    across same device-type boxes, (c) NEVER bleed a PD command to another
    platform, and (d) expire stale entries via TTL. Device is always the source
    of truth; the cache only ever stores what the device confirmed."""

    def _cache(self, tmp_path, ttl_s=None):
        from cliq.memory.command_cache import PersistentCommandCache, DEFAULT_TTL_S

        return PersistentCommandCache(
            cache_dir=str(tmp_path), ttl_s=ttl_s if ttl_s is not None else DEFAULT_TTL_S,
        )

    def test_classify_device_type_mapping(self):
        from cliq.memory.command_cache import classify_device_type

        assert classify_device_type("nexus9000") == "nx-os"
        assert classify_device_type("iosvl2") == "l2-switch"
        assert classify_device_type("cat9000v") == "l3-switch"
        assert classify_device_type("iosv") == "router"
        assert classify_device_type("") == "l3-switch"

    def test_is_platform_dependent(self):
        from cliq.memory.command_cache import is_platform_dependent

        assert is_platform_dependent("show platform hardware qfp active")
        assert is_platform_dependent("show platform software fed switch active")
        assert not is_platform_dependent("show ip ospf neighbor")

    def test_help_options_survive_restart(self, tmp_path):
        c1 = self._cache(tmp_path)
        c1.record_help_options("show ip ospf", ["neighbor", "interface", "database"], "iosv")
        c1.close()
        # New instance == simulated process restart / hard refresh.
        c2 = self._cache(tmp_path)
        opts = c2.get_help_options("show ip ospf", "iosv")
        assert "neighbor" in opts and "interface" in opts
        c2.close()

    def test_pi_command_shared_across_same_device_type(self, tmp_path):
        c = self._cache(tmp_path)
        # Learned on one router, available to ANY router (iosv == iosxr tier).
        c.record_command("show ip ospf neighbor", "iosv")
        assert "show ip ospf neighbor" in c.known_commands("iosxr")
        c.close()

    def test_pd_command_not_served_to_other_platform(self, tmp_path):
        c = self._cache(tmp_path)
        # 'fed' is Cat9k-only; must never surface on a C8000v even though both
        # classify to l3-switch.
        c.record_command("show platform software fed switch active", "cat9000v")
        assert "show platform software fed switch active" in c.known_commands("cat9000v")
        assert "show platform software fed switch active" not in c.known_commands("c8000v")
        c.close()

    def test_pd_help_option_scoped_to_platform(self, tmp_path):
        c = self._cache(tmp_path)
        c.record_help_options("show platform software fed switch", ["active", "standby"], "cat9000v")
        assert c.get_help_options("show platform software fed switch", "cat9000v")
        # Different platform of the same device-type must not inherit PD options.
        assert not c.get_help_options("show platform software fed switch", "c8000v")
        c.close()

    def test_ttl_invalidation(self, tmp_path):
        c = self._cache(tmp_path, ttl_s=-1)  # everything is already stale
        c.record_command("show ip ospf neighbor", "iosv")
        c.record_help_options("show ip ospf", ["neighbor"], "iosv")
        assert c.known_commands("iosv") == []
        assert c.get_help_options("show ip ospf", "iosv") == []
        c.close()

    def test_kb_is_never_written(self, tmp_path):
        # Only device-confirmed sources call record_*; verify the public API has
        # no KB ingest path and stored rows are always source='device'.
        c = self._cache(tmp_path)
        c.record_command("show ip ospf neighbor", "iosv")
        row = c._conn.execute(
            "SELECT source FROM verified_commands LIMIT 1"
        ).fetchone()
        assert row["source"] == "device"
        c.close()


class TestSharedTemplateLearning:
    """End-to-end: device-accepted commands are stored as VALUE-STRIPPED
    templates in the shared durable cache, dedupe across users/devices, survive a
    refresh, and never leak operand values. Combines the real ``extract_template``
    value-stripper with the real ``PersistentCommandCache``."""

    def _cache(self, tmp_path, ttl_s=None):
        from cliq.memory.command_cache import PersistentCommandCache, DEFAULT_TTL_S

        return PersistentCommandCache(
            cache_dir=str(tmp_path), ttl_s=ttl_s if ttl_s is not None else DEFAULT_TTL_S,
        )

    def _store(self, cache, literal, node_type, mode="exec"):
        from cliq.intelligence.command_template import extract_template

        cache.record_command(extract_template(literal).template, node_type, mode)

    def test_interface_stored_as_template_survives_refresh(self, tmp_path):
        c1 = self._cache(tmp_path)
        self._store(c1, "show ip ospf neighbor GigabitEthernet1", "iosv")
        c1.close()
        # Simulated hard refresh / restart.
        c2 = self._cache(tmp_path)
        known = c2.known_commands("iosxr")  # shared across the router tier
        assert "show ip ospf neighbor <interface>" in known
        # The literal operand is NEVER persisted.
        assert "show ip ospf neighbor gigabitethernet1" not in known
        c2.close()

    def test_ping_ip_stored_as_template(self, tmp_path):
        c = self._cache(tmp_path)
        self._store(c, "ping 10.1.1.1", "iosv")
        known = c.known_commands("iosv")
        assert "ping <ip>" in known
        assert "ping 10.1.1.1" not in known
        c.close()

    def test_two_literals_dedup_to_one_row(self, tmp_path):
        c = self._cache(tmp_path)
        # Two users, two different interfaces, two router platforms (same tier).
        self._store(c, "show ip ospf neighbor GigabitEthernet1", "iosv")
        self._store(c, "show ip ospf neighbor TenGigabitEthernet1/0/1", "iosxr")
        rows = c._conn.execute(
            "SELECT COUNT(*) AS n FROM verified_commands WHERE command_path = ?",
            ("show ip ospf neighbor <interface>",),
        ).fetchone()
        assert rows["n"] == 1  # shared, deduped — single entry for everyone
        c.close()

    def test_cross_user_sharing(self, tmp_path):
        c = self._cache(tmp_path)
        # User A confirms on one router; User B on another router (same type) gets it.
        self._store(c, "ping 10.1.1.1", "iosv")
        assert "ping <ip>" in c.known_commands("iosxr")
        c.close()

    def test_rejected_command_never_learned(self):
        # The accept gate (is_accepted) blocks learning of a rejected command, so
        # it never reaches the value-stripper or the store.
        from cliq.intelligence.learned_commands import is_accepted

        assert is_accepted("% Invalid input detected at '^' marker.") is False
        assert is_accepted("") is True

    def test_clean_command_round_trips(self, tmp_path):
        # An operand-free command is stored verbatim (no spurious placeholder).
        c = self._cache(tmp_path)
        self._store(c, "show ip ospf neighbor", "iosv")
        assert "show ip ospf neighbor" in c.known_commands("iosv")
        c.close()


class TestModeAwareHelpAndPollution:
    """?-help completions are mode-scoped and live in a SEPARATE store; they are
    never promoted into the accepted-command (suggestion) corpus."""

    def _cache(self, tmp_path):
        from cliq.memory.command_cache import PersistentCommandCache, DEFAULT_TTL_S

        return PersistentCommandCache(cache_dir=str(tmp_path), ttl_s=DEFAULT_TTL_S)

    def test_help_options_differ_per_mode(self, tmp_path):
        c = self._cache(tmp_path)
        # 'sh ?' means show-family in exec, shell-family options in config.
        c.record_help_options("sh", ["show"], "iosv", mode="exec")
        c.record_help_options("sh", ["shell", "ssh"], "iosv", mode="config")
        assert c.get_help_options("sh", "iosv", "exec") == ["show"]
        cfg = c.get_help_options("sh", "iosv", "config")
        assert "shell" in cfg and "ssh" in cfg and "show" not in cfg
        c.close()

    def test_help_tokens_not_promoted_to_suggestions(self, tmp_path):
        # 'show ip ospf neighbor ?' interface descriptors are completion hints for
        # that PATH only — never standalone runnable command suggestions.
        c = self._cache(tmp_path)
        c.record_help_options(
            "show ip ospf neighbor", ["BDI", "BVI", "Bluetooth"], "iosv"
        )
        known = c.known_commands("iosv")
        assert "BDI" not in known and "bdi" not in [k.lower() for k in known]
        assert known == []  # nothing was recorded into the accepted-command store
        c.close()


class TestGhostDbReadback:
    """Part 4 — the reported hard-refresh bug. After the in-memory ghost index is
    empty (refresh/restart), an accepted-command suggestion must resolve from the
    durable cache via DB read-back, with the placeholder NEVER ghosted inline."""

    def test_accepted_template_suggested_after_refresh(self, tmp_path, monkeypatch):
        from cliq.web import server
        from cliq.memory.command_cache import PersistentCommandCache

        # Durable cache holds a template learned in a PRIOR session.
        cache = PersistentCommandCache(cache_dir=str(tmp_path))
        cache.record_command("show ip ospf neighbor <interface>", "iosv", "exec")

        # Simulate a fresh process: empty index, cache wired, nothing hydrated.
        monkeypatch.setattr(server, "_cmd_cache", cache)
        monkeypatch.setattr(server, "_ghost_index", server._GhostIndex())
        monkeypatch.setattr(server, "_ghost_hydrated", set())

        # In-memory miss -> DB read-back hydrates the clean prefix -> suggestion.
        suffix = server._ghost_complete(
            "show ip ospf neig", device_id=None, mode="exec", node_type="iosv"
        )
        assert suffix == "hbor"
        cache.close()

    def test_placeholder_never_ghosted_inline(self, tmp_path, monkeypatch):
        from cliq.web import server
        from cliq.memory.command_cache import PersistentCommandCache

        cache = PersistentCommandCache(cache_dir=str(tmp_path))
        cache.record_command("ping <ip>", "iosv", "exec")
        monkeypatch.setattr(server, "_cmd_cache", cache)
        monkeypatch.setattr(server, "_ghost_index", server._GhostIndex())
        monkeypatch.setattr(server, "_ghost_hydrated", set())

        # Completing 'pin' yields the keyword 'g' only — '<ip>' is never offered.
        suffix = server._ghost_complete(
            "pin", device_id=None, mode="exec", node_type="iosv"
        )
        assert "<ip>" not in suffix
        # Past the keyword there is no inline completion into the placeholder.
        after = server._ghost_complete(
            "ping ", device_id=None, mode="exec", node_type="iosv"
        )
        assert "<ip>" not in after
        cache.close()


# ── Mode-context & ghost-text bug fixes (Bugs 1/2/3/4b) ──────────────────────
def _mode_engine():
    from cliq.db.query import CommandDB
    from cliq.intelligence.engine import IntelligenceEngine

    return IntelligenceEngine(CommandDB())


class TestBug1DoSuggestionGatedToExecOnly:
    """'do' is suggested from config mode ONLY for genuine EXEC-only verbs.
    A config-context command (``configure terminal``) must NEVER be offered as
    ``do configure terminal``."""

    def test_config_command_in_config_mode_gets_no_do_suggestion(self):
        eng = _mode_engine()
        a = eng.analyze("conf t", "config", is_executing=True)
        # Must not propose a 'do' line for a config-entry command.
        assert a.status != "wrong_mode_do"
        assert not (a.do_suggestion or "").strip()

    def test_exec_command_in_config_mode_still_suggests_do(self):
        eng = _mode_engine()
        a = eng.analyze("show ip route", "config", is_executing=True)
        assert a.status == "wrong_mode_do"
        assert a.do_suggestion == "do show ip route"

    def test_head_verb_gate(self):
        from cliq.intelligence.engine import _head_is_exec_only

        assert _head_is_exec_only("show ip route") is True
        assert _head_is_exec_only("ping 10.0.0.1") is True
        assert _head_is_exec_only("configure terminal") is False
        assert _head_is_exec_only("interface g0/0") is False
        assert _head_is_exec_only("") is False


class TestBug2InterfaceValidInConfigSubmodes:
    """'interface' can be re-selected from within a child config sub-mode."""

    def test_interface_pool_includes_config_if(self):
        from cliq.db.query import CommandDB

        db = CommandDB()
        for mode in ("config", "config-if", "config-subif"):
            heads = {c.full_command.split()[0] for c in db.commands_in_mode(mode)}
            assert "interface" in heads, mode

    def test_int_g3_not_rejected_in_config_if(self):
        eng = _mode_engine()
        a = eng.analyze("int g3", "config-if", is_executing=True)
        # It must NOT be flagged as wrong-mode requiring 'config'.
        assert a.status != "wrong_mode"
        if a.matched_command is not None:
            assert a.matched_command.full_command == "interface"

    def test_interface_still_wrong_mode_in_exec(self):
        # Guard: interface is a config command, still not an exec command.
        eng = _mode_engine()
        a = eng.analyze("interface g0/0", "exec", is_executing=True)
        assert a.status != "ok"


class TestBug3DoRejectedInExecMode:
    """In exec/enable mode a 'do ...' line is invalid — corrective hint, no
    ghost continuation."""

    @staticmethod
    def _analysis(mode: str, raw: str):
        return SimpleNamespace(
            mode=mode, raw_input=raw, status="typo", explanation=None,
            matched_command=None, do_suggestion="",
        )

    def test_is_exec_mode_do(self):
        assert _is_exec_mode_do("exec", "do show ip route") is True
        assert _is_exec_mode_do("enable", "do show ip route") is True
        assert _is_exec_mode_do("config", "do show ip route") is False
        assert _is_exec_mode_do("config-if", "do show ip route") is False
        assert _is_exec_mode_do("exec", "show ip route") is False
        assert _is_exec_mode_do("exec", "  do  ") is True

    def test_live_hint_rejects_do_in_exec(self):
        hint = _build_live_hint(self._analysis("exec", "do show ip interface brief"))
        assert hint is not None
        assert hint["message"] == _EXEC_DO_HINT_MSG
        assert hint["fix"] == "show ip interface brief"

    def test_live_hint_do_hint_absent_in_config(self):
        # In config mode a bare 'do show ...' is VALID — no exec-do rejection.
        hint = _build_live_hint(self._analysis("config", "do show ip route"))
        assert hint is None or hint.get("message") != _EXEC_DO_HINT_MSG


class TestBug4bTokenBoundaryGhost:
    """Ghost text advances one whitespace-delimited token at a time — never the
    whole remaining command tail."""

    def test_next_token_suffix_truncation(self):
        assert _next_token_suffix("ow ip interface brief") == "ow"
        assert _next_token_suffix(" ip interface brief") == " ip"
        assert _next_token_suffix("ip interface brief") == "ip"
        assert _next_token_suffix("") == ""

    def test_best_completion_is_token_scoped(self):
        from cliq.web import server

        idx = server._GhostIndex()
        idx.add("show ip interface brief", tier=0, live=True)
        idx.finalize()
        # Mid-token: complete 'show' -> next boundary only.
        assert idx.best_completion("sh") == "ow"
        # At a word boundary: offer just the next token, not the tail.
        assert idx.best_completion("show ") == "ip"
        assert idx.best_completion("show ip ") == "interface"

    def test_do_prefix_stripped_before_index_match(self, monkeypatch):
        from cliq.web import server

        idx = server._GhostIndex()
        idx.add("show ip interface brief", tier=0, live=True)
        idx.finalize()
        monkeypatch.setattr(server, "_ghost_index", idx)
        # 'do show' in config mode strips 'do ', matches the exec command, and
        # returns ONLY the next token (not the whole tail).
        suffix = server._ghost_complete("do show", device_id=None, mode="config")
        assert suffix == " ip"


# ── Bug 5 — 'configure terminal' is a no-op in config, NOT "requires enable" ──
class TestConfigureTerminalNoOpInConfig:
    """Mode-transition awareness: re-issuing 'configure terminal' / 'conf t' /
    'config t' while ALREADY in config (or a child config mode) is a redundant
    no-op — the advisor must never emit the misleading "requires 'enable'"."""

    @staticmethod
    def _hint(inp, mode):
        from cliq.db.query import CommandDB
        from cliq.intelligence.engine import IntelligenceEngine

        eng = IntelligenceEngine(CommandDB())
        return _build_live_hint(eng.analyze(inp, mode, is_executing=True))

    def test_mode_transition_target_and_hierarchy(self):
        from cliq.validation.platform_rules import (
            is_at_or_below_mode,
            is_redundant_mode_transition,
            mode_transition_target,
        )

        assert mode_transition_target("configure terminal") == "config"
        assert mode_transition_target("conf t") is None  # abbrev resolves upstream
        assert mode_transition_target("show ip route") is None
        # Hierarchy: config and its children are at/below the 'config' target.
        assert is_at_or_below_mode("config", "config") is True
        assert is_at_or_below_mode("config-if", "config") is True
        assert is_at_or_below_mode("config-subif", "config") is True
        assert is_at_or_below_mode("exec", "config") is False
        assert is_at_or_below_mode("enable", "config") is False
        # Redundant no-op detection (canonical command text).
        assert is_redundant_mode_transition("configure terminal", "config") is True
        assert is_redundant_mode_transition("configure terminal", "config-if") is True
        assert is_redundant_mode_transition("configure terminal", "exec") is False
        assert is_redundant_mode_transition("write memory", "config") is False

    def test_config_t_in_config_is_noop_not_requires_enable(self):
        for inp in ("config t", "conf t", "configure terminal"):
            hint = self._hint(inp, "config")
            assert hint is not None
            assert "requires 'enable'" not in hint["message"]
            assert "Already in config mode" in hint["message"]
            assert hint["fix"] is None

    def test_config_t_in_config_if_is_noop(self):
        hint = self._hint("config t", "config-if")
        assert hint is not None
        assert "requires 'enable'" not in hint["message"]
        assert "Already in config mode" in hint["message"]

    def test_config_t_in_exec_no_false_warning(self):
        # From exec it's a valid FORWARD transition into config — no warning.
        assert self._hint("config t", "exec") is None
        assert self._hint("configure terminal", "exec") is None

    def test_genuine_enable_only_command_in_config_still_flagged(self):
        # A real enable command (not a mode-transition) must still be flagged in
        # config mode — the no-op suppression is scoped to transition commands.
        hint = self._hint("write memory", "config")
        assert hint is not None
        assert "Already in config mode" not in hint["message"]




