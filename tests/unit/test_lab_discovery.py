"""
Regression tests for the CML discovery integration gap.

Covers: autonomous controller discovery (ok / missing / SSH / failure /
partial / isolation / secret-safety), Lab Creation intent routing without
accidental config plans, planner grounding in live snapshots, unverified
interface warnings, live-gated deployment confirmation, and multi-turn
discovery retention. All controller I/O uses in-memory fakes.
"""

import json
import os
import tempfile
from types import SimpleNamespace

import pytest

from cliq.ai import lab_discovery
from cliq.labs.deploy import DeploymentError, confirm_deployment, prepare_deployment
from cliq.labs.models import ControllerSnapshot, LabBlueprint, LabLink, LabNode
from cliq.labs.planner import PlannerError, plan_lab
from cliq.labs.validator import validate_blueprint


def _resp(payload):
    return SimpleNamespace(json=lambda: payload)


class FakeCMLAuth:
    def __init__(self, routes=None, sysinfo=None, labs=None, topo=None, failures=None):
        self.routes = routes or {}
        self.sysinfo = sysinfo if sysinfo is not None else {"version": "2.9.0"}
        self.labs = labs if labs is not None else []
        self.topo = topo or {}
        self.failures = failures or {}
        self.calls = []

    def _maybe_fail(self, section):
        if section in self.failures:
            raise self.failures[section]

    def get_system_information(self):
        self.calls.append("sysinfo")
        self._maybe_fail("sysinfo")
        return dict(self.sysinfo)

    def get_labs(self):
        self.calls.append("labs")
        self._maybe_fail("labs")
        return list(self.labs)

    def get_topology(self, lab_id):
        self.calls.append(f"topology:{lab_id}")
        self._maybe_fail("topology")
        return dict(self.topo)

    def _get(self, path):
        self.calls.append(f"GET {path}")
        self._maybe_fail("catalog")
        if path not in self.routes:
            raise RuntimeError(f"404 for {path}")
        return _resp(self.routes[path])


CATALOG_ROUTES = {
    "/node_definitions": [{"id": "iosv"}, {"id": "iosvl2"}, {"id": "server"}],
    "/image_definitions": [{"id": "iosv-15.9"}, {"id": "server-ubuntu"}],
    "/system_stats": {"cpu_total": 16, "cpu_used": 4,
                      "ram_total_mb": 32768, "ram_used_mb": 8192},
    "/system_health": {"ready": True},
    "/system_information": {"version": "2.9.0"},
}


def _lab(ns, lab_id="lab-1", title="Demo", state="DEFINED", owner="admin"):
    return SimpleNamespace(lab_id=lab_id, title=title, state=state, owner=owner)


@pytest.fixture()
def two_controllers(monkeypatch):
    from cliq.web import server
    auth_a = FakeCMLAuth(
        routes=dict(CATALOG_ROUTES),
        sysinfo={"version": "2.9.0", "ready": True},
        labs=[_lab(None, "lab-1", "Existing", "STARTED")],
        topo={"nodes": [{"id": "n1", "label": "R1"}],
              "links": [{"id": "l1", "node_a": "n1", "node_b": "n1"}]})
    auth_b = FakeCMLAuth(
        routes={"/node_definitions": [{"id": "nxosv9000"}],
                "/image_definitions": [],
                "/system_stats": {}, "/system_health": {}, "/system_information": {}},
        sysinfo={"version": "2.8.1"},
        labs=[])
    monkeypatch.setitem(server.SESSIONS, "tok-D1", SimpleNamespace(
        host="cml-a", auth=auth_a, connection_type="cml"))
    monkeypatch.setitem(server.SESSIONS, "tok-D2", SimpleNamespace(
        host="cml-b", auth=auth_b, connection_type="cml"))
    monkeypatch.setitem(server.SESSIONS, "tok-DSSH", SimpleNamespace(
        host="sshbox", auth=None, connection_type="ssh"))
    return {"a": auth_a, "b": auth_b}


def _blueprint():
    return LabBlueprint(
        title="Demo lab",
        nodes=[LabNode(id="R1", label="R1", node_definition="iosv"),
               LabNode(id="SW1", label="SW1", node_definition="iosvl2")],
        links=[LabLink(id="L1", node_a="R1", node_b="SW1",
                       interface_a="Gi0/0", interface_b="Gi0/1")],
    )


# ── 1. Discovery service ─────────────────────────────────────────────────────

