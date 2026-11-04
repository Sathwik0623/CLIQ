"""
Unit tests for CLIQ M1.4 — approval-controlled autonomous configuration.

Covers multi-device plan generation, platform validation, interface/topology
evidence gating, plan-bound single-use approval, ordered execution with
dependency short-circuit, baseline health verdicts, and output/audit
preservation. All device I/O uses in-memory fakes; no live CML devices.
"""

import asyncio
import json
import os
import tempfile
from types import SimpleNamespace

import pytest

from cliq.ai import baseline as baseline_mod
from cliq.ai import config_exec, config_plan
from cliq.ai.device_exec import validate_command_for_platform

PROMPT = (
    "Configure VLAN 10 (Users) and VLAN 20 on SW1. "
    "Gi0/1 access vlan 10. Trunk Gi0/24 allowed vlans 10,20. "
    "PC1 eth0 192.168.10.11/24 gateway 192.168.10.1."
)

VLAN_BRIEF_OK = (
    "VLAN Name                             Status    Ports\n"
    "1    default                          active    Gi0/2\n"
    "10   Users                            active    Gi0/1\n"
    "20   Servers                          active\n"
)
TRUNK_OK = (
    "Port        Mode         Encapsulation  Status        Native vlan\n"
    "Gi0/24      on           802.1q         trunking      1\n"
)
TRUNK_DOWN = (
    "Port        Mode         Encapsulation  Status        Native vlan\n"
    "Gi0/24      on           802.1q         not-trunking  1\n"
)
STATUS_OK = (
    "Port      Name               Status       Vlan       Duplex  Speed\n"
    "Gi0/1                        connected    10         a-full  a-1000\n"
    "Gi0/24                       trunking     trunk      a-full  a-1000\n"
)
MAC_OK = "Mac Address Table\n0011.2233.4455    DYNAMIC     Gi0/1\n"
STP_OK = (
    "VLAN0010\n"
    "  Spanning tree enabled protocol rstp\n"
    "  VLAN0010 is executing the rstp compatible Spanning Tree protocol\n"
    "  Gi0/1                Desg FWD 19         128.1    P2p\n"
)
IFCONFIG_OK = (
    "eth0      Link encap:Ethernet\n"
    "          inet addr:192.168.10.11  Bcast:192.168.10.255  Mask:255.255.255.0\n"
)
ROUTE_OK = (
    "Kernel IP routing table\n"
    "Destination     Gateway         Genmask\n"
    "0.0.0.0         192.168.10.1    0.0.0.0\n"
)
ARP_OK = "Address                  HWaddress\n192.168.10.1             aa:bb:cc:dd:ee:ff\n"
LINUX_PING_OK = (
    "4 packets transmitted, 4 received, 0% packet loss\n"
)
LINUX_PING_FAIL = (
    "4 packets transmitted, 0 received, 100% packet loss\n"
)
IOS_PING_OK = "!!!!!\nSuccess rate is 100 percent (5/5)\n"


class FakeConsoleDevice:
    def __init__(self, outputs=None, prompt="SW1#", connected=True):
        self.outputs = dict(outputs or {})
        self.current_prompt = prompt
        self.is_connected = connected
        self.calls = []

    def execute_paged(self, command, on_chunk=None):
        self.calls.append(command)
        return self.outputs.get(command, ""), False

    def execute(self, command):
        self.calls.append(command)
        return self.outputs.get(command, "")


class FakeAuth:
    def get_topology(self, lab_id):
        return {
            "nodes": [
                {"id": "sw1", "label": "SW1", "type": "iosvl2"},
                {"id": "r1", "label": "R1", "type": "iosv"},
                {"id": "pc1", "label": "PC1", "type": "server"},
            ],
            "links": [
                {"id": "l1", "node_a": "sw1", "node_b": "pc1",
                 "node_a_intf": "Gi0/1", "node_b_intf": "eth0"},
                {"id": "l2", "node_a": "sw1", "node_b": "r1",
                 "node_a_intf": "Gi0/24", "node_b_intf": "Gi0/0"},
            ],
        }


def make_runtime(device, node_type):
    return SimpleNamespace(
        device=device, mode="exec", status="connected",
        prompt=device.current_prompt, node_type=node_type,
        lock=asyncio.Lock(), dead=False, dead_reason="",
        empty_read_strikes=0,
    )


