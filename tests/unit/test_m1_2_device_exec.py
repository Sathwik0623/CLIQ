"""
Unit tests for CLIQ M1.2 - Autonomous Device Command Execution.

Covers the secure Copilot execution tool (``cliq.ai.device_exec``):
platform-aware validation, session/lab ownership, approval enforcement for
configuration-changing commands, timeout/disconnected handling, the REST
endpoints, and the orchestrator tool-event flow.

All device I/O uses in-memory fakes through the REAL ``execute_device_command``
path (ownership -> validation -> approval -> ``runtime.lock`` + console driver
call). No live devices are touched.
"""

import asyncio
import os
import tempfile
from types import SimpleNamespace

import pytest

from cliq.ai.device_exec import (
    AutonomousPlan,
    classify_platform,
    confirm_approval,
    consume_approval_token,
    execute_device_command,
    plan_autonomous_execution,
    prepare_approval,
    validate_command_for_platform,
)

ACCEPTANCE_PROMPT = (
    "Inspect SW1 and retrieve show vlan brief, show interfaces trunk, "
    "show interfaces status and show mac address-table. "
    "Execute these commands yourself and summarize the results."
)


# ── Fakes ────────────────────────────────────────────────────────────────────

class FakeConsoleDevice:
    """Mimics CMLConsoleDevice/SSHConsoleDevice driver surface."""

    def __init__(self, outputs=None, prompt="SW1#", connected=True):
        self.outputs = dict(outputs or {})
        self.current_prompt = prompt
        self.is_connected = connected
        self.calls = []

    def execute_paged(self, command, on_chunk=None):
        self.calls.append(command)
        return self.outputs.get(command, f"<output of {command}>"), False

    def execute(self, command):
        self.calls.append(command)
        return self.outputs.get(command, f"<output of {command}>")


class FakeSlowDevice(FakeConsoleDevice):
    def execute_paged(self, command, on_chunk=None):
        import time
        self.calls.append(command)
        time.sleep(15)
        return "too slow", False


def make_runtime(device, node_type="iosvl2"):
    return SimpleNamespace(
        device=device,
        mode="exec",
        status="connected",
        prompt=getattr(device, "current_prompt", ""),
        node_type=node_type,
        lock=asyncio.Lock(),
        dead=False,
        dead_reason="",
        empty_read_strikes=0,
    )


@pytest.fixture()
def session_env(monkeypatch):
    """Two sessions: tok-A owns lab-1 (SW1 iosvl2, R1 iosv, SRV1 server)."""
    from cliq.web import server

    sw1 = FakeConsoleDevice(
        outputs={
            "show vlan brief": "VLAN Name Status Ports\n1 default active Gi0/0",
            "show interfaces trunk": "Port Mode Encapsulation Status\nGi0/1 on 802.1q trunking",
            "show interfaces status": "Port Name Status Vlan\nGi0/0 connected 1",
            "show mac address-table": "Mac Address Table\n0011.2233.4455 DYNAMIC Gi0/0",
        },
        prompt="SW1#",
    )
    r1 = FakeConsoleDevice(outputs={"show ip route": "C 10.0.0.0/24 via Gi0/0"}, prompt="R1#")
    srv1 = FakeConsoleDevice(
        outputs={"ifconfig": "eth0 Link encap:Ethernet", "ping 8.8.8.8 -c 4": "4 packets transmitted, 4 received"},
        prompt="SRV1:~#",
    )
    monkeypatch.setitem(server.SESSIONS, "tok-A", SimpleNamespace(host="cml", auth=object()))
    monkeypatch.setitem(server.SESSIONS, "tok-B", SimpleNamespace(host="cml", auth=object()))
    monkeypatch.setitem(server.DEVICES_BY_TOKEN, "tok-A", {
        "sw1": {"id": "sw1", "lab_id": "lab-1", "label": "SW1", "type": "iosvl2"},
        "r1": {"id": "r1", "lab_id": "lab-1", "label": "R1", "type": "iosv"},
        "srv1": {"id": "srv1", "lab_id": "lab-1", "label": "SRV1", "type": "server"},
    })
    monkeypatch.setitem(server.DEVICES_BY_TOKEN, "tok-B", {
        "other": {"id": "other", "lab_id": "lab-9", "label": "OTHER", "type": "iosv"},
    })
    monkeypatch.setitem(server._DEVICE_RUNTIMES, ("tok-A", "sw1"), make_runtime(sw1, "iosvl2"))
    monkeypatch.setitem(server._DEVICE_RUNTIMES, ("tok-A", "r1"), make_runtime(r1, "iosv"))
    monkeypatch.setitem(server._DEVICE_RUNTIMES, ("tok-A", "srv1"), make_runtime(srv1, "server"))
    return {"sw1": sw1, "r1": r1, "srv1": srv1}


