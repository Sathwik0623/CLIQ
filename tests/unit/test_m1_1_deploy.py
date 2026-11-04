"""
Mocked integration tests for CLIQ M1.1 deployment flow.

Prepare → approve → confirm → poll → topology, with a scripted fake
ClientLibrary/Lab. NOTHING here contacts a live controller: the SDK client
factory is patched, and fakes raise on any forbidden call (start/stop/wipe/
remove/delete/configuration).
"""

import time
import uuid

import pytest

from cliq.labs.deploy import DeploymentError, prepare_deployment


def _valid_blueprint_dict() -> dict:
    return {
        "version": "1.0",
        "title": "Demo lab",
        "nodes": [
            {"id": "R1", "label": "R1", "node_definition": "iosv"},
            {"id": "R2", "label": "R2", "node_definition": "iosv"},
            {"id": "SW1", "label": "SW1", "node_definition": "iosvl2"},
        ],
        "links": [
            {"id": "L1", "node_a": "R1", "node_b": "R2",
             "interface_a": "Gi0/0", "interface_b": "Gi0/0"},
            {"id": "L2", "node_a": "R1", "node_b": "SW1",
             "interface_a": "Gi0/1", "interface_b": "Gi0/0"},
        ],
    }


_FORBIDDEN_ATTRS = ("start", "stop", "wipe", "remove", "delete",
                    "remove_lab", "configuration")


class _FakeIface:
    def __init__(self, iid, label):
        self.id = iid
        self.label = label


class _FakeNode:
    def __init__(self, nid, label):
        self.id = nid
        self.label = label


class _FakeLink:
    def __init__(self, lid):
        self.id = lid


class _FakeLab:
    def __init__(self, lab_id="lab-123", fail_on=(), calls=None):
        self.id = lab_id
        self._fail_on = set(fail_on)
        self.calls = calls if calls is not None else []
        self._slot = 0

    def __getattr__(self, name):
        if name in _FORBIDDEN_ATTRS:
            raise AssertionError(f"forbidden SDK call: {name}")
        raise AttributeError(name)

    def create_node(self, label, node_definition, x=0, y=0, wait=False, **kwargs):
        assert wait is False, "deploy must never block on convergence"
        self.calls.append(("create_node", label, node_definition, x, y, kwargs))
        if f"node:{label}" in self._fail_on:
            raise RuntimeError(f"controller refused node {label}")
        return _FakeNode(f"node-{label}", label)

    def create_interface(self, node, slot=None, wait=False):
        assert wait is False
        self.calls.append(("create_interface", node.id, slot))
        if "interface" in self._fail_on:
            raise RuntimeError("controller refused interface")
        self._slot += 1
        return _FakeIface(f"iface-{self._slot}", f"Ethernet0/{self._slot - 1}")

    def create_link(self, i1, i2, wait=False):
        assert wait is False
        self.calls.append(("create_link", i1.id, i2.id))
        if "link" in self._fail_on:
            raise RuntimeError("controller refused link")
        return _FakeLink(f"link-{i1.id}-{i2.id}")

    def state(self):
        self.calls.append(("state",))
        return "DEFINED"


class _FakeClient:
    def __init__(self, existing_titles=(), lab=None, calls=None):
        self._existing = list(existing_titles)
        self._lab = lab if lab is not None else _FakeLab()
        self.calls = calls if calls is not None else []

    def __getattr__(self, name):
        if name in _FORBIDDEN_ATTRS:
            raise AssertionError(f"forbidden SDK call: {name}")
        raise AttributeError(name)

    def find_labs_by_title(self, title):
        self.calls.append(("find_labs_by_title", title))
        return list(self._existing)

    def create_lab(self, title=None, description=None, notes=None):
        self.calls.append(("create_lab", title, description, notes))
        return self._lab


class _FakeCMLAuth:
    def __init__(self, routes=None, topology=None):
        self.routes = routes or {}
        self.topology = topology or {"nodes": [], "links": []}
        self.calls: list[str] = []

    class _Resp:
        def __init__(self, payload):
            self._payload = payload

        def json(self):
            return self._payload

    def _get(self, path):
        self.calls.append(path)
        return _FakeCMLAuth._Resp(self.routes.get(path, []))

    def get_topology(self, lab_id):
        self.calls.append(f"TOPO:{lab_id}")
        return self.topology