@pytest.fixture()
def session_env(monkeypatch):
    from cliq.web import server

    sw1 = FakeConsoleDevice(
        outputs={
            "show vlan brief": VLAN_BRIEF_OK,
            "show interfaces trunk": TRUNK_OK,
            "show interfaces status": STATUS_OK,
            "show mac address-table": MAC_OK,
            "show spanning-tree vlan 10": STP_OK.replace("VLAN0010", "VLAN0010"),
            "show spanning-tree vlan 20": STP_OK.replace("VLAN0010", "VLAN0020"),
        },
        prompt="SW1#",
    )
    pc1 = FakeConsoleDevice(
        outputs={
            "ifconfig -a": IFCONFIG_OK,
            "route -n": ROUTE_OK,
            "arp -n": ARP_OK,
            "ping -c 4 192.168.10.1": LINUX_PING_OK,
            "which ifconfig route arp ip sudo": "/sbin/ifconfig\n/sbin/route\n/bin/arp\n",
        },
        prompt="PC1:~#",
    )
    monkeypatch.setitem(server.SESSIONS, "tok-A",
                        SimpleNamespace(host="cml", auth=FakeAuth()))
    monkeypatch.setitem(server.DEVICES_BY_TOKEN, "tok-A", {
        "sw1": {"id": "sw1", "lab_id": "lab-1", "label": "SW1", "type": "iosvl2"},
        "r1": {"id": "r1", "lab_id": "lab-1", "label": "R1", "type": "iosv"},
        "pc1": {"id": "pc1", "lab_id": "lab-1", "label": "PC1", "type": "server"},
    })
    monkeypatch.setitem(server._DEVICE_RUNTIMES, ("tok-A", "sw1"), make_runtime(sw1, "iosvl2"))
    monkeypatch.setitem(server._DEVICE_RUNTIMES, ("tok-A", "pc1"), make_runtime(pc1, "server"))
    return {"sw1": sw1, "pc1": pc1}


async def _drain_execute(session_token, plan_id, approval_token):
    events = [ev async for ev in config_exec.execute_plan(session_token, plan_id, approval_token)]
    return events


async def _approved_token(plan_id, session="tok-A"):
    ticket, err = config_exec.prepare_plan(session, plan_id)
    assert err == "", err
    rec, err = config_exec.approve_plan(session, ticket["ticket_id"])
    assert err == "", err
    return rec["approval_token"]


# ── 1. Multi-device plan generation ──────────────────────────────────────────

@pytest.mark.anyio
async def test_plan_generation_multi_device(session_env):
    plan, err = await config_plan.build_plan("tok-A", PROMPT)
    assert err == "" and plan is not None
    assert plan.lab_id == "lab-1"
    assert plan.fingerprint
    by_id = {d.device_id: d for d in plan.devices}
    assert set(by_id) == {"sw1", "pc1"}
    assert by_id["sw1"].platform == "cisco_ios" and by_id["sw1"].role == "switch"
    assert by_id["pc1"].platform == "linux" and by_id["pc1"].role == "endpoint"
    sw_cmds = [s.command for s in by_id["sw1"].steps]
    assert sw_cmds[0] == "configure terminal" and sw_cmds[-1] == "end"
    assert "vlan 10" in sw_cmds and "name Users" in sw_cmds and "vlan 20" in sw_cmds
    assert "interface Gi0/1" in sw_cmds and "switchport access vlan 10" in sw_cmds
    assert "interface Gi0/24" in sw_cmds
    assert "switchport trunk allowed vlan 10,20" in sw_cmds
    pc_cmds = [s.command for s in by_id["pc1"].steps]
    assert pc_cmds[0] == "ifconfig eth0 192.168.10.11 netmask 255.255.255.0"
    assert pc_cmds[1] == "route add default gw 192.168.10.1"
    # Chained dependencies within each device.
    for dp in plan.devices:
        for prev, cur in zip(dp.steps, dp.steps[1:]):
            assert cur.depends_on[0] == prev.step_id
    # Cross-device: endpoint addressing waits for the peer access port.
    assert any(d.startswith("sw1:") for d in by_id["pc1"].steps[0].depends_on)
    # Expected connectivity derived (gateway ping).
    assert any(c.dst_ip == "192.168.10.1" for c in plan.connectivity)
    assert plan.to_dict()["blocking_open"] == []


