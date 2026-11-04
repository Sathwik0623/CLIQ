"""
Regression tests for confirmed H1/H2 configuration-planner defects.

Covers: negation-aware intent parsing (excluded devices, prohibited VLANs,
only-scope, no-gateway), router rejection for L2 switch configuration,
requirements-coverage gates, validate-vs-build lifecycle control, read-only
discovery gating, and conversation-scoped multi-turn requirements.
Mocked runtimes only; no live devices.
"""

import asyncio
import os
import tempfile
from types import SimpleNamespace

import pytest

from cliq.ai import config_exec, config_plan

STATUS_3PORT = (
    "Port      Name               Status       Vlan       Duplex  Speed\n"
    "Gi0/1                        connected    trunk      a-full  a-1000\n"
    "Gi0/2                        connected    10         a-full  a-1000\n"
    "Gi0/3                        connected    20         a-full  a-1000\n"
)
IFCONFIG_OK = (
    "eth0      Link encap:Ethernet\n"
    "          inet addr:192.168.10.5  Bcast:192.168.10.255  Mask:255.255.255.0\n"
)
WHICH_BUSYBOX = "/sbin/ifconfig\n/sbin/route\n/bin/arp\n"


class FakeConsoleDevice:
    def __init__(self, outputs=None, prompt="SW#"):
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
    def get_topology(self, lab_id):
        return {
            "links": [
                {"id": "l1", "node_a": "sw1", "node_b": "sw2",
                 "node_a_intf": "Gi0/1", "node_b_intf": "Gi0/1"},
            ],
        }


def _runtime(device, node_type):
    return SimpleNamespace(
        device=device, mode="exec", status="connected",
        prompt=device.current_prompt, node_type=node_type,
        lock=asyncio.Lock(), dead=False, dead_reason="",
        empty_read_strikes=0,
    )


@pytest.fixture()
def net_env(monkeypatch):
    from cliq.web import server

    monkeypatch.setitem(server.SESSIONS, "tok-N",
                        SimpleNamespace(host="cml", auth=FakeAuth()))
    monkeypatch.setitem(server.SESSIONS, "tok-NB",
                        SimpleNamespace(host="cml", auth=FakeAuth()))
    monkeypatch.setitem(server.SESSIONS, "tok-NC",
                        SimpleNamespace(host="cml", auth=object()))
    inventory = {
        "sw1": {"id": "sw1", "lab_id": "lab-1", "label": "SW1", "type": "iosvl2"},
        "sw2": {"id": "sw2", "lab_id": "lab-1", "label": "SW2", "type": "iosvl2"},
        "r1": {"id": "r1", "lab_id": "lab-1", "label": "R1", "type": "iosv"},
        "r2": {"id": "r2", "lab_id": "lab-1", "label": "R2", "type": "iosv"},
        "pc1": {"id": "pc1", "lab_id": "lab-1", "label": "PC1", "type": "server"},
        "pc2": {"id": "pc2", "lab_id": "lab-1", "label": "PC2", "type": "server"},
        "pc3": {"id": "pc3", "lab_id": "lab-1", "label": "PC3", "type": "server"},
        "pc4": {"id": "pc4", "lab_id": "lab-1", "label": "PC4", "type": "server"},
        "sw9": {"id": "sw9", "lab_id": "lab-9", "label": "SW9", "type": "iosvl2"},
    }
    monkeypatch.setitem(server.DEVICES_BY_TOKEN, "tok-N", dict(inventory))
    monkeypatch.setitem(server.DEVICES_BY_TOKEN, "tok-NB", dict(inventory))
    monkeypatch.setitem(server.DEVICES_BY_TOKEN, "tok-NC", {
        "sw1": dict(inventory["sw1"]),
    })
    devs = {}
    for sw in ("sw1", "sw2"):
        devs[sw] = FakeConsoleDevice(
            outputs={"show interfaces status": STATUS_3PORT}, prompt="SW#")
        monkeypatch.setitem(server._DEVICE_RUNTIMES, ("tok-N", sw),
                            _runtime(devs[sw], "iosvl2"))
    devs["sw1c"] = FakeConsoleDevice(
        outputs={"show interfaces status": STATUS_3PORT}, prompt="SW#")
    monkeypatch.setitem(server._DEVICE_RUNTIMES, ("tok-NC", "sw1"),
                        _runtime(devs["sw1c"], "iosvl2"))
    for pc in ("pc1", "pc2", "pc3", "pc4"):
        devs[pc] = FakeConsoleDevice(
            outputs={"ifconfig -a": IFCONFIG_OK,
                     "which ifconfig route arp ip sudo": WHICH_BUSYBOX},
            prompt="PC:~#")
        monkeypatch.setitem(server._DEVICE_RUNTIMES, ("tok-N", pc),
                            _runtime(devs[pc], "server"))
    return devs


