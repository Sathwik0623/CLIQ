"""Bug P (Ask path) — device > cache > KB reconciliation for the Q&A advisory.

These tests verify that the natural-language "Ask CLIQ" path defers to the live
device exactly like the terminal/Explain paths:

* a command the device SYNTAX-rejected is suppressed (never offered as valid),
* a routing/switching command is gated by the device's capability class,
* a platform-mismatched KB row is demoted to enrichment-only (kept, flagged),
* with no device connected the KB-only behavior is preserved (graceful fallback).

The reconciliation helpers are pure (aside from best-effort observability
logging), so they are unit-tested directly with lightweight fakes instead of
spinning up the full FastAPI app + KB.
"""

from __future__ import annotations

from types import SimpleNamespace

from cliq.web.server import (
    _apply_qa_reconciliation,
    _filter_sequence_for_device,
    _reconcile_qa_result,
)


def _result(command, *, platform="iosxe", description="", source="OSPF PDF",
            confidence=0.9, mode="exec", match_type="exact"):
    """Build a fake retrieval result with the attributes the helpers read."""
    return SimpleNamespace(
        command=command,
        description=description or f"{command} description",
        platform=platform,
        mode=mode,
        confidence_score=confidence,
        source_attribution=source,
        match_type=match_type,
    )


def _runtime(node_type, *, capability, rejected=()):
    """Build a fake DeviceRuntime-like object."""
    return SimpleNamespace(
        node_type=node_type,
        capability=frozenset(capability),
        rejected=set(rejected),
    )


def _response(results, groups=()):
    grouped = [SimpleNamespace(label=lbl, results=res) for lbl, res in groups]
    return SimpleNamespace(results=list(results), grouped_results=grouped)


# Common fixtures.
ROUTER = lambda **kw: _runtime("iosv", capability={"routing"}, **kw)        # noqa: E731
L3_BOTH = lambda **kw: _runtime("cat8000v", capability={"routing", "switching"}, **kw)  # noqa: E731
L2_SWITCH = lambda **kw: _runtime("iosvl2", capability={"switching"}, **kw)  # noqa: E731


class TestRejectionSuppression:
    def test_device_rejected_command_is_suppressed(self):
        # R1 (iosv) rejected 'show vlan'; asking about VLANs must NOT present it.
        rt = ROUTER(rejected={"show vlan"})
        outcome, serialized = _reconcile_qa_result(_result("show vlan", platform="iosxe"), rt, "iosv")
        assert outcome == "suppressed"
        assert serialized is None

    def test_rejection_is_case_insensitive(self):
        rt = ROUTER(rejected={"SHOW VLAN"})
        outcome, serialized = _reconcile_qa_result(_result("show vlan"), rt, "iosv")
        assert outcome == "suppressed"
        assert serialized is None


class TestCapabilityGate:
    def test_switching_command_gated_on_router(self):
        # Even without an explicit rejection, a switching command must not
        # surface on a routing-only device.
        rt = ROUTER()
        outcome, serialized = _reconcile_qa_result(_result("show vlan brief"), rt, "iosv")
        assert outcome == "suppressed"
        assert serialized is None

    def test_spanning_tree_gated_on_router(self):
        rt = ROUTER()
        outcome, _ = _reconcile_qa_result(_result("show spanning-tree"), rt, "iosv")
        assert outcome == "suppressed"

    def test_routing_command_surfaces_on_both_device(self):
        # C8k routes AND switches → OSPF passes the capability gate, and since
        # both classify to l3-switch there is no platform demotion: clean pass.
        rt = L3_BOTH()
        outcome, serialized = _reconcile_qa_result(
            _result("show ip ospf neighbor", platform="iosxe"), rt, "cat8000v",
        )
        assert outcome == "passed"
        assert serialized is not None
        assert "reconcile" not in serialized
        assert serialized["command"] == "show ip ospf neighbor"

    def test_capability_neutral_command_always_passes(self):
        # 'show version' is capability-neutral ('both') → never gated.
        rt = ROUTER()
        outcome, serialized = _reconcile_qa_result(
            _result("show version", platform="iosv"), rt, "iosv",
        )
        assert outcome == "passed"
        assert serialized is not None


class TestPlatformDemotion:
    def test_platform_mismatch_demoted_to_enrichment(self):
        # iosv (router) asking OSPF: routing IS allowed, but the KB row is from
        # an iosxe (l3-switch) doc → kept but flagged enrichment-only.
        rt = ROUTER()
        outcome, serialized = _reconcile_qa_result(
            _result("show ip ospf neighbor", platform="iosxe", source="OSPF PDF"),
            rt, "iosv",
        )
        assert outcome == "enrichment_only"
        assert serialized is not None
        assert serialized["reconcile"] == "enrichment_only"
        assert "enrichment only" in serialized["source"].lower()
        # The command still surfaces (discovery preserved).
        assert serialized["command"] == "show ip ospf neighbor"

    def test_same_device_type_not_demoted(self):
        rt = L3_BOTH()
        outcome, serialized = _reconcile_qa_result(
            _result("show ip ospf neighbor", platform="iosxe"), rt, "cat8000v",
        )
        assert outcome == "passed"
        assert "reconcile" not in serialized