# ── Platform classification ──────────────────────────────────────────────────

def test_classify_platform():
    assert classify_platform("iosv") == "cisco_ios"
    assert classify_platform("iosvl2") == "cisco_ios"
    assert classify_platform("IOSvL2") == "cisco_ios"
    assert classify_platform("server") == "linux"
    assert classify_platform("desktop") == "linux"
    assert classify_platform("") == ""
    assert classify_platform("unknown") == ""
    assert classify_platform("external_connector") == ""
    assert classify_platform("", "cisco_ios") == "cisco_ios"


# ── Command validation ───────────────────────────────────────────────────────

def test_ios_show_is_read_only():
    for cmd in ("show vlan brief", "show interfaces trunk",
                "show interfaces status", "show mac address-table",
                "show ip route", "show running-config", "ping 10.0.0.1"):
        v = validate_command_for_platform(cmd, "cisco_ios")
        assert v.ok and v.read_only, cmd


def test_ios_pipe_modifiers():
    v = validate_command_for_platform("show run | include hostname", "cisco_ios")
    assert v.ok and v.read_only
    v = validate_command_for_platform("show run | grep hostname", "cisco_ios")
    assert not v.ok and v.error_code == "unsupported_command"


def test_ios_config_requires_approval_flag():
    for cmd in ("configure terminal", "interface Gi0/1", "hostname SW1",
                "vlan 10", "no shutdown", "do show vlan briefx"):
        v = validate_command_for_platform(cmd, "cisco_ios")
        assert v.ok, cmd
    assert not validate_command_for_platform("configure terminal", "cisco_ios").read_only
    assert not validate_command_for_platform("interface Gi0/1", "cisco_ios").read_only
    # 'do'-prefixed show stays read-only.
    assert validate_command_for_platform("do show vlan brief", "cisco_ios").read_only


def test_ios_rejects_linux_commands():
    for cmd in ("ifconfig", "ifconfig eth0", "route -n", "arp -a", "netstat -rn"):
        v = validate_command_for_platform(cmd, "cisco_ios")
        assert not v.ok and v.error_code == "platform_mismatch", cmd


def test_linux_diagnostics_are_read_only():
    for cmd in ("ifconfig", "ifconfig eth0", "route", "route -n",
                "arp -a", "ping 8.8.8.8", "traceroute 8.8.8.8",
                "ip route show", "ip addr show", "netstat -rn"):
        v = validate_command_for_platform(cmd, "linux")
        assert v.ok and v.read_only, cmd


def test_linux_ping_gets_count():
    v = validate_command_for_platform("ping 8.8.8.8", "linux")
    assert v.send_command == "ping 8.8.8.8 -c 4"
    v = validate_command_for_platform("ping -c 2 8.8.8.8", "linux")
    assert v.send_command == "ping -c 2 8.8.8.8"


def test_linux_rejects_ios_commands():
    for cmd in ("show vlan brief", "show ip route", "configure terminal",
                "switchport mode access", "vlan 10"):
        v = validate_command_for_platform(cmd, "linux")
        assert not v.ok and v.error_code == "platform_mismatch", cmd


def test_linux_config_forms_need_approval():
    for cmd in ("ifconfig eth0 192.168.1.2", "ifconfig eth0 up",
                "route add default gw 10.0.0.1", "arp -d 10.0.0.1",
                "ip addr add 10.0.0.2/24 dev eth0"):
        v = validate_command_for_platform(cmd, "linux")
        assert v.ok and not v.read_only, cmd