def _cmds(plan, dev_id):
    dp = next(d for d in plan.devices if d.device_id == dev_id)
    return [s.command for s in dp.steps]


def _blocking(plan):
    return [q for q in plan.questions if q.blocking and not q.resolved]


# ── 1-4. Negation-aware intent parsing ───────────────────────────────────────

@pytest.mark.anyio
@pytest.mark.parametrize("exclusion", [
    "R1 and R2 are excluded.",
    "EXCLUDE R1 AND R2 COMPLETELY.",
    "Confirm no R1/R2 targets.",
    "All switches except R1 and R2.",
])
async def test_excluded_routers_never_targeted(net_env, exclusion):
    plan, err = await config_plan.build_plan(
        "tok-N", f"Configure VLAN 10 on SW1, R1 and R2. {exclusion}")
    assert err == "" and plan is not None
    by_id = {d.device_id for d in plan.devices}
    assert "r1" not in by_id and "r2" not in by_id
    assert "sw1" in by_id
    assert any("excluded" in a.lower() for a in plan.assumptions)


@pytest.mark.anyio
async def test_prohibited_vlans_excluded(net_env):
    plan, err = await config_plan.build_plan(
        "tok-N", "Create VLANs 10 and 20 on SW1. No VLAN 2 or VLAN 802.")
    assert err == "" and plan is not None
    assert "vlan 2" not in _cmds(plan, "sw1")
    assert not any(c.strip() == "vlan 802" for c in _cmds(plan, "sw1"))
    assert "vlan 10" in _cmds(plan, "sw1") and "vlan 20" in _cmds(plan, "sw1")
    assert ",2," not in f",{plan.title}," and "802" not in plan.title


@pytest.mark.anyio
async def test_no_vlan_1_creation(net_env):
    plan, err = await config_plan.build_plan(
        "tok-N", "Configure VLAN 10 on SW1. No VLAN 1 creation.")
    assert err == "" and plan is not None
    assert not any(c.strip() == "vlan 1" for c in _cmds(plan, "sw1"))
    assert "vlan 10" in _cmds(plan, "sw1")


@pytest.mark.anyio
async def test_only_scope_constrains_devices(net_env):
    plan, err = await config_plan.build_plan(
        "tok-N", "Only SW1 and SW2. Configure VLAN 10 on SW1, SW2 and R1.")
    assert err == "" and plan is not None
    assert {d.device_id for d in plan.devices} == {"sw1", "sw2"}


def test_only_section_form_and_scope_terminator(net_env):
    from cliq.web import server
    inv = server.DEVICES_BY_TOKEN["tok-N"]
    neg = config_plan.extract_negations(
        "ONLY THESE DEVICES:\nSW1\nSW2\n\nEXCLUDE R1 AND R2.", inv)
    assert neg.only_devices == ["sw1", "sw2"]
    assert neg.excluded_devices == ["r1", "r2"]
    neg2 = config_plan.extract_negations("No VLAN 2, but create VLAN 10.", inv)
    assert neg2.prohibited_vlans == [2]


