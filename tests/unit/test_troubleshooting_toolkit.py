#!/usr/bin/env python3
"""Unit tests for the CLIQ Troubleshooting Toolkit v2 (formerly the OSPF toolkit).

This suite is PROTOCOL-AGNOSTIC by construction: every schema/field/impactful
check is parametrized over *every* ``command_db/troubleshooting/*.yaml`` recipe
discovered at collection time, and every engine behaviour (flexible intent,
capability gating, live-device rejection, mode adaptation, ?-validation
suppression, sticky-context ranking bias, and discovery source merge/dedupe/
attribution) is exercised generically against the loaded toolkits. Dropping an
EIGRP/BGP recipe into the directory therefore needs ZERO new test files — the
parametrization picks it up automatically.

All device state is injected (capability / rejected / mode / ?-tokens) so the
tests stay pure, offline, and synchronous — mirroring the in-memory hot-path
contract. The temp-dir extensibility proof remains.
"""

from pathlib import Path
from types import SimpleNamespace

import asyncio

import pytest

from cliq.intelligence.troubleshooting import (
    _TOOLKITS_DIR,
    build_toolkit_payload,
    capability_allows,
    detect_discovery_intent,
    detect_toolkit_intent,
    get_toolkit,
    load_toolkits,
    merge_discovery_sources,
)
from cliq.web.server import (
    _GhostIndex,
    _active_context_protocol,
    _build_discovery_reference,
    _build_toolkit_advisory,
    _protocol_for_command,
    _set_active_context,
    _toolkit_mode_command,
)


ROUTING = frozenset({"routing"})
SWITCHING = frozenset({"switching"})

_TOOLKIT_DIR_PATH = Path(_TOOLKITS_DIR)


def _discover_yaml_files() -> list[Path]:
    """Every shipped troubleshooting recipe (auto-discovered)."""
    if not _TOOLKIT_DIR_PATH.exists():
        return []
    return sorted(_TOOLKIT_DIR_PATH.glob("*.yaml"))


_YAML_FILES = _discover_yaml_files()
_PROTOCOLS = sorted(load_toolkits(refresh=True).keys())


def _rt(capability=ROUTING, rejected=None, mode="exec", active_context=None):
    """Build a minimal runtime stand-in (no live device)."""
    return SimpleNamespace(
        capability=capability,
        rejected=set(rejected or set()),
        mode=mode,
        node_type="",
        active_context=active_context,
        device=None,
    )


# --------------------------------------------------------------------------- #
# Schema / required fields — parametrized over EVERY recipe file
# --------------------------------------------------------------------------- #

@pytest.mark.skipif(not _YAML_FILES, reason="no troubleshooting recipes found")
class TestRecipeSchema:
    @pytest.mark.parametrize("yaml_path", _YAML_FILES, ids=lambda p: p.stem)
    def test_recipe_loads_and_has_required_fields(self, yaml_path):
        toolkits = load_toolkits(str(yaml_path.parent), refresh=True)
        protocol = yaml_path.stem
        assert protocol in toolkits, f"{protocol} recipe failed to load"
        tk = toolkits[protocol]
        assert tk.protocol == protocol
        assert tk.title, "title is required"
        assert tk.phases, "at least one diagnostic phase is required"

    @pytest.mark.parametrize("protocol", _PROTOCOLS)
    def test_every_command_has_a_base_and_mode(self, protocol):
        payload = build_toolkit_payload(
            protocol, capability=ROUTING, mode_adapter=_toolkit_mode_command,
        )
        assert payload is not None
        for phase in payload["phases"]:
            assert phase["name"]
            assert phase["commands"], "a phase must not be empty"
            for c in phase["commands"]:
                assert c["command"]
                assert c["base_command"]
                assert "impactful" in c

    @pytest.mark.parametrize("protocol", _PROTOCOLS)
    def test_debug_and_clear_commands_flagged_impactful(self, protocol):
        payload = build_toolkit_payload(protocol, capability=ROUTING)
        assert payload is not None
        for phase in payload["phases"]:
            for c in phase["commands"]:
                head = c["base_command"].split()[0]
                if head in ("debug", "clear"):
                    assert c["impactful"] is True, (
                        f"{c['base_command']} must be flagged impactful"
                    )
                if head == "show":
                    assert c["impactful"] is False

    @pytest.mark.parametrize("protocol", _PROTOCOLS)
    def test_safety_note_present_iff_impactful(self, protocol):
        payload = build_toolkit_payload(protocol, capability=ROUTING)
        assert payload is not None
        has_impactful = any(
            c["impactful"] for p in payload["phases"] for c in p["commands"]
        )
        assert bool(payload["safety_note"]) == has_impactful

    @pytest.mark.parametrize("protocol", _PROTOCOLS)
    def test_no_auto_run_affordance_anywhere(self, protocol):
        payload = build_toolkit_payload(protocol, capability=ROUTING)
        for p in payload["phases"]:
            for c in p["commands"]:
                assert "auto_run" not in c
                assert "execute" not in c