def test_discovery_ok(two_controllers):
    res = lab_discovery.discover_environment("tok-D1", lab_id="lab-1")
    assert res.ok and not res.partial and res.error_code == ""
    assert res.host == "cml-a"
    assert res.version == "2.9.0"
    assert res.ready is True
    assert res.node_definitions == ["iosv", "iosvl2", "server"]
    assert res.image_definitions == ["iosv-15.9", "server-ubuntu"]
    assert res.resources and res.resources["cpu_total"] == 16
    assert [lab["title"] for lab in res.labs] == ["Existing"]
    assert res.topology["lab_id"] == "lab-1"
    assert len(res.topology["nodes"]) == 1


def test_discovery_topology_only_when_scoped(two_controllers):
    res = lab_discovery.discover_environment("tok-D1")
    assert res.ok and res.topology is None
    assert not any(c.startswith("topology:") for c in two_controllers["a"].calls)


def test_discovery_no_session(two_controllers):
    res = lab_discovery.discover_environment("tok-bogus")
    assert not res.ok and res.error_code == lab_discovery.NO_SESSION
    assert res.node_definitions is None and res.labs is None


def test_discovery_ssh_rejected(two_controllers):
    res = lab_discovery.discover_environment("tok-DSSH")
    assert not res.ok and res.error_code == lab_discovery.NON_CML_SESSION
    assert "SSH" in res.message


def test_discovery_auth_failure(monkeypatch):
    from cliq.web import server
    auth = FakeCMLAuth(failures={
        "sysinfo": RuntimeError("token expired"),
        "catalog": RuntimeError("Not authenticated"),
        "labs": RuntimeError("login required"),
    })
    monkeypatch.setitem(server.SESSIONS, "tok-DA", SimpleNamespace(
        host="cml-a", auth=auth, connection_type="cml"))
    res = lab_discovery.discover_environment("tok-DA")
    assert not res.ok and not res.partial
    assert res.error_code == lab_discovery.AUTH_FAILURE


def test_discovery_controller_unavailable(monkeypatch):
    from cliq.web import server
    auth = FakeCMLAuth(failures={
        "sysinfo": ConnectionError("Failed to establish a new connection"),
        "catalog": ConnectionError("Failed to establish a new connection"),
        "labs": ConnectionError("Failed to establish a new connection"),
    })
    monkeypatch.setitem(server.SESSIONS, "tok-DC", SimpleNamespace(
        host="cml-a", auth=auth, connection_type="cml"))
    res = lab_discovery.discover_environment("tok-DC")
    assert not res.ok and res.error_code == lab_discovery.CONTROLLER_UNAVAILABLE


def test_discovery_partial(two_controllers, monkeypatch):
    from cliq.web import server
    auth = FakeCMLAuth(
        routes=dict(CATALOG_ROUTES), sysinfo={"version": "2.9.0"},
        failures={"labs": RuntimeError("boom")})
    monkeypatch.setitem(server.SESSIONS, "tok-DP", SimpleNamespace(
        host="cml-a", auth=auth, connection_type="cml"))
    res = lab_discovery.discover_environment("tok-DP")
    assert not res.ok and res.partial
    assert res.node_definitions == ["iosv", "iosvl2", "server"]
    assert res.labs is None
    assert "labs" in res.unavailable


def test_discovery_session_isolation(two_controllers):
    res_a = lab_discovery.discover_environment("tok-D1")
    res_b = lab_discovery.discover_environment("tok-D2")
    assert res_a.host == "cml-a" and res_b.host == "cml-b"
    assert res_a.node_definitions != res_b.node_definitions
    assert "nxosv9000" not in (res_a.node_definitions or [])


def test_discovery_errors_scrub_secrets(monkeypatch):
    from cliq.web import server
    evil = Exception('login failed for admin Bearer abc123 token=xyz "password": "pw"')
    auth = FakeCMLAuth(failures={"sysinfo": evil, "catalog": evil, "labs": evil})
    monkeypatch.setitem(server.SESSIONS, "tok-DE", SimpleNamespace(
        host="cml-a", auth=auth, connection_type="cml"))
    res = lab_discovery.discover_environment("tok-DE")
    assert "abc123" not in res.message and '"pw"' not in res.message


def test_discovery_prompt_block_marks_unavailable(two_controllers):
    res = lab_discovery.discover_environment("tok-D2")
    assert res.ok  # empty sections are reported, not fatal
    assert res.resources is None
    block = lab_discovery.format_discovery_for_prompt(res)
    assert "cml-b" in block and "nxosv9000" in block
    assert "Unavailable" in block and "resources" in block


# ── 2. Copilot routing ───────────────────────────────────────────────────────