def test_critical_destructive_always_blocked():
    for cmd in ("reload", "write erase", "erase startup-config", "format flash:"):
        assert validate_command_for_platform(cmd, "cisco_ios").error_code == "blocked_destructive"
        assert validate_command_for_platform(cmd, "linux").error_code in (
            "blocked_destructive", "platform_mismatch", "unsupported_command")


def test_chaining_and_shape_rejected():
    for cmd in ("show version; reload", "show ip route && show x",
                "ping 1.1.1.1 || echo hi", "show `id` version", "show $(id)"):
        v = validate_command_for_platform(cmd, "cisco_ios")
        assert not v.ok and v.error_code == "multi_command", cmd
    assert validate_command_for_platform("", "cisco_ios").error_code == "empty_command"
    assert validate_command_for_platform("show\nversion", "cisco_ios").error_code == "multi_command"
    assert validate_command_for_platform("x" * 600, "cisco_ios").error_code == "command_too_long"


# ── Intent planning ──────────────────────────────────────────────────────────

def test_planner_acceptance_prompt():
    plan = plan_autonomous_execution(
        ACCEPTANCE_PROMPT, [{"id": "sw1", "label": "SW1"}], {"sw1": "lab-1"})
    assert isinstance(plan, AutonomousPlan)
    assert plan.device_id == "sw1"
    assert plan.lab_id == "lab-1"
    assert plan.commands == ["show vlan brief", "show interfaces trunk",
                             "show interfaces status", "show mac address-table"]


def test_planner_ignores_advisory_questions():
    plan = plan_autonomous_execution(
        "What does show vlan brief do on SW1?", [{"id": "sw1", "label": "SW1"}])
    assert plan is None


def test_planner_rejects_ambiguous_devices():
    plan = plan_autonomous_execution(
        "Run show vlan brief on SW1 and SW2 yourself",
        [{"id": "sw1", "label": "SW1"}, {"id": "sw2", "label": "SW2"}])
    assert plan is None


def test_planner_linux_commands():
    plan = plan_autonomous_execution(
        "Check SRV1 yourself: run ifconfig and route -n",
        [{"id": "srv1", "label": "SRV1"}], {"srv1": "lab-1"})
    assert plan is not None and plan.device_id == "srv1"
    assert "ifconfig" in plan.commands and "route -n" in plan.commands


# ── Execution: happy paths ───────────────────────────────────────────────────

@pytest.mark.anyio
async def test_ios_exec_success(session_env):
    res = await execute_device_command("tok-A", "lab-1", "sw1", "show vlan brief")
    assert res.success and res.error_code == ""
    assert res.platform == "cisco_ios"
    assert res.device_label == "SW1"
    assert "VLAN Name" in res.stdout
    assert res.exit_status == 0
    assert session_env["sw1"].calls == ["show vlan brief"]


@pytest.mark.anyio
async def test_linux_exec_success(session_env):
    res = await execute_device_command("tok-A", "lab-1", "srv1", "ifconfig")
    assert res.success
    assert res.platform == "linux"
    assert "eth0" in res.stdout


@pytest.mark.anyio
async def test_linux_ping_exec_sends_count(session_env):
    res = await execute_device_command("tok-A", "lab-1", "srv1", "ping 8.8.8.8")
    assert res.success
    assert session_env["srv1"].calls == ["ping 8.8.8.8 -c 4"]
    assert "packets transmitted" in res.stdout


# ── Execution: platform mismatch never touches the device ────────────────────

@pytest.mark.anyio
async def test_ios_rejects_linux_without_device_call(session_env):
    res = await execute_device_command("tok-A", "lab-1", "sw1", "ifconfig")
    assert not res.success and res.error_code == "platform_mismatch"
    assert session_env["sw1"].calls == []


@pytest.mark.anyio
async def test_linux_rejects_ios_without_device_call(session_env):
    res = await execute_device_command("tok-A", "lab-1", "srv1", "show vlan brief")
    assert not res.success and res.error_code == "platform_mismatch"
    assert session_env["srv1"].calls == []


# ── Execution: ownership ─────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_unauthorized_token(session_env):
    res = await execute_device_command("nope", "lab-1", "sw1", "show vlan brief")
    assert res.error_code == "unauthorized"
    assert session_env["sw1"].calls == []