# --------------------------------------------------------------------------- #
# Feature 1 — flexible, conservative intent (generic, data-driven)
# --------------------------------------------------------------------------- #

@pytest.mark.skipif(not _PROTOCOLS, reason="no toolkits loaded")
class TestFlexibleIntent:
    @pytest.mark.parametrize("protocol", _PROTOCOLS)
    @pytest.mark.parametrize("template", [
        "troubleshoot {p}",
        "troubleshooting {p}",
        "working on {p}",
        "i'm on {p}",
        "need {p} commands to troubleshoot",
        "help with {p}",
        "{p} commands",
    ])
    def test_flexible_phrasings_surface_toolkit(self, protocol, template):
        assert detect_toolkit_intent(template.format(p=protocol)) == protocol

    @pytest.mark.parametrize("text", [
        "show version",
        "how do I configure a vlan",
        "what time is it",
        "reload the router",
    ])
    def test_conservative_no_false_trigger(self, text):
        assert detect_toolkit_intent(text) is None

    @pytest.mark.parametrize("protocol", _PROTOCOLS)
    def test_bare_protocol_word_does_not_fire(self, protocol):
        # A bare protocol word with no signal/command context stays silent.
        assert detect_toolkit_intent(protocol) is None

    def test_empty_input(self):
        assert detect_toolkit_intent("") is None

    @pytest.mark.parametrize("protocol", _PROTOCOLS)
    def test_partial_command_context_triggers(self, protocol):
        tk = get_toolkit(protocol)
        if not tk.intent.command_prefixes:
            pytest.skip("recipe declares no command prefixes")
        assert detect_toolkit_intent(tk.intent.command_prefixes[0]) == protocol


# --------------------------------------------------------------------------- #
# Capability gating (device-is-truth: permissive when unknown)
# --------------------------------------------------------------------------- #

@pytest.mark.skipif(not _PROTOCOLS, reason="no toolkits loaded")
class TestCapabilityGating:
    @pytest.mark.parametrize("protocol", _PROTOCOLS)
    def test_surfaces_on_matching_capability(self, protocol):
        tk = get_toolkit(protocol)
        caps = frozenset({tk.required_capability}) if tk.required_capability else frozenset()
        payload = build_toolkit_payload(protocol, capability=caps)
        assert payload is not None

    @pytest.mark.parametrize("protocol", _PROTOCOLS)
    def test_suppressed_on_mismatched_capability(self, protocol):
        tk = get_toolkit(protocol)
        if not tk.required_capability:
            pytest.skip("recipe has no capability requirement")
        wrong = SWITCHING if tk.required_capability == "routing" else ROUTING
        assert build_toolkit_payload(protocol, capability=wrong) is None

    @pytest.mark.parametrize("protocol", _PROTOCOLS)
    def test_permissive_when_capability_unknown(self, protocol):
        assert build_toolkit_payload(protocol, capability=frozenset()) is not None

    def test_capability_allows_helper(self):
        assert capability_allows("routing", ROUTING) is True
        assert capability_allows("routing", SWITCHING) is False
        assert capability_allows("routing", frozenset()) is True
        assert capability_allows("", SWITCHING) is True


# --------------------------------------------------------------------------- #
# Mode adaptation (exec vs `do show` in config) — reuses the do-gating table
# --------------------------------------------------------------------------- #

