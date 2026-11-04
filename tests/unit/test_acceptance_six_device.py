"""
Regression tests for the failed six-device read-only discovery acceptance test.

Covers: intent routing of the exact acceptance request (no config plan,
discovery triggered), the "deployed topology" verb fix, show-context trunk
exclusion, lab title -> UUID resolution, six-node topology inventory,
inventory-driven telemetry, multi-device autonomous execution with per-device
association and unreachable-device isolation, hostname extraction, the
"ifconfig -" artifact, discovery/config requirements isolation, single-device
compatibility, and Lab Creation routing safety.

All device and controller I/O uses in-memory fakes. No live CML is touched.
"""

import asyncio
import os
import tempfile
from types import SimpleNamespace

import pytest

ACCEPTANCE_REQUEST = (
    "Inspect the currently active lab, Enterprise Two-Department Isolated Lab. "
    "Perform read-only discovery only. Do not configure devices, change "
    "interfaces, or modify the lab.\n"
    "\n"
    "For SW1 and SW2, collect:\n"
    "- show vlan brief\n"
    "- show interfaces status\n"
    "- show interfaces trunk\n"
    "- show mac address-table dynamic\n"
    "- show running-config\n"
    "\n"
    "For ENG1, ENG2, FIN1 and FIN2, collect:\n"
    "- hostname\n"
    "- ifconfig\n"
    "- route -n\n"
    "- arp -n\n"
    "\n"
    "Compare the discovered live interfaces with the deployed topology and "
    "report any mismatches."
)

SWITCH_COMMANDS = [
    "show vlan brief",
    "show interfaces status",
    "show interfaces trunk",
    "show mac address-table dynamic",
    "show running-config",
]
ENDPOINT_COMMANDS = ["hostname", "ifconfig", "route -n", "arp -n"]
SIX_LABELS = ["SW1", "SW2", "ENG1", "ENG2", "FIN1", "FIN2"]
LAB_TITLE = "Enterprise Two-Department Isolated Lab"
LAB_UUID = "lab-ent-uuid-0001"


# ── Fakes ────────────────────────────────────────────────────────────────────

class FakeConsoleDevice:
    """Mimics CMLConsoleDevice/SSHConsoleDevice driver surface."""

    def __init__(self, outputs=None, prompt="DEV#"):
        self.outputs = dict(outputs or {})
        self.current_prompt = prompt
        self.is_connected = True
        self.calls = []

    def execute_paged(self, command, on_chunk=None):
        self.calls.append(command)
        return self.outputs.get(command, f"<output of {command}>"), False

    def execute(self, command):
        self.calls.append(command)
        return self.outputs.get(command, f"<output of {command}>")


def _resp(payload):
    return SimpleNamespace(json=lambda: payload)


class FakeCMLAuth:
    def __init__(self, routes=None, sysinfo=None, labs=None, topo=None):
        self.routes = routes or {}
        self.sysinfo = sysinfo if sysinfo is not None else {"version": "2.9.0"}
        self.labs = labs if labs is not None else []
        self.topo = topo or {}
        self.calls = []

    def get_system_information(self):
        self.calls.append("sysinfo")
        return dict(self.sysinfo)

    def get_labs(self):
        self.calls.append("labs")
        return list(self.labs)

    def get_topology(self, lab_id):
        self.calls.append(f"topology:{lab_id}")
        return dict(self.topo)

    def _get(self, path):
        self.calls.append(f"GET {path}")
        if path not in self.routes:
            raise RuntimeError(f"404 for {path}")
        return _resp(self.routes[path])


def _switch_outputs(label):
    return {
        "show vlan brief": "VLAN Name Status Ports\n10 ENG active Gi0/2\n20 FIN active Gi0/3",
        "show interfaces status": "Port Name Status Vlan\nGi0/1 trunk trunk\nGi0/2 connected 10",
        "show interfaces trunk": "Port Mode Encapsulation Status\nGi0/1 on 802.1q trunking",
        "show mac address-table dynamic": "Mac Address Table\n0011.2233.4455 DYNAMIC Gi0/2",
        "show running-config": f"hostname {label}\nvlan 10\nvlan 20",
    }