# Live-controller catalog fixture: confirmation requires live-validated
# tickets, so success-path tests prepare with live=True against these routes.
_LIVE_ROUTES = {
    "/node_definitions": [{"id": "iosv"}, {"id": "iosvl2"}],
    "/image_definitions": [],
    "/system_stats": {},
    "/system_health": {},
    "/system_information": {"version": "2.9.0"},
}


def _live_auth(**kwargs):
    kwargs.setdefault("routes", dict(_LIVE_ROUTES))
    return _FakeCMLAuth(**kwargs)


@pytest.fixture
def deploy_session():
    from cliq.web.server import SESSIONS, AuthSession

    created: list[str] = []

    def _make(auth=None):
        token = f"dep-test-{uuid.uuid4().hex}"
        SESSIONS[token] = AuthSession(
            host="cml.test", username="u", password="p", auth=auth)
        created.append(token)
        return token, {"Authorization": f"Bearer {token}"}

    yield _make
    for token in created:
        SESSIONS.pop(token, None)


def _client():
    from fastapi.testclient import TestClient
    from cliq.web.server import app
    return TestClient(app)


def _poll_terminal(client, headers, job_id, timeout_s=10.0) -> dict:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        res = client.get(f"/api/labs/deploy/{job_id}", headers=headers)
        assert res.status_code == 200, res.text
        body = res.json()
        if body["status"] in ("succeeded", "failed"):
            return body
        time.sleep(0.05)
    raise AssertionError("job did not reach a terminal state")


# -----------------------------------------------------------------------------
# 1. Approval tickets
# -----------------------------------------------------------------------------

def test_prepare_mints_ticket_offline(deploy_session):
    _, headers = deploy_session(auth=_FakeCMLAuth())
    res = _client().post("/api/labs/deploy/prepare",
                         json={"blueprint": _valid_blueprint_dict()},
                         headers=headers)
    assert res.status_code == 200, res.text
    ticket = res.json()
    assert ticket["ticket_id"].startswith("ticket-")
    assert ticket["lab_title"] == "Demo lab"
    assert ticket["validation"]["valid"] is True
    assert ticket["blueprint_hash"]
    assert ticket["expires_at"] > time.time()


def test_prepare_refuses_invalid_blueprint(deploy_session):
    _, headers = deploy_session(auth=_FakeCMLAuth())
    bad = _valid_blueprint_dict()
    bad["nodes"][0]["node_definition"] = "quantum-router"
    res = _client().post("/api/labs/deploy/prepare",
                         json={"blueprint": bad}, headers=headers)
    assert res.status_code == 422
    assert "refusing" in res.json()["detail"]


def test_prepare_rejects_bad_title(deploy_session):
    _, headers = deploy_session(auth=_FakeCMLAuth())
    res = _client().post("/api/labs/deploy/prepare",
                         json={"blueprint": _valid_blueprint_dict(),
                               "lab_title": "../evil"},
                         headers=headers)
    assert res.status_code == 422


def test_prepare_requires_auth():
    res = _client().post("/api/labs/deploy/prepare",
                         json={"blueprint": _valid_blueprint_dict()})
    assert res.status_code == 401


def test_prepare_unit_rejects_invalid():
    from cliq.labs.models import LabBlueprint

    bad = _valid_blueprint_dict()
    bad["nodes"][0]["node_definition"] = "nope"
    with pytest.raises(DeploymentError, match="refusing"):
        prepare_deployment("tok", LabBlueprint.model_validate(bad))


# -----------------------------------------------------------------------------
# 2. Confirm + full mocked deployment sequence
# -----------------------------------------------------------------------------

