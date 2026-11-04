"""
Regression tests for M1.4 live planning defects.

1. "802.1Q trunk" must never create VLAN 802; genuine VLAN 802 still works.
2. Linux endpoints render only capability-verified commands (BusyBox
   ifconfig/route baseline); sudo is never rendered; missing capability
   evidence blocks the plan with a question.
3. Trunk intent naming both ends configures both switches.

Mocked runtimes only; no live devices.
"""

import asyncio
from types import SimpleNamespace

import pytest

from cliq.ai import config_exec, config_plan
from cliq.ai.config_plan import parse_config_intent
from cliq.ai.device_exec import (
    plan_autonomous_execution,
    validate_command_for_platform,
)


class FakeConsoleDevice:
    def __init__(self, outputs=None, prompt="SW1#"):
        self.outputs = dict(outputs or {})
        self.current_prompt = prompt
        self.is_connected = True
        self.calls = []

    def execute_paged(self, command, on_chunk=None):
        self.calls.append(command)
        return self.outputs.get(command, ""), False

    def execute(self, command):
        self.calls.append(command)
        return self.outputs.get(command, "")


class FakeAuth:
    def __init__(self, links):
        self.links = links

    def get_topology(self, lab_id):
        return {
            "nodes": [
                {"id": "sw1", "label": "SW1", "type": "iosvl2"},
                {"id": "sw2", "label": "SW2", "type": "iosvl2"},
                {"id": "pc1", "label": "PC1", "type": "server"},
            ],
            "links": self.links,
        }


STATUS_2PORT = (
    "Port      Name               Status       Vlan\n"
    "Gi0/1                        connected    1\n"
    "Gi0/2                        connected    1\n"
)
WHICH_BUSYBOX = "/sbin/ifconfig\n/sbin/route\n/bin/arp\n"
WHICH_IP_ONLY = "/sbin/ip\n"


def _runtime(device, node_type):
    return SimpleNamespace(
        device=device, mode="exec", status="connected",
        prompt=device.current_prompt, node_type=node_type,
        lock=asyncio.Lock(), dead=False, dead_reason="",
        empty_read_strikes=0,
    )


@pytest.fixture()
def session_env(monkeypatch):
    from cliq.web import server

    links = [
        {"id": "l1", "node_a": "sw1", "node_b": "sw2",
         "node_a_intf": "Gi0/1", "node_b_intf": "Gi0/1"},
        {"id": "l2", "node_a": "sw1", "node_b": "pc1",
         "node_a_intf": "Gi0/2", "node_b_intf": "eth0"},
    ]
    sw1 = FakeConsoleDevice({"show interfaces status": STATUS_2PORT}, "SW1#")
    sw2 = FakeConsoleDevice({"show interfaces status": STATUS_2PORT}, "SW2#")
    pc1 = FakeConsoleDevice(
        {"ifconfig -a": "eth0 Link encap", "which ifconfig route arp ip sudo": WHICH_BUSYBOX},
        "PC1:~#",
    )
    monkeypatch.setitem(server.SESSIONS, "tok-A",
                        SimpleNamespace(host="cml", auth=FakeAuth(links)))
    monkeypatch.setitem(server.DEVICES_BY_TOKEN, "tok-A", {
        "sw1": {"id": "sw1", "lab_id": "lab-1", "label": "SW1", "type": "iosvl2"},
        "sw2": {"id": "sw2", "lab_id": "lab-1", "label": "SW2", "type": "iosvl2"},
        "pc1": {"id": "pc1", "lab_id": "lab-1", "label": "PC1", "type": "server"},
    })
    monkeypatch.setitem(server._DEVICE_RUNTIMES, ("tok-A", "sw1"), _runtime(sw1, "iosvl2"))
    monkeypatch.setitem(server._DEVICE_RUNTIMES, ("tok-A", "sw2"), _runtime(sw2, "iosvl2"))
    monkeypatch.setitem(server._DEVICE_RUNTIMES, ("tok-A", "pc1"), _runtime(pc1, "server"))
    return {"sw1": sw1, "sw2": sw2, "pc1": pc1}


def _trunk_cmds(plan, dev_id):
    dp = next(d for d in plan.devices if d.device_id == dev_id)
    return [s.command for s in dp.steps]