@pytest.mark.skipif(not _PROTOCOLS, reason="no toolkits loaded")
class TestModeAdaptation:
    @pytest.mark.parametrize("protocol", _PROTOCOLS)
    def test_exec_commands_plain(self, protocol):
        payload = build_toolkit_payload(
            protocol, mode="exec", capability=ROUTING,
            mode_adapter=_toolkit_mode_command,
        )
        cmds = [c["command"] for p in payload["phases"] for c in p["commands"]]
        assert not any(c.startswith("do ") for c in cmds)

    @pytest.mark.parametrize("protocol", _PROTOCOLS)
    def test_config_mode_prefixes_exec_verbs_with_do(self, protocol):
        payload = build_toolkit_payload(
            protocol, mode="config", capability=ROUTING,
            mode_adapter=_toolkit_mode_command,
        )
        for p in payload["phases"]:
            for c in p["commands"]:
                head = c["base_command"].split()[0]
                if head in ("show", "debug", "ping", "traceroute", "clear"):
                    assert c["command"].startswith("do "), c["command"]
                # base_command is always the bare, un-prefixed form.
                assert not c["base_command"].startswith("do ")

    def test_do_prefix_helper_direct(self):
        assert _toolkit_mode_command("show ip ospf", "config-if") == "do show ip ospf"
        assert _toolkit_mode_command("show ip ospf", "exec") == "show ip ospf"


# --------------------------------------------------------------------------- #
# Live-device rejection (Feature 4) — rejected commands are suppressed
# --------------------------------------------------------------------------- #

@pytest.mark.skipif(not _PROTOCOLS, reason="no toolkits loaded")
class TestRejectionSuppression:
    @pytest.mark.parametrize("protocol", _PROTOCOLS)
    def test_rejected_command_excluded(self, protocol):
        payload = build_toolkit_payload(protocol, capability=ROUTING)
        first = payload["phases"][0]["commands"][0]["base_command"]
        gated = build_toolkit_payload(
            protocol, capability=ROUTING, rejected=frozenset({first}),
        )
        remaining = [
            c["base_command"]
            for p in (gated["phases"] if gated else []) for c in p["commands"]
        ]
        assert first not in remaining

    @pytest.mark.parametrize("protocol", _PROTOCOLS)
    def test_rejection_case_insensitive(self, protocol):
        payload = build_toolkit_payload(protocol, capability=ROUTING)
        first = payload["phases"][0]["commands"][0]["base_command"]
        gated = build_toolkit_payload(
            protocol, capability=ROUTING, rejected=frozenset({first.upper()}),
        )
        remaining = [
            c["base_command"]
            for p in (gated["phases"] if gated else []) for c in p["commands"]
        ]
        assert first not in remaining


# --------------------------------------------------------------------------- #
# Feature 2 — sticky protocol context: ranking BIAS (never a filter)
# --------------------------------------------------------------------------- #

class TestStickyContextBias:
    def _index(self):
        idx = _GhostIndex()
        # Same tier + count so only the bias can reorder them.
        idx.add("show ip ospf neighbor", tier=1, count=5, live=True)
        idx.add("show version", tier=1, count=5, live=True)
        idx.add("show ip interface brief", tier=1, count=5, live=True)
        idx.finalize()
        return idx

    def test_bias_ranks_matching_protocol_first(self):
        idx = self._index()
        # With an OSPF bias, 'show ip o...' completions prefer the ospf key.
        biased = idx.best_completion_biased("show ip ", "ospf")
        assert biased == "ospf"

    def test_bias_does_not_filter_unrelated(self):
        idx = self._index()
        # 'show ve...' still completes to 'version' even while biased to ospf —
        # bias re-orders, it never removes candidates.
        assert idx.best_completion_biased("show ve", "ospf") == "rsion"

    def test_no_bias_matches_plain_completion(self):
        idx = self._index()
        assert idx.best_completion_biased("show ve", None) == idx.best_completion("show ve")

    def test_set_and_read_active_context_roundtrip(self):
        rt = _rt()
        _set_active_context(rt, "ospf", "advisory")
        assert _active_context_protocol(rt) == "ospf"
        assert rt.active_context["source"] == "advisory"

    def test_context_idle_timeout_clears(self):
        rt = _rt()
        _set_active_context(rt, "ospf", "cli")
        rt.active_context["ts"] = 0  # far in the past → idle-expired
        assert _active_context_protocol(rt) is None
        assert rt.active_context is None

    @pytest.mark.parametrize("protocol", _PROTOCOLS)
    def test_protocol_for_command_maps_prefix(self, protocol):
        tk = get_toolkit(protocol)
        if not tk.intent.command_prefixes:
            pytest.skip("recipe declares no command prefixes")
        prefix = tk.intent.command_prefixes[0]
        assert _protocol_for_command(prefix + " neighbor") == protocol

    def test_protocol_for_command_none_for_unrelated(self):
        assert _protocol_for_command("show version") is None


