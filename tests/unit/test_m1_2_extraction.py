"""
Regression tests for the M1.2 live-acceptance findings.

Live UI showed two unintended failed executions:
1. Prose ("Show the actual execution output for each command") was planned as
   an IOS command.
2. Uppercase ``ARP`` was sent verbatim to a Linux endpoint (``command not
   found`` — Linux is case-sensitive).

These tests pin the corrected behavior: strict command extraction, planner
pre-validation against the target platform, safe case handling, rejection
before execution with reasons, and zero unintended device I/O. All device I/O
uses in-memory fakes; no live CML devices are touched.
"""

import asyncio
import os
import tempfile
from types import SimpleNamespace

import pytest

from cliq.ai.device_exec import (
    plan_autonomous_execution,
    validate_command_for_platform,
)

PROSE_SENTENCE = "Show the actual execution output for each command."
MIXED_PROMPT = (
    "Inspect SW1 and retrieve show vlan brief, show interfaces trunk, "
    "show interfaces status and show mac address-table. "
    "Execute these commands yourself and summarize the results. "
    + PROSE_SENTENCE
)

DEVICES = [
    {"id": "sw1", "label": "SW1", "node_type": "iosvl2"},
    {"id": "pc1", "label": "PC1", "node_type": "server"},
]
LABS = {"sw1": "lab-1", "pc1": "lab-1"}
PLATFORMS = {"sw1": "cisco_ios", "pc1": "linux"}


class FakeConsoleDevice:
    def __init__(self, outputs=None, prompt="PC1:~#"):
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
        outputs={"show vlan brief": "VLAN Name Status\n1 default active"},
        prompt="SW1#",
    )
    pc1 = FakeConsoleDevice(
        outputs={
            "ifconfig -a": "eth0 Link encap",
            "route -n": "Kernel IP routing table",
            "arp -n": "Address HWaddress",
            "arp": "Address HWaddress",
        },
        prompt="PC1:~#",
    )
    monkeypatch.setitem(server.SESSIONS, "tok-A", SimpleNamespace(host="cml", auth=object()))
    monkeypatch.setitem(server.DEVICES_BY_TOKEN, "tok-A", {
        "sw1": {"id": "sw1", "lab_id": "lab-1", "label": "SW1", "type": "iosvl2"},
        "pc1": {"id": "pc1", "lab_id": "lab-1", "label": "PC1", "type": "server"},
    })
    monkeypatch.setitem(server._DEVICE_RUNTIMES, ("tok-A", "sw1"), make_runtime(sw1, "iosvl2"))
    monkeypatch.setitem(server._DEVICE_RUNTIMES, ("tok-A", "pc1"), make_runtime(pc1, "server"))
    return {"sw1": sw1, "pc1": pc1}


# ── Natural language is never treated as a command ───────────────────────────

def test_prose_show_sentence_never_extracted():
    plan = plan_autonomous_execution(
        "Check SW1 yourself. " + PROSE_SENTENCE, DEVICES, LABS, PLATFORMS)
    assert plan is None


def test_prose_fragments_never_extracted():
    for prose in ("show me the results on SW1, run it yourself",
                  "Please show the summary for SW1 and execute it yourself",
                  "SW1: show the details yourself"):
        plan = plan_autonomous_execution(prose, DEVICES, LABS, PLATFORMS)
        assert plan is None, prose


def test_mixed_prompt_extracts_only_real_commands():
    plan = plan_autonomous_execution(MIXED_PROMPT, DEVICES, LABS, PLATFORMS)
    assert plan is not None
    assert plan.commands == ["show vlan brief", "show interfaces trunk",
                             "show interfaces status", "show mac address-table"]
    assert plan.rejected == []


def test_show_without_known_keyword_not_planned():
    plan = plan_autonomous_execution(
        "Retrieve show thingummy on SW1 yourself", DEVICES, LABS, PLATFORMS)
    assert plan is None


def test_show_args_preserved_and_prepositions_excluded():
    plan = plan_autonomous_execution(
        "Retrieve show ip route 10.0.0.0 on SW1 yourself", DEVICES, LABS, PLATFORMS)
    assert plan is not None
    assert plan.commands == ["show ip route 10.0.0.0"]
    plan = plan_autonomous_execution(
        "Get show ip interface brief. Execute it on SW1 yourself",
        DEVICES, LABS, PLATFORMS)
    assert plan is not None
    assert plan.commands == ["show ip interface brief"]