def test_confirm_runs_exact_sdk_sequence(deploy_session, monkeypatch):
    token, headers = deploy_session(auth=_live_auth())
    client = _client()
    prep = client.post("/api/labs/deploy/prepare",
                       json={"blueprint": _valid_blueprint_dict(), "live": True},
                       headers=headers).json()

    calls: list = []
    fake_lab = _FakeLab(calls=calls)
    fake_client = _FakeClient(lab=fake_lab, calls=calls)
    monkeypatch.setattr("cliq.labs.deploy.build_client_library",
                        lambda h, u, p: fake_client)

    res = client.post("/api/labs/deploy/confirm",
                      json={"ticket_id": prep["ticket_id"],
                            "idempotency_key": "demo-1"},
                      headers=headers)
    assert res.status_code == 202, res.text
    job_id = res.json()["job_id"]

    final = _poll_terminal(client, headers, job_id)
    assert final["status"] == "succeeded", final
    assert final["lab_id"] == "lab-123"
    assert final["lab_state"] == "DEFINED"
    assert len(final["interface_map"]) == 4  # 2 links x 2 ends

    kinds = [c[0] for c in calls]
    assert kinds[0] == "find_labs_by_title"
    assert kinds[1] == "create_lab"
    assert calls[1][1] == "Demo lab"  # title passed through
    node_calls = [c for c in calls if c[0] == "create_node"]
    assert [c[1] for c in node_calls] == ["R1", "R2", "SW1"]
    assert node_calls[0][2] == "iosv" and node_calls[2][2] == "iosvl2"
    assert all(c[5].get("tags") == ["cliq"] for c in node_calls)
    assert len([c for c in calls if c[0] == "create_interface"]) == 4
    assert len([c for c in calls if c[0] == "create_link"]) == 2
    # wait=False asserted inside the fakes on every mutating call.


def test_confirm_unknown_ticket_is_404(deploy_session, monkeypatch):
    _, headers = deploy_session(auth=_FakeCMLAuth())
    monkeypatch.setattr("cliq.labs.deploy.build_client_library",
                        lambda h, u, p: _FakeClient())
    res = _client().post("/api/labs/deploy/confirm",
                         json={"ticket_id": "ticket-nope"}, headers=headers)
    assert res.status_code == 404


def test_confirm_rejects_ssh_session_without_cml(deploy_session, monkeypatch):
    _, headers = deploy_session(auth=None)
    monkeypatch.setattr("cliq.labs.deploy.build_client_library",
                        lambda h, u, p: _FakeClient())
    res = _client().post("/api/labs/deploy/confirm",
                         json={"ticket_id": "ticket-x"}, headers=headers)
    assert res.status_code == 409


def test_confirm_requires_auth():
    res = _client().post("/api/labs/deploy/confirm",
                         json={"ticket_id": "ticket-x"})
    assert res.status_code == 401


def test_confirm_sdk_missing_is_501(deploy_session, monkeypatch):
    import sys

    _, headers = deploy_session(auth=_FakeCMLAuth())
    monkeypatch.setitem(sys.modules, "virl2_client", None)
    res = _client().post("/api/labs/deploy/confirm",
                         json={"ticket_id": "ticket-x"}, headers=headers)
    assert res.status_code == 501


# -----------------------------------------------------------------------------
# 3. Idempotency: no duplicate labs
# -----------------------------------------------------------------------------

def test_confirm_idempotent_same_key_single_lab(deploy_session, monkeypatch):
    _, headers = deploy_session(auth=_live_auth())
    client = _client()
    prep = client.post("/api/labs/deploy/prepare",
                       json={"blueprint": _valid_blueprint_dict(), "live": True},
                       headers=headers).json()

    calls: list = []
    fake_client = _FakeClient(lab=_FakeLab(calls=calls), calls=calls)
    monkeypatch.setattr("cliq.labs.deploy.build_client_library",
                        lambda h, u, p: fake_client)

    first = client.post("/api/labs/deploy/confirm",
                        json={"ticket_id": prep["ticket_id"],
                              "idempotency_key": "once-1"},
                        headers=headers)
    second = client.post("/api/labs/deploy/confirm",
                         json={"ticket_id": prep["ticket_id"],
                               "idempotency_key": "once-1"},
                         headers=headers)
    assert first.status_code == 202 and second.status_code == 202
    assert first.json()["job_id"] == second.json()["job_id"]
    assert second.json()["created"] is False
    _poll_terminal(client, headers, first.json()["job_id"])
    assert len([c for c in calls if c[0] == "create_lab"]) == 1