@pytest.mark.anyio
async def test_device_not_found(session_env):
    res = await execute_device_command("tok-A", "lab-1", "ghost", "show vlan brief")
    assert res.error_code == "device_not_found"


@pytest.mark.anyio
async def test_wrong_lab_rejected(session_env):
    res = await execute_device_command("tok-A", "lab-2", "sw1", "show vlan brief")
    assert res.error_code == "wrong_lab"
    assert session_env["sw1"].calls == []


@pytest.mark.anyio
async def test_cross_session_device_not_leaked(session_env):
    res = await execute_device_command("tok-A", "lab-9", "other", "show vlan brief")
    assert res.error_code == "device_not_found"


# ── Execution: approval enforcement ──────────────────────────────────────────

@pytest.mark.anyio
async def test_config_requires_approval_then_executes(session_env):
    held = await execute_device_command("tok-A", "lab-1", "sw1", "configure terminal")
    assert not held.success and held.error_code == "approval_required"
    assert held.approval_required and held.ticket
    assert session_env["sw1"].calls == []

    ticket_id = held.ticket["ticket_id"]
    rec = confirm_approval("tok-A", ticket_id)
    assert rec and rec["approval_token"]

    ran = await execute_device_command(
        "tok-A", "lab-1", "sw1", "configure terminal",
        approval_token=rec["approval_token"])
    assert ran.success
    assert session_env["sw1"].calls == ["configure terminal"]


@pytest.mark.anyio
async def test_approval_token_single_use(session_env):
    held = await execute_device_command("tok-A", "lab-1", "sw1", "hostname SW1")
    rec = confirm_approval("tok-A", held.ticket["ticket_id"])
    first = await execute_device_command(
        "tok-A", "lab-1", "sw1", "hostname SW1", approval_token=rec["approval_token"])
    assert first.success
    second = await execute_device_command(
        "tok-A", "lab-1", "sw1", "hostname SW1", approval_token=rec["approval_token"])
    assert second.error_code == "approval_required"


@pytest.mark.anyio
async def test_approval_bound_to_exact_device_command(session_env):
    ticket = prepare_approval("tok-A", "lab-1", "sw1", "SW1", "cisco_ios", "hostname SW1")
    rec = confirm_approval("tok-A", ticket["ticket_id"])
    # Same token on a different device must fail.
    assert not consume_approval_token("tok-A", "lab-1", "r1", "hostname SW1", rec["approval_token"])
    # Same token on a different command must fail.
    assert not consume_approval_token("tok-A", "lab-1", "sw1", "hostname OTHER", rec["approval_token"])


@pytest.mark.anyio
async def test_foreign_ticket_rejected(session_env):
    ticket = prepare_approval("tok-B", "lab-9", "other", "OTHER", "cisco_ios", "hostname X")
    assert confirm_approval("tok-A", ticket["ticket_id"]) is None


@pytest.mark.anyio
async def test_blocked_destructive_never_executes(session_env):
    res = await execute_device_command("tok-A", "lab-1", "sw1", "reload")
    assert res.error_code == "blocked_destructive"
    assert session_env["sw1"].calls == []


# ── Execution: timeout / disconnected / device errors ────────────────────────

@pytest.mark.anyio
async def test_timeout(session_env, monkeypatch):
    from cliq.web import server
    slow = FakeSlowDevice(prompt="SW1#")
    monkeypatch.setitem(server._DEVICE_RUNTIMES, ("tok-A", "sw1"), make_runtime(slow, "iosvl2"))
    res = await execute_device_command("tok-A", "lab-1", "sw1", "show vlan brief", timeout=5)
    assert res.error_code == "timeout"
    assert res.exit_status == 124


@pytest.mark.anyio
async def test_disconnected_no_runtime(session_env, monkeypatch):
    from cliq.web import server
    monkeypatch.delitem(server._DEVICE_RUNTIMES, ("tok-A", "sw1"))
    res = await execute_device_command("tok-A", "lab-1", "sw1", "show vlan brief")
    assert res.error_code == "device_disconnected"