# ── Planner pre-validation: platform mismatches rejected, never planned ──────

def test_ios_command_rejected_for_linux_at_plan_time():
    plan = plan_autonomous_execution(
        "Retrieve show vlan brief on PC1 yourself", DEVICES, LABS, PLATFORMS)
    assert plan is not None
    assert plan.commands == []
    assert len(plan.rejected) == 1
    assert plan.rejected[0].error_code == "platform_mismatch"


def test_linux_command_rejected_for_cisco_at_plan_time():
    plan = plan_autonomous_execution(
        "Run ifconfig on SW1 yourself", DEVICES, LABS, PLATFORMS)
    assert plan is not None
    assert plan.commands == []
    assert plan.rejected[0].command == "ifconfig"
    assert plan.rejected[0].error_code == "platform_mismatch"


def test_mixed_valid_and_mismatched_splits_cleanly():
    plan = plan_autonomous_execution(
        "Run ifconfig and show vlan brief on PC1 yourself", DEVICES, LABS, PLATFORMS)
    assert plan is not None
    assert plan.commands == ["ifconfig"]
    assert [r.command for r in plan.rejected] == ["show vlan brief"]


# ── Capitalization handled safely ────────────────────────────────────────────

def test_uppercase_linux_command_normalized_for_send():
    v = validate_command_for_platform("ARP -n", "linux")
    assert v.ok and v.read_only
    assert v.send_command == "arp -n"
    v = validate_command_for_platform("IFCONFIG", "linux")
    assert v.send_command == "ifconfig"
    v = validate_command_for_platform("ROUTE -N", "linux")
    assert v.send_command == "route -n"


def test_linux_args_preserved_verbatim():
    v = validate_command_for_platform("ping -c 2 HOST.Example.COM", "linux")
    assert v.send_command == "ping -c 2 HOST.Example.COM"


def test_ios_case_insensitive_verbatim():
    v = validate_command_for_platform("Show Vlan Brief", "cisco_ios")
    assert v.ok and v.read_only
    assert v.send_command == "Show Vlan Brief"


@pytest.mark.anyio
async def test_uppercase_arp_executes_normalized(session_env):
    from cliq.ai.device_exec import execute_device_command
    res = await execute_device_command("tok-A", "lab-1", "pc1", "ARP -n")
    assert res.success
    assert res.command == "ARP -n"
    assert res.send_command == "arp -n"
    assert session_env["pc1"].calls == ["arp -n"]
    assert "HWaddress" in res.stdout


# ── Valid commands keep working on supported platforms ───────────────────────

def test_acceptance_commands_still_planned_with_platform_known():
    plan = plan_autonomous_execution(MIXED_PROMPT, DEVICES, LABS, PLATFORMS)
    assert plan is not None and len(plan.commands) == 4


def test_linux_busybox_trio_planned():
    plan = plan_autonomous_execution(
        "Check PC1 yourself: run ifconfig -a, route -n and arp -n",
        DEVICES, LABS, PLATFORMS)
    assert plan is not None
    assert plan.commands == ["ifconfig -a", "route -n", "arp -n"]
    assert plan.rejected == []


@pytest.mark.anyio
async def test_linux_trio_executes(session_env):
    from cliq.ai.device_exec import execute_device_command
    for cmd, marker in (("ifconfig -a", "eth0"), ("route -n", "routing table"),
                        ("arp -n", "HWaddress")):
        res = await execute_device_command("tok-A", "lab-1", "pc1", cmd)
        assert res.success, cmd
        assert marker in res.stdout, cmd


# ── Orchestrator: only planned commands reach the runtime ────────────────────

def _fake_provider_module(monkeypatch):
    from cliq.ai.provider import StreamChunk

    class FakeProvider:
        def get_default_model(self):
            return "fake-model"

        def list_models(self):
            return []

        async def chat_stream(self, messages, model=None):
            yield StreamChunk(delta="done.", is_finished=True, finish_reason="stop")

    import cliq.ai.orchestrator as orch_mod
    monkeypatch.setattr(orch_mod, "get_provider", lambda _pid="meta": FakeProvider())