def test_confirm_consumed_ticket_replays_job(deploy_session, monkeypatch):
    _, headers = deploy_session(auth=_live_auth())
    client = _client()
    prep = client.post("/api/labs/deploy/prepare",
                       json={"blueprint": _valid_blueprint_dict(), "live": True},
                       headers=headers).json()
    monkeypatch.setattr("cliq.labs.deploy.build_client_library",
                        lambda h, u, p: _FakeClient())
    first = client.post("/api/labs/deploy/confirm",
                        json={"ticket_id": prep["ticket_id"]}, headers=headers)
    second = client.post("/api/labs/deploy/confirm",
                         json={"ticket_id": prep["ticket_id"]}, headers=headers)
    assert first.json()["job_id"] == second.json()["job_id"]
    assert second.json()["created"] is False


def test_deploy_refuses_duplicate_title(deploy_session, monkeypatch):
    _, headers = deploy_session(auth=_live_auth())
    client = _client()
    prep = client.post("/api/labs/deploy/prepare",
                       json={"blueprint": _valid_blueprint_dict(), "live": True},
                       headers=headers).json()

    calls: list = []
    # Ticket title is "Demo lab"; collide on it:
    fake_client = _FakeClient(existing_titles=["Demo lab"], calls=calls)
    monkeypatch.setattr("cliq.labs.deploy.build_client_library",
                        lambda h, u, p: fake_client)
    res = client.post("/api/labs/deploy/confirm",
                      json={"ticket_id": prep["ticket_id"]}, headers=headers)
    final = _poll_terminal(client, headers, res.json()["job_id"])
    assert final["status"] == "failed"
    assert "already exists" in final["error"]
    assert [c for c in calls if c[0] == "create_lab"] == []


# -----------------------------------------------------------------------------
# 4. Failure handling: halt, report, never retry uncertain writes
# -----------------------------------------------------------------------------

def test_deploy_halts_on_node_failure_no_retry(deploy_session, monkeypatch):
    _, headers = deploy_session(auth=_live_auth())
    client = _client()
    prep = client.post("/api/labs/deploy/prepare",
                       json={"blueprint": _valid_blueprint_dict(), "live": True},
                       headers=headers).json()

    calls: list = []
    fake_client = _FakeClient(lab=_FakeLab(fail_on={"node:R2"}, calls=calls),
                              calls=calls)
    monkeypatch.setattr("cliq.labs.deploy.build_client_library",
                        lambda h, u, p: fake_client)
    res = client.post("/api/labs/deploy/confirm",
                      json={"ticket_id": prep["ticket_id"]}, headers=headers)
    final = _poll_terminal(client, headers, res.json()["job_id"])
    assert final["status"] == "failed"
    assert "R2" in final["error"]
    # Exactly one attempt per node up to the failure; nothing after.
    node_calls = [c for c in calls if c[0] == "create_node"]
    assert [c[1] for c in node_calls] == ["R1", "R2"]
    assert [c for c in calls if c[0] == "create_link"] == []
    # Partial state is reported honestly: lab exists, R2 step failed.
    assert final["lab_id"] == "lab-123"
    failed_steps = [s for s in final["steps"] if s["status"] == "failed"]
    assert len(failed_steps) == 1 and "R2" in failed_steps[0]["label"]


def test_ticket_expiry_is_410(deploy_session, monkeypatch):
    from cliq.labs import deploy as _deploy

    _, headers = deploy_session(auth=_FakeCMLAuth())
    client = _client()
    prep = client.post("/api/labs/deploy/prepare",
                       json={"blueprint": _valid_blueprint_dict()},
                       headers=headers).json()
    monkeypatch.setattr("cliq.labs.deploy.build_client_library",
                        lambda h, u, p: _FakeClient())
    # Expire the ticket directly (white-box unit aspect).
    import time as _time
    auth_header = headers["Authorization"].split(None, 1)[1]
    _deploy._TICKETS[(auth_header, prep["ticket_id"])].expires_at = (
        _time.time() - 1)
    res = client.post("/api/labs/deploy/confirm",
                      json={"ticket_id": prep["ticket_id"]}, headers=headers)
    assert res.status_code == 410