@pytest.mark.anyio
async def test_disconnected_dead_runtime(session_env):
    from cliq.web import server
    rt = server._DEVICE_RUNTIMES[("tok-A", "sw1")]
    rt.dead = True
    rt.dead_reason = "socket_close"
    try:
        res = await execute_device_command("tok-A", "lab-1", "sw1", "show vlan brief")
    finally:
        rt.dead = False
        rt.dead_reason = ""
    assert res.error_code == "device_disconnected"


@pytest.mark.anyio
async def test_device_error_markers_reported(session_env):
    session_env["sw1"].outputs["show interfaces trunk"] = (
        "% Invalid input detected at '^' marker.")
    res = await execute_device_command("tok-A", "lab-1", "sw1", "show interfaces trunk")
    assert not res.success
    assert res.error_code == "device_error"
    assert res.exit_status == 1
    assert "% Invalid input" in res.stdout
    assert "% Invalid input" in res.error


# ── Orchestrator integration ─────────────────────────────────────────────────

def test_orchestrator_plan_and_format(session_env):
    from cliq.ai.conversations import ConversationStore
    from cliq.ai.orchestrator import CopilotOrchestrator

    with tempfile.TemporaryDirectory() as tmp:
        orch = CopilotOrchestrator(store=ConversationStore(db_path=os.path.join(tmp, "c.sqlite")))
        plan = orch._plan_autonomous("tok-A", ACCEPTANCE_PROMPT)
        assert plan is not None
        assert plan.device_id == "sw1" and plan.lab_id == "lab-1"
        assert len(plan.commands) == 4
        block = orch._format_tool_results(plan, [{
            "command": "show vlan brief", "success": True, "exit_status": 0,
            "stdout": "VLAN Name Status", "error": "", "error_code": "",
        }])
        assert "COPILOT AUTONOMOUS EXECUTION RESULTS" in block
        assert "VLAN Name Status" in block


@pytest.mark.anyio
async def test_orchestrator_stream_returns_actual_output(session_env, monkeypatch):
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
            yield StreamChunk(delta="SW1 has VLAN 1 active.", is_finished=True,
                              finish_reason="stop")

    import cliq.ai.orchestrator as orch_mod
    monkeypatch.setattr(orch_mod, "get_provider", lambda _pid="meta": FakeProvider())

    with tempfile.TemporaryDirectory() as tmp:
        store = ConversationStore(db_path=os.path.join(tmp, "c.sqlite"))
        orch = CopilotOrchestrator(store=store)
        conv = store.create_conversation(title="t", session_token="tok-A")
        events = []
        async for ev in orch.handle_user_message_stream(
                conversation_id=conv.id, user_text=ACCEPTANCE_PROMPT,
                session_token="tok-A"):
            events.append(ev)

    kinds = [e.get("type") for e in events]
    assert kinds.count("tool_call") == 4
    assert kinds.count("tool_result") == 4
    results = [e["result"] for e in events if e.get("type") == "tool_result"]
    assert all(r["success"] for r in results)
    assert "VLAN Name" in results[0]["stdout"]
    assert "trunking" in results[1]["stdout"]
    assert events[-1]["type"] == "done"
    # No manual-insertion dependency: outputs arrived as tool results. The four
    # planned commands run first; the pre-existing auto-context service may add
    # its own 'show ip interface brief' afterwards.
    assert session_env["sw1"].calls[:4] == [
        "show vlan brief", "show interfaces trunk",
        "show interfaces status", "show mac address-table"]


# ── REST endpoints ───────────────────────────────────────────────────────────

def test_rest_exec_roundtrip(session_env):
    from fastapi.testclient import TestClient
    from cliq.web.server import app

    client = TestClient(app)
    ok = client.post("/api/copilot/exec?token=tok-A", json={
        "lab_id": "lab-1", "device_id": "sw1", "command": "show vlan brief"})
    assert ok.status_code == 200, ok.text
    assert ok.json()["stdout"].find("VLAN Name") >= 0

    held = client.post("/api/copilot/exec?token=tok-A", json={
        "lab_id": "lab-1", "device_id": "sw1", "command": "configure terminal"})
    assert held.status_code == 403
    ticket_id = held.json()["ticket"]["ticket_id"]

    conf = client.post("/api/copilot/exec/confirm?token=tok-A", json={"ticket_id": ticket_id})
    assert conf.status_code == 200
    appr = conf.json()["approval_token"]

    ran = client.post("/api/copilot/exec?token=tok-A", json={
        "lab_id": "lab-1", "device_id": "sw1", "command": "configure terminal",
        "approval_token": appr})
    assert ran.status_code == 200
    assert ran.json()["success"] is True