@pytest.mark.anyio
async def test_no_default_gateways(net_env):
    plan, err = await config_plan.build_plan(
        "tok-N", "PC1 eth0 192.168.10.11/24 gateway 192.168.10.1. No default gateways.")
    assert err == "" and plan is not None
    assert not any("default gw" in c or "default via" in c for c in _cmds(plan, "pc1"))
    assert any("ifconfig eth0 192.168.10.11" in c for c in _cmds(plan, "pc1"))


@pytest.mark.anyio
async def test_prohibited_access_vlan_conflict_blocks(net_env):
    plan, err = await config_plan.build_plan(
        "tok-N", "Configure VLAN 10 on SW1. Gi0/2 access vlan 2. No VLAN 2.")
    assert err == "" and plan is not None
    assert any(q.kind == "prohibited_vlan" and q.blocking and not q.resolved
               for q in plan.questions)
    assert "switchport access vlan 2" not in _cmds(plan, "sw1")


# ── 5. Router rejection ──────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_router_switchport_rejected(net_env):
    plan, err = await config_plan.build_plan("tok-N", "Configure VLAN 10 on R1.")
    assert err == "" and plan is not None
    assert _cmds(plan, "r1") == []
    assert any(q.kind == "router_target" and q.blocking and not q.resolved
               for q in plan.questions)
    ticket, perr = config_exec.prepare_plan("tok-N", plan.plan_id)
    assert ticket is None and perr.startswith("blocked:")


def test_plan_problems_flag_router_l2_steps():
    from cliq.ai.config_plan import ConfigPlan, DevicePlan, PlanStep
    plan = ConfigPlan(plan_id="cfg-t", owner_token="tok-N", lab_id="lab-1")
    plan.devices.append(DevicePlan(
        device_id="r1", device_label="R1", platform="cisco_ios",
        role="router", node_type="iosv",
        steps=[PlanStep(step_id="r1:1", device_id="r1", command="vlan 10")]))
    problems = config_exec._plan_problems(plan)
    assert any("R1" in p and "Layer-2" in p for p in problems)


@pytest.mark.anyio
async def test_router_answer_remove_drops_target(net_env):
    plan, err = await config_plan.build_plan(
        "tok-N", "Configure VLAN 10 on SW1 and R1.")
    assert err == "" and plan is not None
    q = next(q for q in plan.questions if q.kind == "router_target")
    updated, aerr = config_plan.apply_answers(plan.plan_id, {q.qid: "remove"}, "tok-N")
    assert aerr == "" and updated is not None
    assert "r1" not in {d.device_id for d in updated.devices}
    assert "sw1" in {d.device_id for d in updated.devices}


@pytest.mark.anyio
async def test_approve_rechecks_problems(net_env):
    plan, err = await config_plan.build_plan("tok-N", "Configure VLAN 10 on SW1.")
    assert err == "" and plan is not None
    ticket, perr = config_exec.prepare_plan("tok-N", plan.plan_id)
    assert perr == ""
    # A blocking question raised after prepare (no fingerprint impact) must
    # fail approval, not slip through on the old ticket.
    from cliq.ai.config_plan import PlanQuestion
    plan.questions.append(PlanQuestion(qid="q-late", kind="missing_coverage",
                                       blocking=True, message="late blocker"))
    rec, aerr = config_exec.approve_plan("tok-N", ticket["ticket_id"])
    assert rec is None and aerr.startswith("blocked:")


# ── 6-8. Coverage gates ──────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_both_trunk_ends_required(net_env):
    plan, err = await config_plan.build_plan(
        "tok-NC", "Trunk Gi0/1 on SW1 allowing VLANs 10 and 20, both ends.",
        device_ids=["sw1"])
    assert err == "" and plan is not None
    assert any(q.kind == "missing_trunk_end" and q.blocking and not q.resolved
               for q in plan.questions)