# -----------------------------------------------------------------------------
# 5. Ownership + status polling + topology
# -----------------------------------------------------------------------------

def test_job_status_ownership_404(deploy_session, monkeypatch):
    token_a, headers_a = deploy_session(auth=_live_auth())
    _, headers_b = deploy_session(auth=_FakeCMLAuth())
    assert token_a
    client = _client()
    prep = client.post("/api/labs/deploy/prepare",
                       json={"blueprint": _valid_blueprint_dict(), "live": True},
                       headers=headers_a).json()
    monkeypatch.setattr("cliq.labs.deploy.build_client_library",
                        lambda h, u, p: _FakeClient())
    job_id = client.post("/api/labs/deploy/confirm",
                         json={"ticket_id": prep["ticket_id"]},
                         headers=headers_a).json()["job_id"]
    # Owner can poll; foreign session reads 404 (no leak).
    assert client.get(f"/api/labs/deploy/{job_id}",
                      headers=headers_a).status_code == 200
    assert client.get(f"/api/labs/deploy/{job_id}",
                      headers=headers_b).status_code == 404
    # Foreign session cannot confirm owner's ticket either.
    res = client.post("/api/labs/deploy/confirm",
                      json={"ticket_id": prep["ticket_id"]}, headers=headers_b)
    assert res.status_code == 404


def test_deploy_topology_reactflow_shape(deploy_session, monkeypatch):
    _, headers = deploy_session(auth=_live_auth(topology={
        "nodes": [{"id": "n1", "label": "R1", "type": "iosv",
                   "state": "DEFINED", "x": 10, "y": 20}],
        "links": [{"id": "l1", "label": "E0/0 <-> E0/0",
                   "node_a": "n1", "node_b": "n1",
                   "node_a_intf": "Ethernet0/0", "node_b_intf": "Ethernet0/1"}],
    }))
    client = _client()
    prep = client.post("/api/labs/deploy/prepare",
                       json={"blueprint": _valid_blueprint_dict(), "live": True},
                       headers=headers).json()
    monkeypatch.setattr("cliq.labs.deploy.build_client_library",
                        lambda h, u, p: _FakeClient())
    job_id = client.post("/api/labs/deploy/confirm",
                         json={"ticket_id": prep["ticket_id"]},
                         headers=headers).json()["job_id"]
    _poll_terminal(client, headers, job_id)
    res = client.get(f"/api/labs/deploy/{job_id}/topology", headers=headers)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["lab_id"] == "lab-123"
    assert body["nodes"][0]["position"] == {"x": 10, "y": 20}
    assert body["nodes"][0]["data"]["label"] == "R1"
    assert body["links"][0]["sourceHandle"] == "Ethernet0/0"


def test_deploy_topology_failed_job_is_409(deploy_session, monkeypatch):
    _, headers = deploy_session(auth=_live_auth())
    client = _client()
    prep = client.post("/api/labs/deploy/prepare",
                       json={"blueprint": _valid_blueprint_dict(), "live": True},
                       headers=headers).json()
    fake_client = _FakeClient(existing_titles=["Demo lab"])
    monkeypatch.setattr("cliq.labs.deploy.build_client_library",
                        lambda h, u, p: fake_client)
    job_id = client.post("/api/labs/deploy/confirm",
                         json={"ticket_id": prep["ticket_id"]},
                         headers=headers).json()["job_id"]
    _poll_terminal(client, headers, job_id)
    res = client.get(f"/api/labs/deploy/{job_id}/topology", headers=headers)
    assert res.status_code == 409


# -----------------------------------------------------------------------------
# 6. Static guard: deploy code has no destructive/configure paths
# -----------------------------------------------------------------------------