def test_rest_exec_rejections(session_env):
    from fastapi.testclient import TestClient
    from cliq.web.server import app

    client = TestClient(app)
    assert client.post("/api/copilot/exec?token=bogus", json={
        "lab_id": "lab-1", "device_id": "sw1", "command": "show vlan brief"}).status_code == 401
    wrong = client.post("/api/copilot/exec?token=tok-A", json={
        "lab_id": "lab-1", "device_id": "sw1", "command": "ifconfig"})
    assert wrong.status_code == 422
    assert wrong.json()["error_code"] == "platform_mismatch"
    lab = client.post("/api/copilot/exec?token=tok-A", json={
        "lab_id": "lab-2", "device_id": "sw1", "command": "show vlan brief"})
    assert lab.status_code == 403
    missing = client.post("/api/copilot/exec?token=tok-A", json={
        "lab_id": "lab-1", "device_id": "ghost", "command": "show vlan brief"})
    assert missing.status_code == 404
    spec = client.get("/api/copilot/exec/tool-spec")
    assert spec.status_code == 200
    assert spec.json()["name"] == "execute_device_command"


# ── Pager drain / command-output association ────────────────────────────────

class FakePagedConsoleDevice:
    """Simulates CML console --More-- pager mechanics.

    Each scripted command yields a list of pages. Crucially, like real IOS,
    a new command submitted while the pager is pending does NOT execute:
    its bytes hit the pager and the caller receives the leftover page
    instead — the exact SW1/SW2 misassociation failure.
    """

    def __init__(self, scripts, prompt="SW1#"):
        self.scripts = dict(scripts)
        self.current_prompt = prompt
        self.is_connected = True
        self.calls = []
        self.pending = []

    def execute_paged(self, command, on_chunk=None):
        self.calls.append(command)
        if self.pending:
            leftover = self.pending.pop(0)
            return f"[DESYNC: got leftover page instead of {command!r}]\n" + leftover, bool(self.pending)
        pages = list(self.scripts.get(command, [f"<output of {command}>"]))
        first = pages.pop(0)
        self.pending = pages
        return first, bool(pages)

    def pager_send_key(self, key):
        if key == "q":
            self.pending = []
            return "", False
        if self.pending:
            return self.pending.pop(0), bool(self.pending)
        return "", False


@pytest.fixture()
def paged_env(monkeypatch):
    """tok-P owns lab-1 with one paged IOS switch (SW1)."""
    from cliq.web import server

    sw1 = FakePagedConsoleDevice(scripts={
        "show running-config": ["hostname SW1\nvlan 10", "vlan 20\ninterface Gi0/1", "end"],
        "show vlan brief": ["VLAN Name Status\n10 ENG active"],
        "show bad": ["% Invalid input detected"],
    })
    monkeypatch.setitem(server.SESSIONS, "tok-P", SimpleNamespace(host="cml", auth=object()))
    monkeypatch.setitem(server.DEVICES_BY_TOKEN, "tok-P", {
        "sw1": {"id": "sw1", "lab_id": "lab-1", "label": "SW1", "type": "iosvl2"},
    })
    monkeypatch.setitem(server._DEVICE_RUNTIMES, ("tok-P", "sw1"), make_runtime(sw1, "iosvl2"))
    return {"sw1": sw1}


@pytest.mark.anyio
async def test_paged_output_fully_drained(paged_env):
    res = await execute_device_command("tok-P", "lab-1", "sw1", "show running-config")
    assert res.success
    assert "hostname SW1" in res.stdout
    assert "vlan 20" in res.stdout
    assert res.stdout.rstrip().endswith("end")
    assert "DESYNC" not in res.stdout
    assert res.command == "show running-config"
    assert res.device_id == "sw1" and res.lab_id == "lab-1"
    # Device left at a clean prompt: nothing pending for the next command.
    assert paged_env["sw1"].pending == []