# --------------------------------------------------------------------------- #
# Feature 3 — discovery intent + source merge / dedupe / attribution
# --------------------------------------------------------------------------- #

@pytest.mark.skipif(not _PROTOCOLS, reason="no toolkits loaded")
class TestDiscoveryIntent:
    @pytest.mark.parametrize("protocol", _PROTOCOLS)
    def test_enumeration_phrases_trigger_discovery(self, protocol):
        assert detect_discovery_intent(f"show me all {protocol} commands") == (protocol, "all")
        assert detect_discovery_intent(f"list {protocol} show commands") == (protocol, "show")
        assert detect_discovery_intent(f"{protocol} debug commands") == (protocol, "debug")

    @pytest.mark.parametrize("protocol", _PROTOCOLS)
    def test_plain_commands_defers_to_toolkit(self, protocol):
        # "<proto> commands" (no enumeration signal/scope) is a TOOLKIT query,
        # not discovery — discovery must stay silent so the two don't collide.
        assert detect_discovery_intent(f"{protocol} commands") is None

    def test_troubleshoot_is_not_discovery(self):
        assert detect_discovery_intent("troubleshoot ospf") is None


class TestDiscoveryMerge:
    def test_authority_dedupe_device_over_learned_over_kb(self):
        payload = merge_discovery_sources(
            "ospf",
            device=["show ip ospf neighbor"],
            learned=["show ip ospf neighbor", "show ip ospf interface"],
            kb=["show ip ospf neighbor", "show ip ospf database"],
            capability=ROUTING,
            required_capability="routing",
        )
        assert payload is not None
        by_cmd = {
            c["base_command"]: c
            for g in payload["groups"] for c in g["commands"]
        }
        # The command present in all three keeps the highest authority (device).
        assert by_cmd["show ip ospf neighbor"]["source"] == "device"
        assert by_cmd["show ip ospf neighbor"]["verified"] is True
        # Learned-only and KB-only keep their own attribution.
        assert by_cmd["show ip ospf interface"]["source"] == "learned"
        assert by_cmd["show ip ospf database"]["source"] == "kb"
        assert by_cmd["show ip ospf database"]["verified"] is False

    def test_scope_filters_to_show_only(self):
        payload = merge_discovery_sources(
            "ospf",
            device=["show ip ospf neighbor", "debug ip ospf adj", "clear ip ospf process"],
            scope="show",
            capability=ROUTING,
            required_capability="routing",
        )
        bases = [c["base_command"] for g in payload["groups"] for c in g["commands"]]
        assert bases == ["show ip ospf neighbor"]

    def test_impactful_debug_and_clear_marked(self):
        payload = merge_discovery_sources(
            "ospf",
            device=["debug ip ospf adj", "clear ip ospf process"],
            capability=ROUTING,
            required_capability="routing",
        )
        for g in payload["groups"]:
            for c in g["commands"]:
                assert c["impactful"] is True
        assert payload["safety_note"]

    def test_rejected_command_suppressed(self):
        payload = merge_discovery_sources(
            "ospf",
            device=["show ip ospf neighbor", "show ip ospf database"],
            rejected=frozenset({"show ip ospf database"}),
            capability=ROUTING,
            required_capability="routing",
        )
        bases = [c["base_command"] for g in payload["groups"] for c in g["commands"]]
        assert "show ip ospf database" not in bases
        assert "show ip ospf neighbor" in bases

    def test_capability_gate_suppresses_whole_reference(self):
        assert merge_discovery_sources(
            "ospf",
            device=["show ip ospf neighbor"],
            capability=SWITCHING,
            required_capability="routing",
        ) is None

    def test_offline_marks_everything_unverified(self):
        payload = merge_discovery_sources(
            "ospf",
            learned=["show ip ospf neighbor"],
            kb=["show ip ospf database"],
            capability=ROUTING,
            required_capability="routing",
            device_available=False,
        )
        assert payload["device_available"] is False
        assert payload["unverified_note"]
        assert all(
            c["verified"] is False
            for g in payload["groups"] for c in g["commands"]
        )


