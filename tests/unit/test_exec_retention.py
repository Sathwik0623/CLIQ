"""
Regression tests for historical autonomous execution evidence retention.

Execution results persist in assistant-message metadata (``tool_results``);
later turns in the SAME conversation must be able to retrieve and cite them.
All device I/O uses in-memory fakes. No live CML is touched.
"""

import asyncio
import os
import tempfile
from types import SimpleNamespace

import pytest


class FakeConsoleDevice:
    def __init__(self, outputs=None, prompt="SW1#"):
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


@pytest.fixture()
def retain_env(monkeypatch):
    """tok-R owns lab-1 (SW1) and lab-2 (SW2)."""
    from cliq.web import server

    sw1 = FakeConsoleDevice(outputs={
        "show vlan brief": "VLAN-BRIEF-SW1",
        "show interfaces status": "STATUS-SW1",
    })
    sw2 = FakeConsoleDevice(outputs={"show vlan brief": "VLAN-BRIEF-SW2"},
                            prompt="SW2#")

    def rt(dev, node_type, prompt):
        return SimpleNamespace(device=dev, mode="exec", status="connected",
                               prompt=prompt, node_type=node_type,
                               lock=asyncio.Lock(), dead=False,
                               dead_reason="", empty_read_strikes=0)

    monkeypatch.setitem(server.SESSIONS, "tok-R",
                        SimpleNamespace(host="cml", auth=object()))
    monkeypatch.setitem(server.DEVICES_BY_TOKEN, "tok-R", {
        "sw1": {"id": "sw1", "lab_id": "lab-1", "label": "SW1", "type": "iosvl2"},
        "sw2": {"id": "sw2", "lab_id": "lab-2", "label": "SW2", "type": "iosvl2"},
    })
    monkeypatch.setitem(server._DEVICE_RUNTIMES, ("tok-R", "sw1"),
                        rt(sw1, "iosvl2", "SW1#"))
    monkeypatch.setitem(server._DEVICE_RUNTIMES, ("tok-R", "sw2"),
                        rt(sw2, "iosvl2", "SW2#"))
    return {"sw1": sw1, "sw2": sw2}


def _orch(monkeypatch, tmp, seen):
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
            seen.append(list(messages))
            yield StreamChunk(delta="noted.", is_finished=True,
                              finish_reason="stop")

    import cliq.ai.orchestrator as orch_mod
    monkeypatch.setattr(orch_mod, "get_provider", lambda _pid="meta": FakeProvider())
    store = ConversationStore(db_path=os.path.join(tmp, "c.sqlite"))
    return CopilotOrchestrator(store=store), store


async def _turn(orch, store, conv_id, token, text):
    return [ev async for ev in orch.handle_user_message_stream(
        conversation_id=conv_id, session_token=token, user_text=text)]


def _result(device_id="sw1", label="SW1", lab="lab-1", command="show vlan brief",
            stdout="OUT", stderr="", success=True, exit_status=0,
            error="", error_code=""):
    return {"device_id": device_id, "device_label": label, "lab_id": lab,
            "platform": "cisco_ios", "command": command,
            "send_command": command, "stdout": stdout, "stderr": stderr,
            "success": success, "exit_status": exit_status,
            "error": error, "error_code": error_code, "latency_ms": 1.5}


# ── Same-conversation retrieval + assessment injection ───────────────────────

@pytest.mark.anyio
async def test_same_conversation_historical_retrieval(retain_env, monkeypatch):
    from cliq.ai.orchestrator import CopilotOrchestrator

    with tempfile.TemporaryDirectory() as tmp:
        orch, store = _orch(monkeypatch, tmp, [])
        conv = store.create_conversation(title="t", session_token="tok-R")
        await _turn(orch, store, conv.id, "tok-R",
                   "SW1: show vlan brief, show interfaces status. Run them yourself.")
        conv = store.get_conversation(conv.id, session_token="tok-R")
        entries = CopilotOrchestrator._prior_exec_results(conv)
        assert len(entries) == 2
        by_cmd = {e["command"]: e for e in entries}
        assert by_cmd["show vlan brief"]["stdout"] == "VLAN-BRIEF-SW1"
        assert by_cmd["show interfaces status"]["stdout"] == "STATUS-SW1"
        e = by_cmd["show vlan brief"]
        assert (e["device_id"], e["device_label"], e["lab_id"]) == ("sw1", "SW1", "lab-1")
        assert e["platform"] == "cisco_ios" and e["exit_status"] == 0
        assert e["success"] is True