def test_deploy_sanctioned_sdk_surface():
    """deploy.py/routes.py: only the approved SDK surface may exist.

    Sanctioned: function-local ``virl2_client`` imports (availability probe +
    factory), one ``ClientLibrary(...)`` construction, and the verified calls
    ``create_lab`` / ``create_node`` / ``create_interface`` / ``create_link`` /
    ``find_labs_by_title`` / ``state`` / ``details``. Everything destructive
    or configuring is forbidden. (``worker.start()`` is a threading call,
    not a lab/node start.)
    """
    from pathlib import Path

    labs_dir = Path(__file__).resolve().parents[2] / "src" / "cliq" / "labs"
    deploy_text = (labs_dir / "deploy.py").read_text(encoding="utf-8")
    routes_text = (labs_dir / "routes.py").read_text(encoding="utf-8")

    # 1. SDK imports only inside functions (never module-top-level).
    for name, text in (("deploy.py", deploy_text), ("routes.py", routes_text)):
        for lineno, line in enumerate(text.splitlines(), 1):
            if "virl2_client" in line:
                assert line[:1] in (" ", "\t"), f"{name}:{lineno} top-level SDK use"

    # 2. Exactly one client construction, inside the factory.
    assert deploy_text.count("ClientLibrary(") == 1
    assert "ClientLibrary(" not in routes_text

    # 3. No destructive / configure / raw-HTTP paths in either file.
    # (Stop lives in lifecycle.py; here only docstring mentions may exist.)
    forbidden = (".wipe(", ".remove(", "remove_lab",
                 "_remove", ".delete(", "session.post", "session.put",
                 "session.delete", ".put(", "configuration=",
                 '"configuration"', "'configuration'",
                 "lab.start(", "node.start(", "link.start(", "client.start(",
                 "lab.stop(", "node.stop(", "link.stop(", "client.stop(")
    for name, text in (("deploy.py", deploy_text), ("routes.py", routes_text)):
        for lineno, line in enumerate(text.splitlines(), 1):
            if line.strip().startswith(("@router.post", "@router.get")):
                continue
            for token in forbidden:
                assert token not in line, f"{name}:{lineno} has {token!r}"
            # Docstring literals use double backticks; only bare code counts.
            if ".start(" in line and "``" not in line:
                assert "worker.start()" in line, f"{name}:{lineno} has .start("
            if ".stop(" in line:
                assert "``" in line, f"{name}:{lineno} has bare .stop("

    # 4. The sanctioned create calls exist (allowlist present, spelled right).
    for call in ("create_lab(", "create_node(", "create_interface(",
                 "create_link(", "find_labs_by_title(", ".state()"):
        assert call in deploy_text, f"deploy.py missing sanctioned {call}"


# -----------------------------------------------------------------------------
# 8. Lab-title normalization + validation
# -----------------------------------------------------------------------------

#: Exact title from the reported 422 (en dash U+2013 + ampersand).
REPORTED_TITLE = (
    "Enterprise Campus Network \u2013 VLAN Segmentation & Redundancy"
)


def test_normalize_lab_title_accepts_reported_title():
    from cliq.labs.deploy import normalize_lab_title

    assert normalize_lab_title(REPORTED_TITLE) == \
        "Enterprise Campus Network - VLAN Segmentation & Redundancy"


def test_normalize_lab_title_accepts_plain_names():
    from cliq.labs.deploy import normalize_lab_title

    assert normalize_lab_title("Enterprise Campus Network") == \
        "Enterprise Campus Network"
    assert normalize_lab_title("Enterprise-Campus-VLAN-Redundancy") == \
        "Enterprise-Campus-VLAN-Redundancy"
    assert normalize_lab_title("R&D Lab (v2), core: up!") == \
        "R&D Lab (v2), core: up!"


def test_normalize_lab_title_collapses_whitespace_and_dashes():
    from cliq.labs.deploy import normalize_lab_title

    assert normalize_lab_title("  Core  \u2014  dist  ") == "Core - dist"
    assert normalize_lab_title("a\u2014b") == "a-b"  # em dash
    assert normalize_lab_title("a\u2012b") == "a-b"  # figure dash
    assert normalize_lab_title("a\tb\nc") == "a b c"  # tab/newline collapse
    assert normalize_lab_title("x" * 128) == "x" * 128  # boundary ok


