"""
Regression tests for CLIQ UI usability + conversation lifecycle fix.

Covers: device display classification, fail-closed auth on conversation /
upload / context / exec / config routes, cross-session ownership of
conversations / plans / baselines, and conversation persistence across a
simulated backend restart. All device I/O uses in-memory fakes.
"""

import asyncio
from types import SimpleNamespace

import pytest

from cliq.ai import baseline as baseline_mod
from cliq.ai import config_plan
from cliq.ai.conversations import ConversationStore
from cliq.ai.device_exec import classify_platform, describe_platform


VLAN_BRIEF_OK = (
    "VLAN Name                             Status    Ports\n"
    "1    default                          active    Gi0/2\n"
    "10   Users                            active    Gi0/1\n"
)
TRUNK_OK = (
    "Port        Mode         Encapsulation  Status        Native vlan\n"
    "Gi0/24      on           802.1q         trunking      1\n"
)
STATUS_OK = (
    "Port      Name               Status       Vlan       Duplex  Speed\n"
    "Gi0/1                        connected    10         a-full  a-1000\n"
)
MAC_OK = "Mac Address Table\n0011.2233.4455    DYNAMIC     Gi0/1\n"
STP_OK = (
    "VLAN0010\n"
    "  Spanning tree enabled protocol rstp\n"
    "  VLAN0010 is executing the rstp compatible Spanning Tree protocol\n"
    "  Gi0/1                Desg FWD 19         128.1    P2p\n"
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


def _runtime(device, node_type):
    return SimpleNamespace(
        device=device, mode="exec", status="connected",
        prompt=device.current_prompt, node_type=node_type,
        lock=asyncio.Lock(), dead=False, dead_reason="",
        empty_read_strikes=0,
    )


@pytest.fixture()
def two_sessions(monkeypatch, tmp_path):
    """Two authenticated sessions with isolated inventories + isolated copilot DB."""
    from cliq.web import server
    from cliq.web import copilot_routes

    sw1 = FakeConsoleDevice(
        outputs={
            "show vlan brief": VLAN_BRIEF_OK,
            "show interfaces trunk": TRUNK_OK,
            "show interfaces status": STATUS_OK,
            "show mac address-table": MAC_OK,
            "show spanning-tree vlan 10": STP_OK,
            "show spanning-tree vlan 20": STP_OK.replace("VLAN0010", "VLAN0020"),
        },
        prompt="SW1#",
    )
    monkeypatch.setitem(server.SESSIONS, "tok-A", SimpleNamespace(host="cml", auth=object()))
    monkeypatch.setitem(server.SESSIONS, "tok-B", SimpleNamespace(host="cml", auth=object()))
    monkeypatch.setitem(server.DEVICES_BY_TOKEN, "tok-A", {
        "sw1": {"id": "sw1", "lab_id": "lab-1", "label": "SW1", "type": "iosvl2"},
    })
    monkeypatch.setitem(server.DEVICES_BY_TOKEN, "tok-B", {
        "sw9": {"id": "sw9", "lab_id": "lab-9", "label": "SW9", "type": "iosvl2"},
    })
    monkeypatch.setitem(server._DEVICE_RUNTIMES, ("tok-A", "sw1"), _runtime(sw1, "iosvl2"))
    iso_store = ConversationStore(db_path=str(tmp_path / "copilot.sqlite"))
    monkeypatch.setattr(copilot_routes, "store", iso_store)
    return {"sw1": sw1}


def _client():
    from fastapi.testclient import TestClient
    from cliq.web.server import app
    return TestClient(app)


# ── 1. Device display classification ─────────────────────────────────────────

def test_describe_platform_labels():
    assert describe_platform("iosv") == "Cisco IOS Router"
    assert describe_platform("IOSv") == "Cisco IOS Router"
    assert describe_platform("iosvl2") == "Cisco IOSvL2 Switch"
    assert describe_platform("server") == "Linux Endpoint"
    assert describe_platform("desktop") == "Linux Endpoint"
    assert describe_platform("linux") == "Linux Endpoint"
    assert describe_platform("unknown") == "Unknown platform"
    assert describe_platform("") == "Unknown platform"
    assert describe_platform("external_connector") == "Unknown platform"


def test_classify_platform_execution_validation_intact():
    assert classify_platform("iosv") == "cisco_ios"
    assert classify_platform("iosvl2") == "cisco_ios"
    assert classify_platform("server") == "linux"
    assert classify_platform("desktop") == "linux"
    assert classify_platform("external_connector") == ""
    assert classify_platform("") == ""


# ── 2. Fail-closed auth (401) ────────────────────────────────────────────────

def test_unauthenticated_requests_rejected(two_sessions):
    client = _client()
    # No token at all.
    assert client.get("/api/copilot/conversations").status_code == 401
    assert client.post("/api/copilot/conversations", json={"title": "x"}).status_code == 401
    assert client.get("/api/copilot/conversations/c1").status_code == 401
    assert client.patch("/api/copilot/conversations/c1", json={"title": "y"}).status_code == 401
    assert client.delete("/api/copilot/conversations/c1").status_code == 401
    assert client.post("/api/copilot/conversations/c1/messages", json={"content": "hi"}).status_code == 401
    assert client.post("/api/copilot/upload", json={
        "conversation_id": "c1", "filename": "a.log", "content": "x"}).status_code == 401
    assert client.get("/api/copilot/context/devices").status_code == 401
    assert client.get("/api/copilot/context/query?device_id=sw1").status_code == 401
    assert client.post("/api/copilot/exec/prepare", json={
        "lab_id": "lab-1", "device_id": "sw1", "command": "show vlan brief"}).status_code == 401
    assert client.post("/api/copilot/exec/confirm", json={"ticket_id": "t"}).status_code == 401
    assert client.post("/api/copilot/exec", json={
        "lab_id": "lab-1", "device_id": "sw1", "command": "show vlan brief"}).status_code == 401
    assert client.post("/api/copilot/config/plan", json={"prompt": "hi"}).status_code == 401
    assert client.get("/api/copilot/config/plan/p1").status_code == 401
    assert client.post("/api/copilot/config/plan/p1/answer", json={"answers": {}}).status_code == 401
    assert client.post("/api/copilot/config/prepare", json={"plan_id": "p1"}).status_code == 401
    assert client.post("/api/copilot/config/approve", json={"ticket_id": "t"}).status_code == 401
    assert client.post("/api/copilot/config/execute", json={
        "plan_id": "p1", "approval_token": "t"}).status_code == 401
    assert client.post("/api/copilot/config/baseline", json={
        "lab_id": "lab-1", "device_ids": ["sw1"]}).status_code == 401
    assert client.get("/api/copilot/config/baseline/b1").status_code == 401


def test_bogus_token_rejected(two_sessions):
    client = _client()
    assert client.get("/api/copilot/conversations?token=bogus").status_code == 401
    assert client.get("/api/copilot/conversations/c1?token=bogus").status_code == 401
    assert client.get("/api/copilot/context/devices?token=bogus").status_code == 401
    assert client.get("/api/copilot/config/plan/p1?token=bogus").status_code == 401
    assert client.get("/api/copilot/config/baseline/b1?token=bogus").status_code == 401


# ── 3. Cross-user conversation isolation ─────────────────────────────────────

def test_cross_user_conversation_isolation(two_sessions):
    client = _client()
    created = client.post("/api/copilot/conversations?token=tok-A", json={"title": "A private"})
    assert created.status_code == 200
    conv_id = created.json()["id"]
    try:
        # B's listing must not include A's conversation.
        listing = client.get("/api/copilot/conversations?token=tok-B")
        assert listing.status_code == 200
        assert all(c["id"] != conv_id for c in listing.json()["conversations"])
        # Direct ID access by B is rejected without leaking content.
        got = client.get(f"/api/copilot/conversations/{conv_id}?token=tok-B")
        assert got.status_code == 404
        assert "private" not in got.text.lower()
        # Rename / delete by B are rejected; A still owns the conversation.
        assert client.patch(f"/api/copilot/conversations/{conv_id}?token=tok-B",
                            json={"title": "hijacked"}).status_code == 404
        assert client.delete(f"/api/copilot/conversations/{conv_id}?token=tok-B").status_code == 404
        assert client.get(f"/api/copilot/conversations/{conv_id}?token=tok-A").status_code == 200
        # Uploads into A's conversation with B's token are rejected.
        assert client.post("/api/copilot/upload?token=tok-B", json={
            "conversation_id": conv_id, "filename": "x.log", "content": "x"}).status_code == 404
    finally:
        client.delete(f"/api/copilot/conversations/{conv_id}?token=tok-A")


# ── 4. Cross-user plan / baseline isolation ──────────────────────────────────

def _seed_plan(owner="tok-A"):
    plan = config_plan.ConfigPlan(
        plan_id="cfg-uitest1", owner_token=owner, lab_id="lab-1",
        lab_title="lab-1", title="UI test plan", status="draft",
    )
    config_plan._store_plan(plan)
    return plan.plan_id


def _drop_plan(plan_id):
    with config_plan._plan_lock:
        config_plan._plans.pop(plan_id, None)


def _drop_baseline(baseline_id):
    with baseline_mod._lock:
        baseline_mod._baselines.pop(baseline_id, None)
        baseline_mod._baseline_owners.pop(baseline_id, None)


def test_cross_user_plan_isolation(two_sessions):
    client = _client()
    plan_id = _seed_plan("tok-A")
    try:
        # B cannot read, answer, review, or approve A's plan.
        assert client.get(f"/api/copilot/config/plan/{plan_id}?token=tok-B").status_code == 403
        assert client.post(f"/api/copilot/config/plan/{plan_id}/answer?token=tok-B",
                           json={"answers": {}}).status_code == 403
        assert client.post("/api/copilot/config/prepare?token=tok-B",
                           json={"plan_id": plan_id}).status_code == 403
        assert client.post("/api/copilot/config/approve?token=tok-B",
                           json={"ticket_id": "bogus"}).status_code == 403
        # B cannot execute A's plan even with a fabricated token: rejected, no device I/O.
        ran = client.post("/api/copilot/config/execute?token=tok-B", json={
            "plan_id": plan_id, "approval_token": "bogus"})
        assert ran.status_code == 200
        assert "plan_rejected" in ran.text
        # B cannot launder plan ownership through baseline collection.
        assert client.post("/api/copilot/config/baseline?token=tok-B",
                           json={"plan_id": plan_id}).status_code == 403
        # Owner access still works.
        assert client.get(f"/api/copilot/config/plan/{plan_id}?token=tok-A").status_code == 200
    finally:
        _drop_plan(plan_id)


@pytest.mark.anyio
async def test_cross_user_baseline_isolation(two_sessions):
    client = _client()
    record, err = await baseline_mod.collect_baseline("tok-A", "lab-1", ["sw1"])
    assert err == "", err
    baseline_id = record["baseline_id"]
    try:
        assert baseline_mod.get_baseline_owner(baseline_id) == "tok-A"
        assert client.get(f"/api/copilot/config/baseline/{baseline_id}?token=tok-A").status_code == 200
        denied = client.get(f"/api/copilot/config/baseline/{baseline_id}?token=tok-B")
        assert denied.status_code == 403
        assert "baseline_id" not in denied.text
    finally:
        _drop_baseline(baseline_id)


def test_cross_user_exec_history_scoped_to_session(two_sessions):
    """Exec prepare/confirm/execute resolve devices only within the caller's session."""
    client = _client()
    # tok-B cannot target tok-A's device: it is not in B's inventory.
    res = client.post("/api/copilot/exec/prepare?token=tok-B", json={
        "lab_id": "lab-1", "device_id": "sw1", "command": "show vlan brief"})
    assert res.status_code == 404
    # tok-A cannot claim tok-B's lab for its own device either.
    res = client.post("/api/copilot/exec/prepare?token=tok-A", json={
        "lab_id": "lab-9", "device_id": "sw1", "command": "show vlan brief"})
    assert res.status_code == 403


# ── 5. Persistence across backend restart ────────────────────────────────────

def test_conversations_survive_restart(tmp_path):
    db = str(tmp_path / "restart.sqlite")
    first = ConversationStore(db_path=db)
    conv = first.create_conversation(title="persist me", session_token="tok-A")
    first.add_message(conv.id, "user", "hello", metadata={"k": "v"})
    assert conv.session_token == "tok-A"
    # Simulate a backend restart: a brand-new store over the same file.
    second = ConversationStore(db_path=db)
    restored = second.get_conversation(conv.id, session_token="tok-A")
    assert restored is not None
    assert restored.title == "persist me"
    assert [m.content for m in restored.messages] == ["hello"]
    assert restored.messages[0].metadata == {"k": "v"}
    # Ownership still enforced after restart.
    assert second.get_conversation(conv.id, session_token="tok-B") is None