def _endpoint_outputs(label):
    return {
        "hostname": label.lower(),
        "ifconfig": "eth0 Link encap:Ethernet HWaddr 00:11:22:33:44:55",
        "ifconfig -a": "eth0 Link encap:Ethernet HWaddr 00:11:22:33:44:55",
        "route -n": "Kernel IP routing table\n0.0.0.0 10.10.10.1 UG eth0",
        "arp -n": "Address HWaddress\n10.10.10.1 00:aa:bb:cc:dd:ee",
    }


def _six_topo():
    nodes = [
        {"id": "n-sw1", "label": "SW1", "type": "iosvl2", "state": "BOOTED"},
        {"id": "n-sw2", "label": "SW2", "type": "iosvl2", "state": "BOOTED"},
        {"id": "n-eng1", "label": "ENG1", "type": "server", "state": "BOOTED"},
        {"id": "n-eng2", "label": "ENG2", "type": "server", "state": "BOOTED"},
        {"id": "n-fin1", "label": "FIN1", "type": "server", "state": "BOOTED"},
        {"id": "n-fin2", "label": "FIN2", "type": "server", "state": "BOOTED"},
    ]
    return {"nodes": nodes, "links": []}


@pytest.fixture()
def six_env(monkeypatch):
    """tok-6 owns lab-ent: SW1/SW2 (iosvl2) + 4 Linux endpoints + CML auth."""
    from cliq.web import server

    devs = {}
    runtimes = {}
    for label in SIX_LABELS:
        dev_id = label.lower()
        node_type = "iosvl2" if label.startswith("SW") else "server"
        outs = _switch_outputs(label) if label.startswith("SW") else _endpoint_outputs(label)
        fake = FakeConsoleDevice(outputs=outs, prompt=f"{label}#")
        devs[dev_id] = fake
        runtimes[( "tok-6", dev_id)] = SimpleNamespace(
            device=fake, mode="exec", status="connected", prompt=f"{label}#",
            node_type=node_type, lock=asyncio.Lock(), dead=False,
            dead_reason="", empty_read_strikes=0)
    auth = FakeCMLAuth(
        routes={
            "/node_definitions": [{"id": "iosv"}, {"id": "iosvl2"}, {"id": "server"}],
            "/image_definitions": [{"id": "iosv-15.9"}, {"id": "server-ubuntu"}],
            "/system_stats": {"cpu_total": 16, "cpu_used": 4,
                              "ram_total_mb": 32768, "ram_used_mb": 8192},
            "/system_health": {"ready": True},
            "/system_information": {"version": "2.9.0"},
        },
        sysinfo={"version": "2.9.0", "ready": True},
        labs=[SimpleNamespace(lab_id=LAB_UUID, title=LAB_TITLE,
                              state="STARTED", owner="admin"),
              SimpleNamespace(lab_id="lab-other", title="Other Lab",
                              state="DEFINED", owner="admin")],
        topo=_six_topo())
    monkeypatch.setitem(server.SESSIONS, "tok-6", SimpleNamespace(
        host="cml", auth=auth, connection_type="cml"))
    monkeypatch.setitem(server.DEVICES_BY_TOKEN, "tok-6", {
        label.lower(): {"id": label.lower(), "lab_id": "lab-ent",
                        "label": label,
                        "type": "iosvl2" if label.startswith("SW") else "server"}
        for label in SIX_LABELS
    })
    for key, rt in runtimes.items():
        monkeypatch.setitem(server._DEVICE_RUNTIMES, key, rt)
    return {"devs": devs, "auth": auth}


def _orch(monkeypatch, tmp):
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
            yield StreamChunk(delta="summary.", is_finished=True,
                              finish_reason="stop")

    import cliq.ai.orchestrator as orch_mod
    monkeypatch.setattr(orch_mod, "get_provider", lambda _pid="meta": FakeProvider())
    store = ConversationStore(db_path=os.path.join(tmp, "c.sqlite"))
    return CopilotOrchestrator(store=store), store


# ── 1. Exact acceptance request: no plan, discovery triggered ────────────────

def test_acceptance_request_routes_to_discovery_not_planning(six_env):
    from cliq.ai.network_context import get_network_context_service
    from cliq.ai.orchestrator import CopilotOrchestrator

    orch = CopilotOrchestrator.__new__(CopilotOrchestrator)
    orch.context_service = get_network_context_service()
    mode, ref = CopilotOrchestrator._classify_config_request(
        orch, "tok-6", ACCEPTANCE_REQUEST)
    assert (mode, ref) == ("none", None)
    assert CopilotOrchestrator._should_discover_lab(ACCEPTANCE_REQUEST) is True