@pytest.mark.anyio
async def test_evidence_injection_into_assessment_turn(retain_env, monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        seen = []
        orch, store = _orch(monkeypatch, tmp, seen)
        conv = store.create_conversation(title="t", session_token="tok-R")
        await _turn(orch, store, conv.id, "tok-R",
                   "SW1: show vlan brief. Run it yourself.")
        await _turn(orch, store, conv.id, "tok-R",
                   "Assess the SW1 VLAN state from the earlier outputs.")
        prompt = seen[-1][-1].content
        assert "RETAINED DEVICE EXECUTION RESULTS" in prompt
        assert "EARLIER TURNS, NOT LIVE" in prompt
        assert "VLAN-BRIEF-SW1" in prompt
        assert "show vlan brief" in prompt


@pytest.mark.anyio
async def test_no_cross_conversation_leakage(retain_env, monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        seen = []
        orch, store = _orch(monkeypatch, tmp, seen)
        conv_a = store.create_conversation(title="a", session_token="tok-R")
        await _turn(orch, store, conv_a.id, "tok-R",
                   "SW1: show vlan brief. Run it yourself.")
        conv_b = store.create_conversation(title="b", session_token="tok-R")
        await _turn(orch, store, conv_b.id, "tok-R",
                   "Assess the SW1 VLAN state from the earlier outputs.")
        prompt = seen[-1][-1].content
        assert "VLAN-BRIEF-SW1" not in prompt
        assert "RETAINED DEVICE EXECUTION RESULTS" not in prompt


@pytest.mark.anyio
async def test_fresh_execution_precedence(retain_env, monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        seen = []
        orch, store = _orch(monkeypatch, tmp, seen)
        conv = store.create_conversation(title="t", session_token="tok-R")
        await _turn(orch, store, conv.id, "tok-R",
                   "SW1: show vlan brief. Run it yourself.")
        await _turn(orch, store, conv.id, "tok-R",
                   "SW1: show interfaces status. Run it yourself.")
        prompt = seen[-1][-1].content
        assert "COPILOT AUTONOMOUS EXECUTION RESULTS" in prompt
        assert "STATUS-SW1" in prompt
        assert "RETAINED DEVICE EXECUTION RESULTS" not in prompt


# ── Reader unit behavior (seeded metadata, real store) ───────────────────────

def _seeded_conv(tmp_path, payloads):
    from cliq.ai.conversations import ConversationStore

    store = ConversationStore(db_path=os.path.join(tmp_path, "c.sqlite"))
    conv = store.create_conversation(title="t", session_token="tok-R")
    store.add_message(conv.id, role="user", content="run")
    for i, results in enumerate(payloads):
        store.add_message(conv.id, role="assistant", content=f"reply{i}",
                          metadata={"tool_results": results})
    return store.get_conversation(conv.id, session_token="tok-R")


def test_lab_scoping(tmp_path):
    from cliq.ai.orchestrator import CopilotOrchestrator as C

    conv = _seeded_conv(str(tmp_path), [[
        _result(lab="lab-1", stdout="ONE"),
        _result(device_id="sw2", label="SW2", lab="lab-2", stdout="TWO")]])
    scoped = C._prior_exec_results(conv, lab_id="lab-2")
    assert [e["stdout"] for e in scoped] == ["TWO"]
    scoped = C._prior_exec_results(conv, lab_id="lab-9")
    assert len(scoped) == 2  # no match: nothing hidden


def test_device_scoping_from_turn_text(tmp_path):
    from cliq.ai.orchestrator import CopilotOrchestrator as C

    conv = _seeded_conv(str(tmp_path), [[
        _result(stdout="ONE"),
        _result(device_id="sw2", label="SW2", lab="lab-2", stdout="TWO")]])
    scoped = C._prior_exec_results(conv, user_text="Assess SW2 from earlier.")
    assert [e["stdout"] for e in scoped] == ["TWO"]
    scoped = C._prior_exec_results(conv, user_text="Assess everything.")
    assert len(scoped) == 2


def test_missing_metadata_handling(tmp_path):
    from cliq.ai.conversations import ConversationStore
    from cliq.ai.orchestrator import CopilotOrchestrator as C

    store = ConversationStore(db_path=os.path.join(str(tmp_path), "c.sqlite"))
    conv = store.create_conversation(title="t", session_token="tok-R")
    store.add_message(conv.id, role="user", content="hi")
    store.add_message(conv.id, role="assistant", content="no meta")
    store.add_message(conv.id, role="assistant", content="junk",
                      metadata={"tool_results": "not-a-list"})
    store.add_message(conv.id, role="assistant", content="junk2",
                      metadata={"tool_results": [None, "x", {"command": ""}]})
    conv = store.get_conversation(conv.id, session_token="tok-R")
    assert C._prior_exec_results(conv) == []
    assert C._prior_exec_results(None) == []


def test_character_budget_enforcement(tmp_path):
    from cliq.ai.orchestrator import CopilotOrchestrator as C

    big = "X" * 20000
    conv = _seeded_conv(str(tmp_path), [[_result(stdout=big)]])
    block = C._format_retained_exec_results(C._prior_exec_results(conv))
    assert len(block) < 12000 + 2000
    assert "truncat" in block.lower()
    many = [[_result(command=f"show v{i}", stdout=f"OUT{i}") for i in range(12)]]
    conv = _seeded_conv(str(tmp_path), many)
    assert len(C._prior_exec_results(conv)) == 10


def test_failed_status_preserved(tmp_path):
    from cliq.ai.orchestrator import CopilotOrchestrator as C

    conv = _seeded_conv(str(tmp_path), [[_result(
        success=False, exit_status=1, stdout="% Invalid input",
        error="% Invalid input", error_code="device_error")]])
    (entry,) = C._prior_exec_results(conv)
    assert entry["success"] is False and entry["exit_status"] == 1
    assert entry["error_code"] == "device_error"
    block = C._format_retained_exec_results([entry])
    assert "success=False" in block and "% Invalid input" in block


def test_timestamp_preserved_when_available(tmp_path):
    from cliq.ai.orchestrator import CopilotOrchestrator as C

    conv = _seeded_conv(str(tmp_path), [[_result(stdout="OUT")]])
    (entry,) = C._prior_exec_results(conv)
    assert "recorded_at" in entry and "T" in entry["recorded_at"]
    block = C._format_retained_exec_results([entry])
    assert entry["recorded_at"] in block
    ghost = SimpleNamespace(messages=[SimpleNamespace(metadata={
        "tool_results": [_result(stdout="OUT")]})])
    (entry2,) = C._prior_exec_results(ghost)
    assert "recorded_at" not in entry2  # never fabricated