def _fake_provider(captured=None):
    from cliq.ai.provider import StreamChunk

    class FakeProvider:
        def get_default_model(self):
            return "fake-model"

        def list_models(self):
            return []

        async def chat_stream(self, messages, model=None):
            if captured is not None:
                captured.append(list(messages))
            yield StreamChunk(delta="noted.", is_finished=True, finish_reason="stop")

    return FakeProvider()


@pytest.mark.anyio
@pytest.mark.parametrize("prompt", [
    "Create a network lab for two departments.",
    "Build a lab with two switches and four PCs.",
    "Discover my CML environment.",
    "Check which device images are available.",
])
async def test_lab_intent_routes_to_discovery(two_controllers, monkeypatch, prompt):
    from cliq.ai.conversations import ConversationStore
    from cliq.ai.orchestrator import CopilotOrchestrator
    import cliq.ai.orchestrator as orch_mod

    monkeypatch.setenv("CLIQ_RAG_ENABLED", "0")
    monkeypatch.setattr(orch_mod, "get_provider", lambda _pid="meta": _fake_provider())
    with tempfile.TemporaryDirectory() as tmp:
        store = ConversationStore(db_path=os.path.join(tmp, "c.sqlite"))
        orch = CopilotOrchestrator(store=store)
        orch.context_service = SimpleNamespace(list_devices=lambda tok: [])
        conv = store.create_conversation(title="t", session_token="tok-D1")
        events = [ev async for ev in orch.handle_user_message_stream(
            conversation_id=conv.id, user_text=prompt, session_token="tok-D1")]
    kinds = [e.get("type") for e in events]
    assert "lab_discovery" in kinds
    disc = next(e for e in events if e.get("type") == "lab_discovery")
    assert disc["ok"] is True
    assert disc["summary"]["host"] == "cml-a"
    # Read-only discovery must not mint device executions or config plans.
    assert "config_plan" not in kinds
    assert "tool_call" not in kinds and "tool_result" not in kinds


@pytest.mark.anyio
async def test_discovery_failure_surfaced_not_fabricated(two_controllers, monkeypatch):
    from cliq.ai.conversations import ConversationStore
    from cliq.ai.orchestrator import CopilotOrchestrator
    import cliq.ai.orchestrator as orch_mod

    monkeypatch.setenv("CLIQ_RAG_ENABLED", "0")
    monkeypatch.setattr(orch_mod, "get_provider", lambda _pid="meta": _fake_provider())
    with tempfile.TemporaryDirectory() as tmp:
        store = ConversationStore(db_path=os.path.join(tmp, "c.sqlite"))
        orch = CopilotOrchestrator(store=store)
        orch.context_service = SimpleNamespace(list_devices=lambda tok: [])
        conv = store.create_conversation(title="t", session_token="tok-DSSH")
        events = [ev async for ev in orch.handle_user_message_stream(
            conversation_id=conv.id, user_text="Discover my CML environment.",
            session_token="tok-DSSH")]
    disc = next(e for e in events if e.get("type") == "lab_discovery")
    assert disc["ok"] is False
    assert disc["error_code"] == lab_discovery.NON_CML_SESSION


@pytest.mark.anyio
async def test_multiturn_discovery_retention(two_controllers, monkeypatch):
    from cliq.ai.conversations import ConversationStore
    from cliq.ai.orchestrator import CopilotOrchestrator
    import cliq.ai.orchestrator as orch_mod

    captured = []
    monkeypatch.setenv("CLIQ_RAG_ENABLED", "0")
    monkeypatch.setattr(orch_mod, "get_provider",
                        lambda _pid="meta": _fake_provider(captured))
    with tempfile.TemporaryDirectory() as tmp:
        store = ConversationStore(db_path=os.path.join(tmp, "c.sqlite"))
        orch = CopilotOrchestrator(store=store)
        orch.context_service = SimpleNamespace(list_devices=lambda tok: [])
        conv = store.create_conversation(title="t", session_token="tok-D1")
        turn1 = [ev async for ev in orch.handle_user_message_stream(
            conversation_id=conv.id, user_text="Discover my CML environment.",
            session_token="tok-D1")]
        assert "lab_discovery" in [e.get("type") for e in turn1]
        turn2 = [ev async for ev in orch.handle_user_message_stream(
            conversation_id=conv.id, user_text="What did you find?",
            session_token="tok-D1")]
        # No fresh discovery on the follow-up, but prior context is retained.
        assert "lab_discovery" not in [e.get("type") for e in turn2]
    assert len(captured) == 2
    turn2_text = "\n".join(m.content for m in captured[1])
    assert "RETAINED FROM AN EARLIER TURN" in turn2_text
    assert "cml-a" in turn2_text