def test_lab_inspection_phrasings_trigger_discovery():
    from cliq.ai.orchestrator import CopilotOrchestrator

    for text in ["Inspect the active lab.",
                 "Discover this lab.",
                 "Examine the current topology.",
                 "Read-only device discovery.",
                 "Inspect devices in the named lab."]:
        assert CopilotOrchestrator._should_discover_lab(text) is True, text
    assert CopilotOrchestrator._should_discover_lab("Hello there.") is False


# ── 2. "Deployed topology" never plans; live verbs still work ────────────────

def test_deployed_topology_does_not_plan(six_env):
    from cliq.ai.network_context import get_network_context_service
    from cliq.ai.orchestrator import CopilotOrchestrator

    orch = CopilotOrchestrator.__new__(CopilotOrchestrator)
    orch.context_service = get_network_context_service()
    mode, _ = CopilotOrchestrator._classify_config_request(
        orch, "tok-6",
        "Compare the discovered live interfaces with the deployed topology "
        "and report any mismatches on SW1.")
    assert mode == "none"
    # Genuine deploy/config verbs still route to a build.
    mode, _ = CopilotOrchestrator._classify_config_request(
        orch, "tok-6", "Deploy VLAN 10 configuration on SW1.")
    assert mode == "build"


# ── 3. show-context trunk never becomes a trunk requirement ──────────────────

def test_show_interfaces_trunk_creates_no_trunk_requirement():
    from cliq.ai import config_plan

    intent = config_plan.parse_config_intent(
        "Collect show interfaces trunk and show vlan brief on SW1.")
    assert intent.trunks == []
    # A genuine trunk order still parses.
    intent = config_plan.parse_config_intent(
        "Configure trunk on Gi0/1 allowing VLANs 10 and 20.")
    assert intent.trunks != []


# ── 4. Title resolves to the correct lab UUID ─────────────────────────────────

def test_lab_title_resolves_to_uuid(six_env):
    from cliq.ai import lab_discovery

    labs = [{"id": LAB_UUID, "title": LAB_TITLE},
            {"id": "lab-other", "title": "Other Lab"}]
    assert lab_discovery.resolve_lab_title(
        labs, f"Inspect the currently active lab, {LAB_TITLE}.") == LAB_UUID
    assert lab_discovery.resolve_lab_title(labs, "Inspect the active lab.") is None

    res = lab_discovery.discover_environment(
        "tok-6", lab_title_hint=f"Inspect {LAB_TITLE} read-only.")
    assert res.ok
    assert res.topology["lab_id"] == LAB_UUID
    assert res.lab_title == LAB_TITLE
    assert f"topology:{LAB_UUID}" in six_env["auth"].calls


# ── 5. Topology retrieval returns all six devices ─────────────────────────────

def test_topology_inventory_lists_six_devices(six_env):
    from cliq.ai import lab_discovery

    res = lab_discovery.discover_environment("tok-6", lab_id=LAB_UUID)
    assert res.ok
    assert len(res.topology["nodes"]) == 6
    prompt = lab_discovery.format_discovery_for_prompt(res)
    for label in SIX_LABELS:
        assert label in prompt
    assert "iosvl2" in prompt and "server" in prompt
    summary = lab_discovery.summarize_for_metadata(res)
    assert summary["topology_node_count"] == 6
    assert summary["topology_node_labels"] == SIX_LABELS


# ── 6. Inventory replaces the hardcoded telemetry list ───────────────────────

def test_inventory_driven_telemetry(six_env, monkeypatch):
    from cliq.ai.orchestrator import CopilotOrchestrator

    monkeypatch.setenv("CLIQ_RAG_ENABLED", "0")
    orch = CopilotOrchestrator.__new__(CopilotOrchestrator)
    from cliq.ai.network_context import get_network_context_service
    orch.context_service = get_network_context_service()
    orch.retriever = None
    block = orch._assemble_context_text(
        "tok-6", [], [], user_text="Check ENG1 interface status")
    # ENG1 was never in the old hardcoded R1/R2/SW1/SW2 list.
    assert "[Device ENG1 - IP Interfaces Brief]:" in block
    assert "Link encap" in block
    assert "[Device SW1" not in block
    assert "[Device R1" not in block