# ── 1. 802.1Q handling ───────────────────────────────────────────────────────

def test_802_1q_never_creates_vlan_802():
    intent = parse_config_intent("802.1Q trunk allowing VLANs 10 and 20")
    assert sorted(intent.vlans) == [10, 20]
    assert intent.dot1q is True


def test_create_vlans_basic():
    assert sorted(parse_config_intent("Create VLANs 10 and 20").vlans) == [10, 20]


def test_explicit_vlan_802_recognized():
    assert sorted(parse_config_intent("Create VLAN 802 for legacy ring").vlans) == [802]
    assert sorted(parse_config_intent("VLANs 10 and 802").vlans) == [10, 802]
    intent = parse_config_intent("802.1Q trunk, VLANs 10, 20 and 802")
    assert sorted(intent.vlans) == [10, 20, 802]


@pytest.mark.anyio
async def test_plan_has_no_vlan_802_from_encapsulation(session_env):
    plan, err = await config_plan.build_plan(
        "tok-A", "SW1 Gi0/1 connected to SW2 Gi0/1. 802.1Q trunk allowing VLANs 10 and 20.")
    assert err == "" and plan is not None
    all_cmds = [s.command for d in plan.devices for s in d.steps]
    assert not any(cmd.strip() == "vlan 802" for cmd in all_cmds)
    assert "vlan 10" in all_cmds and "vlan 20" in all_cmds
    assert any("802.1Q" in a for a in plan.assumptions)


# ── 3. Trunk both ends ───────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_trunk_both_ends_of_named_link(session_env):
    plan, err = await config_plan.build_plan(
        "tok-A", "SW1 Gi0/1 connected to SW2 Gi0/1. 802.1Q trunk allowing VLANs 10 and 20.")
    assert err == "" and plan is not None
    assert "sw1" in [d.device_id for d in plan.devices]
    assert "switchport trunk allowed vlan 10,20" in _trunk_cmds(plan, "sw1")
    assert "switchport trunk allowed vlan 10,20" in _trunk_cmds(plan, "sw2")
    assert "interface Gi0/1" in _trunk_cmds(plan, "sw1")
    assert "interface Gi0/1" in _trunk_cmds(plan, "sw2")
    assert not [q for q in plan.questions if q.kind == "ambiguous_target" and not q.resolved]
    assert plan.to_dict()["blocking_open"] == []


@pytest.mark.anyio
async def test_trunk_both_ends_keyword(session_env):
    plan, err = await config_plan.build_plan(
        "tok-A", "Trunk SW1 Gi0/1 and SW2 Gi0/1 allowing VLANs 10 and 20, both ends.")
    assert err == "" and plan is not None
    assert "switchport trunk allowed vlan 10,20" in _trunk_cmds(plan, "sw1")
    assert "switchport trunk allowed vlan 10,20" in _trunk_cmds(plan, "sw2")


@pytest.mark.anyio
async def test_single_end_trunk_unchanged(session_env):
    plan, err = await config_plan.build_plan(
        "tok-A", "Trunk Gi0/1 on SW1 allowing VLANs 10 and 20.")
    assert err == "" and plan is not None
    assert "switchport trunk allowed vlan 10,20" in _trunk_cmds(plan, "sw1")
    # Only the mentioned switch is planned; nothing mirrored without both-ends.
    assert [d.device_id for d in plan.devices] == ["sw1"]


@pytest.mark.anyio
async def test_access_assignments_stay_correct(session_env):
    plan, err = await config_plan.build_plan(
        "tok-A", "Configure VLAN 10 on SW1. Gi0/2 access vlan 10. "
                 "SW1 Gi0/1 connected to SW2 Gi0/1. Trunk allowing VLANs 10 and 20.")
    assert err == "" and plan is not None
    sw1_cmds = _trunk_cmds(plan, "sw1")
    assert "switchport access vlan 10" in sw1_cmds
    assert "switchport trunk allowed vlan 10,20" in sw1_cmds
    assert "switchport trunk allowed vlan 10,20" in _trunk_cmds(plan, "sw2")


# ── 2. BusyBox capability gating ──────────────────────────────────────────────