# ── 2. Platform-specific configuration validation ────────────────────────────

def test_platform_config_validation():
    assert validate_command_for_platform("vlan 10", "cisco_ios").ok
    assert not validate_command_for_platform("vlan 10", "cisco_ios").read_only
    assert validate_command_for_platform("switchport mode trunk", "cisco_ios").ok
    assert validate_command_for_platform(
        "ifconfig eth0 192.168.10.11 netmask 255.255.255.0", "linux").ok
    assert validate_command_for_platform("route add default gw 192.168.10.1", "linux").ok
    assert validate_command_for_platform(
        "switchport mode access", "linux").error_code == "platform_mismatch"
    assert validate_command_for_platform("vlan 10", "linux").error_code == "platform_mismatch"
    assert validate_command_for_platform(
        "ifconfig eth0 1.2.3.4 netmask 255.0.0.0", "cisco_ios").error_code == "platform_mismatch"
    assert validate_command_for_platform("reload", "cisco_ios").error_code == "blocked_destructive"


# ── 3/4. Unknown interfaces + missing evidence ───────────────────────────────

@pytest.mark.anyio
async def test_unknown_interface_blocks_prepare(session_env):
    plan, err = await config_plan.build_plan(
        "tok-A", "Configure VLAN 10 on SW1. Gi0/99 access vlan 10.")
    assert err == "" and plan is not None
    kinds = [q.kind for q in plan.questions if q.blocking and not q.resolved]
    assert "unknown_interface" in kinds
    _ticket, perr = config_exec.prepare_plan("tok-A", plan.plan_id)
    assert perr.startswith("blocked:")


@pytest.mark.anyio
async def test_missing_topology_evidence_blocks_prepare(session_env, monkeypatch):
    from cliq.web import server
    # SSH-like session: no controller topology + unreachable live evidence.
    monkeypatch.setitem(server.SESSIONS, "tok-S", SimpleNamespace(host="h", auth=None))
    monkeypatch.setitem(server.DEVICES_BY_TOKEN, "tok-S", {
        "sw9": {"id": "sw9", "lab_id": "ssh", "label": "SW9", "type": "iosvl2"},
    })
    plan, err = await config_plan.build_plan(
        "tok-S", "Configure VLAN 10 on SW9. Gi0/1 access vlan 10.")
    assert err == "" and plan is not None
    assert plan.evidence["topology_available"] is False
    assert any(q.kind == "unknown_interface" and q.blocking and not q.resolved
               for q in plan.questions)
    _ticket, perr = config_exec.prepare_plan("tok-S", plan.plan_id)
    assert perr.startswith("blocked:")


@pytest.mark.anyio
async def test_answer_resolves_interface_question(session_env):
    plan, err = await config_plan.build_plan(
        "tok-A", "Configure VLAN 10 on SW1. Gi0/99 access vlan 10.")
    qid = next(q.qid for q in plan.questions if q.kind == "unknown_interface")
    v1, fp1 = plan.version, plan.fingerprint
    updated, aerr = config_plan.apply_answers(plan.plan_id, {qid: "Gi0/1"}, "tok-A")
    assert aerr == ""
    assert updated.version == v1 + 1 and updated.fingerprint != fp1
    assert all(q.resolved for q in updated.questions if q.qid == qid)
    assert any("Gi0/1" in s.command for d in updated.devices for s in d.steps)
    _ticket, perr = config_exec.prepare_plan("tok-A", plan.plan_id)
    assert perr == ""


# ── 5/6/7/8. Approval lifecycle ──────────────────────────────────────────────

@pytest.mark.anyio
async def test_execute_requires_approval(session_env):
    plan, _ = await config_plan.build_plan("tok-A", PROMPT)
    events = await _drain_execute("tok-A", plan.plan_id, "bogus-token")
    assert len(events) == 1 and events[0]["type"] == "plan_rejected"
    assert events[0]["error_code"] == "invalid_token"
    assert session_env["sw1"].calls == [] or all(
        c.startswith("show ") for c in session_env["sw1"].calls)


@pytest.mark.anyio
async def test_approval_bound_to_exact_plan(session_env):
    plan_a, _ = await config_plan.build_plan("tok-A", PROMPT)
    plan_b, _ = await config_plan.build_plan("tok-A", "Configure VLAN 30 on SW1.")
    token_a = await _approved_token(plan_a.plan_id)
    events = await _drain_execute("tok-A", plan_b.plan_id, token_a)
    assert events[0]["type"] == "plan_rejected" and events[0]["error_code"] == "wrong_plan"