class TestSwitchingDeviceSurfacing:
    """LAN-switching commands must SURFACE on switching-capable devices (the
    point of ingesting the lan-switching PDF) while staying gated off routers."""

    def test_vlan_surfaces_on_l2_switch(self):
        # iosvl2 is switching-capable → 'show vlan brief' is NOT suppressed.
        rt = L2_SWITCH()
        outcome, serialized = _reconcile_qa_result(
            _result("show vlan brief", platform="iosxe", source="lan-switching PDF"),
            rt, "iosvl2",
        )
        assert outcome != "suppressed"
        assert serialized is not None
        assert serialized["command"] == "show vlan brief"

    def test_vlan_surfaces_clean_on_l3_switch(self):
        # C8000v (l3-switch; KB iosxe also classifies l3-switch) → clean pass.
        rt = L3_BOTH()
        outcome, serialized = _reconcile_qa_result(
            _result("show vlan brief", platform="iosxe"), rt, "cat8000v",
        )
        assert outcome == "passed"
        assert "reconcile" not in serialized

    def test_spanning_tree_surfaces_on_l2_switch(self):
        rt = L2_SWITCH()
        outcome, serialized = _reconcile_qa_result(
            _result("show spanning-tree vlan 1", platform="iosxe"), rt, "iosvl2",
        )
        assert outcome != "suppressed"
        assert serialized is not None

    def test_routing_gated_off_l2_switch(self):
        # Mirror direction: a routing command must NOT surface on a pure L2 switch.
        rt = L2_SWITCH()
        outcome, serialized = _reconcile_qa_result(
            _result("show ip ospf neighbor", platform="iosxe"), rt, "iosvl2",
        )
        assert outcome == "suppressed"
        assert serialized is None


class TestNoDeviceFallback:
    def test_no_runtime_passes_through_unchanged(self):
        outcome, serialized = _reconcile_qa_result(_result("show vlan"), None, "iosxe")
        assert outcome == "passed"
        assert serialized is not None
        assert "reconcile" not in serialized
        assert serialized["command"] == "show vlan"


class TestApplyReconciliation:
    def test_results_and_groups_reconciled_with_summary(self):
        rt = ROUTER(rejected={"show vlan"})
        resp = _response(
            results=[
                _result("show vlan"),                       # rejected -> suppressed
                _result("show spanning-tree"),              # switching -> suppressed
                _result("show ip ospf neighbor", platform="iosxe"),  # routing -> enrichment
                _result("show version", platform="iosv"),   # neutral, same type -> passed
            ],
            groups=[
                ("Switching", [_result("show vlan brief")]),            # emptied -> dropped
                ("Routing", [_result("show ip route", platform="iosv")]),  # kept
            ],
        )
        results, groups = _apply_qa_reconciliation(resp, rt, "iosv", "how do I check vlans")

        commands = [r["command"] for r in results]
        assert "show vlan" not in commands
        assert "show spanning-tree" not in commands
        assert "show ip ospf neighbor" in commands
        assert "show version" in commands

        # The OSPF row is flagged enrichment-only.
        ospf = next(r for r in results if r["command"] == "show ip ospf neighbor")
        assert ospf["reconcile"] == "enrichment_only"

        # The all-switching group is dropped; the routing group survives.
        labels = [g["label"] for g in groups]
        assert "Switching" not in labels
        assert "Routing" in labels

    def test_no_device_keeps_everything(self):
        resp = _response(results=[_result("show vlan"), _result("show spanning-tree")])
        results, groups = _apply_qa_reconciliation(resp, None, "iosxe", "vlans")
        assert {r["command"] for r in results} == {"show vlan", "show spanning-tree"}


class TestSequenceFilter:
    def test_rejected_and_wrong_capability_steps_dropped(self):
        rt = ROUTER(rejected={"vlan 10"})
        steps = [
            {"command": "vlan 10"},               # rejected -> dropped
            {"command": "switchport mode access"},  # switching -> dropped
            {"command": "router ospf 1"},          # routing -> kept
            {"command": "show running-config"},    # neutral -> kept
        ]
        kept = [s["command"] for s in _filter_sequence_for_device(steps, rt)]
        assert kept == ["router ospf 1", "show running-config"]

    def test_no_device_keeps_all_steps(self):
        steps = [{"command": "vlan 10"}, {"command": "router ospf 1"}]
        assert _filter_sequence_for_device(steps, None) == steps