@pytest.mark.anyio
async def test_sequential_commands_keep_own_output(paged_env):
    first = await execute_device_command("tok-P", "lab-1", "sw1", "show running-config")
    second = await execute_device_command("tok-P", "lab-1", "sw1", "show vlan brief")
    assert first.success and second.success
    assert "hostname SW1" in first.stdout and "end" in first.stdout
    assert "VLAN Name" in second.stdout
    assert "vlan 20" not in second.stdout  # never page 2 of command 1
    assert "DESYNC" not in second.stdout
    assert (first.command, second.command) == ("show running-config", "show vlan brief")


@pytest.mark.anyio
async def test_pager_cap_quits_and_marks(paged_env, monkeypatch):
    import cliq.ai.device_exec as de
    monkeypatch.setattr(de, "MAX_PAGER_PAGES", 2)
    res = await execute_device_command("tok-P", "lab-1", "sw1", "show running-config")
    assert res.success  # partial content, honestly marked
    assert "hostname SW1" in res.stdout
    assert "pager truncated after 2 pages" in res.stdout
    # Pager was quit: the next command starts clean.
    assert paged_env["sw1"].pending == []
    nxt = await execute_device_command("tok-P", "lab-1", "sw1", "show vlan brief")
    assert nxt.success and "VLAN Name" in nxt.stdout


@pytest.mark.anyio
async def test_partial_failure_continues_associated(paged_env):
    bad = await execute_device_command("tok-P", "lab-1", "sw1", "show bad")
    good = await execute_device_command("tok-P", "lab-1", "sw1", "show vlan brief")
    assert bad.success is False
    assert bad.error_code == "device_error" and bad.exit_status == 1
    assert "% Invalid input" in bad.stdout
    assert good.success is True and good.error_code == ""
    assert "VLAN Name" in good.stdout
    assert (bad.command, good.command) == ("show bad", "show vlan brief")


# ── Dispatch: explicit requests must execute or fail loudly ──────────────────

def test_verbless_explicit_request_dispatches():
    from cliq.ai.device_exec import plan_multi_execution

    plans = plan_multi_execution(
        "SW1: show vlan brief, show interfaces status.",
        [{"id": "sw1", "label": "SW1"}],
        {"sw1": "lab-1"}, {"sw1": "cisco_ios"})
    assert len(plans) == 1
    assert plans[0].device_id == "sw1"
    assert plans[0].commands == ["show vlan brief", "show interfaces status"]


def test_declined_request_skips_silently():
    from cliq.ai.device_exec import plan_multi_execution

    devs = [{"id": "sw1", "label": "SW1"}]
    for text in ["Don't run show vlan brief on SW1.",
                 "Do not execute show vlan brief on SW1.",
                 "Never run show vlan brief on SW1 yourself."]:
        assert plan_multi_execution(
            text, devs, {"sw1": "lab-1"}, {"sw1": "cisco_ios"}) == [], text
    # A near-miss with a clause boundary still dispatches.
    plans = plan_multi_execution(
        "I don't know, run show vlan brief on SW1.",
        devs, {"sw1": "lab-1"}, {"sw1": "cisco_ios"})
    assert len(plans) == 1 and plans[0].commands == ["show vlan brief"]


def test_sw1_resolves_exactly_one():
    from cliq.ai.device_exec import plan_multi_execution

    devs = [{"id": "sw1", "label": "SW1"},
            {"id": "sw2", "label": "SW2"},
            {"id": "sw10", "label": "SW10"}]
    lookups = ({"sw1": "lab-1", "sw2": "lab-1", "sw10": "lab-1"},
               {"sw1": "cisco_ios", "sw2": "cisco_ios", "sw10": "cisco_ios"})
    plans = plan_multi_execution("SW1: show vlan brief.", devs, *lookups)
    assert [p.device_id for p in plans] == ["sw1"]