@pytest.mark.anyio
async def test_expired_and_reused_token_rejected(session_env, monkeypatch):
    plan, _ = await config_plan.build_plan("tok-A", PROMPT)
    token = await _approved_token(plan.plan_id)
    real_now = config_exec._now
    monkeypatch.setattr(config_exec, "_now", lambda: real_now() + 3600)
    events = await _drain_execute("tok-A", plan.plan_id, token)
    assert events[0]["error_code"] == "invalid_token"
    monkeypatch.setattr(config_exec, "_now", real_now)
    token2 = await _approved_token(plan.plan_id)
    first = await _drain_execute("tok-A", plan.plan_id, token2)
    assert first[-1]["type"] == "plan_finished"
    second = await _drain_execute("tok-A", plan.plan_id, token2)
    assert second[0]["type"] == "plan_rejected" and second[0]["error_code"] == "token_reused"


@pytest.mark.anyio
async def test_plan_modification_invalidates_approval(session_env):
    plan, _ = await config_plan.build_plan("tok-A", PROMPT)
    token = await _approved_token(plan.plan_id)
    # Simulate an operator-side edit after approval.
    plan.devices[0].steps.append(config_plan.PlanStep(
        step_id="sw1:99", device_id="sw1", command="hostname HACKED"))
    events = await _drain_execute("tok-A", plan.plan_id, token)
    assert events[0]["type"] == "plan_rejected"
    assert events[0]["error_code"] == "plan_modified"
    assert not any("HACKED" in c for c in session_env["sw1"].calls)


# ── 9/10. Failure semantics ──────────────────────────────────────────────────

@pytest.mark.anyio
async def test_failure_stops_dependents_and_not_success(session_env):
    session_env["sw1"].outputs["switchport access vlan 10"] = (
        "% Invalid input detected at '^' marker.")
    plan, _ = await config_plan.build_plan("tok-A", PROMPT)
    token = await _approved_token(plan.plan_id)
    events = await _drain_execute("tok-A", plan.plan_id, token)
    assert events[-1]["type"] == "plan_finished"
    execution = events[-1]["execution"]
    assert execution["status"] == "failed"
    assert "did NOT complete" in execution["summary"]
    failed = [s for s in execution["steps"] if s["status"] == "failed"]
    assert len(failed) == 1 and failed[0]["command"] == "switchport access vlan 10"
    assert "% Invalid input" in failed[0]["stdout"]
    skipped = [s for s in execution["steps"] if s["status"] == "skipped"]
    assert skipped, "dependent steps must be skipped, not executed"
    # Later unique commands never reached any device; the whole endpoint
    # device was skipped via its cross-device dependency.
    assert "interface Gi0/24" not in session_env["sw1"].calls
    assert "switchport trunk allowed vlan 10,20" not in session_env["sw1"].calls
    assert "end" not in session_env["sw1"].calls
    assert not any(c.startswith("ifconfig eth0 192") or c.startswith("route add")
                   for c in session_env["pc1"].calls)


@pytest.mark.anyio
async def test_full_success_stream(session_env):
    plan, _ = await config_plan.build_plan("tok-A", PROMPT)
    token = await _approved_token(plan.plan_id)
    events = await _drain_execute("tok-A", plan.plan_id, token)
    kinds = [e["type"] for e in events]
    assert kinds[0] == "plan_started"
    assert kinds[-1] == "plan_finished"
    execution = events[-1]["execution"]
    assert execution["status"] == "completed"
    assert all(s["status"] == "success" for s in execution["steps"])
    assert "configure terminal" in session_env["sw1"].calls
    assert "ifconfig eth0 192.168.10.11 netmask 255.255.255.0" in session_env["pc1"].calls


# ── 11/12. Baseline verdicts ─────────────────────────────────────────────────