def test_normalize_lab_title_rejects_unsafe_input():
    from cliq.labs.deploy import DeploymentError, normalize_lab_title

    bad_titles = ["", "   ", "x" * 129, "../evil", "a/b", "a\\b", "v1..2",
                  "lab\x00x", "lab\x1fx", "a\u200bb", ".hidden", "-abc",
                  "&co", 'quo"te', "star*", "what?"]
    for bad in bad_titles:
        with pytest.raises(DeploymentError, match="invalid lab title"):
            normalize_lab_title(bad)
    with pytest.raises(DeploymentError, match="must be a string"):
        normalize_lab_title(None)  # type: ignore[arg-type]


def test_prepare_accepts_reported_title_and_reports_original(deploy_session):
    _, headers = deploy_session(auth=_FakeCMLAuth())
    bp = _valid_blueprint_dict()
    bp["title"] = REPORTED_TITLE
    res = _client().post("/api/labs/deploy/prepare",
                         json={"blueprint": bp}, headers=headers)
    assert res.status_code == 200, res.text
    ticket = res.json()
    assert ticket["lab_title"] == \
        "Enterprise Campus Network - VLAN Segmentation & Redundancy"
    assert ticket["lab_title_original"] == REPORTED_TITLE


def test_prepare_rejects_empty_and_long_titles(deploy_session):
    _, headers = deploy_session(auth=_FakeCMLAuth())
    for bad in ("   ", "y" * 129):
        res = _client().post("/api/labs/deploy/prepare",
                             json={"blueprint": _valid_blueprint_dict(),
                                   "lab_title": bad},
                             headers=headers)
        assert res.status_code == 422, bad


def test_confirm_override_is_normalized(deploy_session, monkeypatch):
    _, headers = deploy_session(auth=_live_auth())
    client = _client()
    prep = client.post("/api/labs/deploy/prepare",
                       json={"blueprint": _valid_blueprint_dict(), "live": True},
                       headers=headers).json()
    calls: list = []
    fake_client = _FakeClient(calls=calls)
    monkeypatch.setattr("cliq.labs.deploy.build_client_library",
                        lambda h, u, p: fake_client)
    res = client.post("/api/labs/deploy/confirm",
                      json={"ticket_id": prep["ticket_id"],
                            "lab_title": "R&D \u2013 Core"},
                      headers=headers)
    assert res.status_code == 202, res.text
    assert res.json()["lab_title"] == "R&D - Core"
    final = _poll_terminal(client, headers, res.json()["job_id"])
    assert final["status"] == "succeeded", final
    create_calls = [c for c in calls if c[0] == "create_lab"]
    assert len(create_calls) == 1
    assert create_calls[0][1] == "R&D - Core"


def test_worker_refuses_invalid_title_without_creating(deploy_session):
    from cliq.labs import deploy as deploy_mod
    from cliq.labs.models import LabBlueprint

    token, _ = deploy_session(auth=_FakeCMLAuth())
    ticket = prepare_deployment(
        token, LabBlueprint.model_validate(_valid_blueprint_dict()))
    job_id = "job-tamper-test"
    job = deploy_mod.DeployJob(job_id=job_id, ticket_id=ticket.ticket_id,
                               lab_title="x", idempotency_key=None,
                               blueprint_hash=ticket.digest)
    with deploy_mod._LOCK:
        deploy_mod._JOBS[(token, job_id)] = job
    factory_calls: list = []
    try:
        deploy_mod._run_job(token, job_id, ticket, "../evil",
                            lambda h, u, p: factory_calls.append(1),
                            "h", "u", "p")
        final = deploy_mod.get_job(token, job_id)
        assert final is not None and final.status == "failed"
        assert "refusing to deploy" in (final.error or "")
        assert factory_calls == []  # no client built, nothing created
    finally:
        with deploy_mod._LOCK:
            deploy_mod._JOBS.pop((token, job_id), None)