@pytest.mark.anyio
async def test_stream_emits_only_planned_tool_calls(session_env, monkeypatch):
    from cliq.ai.conversations import ConversationStore
    from cliq.ai.orchestrator import CopilotOrchestrator

    monkeypatch.setenv("CLIQ_RAG_ENABLED", "0")
    _fake_provider_module(monkeypatch)
    with tempfile.TemporaryDirectory() as tmp:
        store = ConversationStore(db_path=os.path.join(tmp, "c.sqlite"))
        orch = CopilotOrchestrator(store=store)
        conv = store.create_conversation(title="t", session_token="tok-A")
        events = [ev async for ev in orch.handle_user_message_stream(
            conversation_id=conv.id, user_text=MIXED_PROMPT, session_token="tok-A")]

    calls = [e for e in events if e.get("type") == "tool_call"]
    assert [c["command"] for c in calls] == [
        "show vlan brief", "show interfaces trunk",
        "show interfaces status", "show mac address-table"]
    assert not [e for e in events if e.get("type") == "tool_rejected"]
    # No prose-derived command reached the runtime.
    assert all("actual execution output" not in c for c in session_env["sw1"].calls)


@pytest.mark.anyio
async def test_stream_rejects_mismatch_without_device_call(session_env, monkeypatch):
    from cliq.ai.conversations import ConversationStore
    from cliq.ai.orchestrator import CopilotOrchestrator

    monkeypatch.setenv("CLIQ_RAG_ENABLED", "0")
    _fake_provider_module(monkeypatch)
    with tempfile.TemporaryDirectory() as tmp:
        store = ConversationStore(db_path=os.path.join(tmp, "c.sqlite"))
        orch = CopilotOrchestrator(store=store)
        conv = store.create_conversation(title="t", session_token="tok-A")
        events = [ev async for ev in orch.handle_user_message_stream(
            conversation_id=conv.id,
            user_text="Retrieve show vlan brief on PC1 yourself",
            session_token="tok-A")]

    assert not [e for e in events if e.get("type") == "tool_call"]
    rejected = [e for e in events if e.get("type") == "tool_rejected"]
    assert len(rejected) == 1
    assert rejected[0]["error_code"] == "platform_mismatch"
    assert "Cisco" in rejected[0]["message"]
    # The mismatched IOS command never reached the Linux runtime; only the
    # platform-appropriate inventory auto-telemetry read ran.
    assert "show vlan brief" not in session_env["pc1"].calls
    assert session_env["pc1"].calls == ["ifconfig -a"]
    assert events[-1]["type"] == "done"


@pytest.mark.anyio
async def test_stream_prose_only_makes_no_cards(session_env, monkeypatch):
    from cliq.ai.conversations import ConversationStore
    from cliq.ai.orchestrator import CopilotOrchestrator

    monkeypatch.setenv("CLIQ_RAG_ENABLED", "0")
    _fake_provider_module(monkeypatch)
    with tempfile.TemporaryDirectory() as tmp:
        store = ConversationStore(db_path=os.path.join(tmp, "c.sqlite"))
        orch = CopilotOrchestrator(store=store)
        conv = store.create_conversation(title="t", session_token="tok-A")
        events = [ev async for ev in orch.handle_user_message_stream(
            conversation_id=conv.id,
            user_text="Check SW1 yourself. " + PROSE_SENTENCE,
            session_token="tok-A")]

    kinds = [e.get("type") for e in events]
    assert "tool_call" not in kinds
    assert "tool_result" not in kinds
    assert "tool_rejected" not in kinds
    # Only the pre-existing auto-context read may touch the device; no prose.
    assert all("actual execution output" not in c for c in session_env["sw1"].calls)


@pytest.mark.anyio
async def test_rejection_reaches_model_context(session_env, monkeypatch):
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
            yield StreamChunk(delta="noted.", is_finished=True, finish_reason="stop")

    import cliq.ai.orchestrator as orch_mod
    monkeypatch.setattr(orch_mod, "get_provider", lambda _pid="meta": FakeProvider())
    with tempfile.TemporaryDirectory() as tmp:
        store = ConversationStore(db_path=os.path.join(tmp, "c.sqlite"))
        orch = CopilotOrchestrator(store=store)
        conv = store.create_conversation(title="t", session_token="tok-A")
        [ev async for ev in orch.handle_user_message_stream(
            conversation_id=conv.id,
            user_text="Run ifconfig on SW1 yourself",
            session_token="tok-A")]

    user_msg = seen["messages"][-1].content
    assert "Not executed: ifconfig" in user_msg
    assert "platform_mismatch" in user_msg