@pytest.mark.anyio
async def test_baseline_healthy_when_all_pass(session_env):
    plan, _ = await config_plan.build_plan("tok-A", PROMPT)
    record, err = await baseline_mod.collect_baseline("tok-A", "lab-1", ["sw1", "pc1"], plan.plan_id)
    assert err == ""
    assert record["verdict"] == "HEALTHY", record["explanation"]
    assert record["devices"]["sw1"]["outputs"]["show vlan brief"]["success"]
    assert any(c["status"] == "passed" and "trunking" in c["description"] for c in record["checks"])
    assert record["connectivity"][0]["status"] == "passed"


@pytest.mark.anyio
async def test_baseline_failed_without_evidence(session_env, monkeypatch):
    from cliq.web import server
    monkeypatch.delitem(server._DEVICE_RUNTIMES, ("tok-A", "pc1"))
    plan, _ = await config_plan.build_plan("tok-A", PROMPT)
    record, err = await baseline_mod.collect_baseline("tok-A", "lab-1", ["sw1", "pc1"], plan.plan_id)
    assert err == ""
    assert record["verdict"] == "FAILED"
    assert "evidence" in record["explanation"].lower()


@pytest.mark.anyio
async def test_baseline_failed_on_connectivity_failure(session_env):
    session_env["pc1"].outputs["ping -c 4 192.168.10.1"] = LINUX_PING_FAIL
    plan, _ = await config_plan.build_plan("tok-A", PROMPT)
    record, err = await baseline_mod.collect_baseline("tok-A", "lab-1", ["sw1", "pc1"], plan.plan_id)
    assert err == ""
    assert record["verdict"] == "FAILED"
    assert record["connectivity"][0]["status"] == "failed"


@pytest.mark.anyio
async def test_baseline_degraded_on_state_mismatch(session_env):
    session_env["sw1"].outputs["show interfaces trunk"] = TRUNK_DOWN
    plan, _ = await config_plan.build_plan("tok-A", PROMPT)
    record, err = await baseline_mod.collect_baseline("tok-A", "lab-1", ["sw1", "pc1"], plan.plan_id)
    assert err == ""
    assert record["verdict"] == "DEGRADED", record["explanation"]
    assert "not trunking" in record["explanation"]


# ── 13. Outputs + audit preserved ────────────────────────────────────────────

@pytest.mark.anyio
async def test_outputs_and_audit_preserved(session_env, monkeypatch):
    import cliq.ai.device_exec as dev_exec
    captured = []
    monkeypatch.setattr(dev_exec, "_audit_record", lambda rec: captured.append(rec))
    plan, _ = await config_plan.build_plan("tok-A", PROMPT)
    token = await _approved_token(plan.plan_id)
    events = await _drain_execute("tok-A", plan.plan_id, token)
    execution = events[-1]["execution"]
    plan_audits = [r for r in captured if r.get("plan_id") == plan.plan_id]
    assert len(plan_audits) == len(execution["steps"])
    assert all(r.get("actor") == "copilot-plan" for r in plan_audits)
    assert all("send_command" in r for r in plan_audits)
    record, _ = await baseline_mod.collect_baseline("tok-A", "lab-1", ["sw1", "pc1"], plan.plan_id)
    assert "VLAN Name" in record["devices"]["sw1"]["outputs"]["show vlan brief"]["stdout"]
    assert record["audit"]["session_prefix"] == "tok-A"[:8]


# ── Orchestrator: plans proposed, never auto-executed ────────────────────────

@pytest.mark.anyio
async def test_orchestrator_proposes_plan_without_executing(session_env, monkeypatch):
    from cliq.ai.conversations import ConversationStore
    from cliq.ai.orchestrator import CopilotOrchestrator
    from cliq.ai.provider import StreamChunk

    monkeypatch.setenv("CLIQ_RAG_ENABLED", "0")

    class FakeProvider:
        def get_default_model(self):
            return "fake-model"

        def list_models(self):
            return []

        async def chat_stream(self, messages, model=None):
            yield StreamChunk(delta="review the plan.", is_finished=True, finish_reason="stop")

    import cliq.ai.orchestrator as orch_mod
    monkeypatch.setattr(orch_mod, "get_provider", lambda _pid="meta": FakeProvider())
    with tempfile.TemporaryDirectory() as tmp:
        store = ConversationStore(db_path=os.path.join(tmp, "c.sqlite"))
        orch = CopilotOrchestrator(store=store)
        conv = store.create_conversation(title="t", session_token="tok-A")
        events = [ev async for ev in orch.handle_user_message_stream(
            conversation_id=conv.id, user_text=PROMPT, session_token="tok-A")]
    plans = [e for e in events if e.get("type") == "config_plan"]
    assert len(plans) == 1
    assert plans[0]["plan"]["devices"]
    # No configuration command may reach any device without approval.
    for dev in session_env.values():
        assert not any(c in ("configure terminal", "vlan 10", "end") or c.startswith("ifconfig eth0 192")
                       for c in dev.calls), dev.calls