@pytest.mark.anyio
async def test_four_endpoint_ips_required(net_env):
    prompt = (
        "Configure endpoints. "
        "PC1: ifconfig eth0 192.168.10.11 netmask 255.255.255.0\n"
        "PC2: ifconfig eth0 192.168.20.11 netmask 255.255.255.0\n"
        "PC3: ifconfig eth0 192.168.10.12 netmask 255.255.255.0\n"
        "PC4: ifconfig eth0 192.168.20.12 netmask 255.255.255.0")
    plan, err = await config_plan.build_plan("tok-N", prompt)
    assert err == "" and plan is not None
    for pc, addr in (("pc1", "192.168.10.11"), ("pc2", "192.168.20.11"),
                     ("pc3", "192.168.10.12"), ("pc4", "192.168.20.12")):
        assert any(f"ifconfig eth0 {addr}" in c for c in _cmds(plan, pc)), pc
    assert not any("sudo" in c for d in plan.devices for s in d.steps
                   for c in [s.command])


@pytest.mark.anyio
async def test_missing_access_blocks_fail_coverage(net_env):
    plan, err = await config_plan.build_plan(
        "tok-N", "Configure VLAN 10 on SW1. Gi0/9 access vlan 10.")
    assert err == "" and plan is not None
    assert any(q.kind == "unknown_interface" and q.blocking and not q.resolved
               for q in plan.questions)
    assert "switchport access vlan 10" not in _cmds(plan, "sw1")


def test_verify_coverage_backstop():
    from cliq.ai.config_plan import ConfigPlan, ParsedIntent
    plan = ConfigPlan(plan_id="cfg-cov", owner_token="t", lab_id="lab-1")
    intent = ParsedIntent(access=[("Gi0/2", 10, "raw"), ("Gi0/3", 20, "raw")])
    config_plan._verify_coverage(intent, {"sw1": [object()]}, {}, {}, 0, 0, 0, plan)
    assert any(q.kind == "missing_coverage" and q.blocking for q in plan.questions)


@pytest.mark.anyio
async def test_colon_form_section_binding(net_env):
    prompt = ("Configure VLAN 10 and VLAN 20.\n"
              "SW1:\n- Gi0/2: access VLAN 10.\n- Gi0/3: access VLAN 20.\n"
              "SW2:\n- Gi0/2: access VLAN 10.\n- Gi0/3: access VLAN 20.")
    plan, err = await config_plan.build_plan("tok-N", prompt)
    assert err == "" and plan is not None
    for sw in ("sw1", "sw2"):
        cmds = _cmds(plan, sw)
        assert cmds.count("switchport access vlan 10") == 1, sw
        assert cmds.count("switchport access vlan 20") == 1, sw


@pytest.mark.anyio
async def test_ambiguous_endpoint_never_silent(net_env):
    plan, err = await config_plan.build_plan(
        "tok-N", "PC1 and PC2 need addresses. 192.168.1.1/24 for the lab.")
    assert err == "" and plan is not None
    assert any(q.kind == "ambiguous_endpoint" and q.blocking and not q.resolved
               for q in plan.questions)


@pytest.mark.anyio
async def test_invalid_ip_never_silent(net_env):
    plan, err = await config_plan.build_plan(
        "tok-N", "PC1 eth0 999.999.1.1 netmask 255.255.255.0.")
    assert err == "" and plan is not None
    assert any(q.kind == "invalid_address" and q.blocking and not q.resolved
               for q in plan.questions)


# ── 9-10. Lifecycle control ──────────────────────────────────────────────────

def _fake_provider():
    from cliq.ai.provider import StreamChunk

    class FakeProvider:
        def get_default_model(self):
            return "fake-model"

        def list_models(self):
            return []

        async def chat_stream(self, messages, model=None):
            yield StreamChunk(delta="noted.", is_finished=True, finish_reason="stop")

    return FakeProvider()