@pytest.mark.anyio
async def test_five_commands_execute_sequentially(paged_env, monkeypatch):
    from cliq.ai.conversations import ConversationStore
    from cliq.ai.orchestrator import CopilotOrchestrator
    from cliq.ai.provider import StreamChunk

    monkeypatch.setenv("CLIQ_RAG_ENABLED", "0")
    seen = {}

    class FakeProvider:
        def get_default_model(self):
            return "fake-model"

        def list_models(self):
            return []

        async def chat_stream(self, messages, model=None):
            seen["messages"] = messages
            yield StreamChunk(delta="done.", is_finished=True, finish_reason="stop")

    import cliq.ai.orchestrator as orch_mod
    monkeypatch.setattr(orch_mod, "get_provider", lambda _pid="meta": FakeProvider())
    with tempfile.TemporaryDirectory() as tmp:
        store = ConversationStore(db_path=os.path.join(tmp, "c.sqlite"))
        orch = CopilotOrchestrator(store=store)
        conv = store.create_conversation(title="t", session_token="tok-P")
        events = [ev async for ev in orch.handle_user_message_stream(
            conversation_id=conv.id, session_token="tok-P",
            user_text=("SW1: show running-config, show vlan brief, "
                       "show interfaces status, show interfaces trunk, "
                       "show mac address-table."))]

    calls = [e for e in events if e.get("type") == "tool_call"]
    assert [c["command"] for c in calls] == [
        "show running-config", "show vlan brief", "show interfaces status",
        "show interfaces trunk", "show mac address-table"]
    results = [e["result"] for e in events if e.get("type") == "tool_result"]
    assert len(results) == 5
    for r, c in zip(results, calls):
        assert r["device_id"] == "sw1" and r["lab_id"] == "lab-1"
        assert r["command"] == c["command"]
    assert "VLAN Name" in results[1]["stdout"]
    assert "COPILOT AUTONOMOUS EXECUTION RESULTS" in seen["messages"][-1].content


@pytest.mark.anyio
async def test_dispatch_failure_reported_explicitly(monkeypatch):
    from cliq.web import server
    from cliq.ai.conversations import ConversationStore
    from cliq.ai.orchestrator import CopilotOrchestrator
    from cliq.ai.provider import StreamChunk

    monkeypatch.setenv("CLIQ_RAG_ENABLED", "0")
    sw1 = FakeConsoleDevice(outputs={"show vlan brief": "VLAN Name"})
    # Inventory WITHOUT lab context: mention matching works (telemetry fires)
    # but no plan survives the lab filter.
    monkeypatch.setitem(server.SESSIONS, "tok-L", SimpleNamespace(host="cml", auth=object()))
    monkeypatch.setitem(server.DEVICES_BY_TOKEN, "tok-L", {
        "sw1": {"id": "sw1", "label": "SW1", "type": "iosvl2"},
    })
    monkeypatch.setitem(server._DEVICE_RUNTIMES, ("tok-L", "sw1"), make_runtime(sw1, "iosvl2"))
    seen = {}

    class FakeProvider:
        def get_default_model(self):
            return "fake-model"

        def list_models(self):
            return []

        async def chat_stream(self, messages, model=None):
            seen["messages"] = messages
            yield StreamChunk(delta="done.", is_finished=True, finish_reason="stop")

    import cliq.ai.orchestrator as orch_mod
    monkeypatch.setattr(orch_mod, "get_provider", lambda _pid="meta": FakeProvider())
    with tempfile.TemporaryDirectory() as tmp:
        store = ConversationStore(db_path=os.path.join(tmp, "c.sqlite"))
        orch = CopilotOrchestrator(store=store)
        conv = store.create_conversation(title="t", session_token="tok-L")
        events = [ev async for ev in orch.handle_user_message_stream(
            conversation_id=conv.id, session_token="tok-L",
            user_text="Collect show vlan brief on SW1.")]

    kinds = [e.get("type") for e in events]
    assert "tool_call" not in kinds  # nothing dispatched...
    rejected = [e for e in events if e.get("type") == "tool_rejected"]
    assert len(rejected) == 1  # ...but the failure is explicit, not silent
    assert rejected[0]["error_code"] == "dispatch_failed"
    assert "SW1" in rejected[0]["message"]
    assert "No commands were dispatched" in seen["messages"][-1].content