# --------------------------------------------------------------------------- #
# Feature 4 — ?-validation posture: unadvertised commands are down-ranked out
# --------------------------------------------------------------------------- #

class TestDeviceValidation:
    def test_device_source_is_authoritative_over_kb(self):
        # A KB command NOT advertised by the device is marked unverified; a
        # device-advertised command is verified. This is the ?-context-help
        # authority the discovery merge encodes.
        payload = merge_discovery_sources(
            "ospf",
            device=["show ip ospf neighbor"],
            kb=["show ip ospf neighbor brief"],  # not advertised by device
            capability=ROUTING,
            required_capability="routing",
        )
        by_cmd = {
            c["base_command"]: c for g in payload["groups"] for c in g["commands"]
        }
        assert by_cmd["show ip ospf neighbor"]["verified"] is True
        assert by_cmd["show ip ospf neighbor brief"]["verified"] is False


# --------------------------------------------------------------------------- #
# Server advisory wiring (toolkit + discovery detection paths)
# --------------------------------------------------------------------------- #

@pytest.mark.skipif(not _PROTOCOLS, reason="no toolkits loaded")
class TestServerAdvisory:
    @pytest.mark.parametrize("protocol", _PROTOCOLS)
    def test_advisory_surfaces_for_routing_runtime(self, protocol):
        payload = _build_toolkit_advisory(f"troubleshoot {protocol}", _rt(), "exec")
        assert payload is not None
        assert payload["protocol"] == protocol

    def test_advisory_none_without_intent(self):
        assert _build_toolkit_advisory("show version", _rt(), "exec") is None

    @pytest.mark.parametrize("protocol", _PROTOCOLS)
    def test_discovery_reference_offline_fallback(self, protocol):
        # No live device → reference still assembles from learned + KB and is
        # marked unverified (graceful offline fallback).
        ref = asyncio.run(
            _build_discovery_reference(
                f"show me all {protocol} commands", _rt(), "exec", None,
            )
        )
        # May be None if neither learned nor KB has any commands for the proto;
        # when present it must be flagged offline/unverified.
        if ref is not None:
            assert ref["device_available"] is False
            assert ref["unverified_note"]


# --------------------------------------------------------------------------- #
# Extensibility — a stub EIGRP recipe dropped in needs NO code change
# --------------------------------------------------------------------------- #

class TestExtensibility:
    def test_stub_eigrp_recipe_is_detected(self, tmp_path):
        (tmp_path / "eigrp.yaml").write_text(
            "protocol: eigrp\n"
            "title: EIGRP Troubleshooting Toolkit\n"
            "required_capability: routing\n"
            "intent:\n"
            "  keywords: ['eigrp']\n"
            "  protocol_aliases: ['eigrp']\n"
            "  phrases: ['troubleshoot eigrp']\n"
            "  command_prefixes: ['show ip eigrp', 'debug ip eigrp']\n"
            "discovery:\n"
            "  walk_paths: ['show ip eigrp', 'debug ip eigrp', 'clear ip eigrp']\n"
            "  learned_prefixes: ['show ip eigrp']\n"
            "  kb_query: 'eigrp'\n"
            "phases:\n"
            "  - name: Neighbor/Adjacency\n"
            "    commands:\n"
            "      - command: show ip eigrp neighbors\n"
            "        description: EIGRP neighbor table.\n"
            "      - command: debug ip eigrp\n"
            "        description: EIGRP event debug.\n"
            "        impactful: true\n",
            encoding="utf-8",
        )
        toolkits = load_toolkits(str(tmp_path), refresh=True)
        assert "eigrp" in toolkits
        # Flexible intent + discovery both work with zero code changes.
        assert detect_toolkit_intent("working on eigrp", directory=str(tmp_path)) == "eigrp"
        payload = build_toolkit_payload(
            "eigrp", capability=ROUTING, directory=str(tmp_path),
        )
        assert payload is not None
        # debug flagged impactful; safety note present.
        assert payload["safety_note"]

    def test_malformed_recipe_is_skipped(self, tmp_path):
        (tmp_path / "broken.yaml").write_text("not: a valid toolkit\n", encoding="utf-8")
        assert load_toolkits(str(tmp_path), refresh=True) == {}


def test_default_dir_loads_at_least_one_recipe():
    toolkits = load_toolkits(refresh=True)
    assert toolkits, "the shipped default directory must contain recipes"
    assert "ospf" in toolkits