@pytest.mark.anyio
async def test_validation_only_retrieves_existing(net_env, monkeypatch):
    from cliq.ai.conversations import ConversationStore
    from cliq.ai.orchestrator import CopilotOrchestrator
    import cliq.ai.orchestrator as orch_mod

    monkeypatch.setenv("CLIQ_RAG_ENABLED", "0")
    monkeypatch.setattr(orch_mod, "get_provider", lambda _pid="meta": _fake_provider())
    created = []
    try:
        with tempfile.TemporaryDirectory() as tmp:
            store = ConversationStore(db_path=os.path.join(tmp, "c.sqlite"))
            orch = CopilotOrchestrator(store=store)
            conv = store.create_conversation(title="t", session_token="tok-N")
            turn1 = [ev async for ev in orch.handle_user_message_stream(
                conversation_id=conv.id,
                user_text="Configure VLAN 10 on SW1. Gi0/2 access vlan 10.",
                session_token="tok-N")]
            plans1 = [e for e in turn1 if e.get("type") == "config_plan"]
            assert len(plans1) == 1
            plan_id = plans1[0]["plan"]["plan_id"]
            created.append(plan_id)
            before = len(config_plan._plans)
            turn2 = [ev async for ev in orch.handle_user_message_stream(
                conversation_id=conv.id,
                user_text=("Perform a final validation of the current draft only. "
                           "Do not regenerate the plan. Confirm VLAN 10 only and "
                           "no R1/R2 targets."),
                session_token="tok-N")]
            # Same plan re-emitted; nothing rebuilt.
            plans2 = [e for e in turn2 if e.get("type") == "config_plan"]
            assert len(plans2) == 1
            assert plans2[0]["plan"]["plan_id"] == plan_id
            assert len(config_plan._plans) == before
    finally:
        for pid in created:
            with config_plan._plan_lock:
                config_plan._plans.pop(pid, None)


@pytest.mark.anyio
async def test_validation_explicit_id(net_env, monkeypatch):
    from cliq.ai.conversations import ConversationStore
    from cliq.ai.orchestrator import CopilotOrchestrator
    import cliq.ai.orchestrator as orch_mod

    monkeypatch.setenv("CLIQ_RAG_ENABLED", "0")
    monkeypatch.setattr(orch_mod, "get_provider", lambda _pid="meta": _fake_provider())
    plan, err = await config_plan.build_plan("tok-N", "Configure VLAN 10 on SW1.")
    assert err == ""
    try:
        with tempfile.TemporaryDirectory() as tmp:
            store = ConversationStore(db_path=os.path.join(tmp, "c.sqlite"))
            orch = CopilotOrchestrator(store=store)
            conv = store.create_conversation(title="t", session_token="tok-N")
            events = [ev async for ev in orch.handle_user_message_stream(
                conversation_id=conv.id,
                user_text=f"Validate plan {plan.plan_id} before review.",
                session_token="tok-N")]
            plans = [e for e in events if e.get("type") == "config_plan"]
            assert len(plans) == 1
            assert plans[0]["plan"]["plan_id"] == plan.plan_id
    finally:
        with config_plan._plan_lock:
            config_plan._plans.pop(plan.plan_id, None)


@pytest.mark.anyio
async def test_validation_unknown_id_builds_nothing(net_env, monkeypatch):
    from cliq.ai.conversations import ConversationStore
    from cliq.ai.orchestrator import CopilotOrchestrator
    import cliq.ai.orchestrator as orch_mod

    monkeypatch.setenv("CLIQ_RAG_ENABLED", "0")
    monkeypatch.setattr(orch_mod, "get_provider", lambda _pid="meta": _fake_provider())
    before = len(config_plan._plans)
    with tempfile.TemporaryDirectory() as tmp:
        store = ConversationStore(db_path=os.path.join(tmp, "c.sqlite"))
        orch = CopilotOrchestrator(store=store)
        conv = store.create_conversation(title="t", session_token="tok-N")
        events = [ev async for ev in orch.handle_user_message_stream(
            conversation_id=conv.id,
            user_text="Validate plan cfg-deadbeef01 before review.",
            session_token="tok-N")]
        assert not [e for e in events if e.get("type") == "config_plan"]
        assert len(config_plan._plans) == before