@pytest.mark.anyio
async def test_orchestrator_ignores_conceptual_questions(session_env, monkeypatch):
    from cliq.ai.conversations import ConversationStore
    from cliq.ai.orchestrator import CopilotOrchestrator
    from cliq.ai.provider import StreamChunk

    monkeypatch.setenv("CLIQ_RAG_ENABLED", "0")

    class FakeProvider:
        def get_default_model(self):
            return "fake-model"

        def list_models(self):
            return []

        async def chat_stream(self, messages, model=None):
            yield StreamChunk(delta="a trunk carries vlans.", is_finished=True, finish_reason="stop")

    import cliq.ai.orchestrator as orch_mod
    monkeypatch.setattr(orch_mod, "get_provider", lambda _pid="meta": FakeProvider())
    with tempfile.TemporaryDirectory() as tmp:
        store = ConversationStore(db_path=os.path.join(tmp, "c.sqlite"))
        orch = CopilotOrchestrator(store=store)
        conv = store.create_conversation(title="t", session_token="tok-A")
        events = [ev async for ev in orch.handle_user_message_stream(
            conversation_id=conv.id, user_text="What is a trunk port?",
            session_token="tok-A")]
    assert not [e for e in events if e.get("type") == "config_plan"]


# ── 14/15 + REST integration ─────────────────────────────────────────────────

@pytest.mark.anyio
async def test_m12_read_only_still_functional(session_env):
    from cliq.ai.device_exec import execute_device_command
    res = await execute_device_command("tok-A", "lab-1", "sw1", "show vlan brief")
    assert res.success and "VLAN Name" in res.stdout


def test_m13_labs_routes_still_mounted():
    from fastapi.testclient import TestClient
    from cliq.web.server import app

    # This FastAPI version keeps included routers lazy (_IncludedRouter), so
    # verify behaviorally: mounted+gated endpoints answer 401, not 404.
    client = TestClient(app)
    assert client.post("/api/labs/plan", json={"prompt": "x"}).status_code == 401
    assert client.post("/api/labs/deploy/prepare",
                       json={"blueprint": {}, "live": False}).status_code == 401


def test_rest_config_roundtrip(session_env):
    from fastapi.testclient import TestClient
    from cliq.web.server import app

    client = TestClient(app)
    created = client.post("/api/copilot/config/plan?token=tok-A", json={"prompt": PROMPT})
    assert created.status_code == 200, created.text
    plan = created.json()
    assert plan["blocking_open"] == []
    plan_id = plan["plan_id"]

    denied = client.post("/api/copilot/config/plan?token=bogus", json={"prompt": PROMPT})
    assert denied.status_code == 401

    prep = client.post("/api/copilot/config/prepare?token=tok-A", json={"plan_id": plan_id})
    assert prep.status_code == 200, prep.text
    ticket_id = prep.json()["ticket"]["ticket_id"]

    appr = client.post("/api/copilot/config/approve?token=tok-A", json={"ticket_id": ticket_id})
    assert appr.status_code == 200, appr.text
    approval_token = appr.json()["approval_token"]

    ran = client.post("/api/copilot/config/execute?token=tok-A",
                      json={"plan_id": plan_id, "approval_token": approval_token})
    assert ran.status_code == 200, ran.text
    stream_events = []
    for line in ran.text.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            payload = line[5:].strip()
            if payload != "[DONE]":
                stream_events.append(json.loads(payload))
    assert stream_events[0]["type"] == "plan_started"
    assert stream_events[-1]["type"] == "plan_finished"
    assert stream_events[-1]["execution"]["status"] == "completed"

    base = client.post("/api/copilot/config/baseline?token=tok-A", json={"plan_id": plan_id})
    assert base.status_code == 200, base.text
    assert base.json()["verdict"] == "HEALTHY"

    fetched = client.get(f"/api/copilot/config/baseline/{base.json()['baseline_id']}?token=tok-A")
    assert fetched.status_code == 200