# ── 7. Multi-device execution across all six devices ─────────────────────────

@pytest.mark.anyio
async def test_stream_executes_all_six_devices(six_env, monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        orch, store = _orch(monkeypatch, tmp)
        conv = store.create_conversation(title="t", session_token="tok-6")
        events = [ev async for ev in orch.handle_user_message_stream(
            conversation_id=conv.id, user_text=ACCEPTANCE_REQUEST,
            session_token="tok-6")]

    kinds = [e.get("type") for e in events]
    # 2 switches x 5 commands + 4 endpoints x 4 commands, each executed.
    assert kinds.count("tool_call") == 26, kinds
    assert kinds.count("tool_result") == 26
    results = [e["result"] for e in events if e.get("type") == "tool_result"]
    assert all(r["success"] for r in results)
    assert {r["device_label"] for r in results} == set(SIX_LABELS)
    # Actual stdout reached the model context (not manual insertion).
    assert any("802.1q trunking" in r["stdout"] for r in results)
    assert any("Link encap" in r["stdout"] for r in results)
    # Discovery ran; no configuration plan was created.
    disc = [e for e in events if e.get("type") == "lab_discovery"]
    assert len(disc) == 1 and disc[0]["ok"] is True
    assert "config_plan" not in kinds
    assert events[-1]["type"] == "done"


# ── 8. One unreachable device hides nothing else ──────────────────────────────

@pytest.mark.anyio
async def test_unreachable_device_isolated(six_env, monkeypatch):
    from cliq.web import server

    # FIN2 loses its runtime; everything else stays connected.
    monkeypatch.delitem(server._DEVICE_RUNTIMES, ("tok-6", "fin2"))
    with tempfile.TemporaryDirectory() as tmp:
        orch, store = _orch(monkeypatch, tmp)
        conv = store.create_conversation(title="t", session_token="tok-6")
        events = [ev async for ev in orch.handle_user_message_stream(
            conversation_id=conv.id, user_text=ACCEPTANCE_REQUEST,
            session_token="tok-6")]

    results = [e["result"] for e in events if e.get("type") == "tool_result"]
    by_dev = {}
    for r in results:
        by_dev.setdefault(r["device_label"], []).append(r)
    # All six devices still report; the five healthy ones succeed.
    assert set(by_dev) == set(SIX_LABELS)
    for label in ["SW1", "SW2", "ENG1", "ENG2", "FIN1"]:
        assert all(r["success"] for r in by_dev[label]), label
    fin2 = by_dev["FIN2"]
    assert len(fin2) == 4
    assert all(not r["success"] for r in fin2)
    assert all(r["error"] for r in fin2)
    assert events[-1]["type"] == "done"


# ── 9. Output stays associated with the correct device ───────────────────────

@pytest.mark.anyio
async def test_results_associated_per_device(six_env, monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        orch, store = _orch(monkeypatch, tmp)
        conv = store.create_conversation(title="t", session_token="tok-6")
        events = [ev async for ev in orch.handle_user_message_stream(
            conversation_id=conv.id, user_text=ACCEPTANCE_REQUEST,
            session_token="tok-6")]

    results = [e["result"] for e in events if e.get("type") == "tool_result"]
    for r in results:
        assert r["lab_id"] == "lab-ent"
        if r["device_label"] in ("SW1", "SW2"):
            assert r["command"] in SWITCH_COMMANDS, r
        else:
            assert r["command"] in ENDPOINT_COMMANDS, r
    # No platform crossover at the runtime: switches never saw Linux
    # commands and endpoints never saw IOS commands.
    devs = six_env["devs"]
    for label in ("SW1", "SW2"):
        for call in devs[label.lower()].calls:
            assert call.startswith("show "), (label, call)
    for label in ("ENG1", "ENG2", "FIN1", "FIN2"):
        for call in devs[label.lower()].calls:
            assert call.split()[0] in ("hostname", "ifconfig", "route", "arp"), (label, call)
    assert set(devs["sw1"].calls) >= set(SWITCH_COMMANDS)
    assert set(devs["eng1"].calls) >= set(ENDPOINT_COMMANDS)


# ── 10. hostname extraction ──────────────────────────────────────────────────

def test_hostname_extracted_for_endpoints(six_env):
    from cliq.ai.orchestrator import CopilotOrchestrator

    orch = CopilotOrchestrator.__new__(CopilotOrchestrator)
    from cliq.ai.network_context import get_network_context_service
    orch.context_service = get_network_context_service()
    plans, skip = orch._plan_autonomous_multi(
        "tok-6", "On ENG1 collect hostname and ifconfig yourself.")
    assert skip == ""
    assert len(plans) == 1
    assert plans[0].device_id == "eng1"
    assert "hostname" in plans[0].commands
    assert "ifconfig" in plans[0].commands


# ── 11. No "ifconfig -" bullet artifact ──────────────────────────────────────

def test_ifconfig_bullet_artifact_absent(six_env):
    from cliq.ai.orchestrator import CopilotOrchestrator

    orch = CopilotOrchestrator.__new__(CopilotOrchestrator)
    from cliq.ai.network_context import get_network_context_service
    orch.context_service = get_network_context_service()
    plans, skip = orch._plan_autonomous_multi(
        "tok-6",
        "For ENG1 collect:\n- hostname\n- ifconfig\n- route -n\n- arp -n\n"
        "Execute these commands yourself.")
    assert skip == ""
    assert len(plans) == 1
    cmds = plans[0].commands
    assert "ifconfig -" not in cmds
    assert not [c for c in cmds if c.endswith(" -")]
    for expected in ENDPOINT_COMMANDS:
        assert expected in cmds


# ── 12. Discovery contaminates no later configuration ────────────────────────

@pytest.mark.anyio
async def test_discovery_leaves_no_requirements(six_env, monkeypatch):
    from cliq.ai import config_plan

    with tempfile.TemporaryDirectory() as tmp:
        orch, store = _orch(monkeypatch, tmp)
        conv = store.create_conversation(title="t", session_token="tok-6")
        events = [ev async for ev in orch.handle_user_message_stream(
            conversation_id=conv.id, user_text=ACCEPTANCE_REQUEST,
            session_token="tok-6")]

    assert "config_plan" not in [e.get("type") for e in events]
    leftovers = [k for k in config_plan._CONV_REQUIREMENTS if k[1] == conv.id]
    assert leftovers == []
    # A later genuine build inherits no trunk requirement from that turn.
    plan, err = await config_plan.build_plan(
        "tok-6", "Configure VLAN 10 on SW1. Gi0/2 access vlan 10.",
        conversation_id=conv.id)
    assert err == "" and plan is not None
    for dp in plan.devices:
        for step in dp.steps:
            assert "switchport mode trunk" not in step.command
    assert [q for q in plan.questions
            if q.kind == "missing_trunk_interface"] == []


# ── 13. Single-device execution keeps working ─────────────────────────────────

def test_single_device_path_preserved(six_env):
    from cliq.ai.orchestrator import CopilotOrchestrator

    orch = CopilotOrchestrator.__new__(CopilotOrchestrator)
    from cliq.ai.network_context import get_network_context_service
    orch.context_service = get_network_context_service()
    plan = orch._plan_autonomous(
        "tok-6",
        "Inspect SW1 and retrieve show vlan brief and show interfaces "
        "status. Execute these commands yourself.")
    assert plan is not None
    assert plan.device_id == "sw1" and plan.lab_id == "lab-ent"
    assert plan.commands == ["show vlan brief", "show interfaces status"]
    multi, skip = orch._plan_autonomous_multi(
        "tok-6",
        "Inspect SW1 and retrieve show vlan brief and show interfaces "
        "status. Execute these commands yourself.")
    assert skip == ""
    assert len(multi) == 1 and multi[0].device_id == "sw1"


# ── 14. Lab Creation routing safety ───────────────────────────────────────────

def test_lab_creation_routing_preserved(six_env):
    from cliq.ai.network_context import get_network_context_service
    from cliq.ai.orchestrator import CopilotOrchestrator

    orch = CopilotOrchestrator.__new__(CopilotOrchestrator)
    orch.context_service = get_network_context_service()
    for text in ["Create a network lab for two departments.",
                 "Build a lab with two switches and four PCs.",
                 "Discover my CML environment.",
                 "Check which device images are available."]:
        assert CopilotOrchestrator._should_discover_lab(text) is True, text
        mode, _ = CopilotOrchestrator._classify_config_request(
            orch, "tok-6", text)
        assert mode == "none", text