@pytest.mark.anyio
async def test_busybox_renders_ifconfig_route_without_sudo(session_env):
    plan, err = await config_plan.build_plan(
        "tok-A", "PC1 eth0 192.168.10.11/24 gateway 192.168.10.1.")
    assert err == "" and plan is not None
    pc_cmds = [s.command for d in plan.devices for s in d.steps]
    assert "ifconfig eth0 192.168.10.11 netmask 255.255.255.0" in pc_cmds
    assert "route add default gw 192.168.10.1" in pc_cmds
    assert not any("sudo" in c for c in pc_cmds)
    assert not any(c.startswith("ip ") for c in pc_cmds)
    assert plan.evidence["endpoint_caps"]["pc1"]["caps"]["ifconfig"] is True
    assert plan.evidence["endpoint_caps"]["pc1"]["caps"]["ip"] is False


@pytest.mark.anyio
async def test_ip_fallback_when_only_ip_present(session_env):
    session_env["pc1"].outputs["which ifconfig route arp ip sudo"] = WHICH_IP_ONLY
    plan, err = await config_plan.build_plan(
        "tok-A", "PC1 eth0 192.168.10.11/24 gateway 192.168.10.1.")
    assert err == "" and plan is not None
    pc_cmds = [s.command for d in plan.devices for s in d.steps]
    assert "ip addr add 192.168.10.11/24 dev eth0" in pc_cmds
    assert "ip route add default via 192.168.10.1" in pc_cmds
    assert not any("sudo" in c for c in pc_cmds)


@pytest.mark.anyio
async def test_missing_capability_evidence_blocks(session_env, monkeypatch):
    from cliq.web import server
    monkeypatch.delitem(server._DEVICE_RUNTIMES, ("tok-A", "pc1"))
    plan, err = await config_plan.build_plan(
        "tok-A", "PC1 eth0 192.168.10.11/24 gateway 192.168.10.1.")
    assert err == "" and plan is not None
    assert any(q.kind == "missing_capability" and q.blocking and not q.resolved
               for q in plan.questions)
    pc_cmds = [s.command for d in plan.devices if d.device_id == "pc1" for s in d.steps]
    assert pc_cmds == []
    _ticket, perr = config_exec.prepare_plan("tok-A", plan.plan_id)
    assert perr.startswith("blocked:")


@pytest.mark.anyio
async def test_empty_capability_blocks_and_answer_recovers(session_env):
    session_env["pc1"].outputs["which ifconfig route arp ip sudo"] = ""
    plan, err = await config_plan.build_plan(
        "tok-A", "PC1 eth0 192.168.10.11/24 gateway 192.168.10.1.")
    assert err == "" and plan is not None
    qids = [q.qid for q in plan.questions if q.kind == "missing_capability"]
    assert qids
    v0 = plan.version
    updated, aerr = config_plan.apply_answers(plan.plan_id, {qids[0]: "ifconfig and route only"}, "tok-A")
    assert aerr == ""
    assert updated.version == v0 + 1
    pc_cmds = [s.command for d in updated.devices if d.device_id == "pc1" for s in d.steps]
    assert "ifconfig eth0 192.168.10.11 netmask 255.255.255.0" in pc_cmds
    assert "route add default gw 192.168.10.1" in pc_cmds
    _ticket, perr = config_exec.prepare_plan("tok-A", plan.plan_id)
    assert perr == ""


# ── M1.2 diagnostics for capability checks ───────────────────────────────────

def test_which_busybox_read_only():
    assert validate_command_for_platform("which ifconfig route", "linux").read_only
    assert validate_command_for_platform("busybox --list", "linux").read_only
    assert validate_command_for_platform("busybox", "linux").read_only
    assert not validate_command_for_platform("busybox ifconfig eth0 1.2.3.4", "linux").read_only
    assert validate_command_for_platform("which ip", "cisco_ios").error_code == "platform_mismatch"


def test_which_extraction_planned():
    plan = plan_autonomous_execution(
        "Check PC1 yourself, run which ip",
        [{"id": "pc1", "label": "PC1"}], {"pc1": "lab1"}, {"pc1": "linux"})
    assert plan is not None and plan.commands == ["which ip"]