# ── 3. Planner grounding ─────────────────────────────────────────────────────

def _grounded_provider(captured, blueprint_json):
    class FakePlannerProvider:
        def is_configured(self):
            return True

        def get_default_model(self):
            return "fake-plan"

        async def chat(self, messages, model=None, temperature=0.2):
            captured.append(list(messages))
            return SimpleNamespace(content=blueprint_json)

    return FakePlannerProvider()


def _bp_json(defs=("iosv", "iosvl2")):
    nodes = [{"id": f"N{i}", "label": f"N{i}", "node_definition": d}
             for i, d in enumerate(defs)]
    links = ([{"id": "L1", "node_a": "N0", "node_b": "N1",
               "interface_a": "Gi0/0", "interface_b": "Gi0/0"}]
             if len(nodes) > 1 else [])
    return ('{"title": "T", "nodes": %s, "links": %s}'
            % (json.dumps(nodes), json.dumps(links)))


@pytest.mark.anyio
async def test_planner_grounded_in_snapshot(monkeypatch):
    import cliq.ai.registry as registry

    captured = []
    monkeypatch.setattr(registry, "get_provider",
                        lambda _pid: _grounded_provider(captured, _bp_json()))
    snap = ControllerSnapshot(node_definitions=["iosv", "iosvl2"],
                              image_definitions=["img-a"])
    result = await plan_lab("Build a lab.", snapshot=snap)
    user_content = captured[0][1].content
    assert "LIVE CONTROLLER FACTS" in user_content
    assert "iosv" in user_content and "img-a" in user_content
    assert any("interfaces_unverified" in w for w in result.warnings)
    assert result.interfaces_verified is False


@pytest.mark.anyio
async def test_planner_rejects_unlisted_definitions(monkeypatch):
    import cliq.ai.registry as registry

    monkeypatch.setattr(registry, "get_provider",
                        lambda _pid: _grounded_provider([], _bp_json(("furture-os",))))
    snap = ControllerSnapshot(node_definitions=["iosv"])
    with pytest.raises(PlannerError, match="not available on this controller"):
        await plan_lab("Build a lab.", snapshot=snap)


def test_validator_flags_unverified_interfaces():
    result = validate_blueprint(_blueprint())
    assert any(i.code == "interfaces_unverified" for i in result.warnings)
    assert result.valid  # warning, not an error: deploy resolves labels live


# ── 4. Live-gated confirmation ───────────────────────────────────────────────

def test_offline_confirm_rejected():
    ticket = prepare_deployment("tok-X", _blueprint())
    assert ticket.live is False
    with pytest.raises(DeploymentError, match="live-controller validation"):
        confirm_deployment("tok-X", ticket.ticket_id,
                           client_factory=lambda h, u, p: None,
                           session_host="h", session_username="u",
                           session_password="p")


def _boom_factory(host, username, password):
    raise RuntimeError("boom")


def test_offline_override_logged_and_explicit(caplog):
    ticket = prepare_deployment("tok-X", _blueprint())
    with caplog.at_level("WARNING", logger="cliq.labs.deploy"):
        job, created = confirm_deployment(
            "tok-X", ticket.ticket_id, client_factory=_boom_factory,
            session_host="h", session_username="u", session_password="p",
            allow_offline=True)
    assert created is True
    assert "OFFLINE DEPLOYMENT OVERRIDE" in caplog.text
    assert ticket.ticket_id in caplog.text
    assert ticket.consumed_by_job_id == job.job_id


def test_ticket_ownership_preserved():
    ticket = prepare_deployment("tok-X", _blueprint())
    with pytest.raises(DeploymentError, match="approval ticket not found"):
        confirm_deployment("tok-other", ticket.ticket_id,
                           client_factory=lambda h, u, p: None,
                           session_host="h", session_username="u",
                           session_password="p")


def test_live_ticket_confirms_past_gate():
    ticket = prepare_deployment("tok-X", _blueprint(), live=True,
                                snapshot=ControllerSnapshot(
                                    node_definitions=["iosv", "iosvl2"]))
    assert ticket.live is True
    # Passes the live gate and launches (the worker fails quietly on the fake).
    job, created = confirm_deployment(
        "tok-X", ticket.ticket_id, client_factory=_boom_factory,
        session_host="h", session_username="u", session_password="p")
    assert created is True and job.ticket_id == ticket.ticket_id