def test_readonly_discovery_no_plan(net_env, monkeypatch):
    from cliq.ai.conversations import ConversationStore
    from cliq.ai.orchestrator import CopilotOrchestrator

    with tempfile.TemporaryDirectory() as tmp:
        orch = CopilotOrchestrator(
            store=ConversationStore(db_path=os.path.join(tmp, "c.sqlite")))
        orch.context_service = SimpleNamespace(list_devices=lambda tok: [
            {"id": "sw1", "label": "SW1"}, {"id": "r1", "label": "R1"},
            {"id": "pc1", "label": "PC1"}])
        readonly = [
            "Retrieve show vlan brief and show interfaces trunk on SW1.",
            "Execute show vlan brief on SW1 read-only.",
            "What is a trunk port?",
            "Continue the Layer 2 scenario discovery on SW1.",
            "Inspect SW1 Gi0/1 status and report.",
        ]
        for text in readonly:
            mode, ref = orch._classify_config_request("tok-N", text)
            assert (mode, ref) == ("none", None), text
        # Positive controls still route correctly.
        mode, _ = orch._classify_config_request(
            "tok-N", "Configure VLAN 10 on SW1. Gi0/2 access vlan 10.")
        assert mode == "build"
        mode, _ = orch._classify_config_request(
            "tok-N", "Trunk Gi0/1 on SW1 allowing VLANs 10 and 20.")
        assert mode == "build"
        mode, _ = orch._classify_config_request(
            "tok-N", "Perform a final validation of the current draft only. "
                     "Do not regenerate the plan.")
        assert mode == "validate"


# ── 11-12. Multi-turn requirements ───────────────────────────────────────────

@pytest.mark.anyio
async def test_multiturn_requirements_available(net_env):
    plan1, err = await config_plan.build_plan(
        "tok-N", "Configure VLAN 10 on SW1.", conversation_id="conv-mt1")
    assert err == "" and plan1 is not None
    try:
        plan2, err = await config_plan.build_plan(
            "tok-N", "Add VLAN 20.", conversation_id="conv-mt1")
        assert err == "" and plan2 is not None
        cmds = _cmds(plan2, "sw1")
        assert "vlan 10" in cmds and "vlan 20" in cmds
    finally:
        with config_plan._plan_lock:
            config_plan._plans.pop(plan1.plan_id, None)
            config_plan._plans.pop(plan2.plan_id, None)
        with config_plan._req_lock:
            config_plan._CONV_REQUIREMENTS.pop(("tok-N", "conv-mt1", "lab-1"), None)


@pytest.mark.anyio
async def test_no_cross_lab_session_merge(net_env):
    first, err = await config_plan.build_plan(
        "tok-N", "Configure VLAN 10 on SW1.", conversation_id="conv-iso")
    assert err == ""
    try:
        other_lab, err = await config_plan.build_plan(
            "tok-N", "Configure VLAN 30 on SW9.", lab_id="lab-9",
            conversation_id="conv-iso")
        assert err == "" and other_lab is not None
        assert "vlan 10" not in _cmds(other_lab, "sw9")
        assert "vlan 30" in _cmds(other_lab, "sw9")
        other_session, err = await config_plan.build_plan(
            "tok-NB", "Add VLAN 20.", conversation_id="conv-iso")
        assert other_session is None or err == "no_devices" or (
            err == "" and "vlan 10" not in
            [s.command for d in other_session.devices for s in d.steps])
    finally:
        with config_plan._plan_lock:
            config_plan._plans.pop(first.plan_id, None)
            config_plan._plans.pop(other_lab.plan_id, None)
            if "other_session" in dir() and other_session is not None:
                config_plan._plans.pop(other_session.plan_id, None)
        with config_plan._req_lock:
            config_plan._CONV_REQUIREMENTS.pop(("tok-N", "conv-iso", "lab-1"), None)
            config_plan._CONV_REQUIREMENTS.pop(("tok-N", "conv-iso", "lab-9"), None)
