"""
Tests for CLIQ M1.1 Part B — lab start lifecycle (mocked, no live CML).

Covers: available-labs listing, state display mapping, start request flow,
polling transitions, duplicate suppression, failure mapping, ownership, and
the static sanctioned-surface guard for lifecycle.py.
"""

import time
import uuid

import pytest

from cliq.labs.lifecycle import display_state


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _Script(list):
    """Per-call response script: popped once per GET, in order.

    A bare ``list`` is a GENUINE controller payload (``GET /labs`` and
    ``GET .../nodes`` return id lists) and is returned whole; only
    ``_Script`` values pop. Conflating the two silently turns every id
    list into a single string.
    """


class _FakeCMLAuth:
    """Scripted CMLAuthManager stand-in. ``_Script`` values pop per call;
    every other value (including bare lists) is returned whole."""

    def __init__(self, routes=None, accessible=True, token="SECRET-TOKEN"):
        self.routes = routes or {}
        self.accessible = accessible
        self.token = token
        self.calls: list[str] = []

    def _get(self, path):
        self.calls.append(path)
        if path not in self.routes:
            raise RuntimeError(f"unexpected CML GET {path}")
        outcome = self.routes[path]
        if isinstance(outcome, _Script):
            if not outcome:
                raise RuntimeError(f"route {path} exhausted")
            outcome = outcome.pop(0)
        return _FakeResp(outcome)

    def is_lab_accessible(self, lab_id):
        self.calls.append(f"ACCESS:{lab_id}")
        return self.accessible


class _FakeStartLab:
    def __init__(self, calls, fail_with=None, fail_stop_with=None):
        self.calls = calls
        self.fail_with = fail_with
        self.fail_stop_with = fail_stop_with

    def __getattr__(self, name):
        if name in ("wipe", "remove", "delete", "configuration"):
            raise AssertionError(f"forbidden SDK call: {name}")
        raise AttributeError(name)

    def start(self, wait=False):
        assert wait is False, "start must never block on convergence"
        self.calls.append(("lab.start",))
        if self.fail_with is not None:
            raise self.fail_with

    def stop(self, wait=False):
        assert wait is False, "stop must never block on convergence"
        self.calls.append(("lab.stop",))
        if self.fail_stop_with is not None:
            raise self.fail_stop_with


class _FakeStartClient:
    def __init__(self, lab=None, calls=None):
        self._lab = lab
        self.calls = calls if calls is not None else []

    def __getattr__(self, name):
        if name in ("stop", "wipe", "remove", "delete", "remove_lab"):
            raise AssertionError(f"forbidden SDK call: {name}")
        raise AttributeError(name)

    def join_existing_lab(self, lab_id, sync_lab=True):
        assert sync_lab is False
        self.calls.append(("join_existing_lab", lab_id))
        if self._lab is None:
            raise RuntimeError("lab gone")
        return self._lab


@pytest.fixture
def cml_session():
    from cliq.web.server import SESSIONS, AuthSession

    created: list[str] = []

    def _make(auth=None):
        token = f"lc-test-{uuid.uuid4().hex}"
        SESSIONS[token] = AuthSession(
            host="cml.test", username="u", password="p", auth=auth)
        created.append(token)
        return token, {"Authorization": f"Bearer {token}"}

    yield _make
    for token in created:
        SESSIONS.pop(token, None)


@pytest.fixture
def ssh_only_session():
    from cliq.web.server import SESSIONS, AuthSession

    token = f"lc-ssh-{uuid.uuid4().hex}"
    SESSIONS[token] = AuthSession(
        host="term.test", username="u", password="p",
        auth=None, connection_type="ssh")
    yield token, {"Authorization": f"Bearer {token}"}
    SESSIONS.pop(token, None)


def _client():
    from fastapi.testclient import TestClient
    from cliq.web.server import app
    return TestClient(app)


def _labs_routes(extra=None):
    routes = {
        "/labs": ["lab-1", "lab-2"],
        "/labs/lab-1": {"lab_title": "Backbone", "state": "DEFINED",
                        "owner_username": "u"},
        "/labs/lab-2": {"lab_title": "Running lab", "state": "STARTED",
                        "owner_username": "u"},
        "/labs/lab-1/nodes": ["n1", "n2", "n3"],
        "/labs/lab-2/nodes": ["n1"],
    }
    if extra:
        routes.update(extra)
    return routes


# -----------------------------------------------------------------------------
# 1. Available-labs listing
# -----------------------------------------------------------------------------

def test_available_requires_auth():
    assert _client().get("/api/labs/available").status_code == 401


def test_available_rejects_ssh_session(ssh_only_session):
    _, headers = ssh_only_session
    assert _client().get("/api/labs/available", headers=headers).status_code == 409


def test_available_shape_and_counts(cml_session):
    _, headers = cml_session(auth=_FakeCMLAuth(_labs_routes()))
    res = _client().get("/api/labs/available", headers=headers)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["count"] == 2
    by_id = {lab["id"]: lab for lab in body["labs"]}
    assert by_id["lab-1"]["title"] == "Backbone"
    assert by_id["lab-1"]["state"] == "DEFINED"
    assert by_id["lab-1"]["state_display"] == "Defined"
    assert by_id["lab-1"]["device_count"] == 3
    assert by_id["lab-2"]["state_display"] == "Running"
    assert by_id["lab-2"]["device_count"] == 1


def test_available_count_none_when_nodes_unreadable(cml_session):
    _, headers = cml_session(auth=_FakeCMLAuth(_labs_routes(
        {"/labs/lab-1/nodes": RuntimeError("nope")})))
    res = _client().get("/api/labs/available", headers=headers)
    assert res.status_code == 200, res.text
    by_id = {lab["id"]: lab for lab in res.json()["labs"]}
    assert by_id["lab-1"]["device_count"] is None


def test_available_marks_unreadable_lab_unknown(cml_session):
    """Unreadable labs are NEVER dropped or shown Defined: they stay on the
    dashboard as Unknown with the controller error preserved."""
    _, headers = cml_session(auth=_FakeCMLAuth(_labs_routes(
        {"/labs/lab-2": RuntimeError("gone")})))
    res = _client().get("/api/labs/available", headers=headers)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["count"] == 2
    by_id = {lab["id"]: lab for lab in body["labs"]}
    assert by_id["lab-2"]["state_display"] == "Unknown"
    assert by_id["lab-2"]["state"] == ""
    assert "gone" in by_id["lab-2"]["error"]
    assert by_id["lab-1"]["state_display"] == "Defined"


def test_available_running_via_node_evidence(cml_session):
    """Dashboard Running from converged + all-active even when /state
    stays DEFINED_ON_CORE (live-controller rule)."""
    routes = _labs_routes({
        "/labs/lab-1/check_if_converged": True,
        "/labs/lab-1/lab_element_state": _elements("BOOTED", "BOOTED", "BOOTED"),
        "/labs/lab-2/check_if_converged": True,
        "/labs/lab-2/lab_element_state": _elements("DEFINED_ON_CORE"),
    })
    # lab-1 details say DEFINED in the base fixture: evidence overrides.
    routes["/labs/lab-1"] = {"lab_title": "Backbone",
                             "state": "DEFINED_ON_CORE",
                             "owner_username": "u"}
    # lab-2's base details say STARTED, which would force Running on its
    # own; the intent here is "converged but zero active stays Defined".
    routes["/labs/lab-2"] = {"lab_title": "Running lab",
                             "state": "DEFINED_ON_CORE",
                             "owner_username": "u"}
    _, headers = cml_session(auth=_FakeCMLAuth(routes))
    res = _client().get("/api/labs/available", headers=headers)
    assert res.status_code == 200, res.text
    by_id = {lab["id"]: lab for lab in res.json()["labs"]}
    assert by_id["lab-1"]["state_display"] == "Running"
    assert by_id["lab-1"]["nodes_booted"] == 3
    assert by_id["lab-1"]["nodes_active"] == 3
    # lab-2: converged but zero active stays Defined (no false Running).
    assert by_id["lab-2"]["state_display"] == "Defined"


def test_available_completed_op_does_not_override(cml_session, monkeypatch):
    """A completed start op never shadows fresh reads on the dashboard."""
    _, headers = cml_session(auth=_FakeCMLAuth({
        **_labs_routes(),
        "/labs/lab-1": {"lab_title": "Backbone", "state": "DEFINED_ON_CORE",
                        "owner_username": "u"},
        "/labs/lab-1/state": _Script(["DEFINED_ON_CORE", "DEFINED_ON_CORE"]),
        "/labs/lab-1/check_if_converged": _Script([False, True]),
        "/labs/lab-1/lab_element_state": _Script([
            _elements("DEFINED_ON_CORE"), _elements("BOOTED")]),
    }))
    monkeypatch.setattr("cliq.labs.deploy.build_client_library",
                        lambda h, u, p: _FakeStartClient(lab=_FakeStartLab([])))
    client = _client()
    op_id = client.post("/api/labs/lab-1/start", headers=headers).json()["op_id"]
    final = client.get(f"/api/labs/start/{op_id}", headers=headers).json()
    assert final["status"] == "running"
    # Fresh dashboard reads govern: scripts are exhausted so element data is
    # None here, and the completed op must not force Running.
    res = client.get("/api/labs/available", headers=headers)
    assert res.status_code == 200, res.text
    by_id = {lab["id"]: lab for lab in res.json()["labs"]}
    assert by_id["lab-1"]["state_display"] == "Defined"


def test_available_controller_failure_is_502_without_secrets(cml_session):
    _, headers = cml_session(auth=_FakeCMLAuth(
        {"/labs": ConnectionError("down")}))
    res = _client().get("/api/labs/available", headers=headers)
    assert res.status_code == 502
    assert "SECRET-TOKEN" not in res.text


# -----------------------------------------------------------------------------
# 2. State display mapping (honest by construction)
# -----------------------------------------------------------------------------

def test_display_state_mapping():
    assert display_state("STARTED") == "Running"
    assert display_state("started") == "Running"
    assert display_state("STARTED", "failed") == "Running"  # live wins
    assert display_state("DEFINED") == "Defined"
    assert display_state("STOPPED") == "Defined"  # stopped, raw preserved
    assert display_state("DEFINED", "starting") == "Starting"
    assert display_state("STARTED", "starting") == "Starting"  # in-flight wins
    assert display_state("STARTED", "stopping") == "Stopping"  # still stopping
    assert display_state("DEFINED", "stopping") == "Stopping"
    assert display_state("DEFINED", "stopped") == "Defined"
    assert display_state("DEFINED", "failed") == "Failed"
    assert display_state("") == "Unknown"
    assert display_state("   ") == "Unknown"
    # Node evidence (live-controller rule): converged + EVERY node active
    # reads Running even when /state stays DEFINED_ON_CORE. Convergence
    # alone, partial activity, and missing element data never read Running.
    assert display_state("DEFINED_ON_CORE", None, converged=True,
                         booted=2, active=2, total=2) == "Running"
    assert display_state("DEFINED_ON_CORE", None, converged=True,
                         booted=2, active=1, total=2) == "Defined"
    assert display_state("DEFINED_ON_CORE", None, converged=True,
                         booted=0, active=0, total=2) == "Defined"
    assert display_state("DEFINED_ON_CORE", None, converged=True,
                         booted=2, total=2) == "Defined"
    assert display_state("DEFINED_ON_CORE", None, converged=False,
                         booted=2, active=2, total=2) == "Defined"
    # Partial boot is Running (lab-level) but not fully ready: booted <
    # total is preserved alongside, never merged into the badge.
    assert display_state("STARTED", None, converged=False,
                         booted=3, active=9, total=10) == "Running"
    assert display_state("DEFINED_ON_CORE", "running") == "Running"


# -----------------------------------------------------------------------------
# 3. Start flow (mocked SDK)
# -----------------------------------------------------------------------------

def _elements(*node_states):
    return {"nodes": {f"n{i}": state for i, state in enumerate(node_states)}}


def _start_routes(state_script, converged_script=None, element_script=None):
    return {
        "/labs/lab-9": {"lab_title": "Demo9", "state": "DEFINED"},
        "/labs/lab-9/state": _Script(state_script),
        "/labs/lab-9/check_if_converged": (
            _Script(converged_script) if converged_script is not None
            else False),
        "/labs/lab-9/lab_element_state": (
            _Script(element_script) if element_script is not None
            else _elements()),
    }


def test_start_requires_auth():
    assert _client().post("/api/labs/lab-9/start").status_code == 401


def test_start_rejects_ssh_session(ssh_only_session):
    _, headers = ssh_only_session
    res = _client().post("/api/labs/lab-9/start", headers=headers)
    assert res.status_code == 409


def test_start_rejects_bad_lab_id(cml_session):
    _, headers = cml_session(auth=_FakeCMLAuth({}))
    client = _client()
    res = client.post("/api/labs/a%3Bb/start", headers=headers)
    assert res.status_code == 422
    # Path traversal never reaches the controller. The HTTP client
    # normalizes "/api/labs/../evil/start" to "/api/evil/start" before it
    # is sent, so no API route matches: Starlette answers 405 via the
    # GET-only SPA catch-all path (405 fails closed — nothing is served
    # and no controller call is made). 404/422 are accepted too since
    # normalization behavior varies by client/framework version.
    res = client.post("/api/labs/../evil/start", headers=headers)
    assert res.status_code in (404, 405, 422)


def test_validate_lab_id_rejects_traversal_direct():
    """Validator-level pin (socket-free): whatever reaches the router is
    rejected before any controller path is built."""
    from cliq.labs.discovery import DiscoveryError, validate_lab_id

    for bad in ("../evil", "..\\evil", "a/b", "a\\b", "..", "", "  ",
                "a;b", "lab id", ".", "v1..2"):
        try:
            validate_lab_id(bad)
        except DiscoveryError:
            pass
        else:
            raise AssertionError(f"validate_lab_id accepted {bad!r}")
    assert validate_lab_id("4be558aa-12bb-4e5f-a85e-6283016d1718")


def test_start_sdk_missing_is_501(cml_session, monkeypatch):
    import sys

    _, headers = cml_session(auth=_FakeCMLAuth(_start_routes(["DEFINED"])))
    monkeypatch.setitem(sys.modules, "virl2_client", None)
    res = _client().post("/api/labs/lab-9/start", headers=headers)
    assert res.status_code == 501


def test_start_lab_not_accessible_is_404(cml_session, monkeypatch):
    _, headers = cml_session(auth=_FakeCMLAuth(
        _start_routes(["DEFINED"]), accessible=False))
    monkeypatch.setattr("cliq.labs.deploy.build_client_library",
                        lambda h, u, p: _FakeStartClient())
    res = _client().post("/api/labs/lab-9/start", headers=headers)
    assert res.status_code == 404


def test_start_already_running_skips_put(cml_session, monkeypatch):
    _, headers = cml_session(auth=_FakeCMLAuth(_start_routes(["STARTED"])))
    calls: list = []

    def _factory(h, u, p):
        calls.append("factory")
        return _FakeStartClient()

    monkeypatch.setattr("cliq.labs.deploy.build_client_library", _factory)
    res = _client().post("/api/labs/lab-9/start", headers=headers)
    assert res.status_code == 202, res.text
    body = res.json()
    assert body["status"] == "running"
    assert body["state_display"] == "Running"
    assert calls == []  # no SDK client, no PUT


def test_start_success_then_poll_to_running(cml_session, monkeypatch):
    """Live-controller regression: /state stays DEFINED_ON_CORE while the
    lab boots (CML dashboard green) — completion comes from convergence."""
    _, headers = cml_session(auth=_FakeCMLAuth(_start_routes(
        ["DEFINED_ON_CORE"] * 3,
        converged_script=[False, False, True],
        element_script=[
            _elements("DEFINED_ON_CORE", "DEFINED_ON_CORE"),
            _elements("STARTED", "QUEUED"),
            _elements("BOOTED", "BOOTED"),
        ],
    )))
    calls: list = []
    lab = _FakeStartLab(calls)
    monkeypatch.setattr("cliq.labs.deploy.build_client_library",
                        lambda h, u, p: _FakeStartClient(lab=lab, calls=calls))
    client = _client()
    res = client.post("/api/labs/lab-9/start", headers=headers)
    assert res.status_code == 202, res.text
    op_id = res.json()["op_id"]
    assert res.json()["status"] == "starting"

    first = client.get(f"/api/labs/start/{op_id}", headers=headers).json()
    assert first["status"] == "starting"
    assert first["state_display"] == "Starting"
    assert first["nodes_booted"] == 0
    assert first["nodes_total"] == 2
    second = client.get(f"/api/labs/start/{op_id}", headers=headers).json()
    assert second["status"] == "running"
    assert second["state_display"] == "Running"
    assert second["raw_state"] == "DEFINED_ON_CORE"
    assert second["converged"] is True
    assert second["nodes_booted"] == 2
    assert [c for c in calls if c == ("lab.start",)] == [("lab.start",)]


def test_start_completes_on_started_state(cml_session, monkeypatch):
    """The is_active corroboration still completes even if convergence
    never reports (either controller signal suffices)."""
    _, headers = cml_session(auth=_FakeCMLAuth(_start_routes(
        ["DEFINED", "STARTED"], converged_script=[False, False])))
    monkeypatch.setattr("cliq.labs.deploy.build_client_library",
                        lambda h, u, p: _FakeStartClient(lab=_FakeStartLab([])))
    client = _client()
    op_id = client.post("/api/labs/lab-9/start", headers=headers).json()["op_id"]
    final = client.get(f"/api/labs/start/{op_id}", headers=headers).json()
    assert final["status"] == "running"
    assert final["raw_state"] == "STARTED"


def test_start_skips_when_node_evidence_running(cml_session, monkeypatch):
    """DEFINED_ON_CORE + converged + all booted pre-read: no PUT."""
    _, headers = cml_session(auth=_FakeCMLAuth(_start_routes(
        ["DEFINED_ON_CORE"], converged_script=[True],
        element_script=[_elements("BOOTED", "BOOTED")])))
    calls: list = []

    def _factory(h, u, p):
        calls.append("factory")
        return _FakeStartClient()

    monkeypatch.setattr("cliq.labs.deploy.build_client_library", _factory)
    res = _client().post("/api/labs/lab-9/start", headers=headers)
    assert res.status_code == 202, res.text
    assert res.json()["status"] == "running"
    assert calls == []


def test_start_proceeds_when_defined_but_unconverged(cml_session, monkeypatch):
    """DEFINED + converged + zero booted (stable defined lab) is NOT
    running evidence: the PUT must still be issued."""
    _, headers = cml_session(auth=_FakeCMLAuth(_start_routes(
        ["DEFINED", "STARTED"], converged_script=[True, True],
        element_script=[_elements("DEFINED_ON_CORE"), _elements("BOOTED")])))
    calls: list = []
    lab = _FakeStartLab(calls)
    monkeypatch.setattr("cliq.labs.deploy.build_client_library",
                        lambda h, u, p: _FakeStartClient(lab=lab, calls=calls))
    client = _client()
    res = client.post("/api/labs/lab-9/start", headers=headers)
    assert res.json()["status"] == "starting", res.json()
    assert [c for c in calls if c == ("lab.start",)] == [("lab.start",)]


def test_start_duplicate_returns_existing_single_put(cml_session, monkeypatch):
    _, headers = cml_session(auth=_FakeCMLAuth(
        _start_routes(["DEFINED", "DEFINED"])))
    calls: list = []
    lab = _FakeStartLab(calls)
    monkeypatch.setattr("cliq.labs.deploy.build_client_library",
                        lambda h, u, p: _FakeStartClient(lab=lab, calls=calls))
    client = _client()
    first = client.post("/api/labs/lab-9/start", headers=headers).json()
    second = client.post("/api/labs/lab-9/start", headers=headers).json()
    assert first["op_id"] == second["op_id"]
    assert second["created"] is False
    assert [c for c in calls if c == ("lab.start",)] == [("lab.start",)]


def test_start_failure_surfaces_controller_message(cml_session, monkeypatch):
    _, headers = cml_session(auth=_FakeCMLAuth(_start_routes(["DEFINED"])))
    calls: list = []
    lab = _FakeStartLab(calls, fail_with=RuntimeError(
        "Client error - insufficient resources on compute"))
    monkeypatch.setattr("cliq.labs.deploy.build_client_library",
                        lambda h, u, p: _FakeStartClient(lab=lab, calls=calls))
    res = _client().post("/api/labs/lab-9/start", headers=headers)
    assert res.status_code == 202, res.text
    body = res.json()
    assert body["status"] == "failed"
    assert body["state_display"] == "Failed"
    assert "insufficient resources" in body["error"]
    assert [c for c in calls if c == ("lab.start",)] == [("lab.start",)]


def test_start_poll_transport_blip_rides_through(cml_session, monkeypatch):
    _, headers = cml_session(auth=_FakeCMLAuth(_start_routes(
        ["DEFINED", ConnectionError("blip"), "STARTED"])))
    monkeypatch.setattr("cliq.labs.deploy.build_client_library",
                        lambda h, u, p: _FakeStartClient(lab=_FakeStartLab([])))
    client = _client()
    op_id = client.post("/api/labs/lab-9/start", headers=headers).json()["op_id"]
    mid = client.get(f"/api/labs/start/{op_id}", headers=headers).json()
    assert mid["status"] == "starting"  # blip does not fail the op
    final = client.get(f"/api/labs/start/{op_id}", headers=headers).json()
    assert final["status"] == "running"


def test_start_timeout_fails(cml_session, monkeypatch):
    from cliq.labs import lifecycle as _lc

    _, headers = cml_session(auth=_FakeCMLAuth(_start_routes(["DEFINED"] * 4)))
    monkeypatch.setattr("cliq.labs.deploy.build_client_library",
                        lambda h, u, p: _FakeStartClient(lab=_FakeStartLab([])))
    client = _client()
    op_id = client.post("/api/labs/lab-9/start", headers=headers).json()["op_id"]
    auth_header = headers["Authorization"].split(None, 1)[1]
    _lc._OPS[(auth_header, op_id)].created_at = time.time() - 9999
    res = client.get(f"/api/labs/start/{op_id}", headers=headers).json()
    assert res["status"] == "failed"
    assert "timed out" in res["error"]


def test_start_ownership_and_unknown_op(cml_session, monkeypatch):
    token_a, headers_a = cml_session(auth=_FakeCMLAuth(
        _start_routes(["DEFINED"] * 3)))
    _, headers_b = cml_session(auth=_FakeCMLAuth(_start_routes(["DEFINED"])))
    assert token_a
    monkeypatch.setattr("cliq.labs.deploy.build_client_library",
                        lambda h, u, p: _FakeStartClient(lab=_FakeStartLab([])))
    client = _client()
    op_id = client.post("/api/labs/lab-9/start", headers=headers_a).json()["op_id"]
    assert client.get(f"/api/labs/start/{op_id}",
                      headers=headers_a).status_code == 200
    assert client.get(f"/api/labs/start/{op_id}",
                      headers=headers_b).status_code == 404
    assert client.get("/api/labs/start/op-nope",
                      headers=headers_a).status_code == 404
    assert client.get("/api/labs/start/..",
                      headers=headers_a).status_code in (404, 422)


# -----------------------------------------------------------------------------
# 4. Static guard: lifecycle.py sanctioned surface
# -----------------------------------------------------------------------------

# -----------------------------------------------------------------------------
# 5. Stop flow (mocked SDK)
# -----------------------------------------------------------------------------

def test_stop_success_then_poll_to_stopped(cml_session, monkeypatch):
    _, headers = cml_session(auth=_FakeCMLAuth(_start_routes(
        ["STARTED", "STARTED", "DEFINED"],
        converged_script=[False, False, True])))
    calls: list = []
    lab = _FakeStartLab(calls)
    monkeypatch.setattr("cliq.labs.deploy.build_client_library",
                        lambda h, u, p: _FakeStartClient(lab=lab, calls=calls))
    client = _client()
    res = client.post("/api/labs/lab-9/stop", headers=headers)
    assert res.status_code == 202, res.text
    assert res.json()["status"] == "stopping"
    assert res.json()["kind"] == "stop"
    op_id = res.json()["op_id"]
    mid = client.get(f"/api/labs/start/{op_id}", headers=headers).json()
    assert mid["status"] == "stopping"
    assert mid["state_display"] == "Stopping"
    final = client.get(f"/api/labs/start/{op_id}", headers=headers).json()
    assert final["status"] == "stopped"
    assert final["state_display"] == "Defined"
    assert final["raw_state"] == "DEFINED"
    assert [c for c in calls if c == ("lab.stop",)] == [("lab.stop",)]
    assert [c for c in calls if c == ("lab.start",)] == []


def test_stop_already_stopped_skips_put(cml_session, monkeypatch):
    _, headers = cml_session(auth=_FakeCMLAuth(_start_routes(["DEFINED"])))
    calls: list = []

    def _factory(h, u, p):
        calls.append("factory")
        return _FakeStartClient()

    monkeypatch.setattr("cliq.labs.deploy.build_client_library", _factory)
    res = _client().post("/api/labs/lab-9/stop", headers=headers)
    assert res.status_code == 202, res.text
    assert res.json()["status"] == "stopped"
    assert calls == []


def test_stop_duplicate_returns_existing_single_put(cml_session, monkeypatch):
    _, headers = cml_session(auth=_FakeCMLAuth(
        _start_routes(["STARTED", "STARTED"])))
    calls: list = []
    lab = _FakeStartLab(calls)
    monkeypatch.setattr("cliq.labs.deploy.build_client_library",
                        lambda h, u, p: _FakeStartClient(lab=lab, calls=calls))
    client = _client()
    first = client.post("/api/labs/lab-9/stop", headers=headers).json()
    second = client.post("/api/labs/lab-9/stop", headers=headers).json()
    assert first["op_id"] == second["op_id"]
    assert second["created"] is False
    assert [c for c in calls if c == ("lab.stop",)] == [("lab.stop",)]


def test_stop_failure_surfaces_message(cml_session, monkeypatch):
    _, headers = cml_session(auth=_FakeCMLAuth(_start_routes(["STARTED"])))
    calls: list = []
    lab = _FakeStartLab(calls, fail_stop_with=RuntimeError("controller busy"))
    monkeypatch.setattr("cliq.labs.deploy.build_client_library",
                        lambda h, u, p: _FakeStartClient(lab=lab, calls=calls))
    res = _client().post("/api/labs/lab-9/stop", headers=headers)
    assert res.status_code == 202, res.text
    assert res.json()["status"] == "failed"
    assert "controller busy" in res.json()["error"]
    assert [c for c in calls if c == ("lab.stop",)] == [("lab.stop",)]


def test_start_stop_overlap_is_409(cml_session, monkeypatch):
    _, headers = cml_session(auth=_FakeCMLAuth(
        _start_routes(["DEFINED", "DEFINED", "STARTED", "STARTED"])))
    calls: list = []
    lab = _FakeStartLab(calls)
    monkeypatch.setattr("cliq.labs.deploy.build_client_library",
                        lambda h, u, p: _FakeStartClient(lab=lab, calls=calls))
    client = _client()
    started = client.post("/api/labs/lab-9/start", headers=headers)
    assert started.status_code == 202
    refused = client.post("/api/labs/lab-9/stop", headers=headers)
    assert refused.status_code == 409
    assert "already in progress" in refused.json()["detail"]
    assert [c for c in calls if c == ("lab.stop",)] == []


def test_stop_requires_auth_and_session():
    client = _client()
    assert client.post("/api/labs/lab-9/stop").status_code == 401


def test_stop_rejects_ssh_session(ssh_only_session):
    _, headers = ssh_only_session
    res = _client().post("/api/labs/lab-9/stop", headers=headers)
    assert res.status_code == 409


# -----------------------------------------------------------------------------
# 6. Session preservation + 404 shape contract (Part B reference)
# -----------------------------------------------------------------------------

def test_lifecycle_preserves_session(cml_session, monkeypatch):
    from cliq.web.server import SESSIONS

    token, headers = cml_session(auth=_FakeCMLAuth(
        {**_labs_routes(), **_start_routes(["DEFINED", "STARTED"])}))
    monkeypatch.setattr("cliq.labs.deploy.build_client_library",
                        lambda h, u, p: _FakeStartClient(lab=_FakeStartLab([])))
    before = SESSIONS[token]
    client = _client()
    assert client.get("/api/labs/available", headers=headers).status_code == 200
    op_id = client.post("/api/labs/lab-9/start", headers=headers).json()["op_id"]
    client.get(f"/api/labs/start/{op_id}", headers=headers)
    after = SESSIONS.get(token)
    assert after is before
    assert after.auth is before.auth


def test_own_404s_carry_explicit_detail():
    """All labs 404s carry explicit details — a bare "Not Found" can only
    come from Starlette itself (stale backend predating the called route).
    The dashboard uses this rule to explain the red Not Found banner."""
    from fastapi.testclient import TestClient
    from cliq.web.server import SESSIONS, AuthSession, app

    token = "lc-404-shape"
    SESSIONS[token] = AuthSession(host="h", username="u", password="p",
                                  auth=_FakeCMLAuth({}))
    try:
        res = TestClient(app).get(
            "/api/labs/start/op-nope",
            headers={"Authorization": f"Bearer {token}"})
    finally:
        SESSIONS.pop(token, None)
    assert res.status_code == 404
    assert res.json() == {"detail": "start operation not found"}


def test_lifecycle_sanctioned_surface():
    from pathlib import Path

    text = (Path(__file__).resolve().parents[2] / "src" / "cliq" / "labs"
            / "lifecycle.py").read_text(encoding="utf-8")
    # No SDK imports or client construction here (factory is injected).
    assert "virl2_client" not in text
    assert "ClientLibrary(" not in text
    # Only the verified start/stop calls + join may exist.
    assert "lab.start(wait=False)" in text
    assert "lab.stop(wait=False)" in text
    assert "join_existing_lab(" in text
    forbidden = (".wipe(", ".remove(", "remove_lab",
                 "_remove", ".delete(", "session.post", "session.put",
                 "session.delete", ".put(", "configuration=",
                 '"configuration"', "'configuration'")
    for lineno, line in enumerate(text.splitlines(), 1):
        for token in forbidden:
            assert token not in line, f"lifecycle.py:{lineno} has {token!r}"
        # Docstring literals use double backticks; only bare code counts.
        if ".start(" in line and "``" not in line:
            assert "lab.start(wait=False)" in line, \
                f"lifecycle.py:{lineno} unsanctioned .start("
        if ".stop(" in line and "``" not in line:
            assert "lab.stop(wait=False)" in line, \
                f"lifecycle.py:{lineno} unsanctioned .stop("


# -----------------------------------------------------------------------------
# Live-probe regression (socket-free: direct calls, no TestClient)
#
# Shapes below are verbatim from the live read-only probe of
# Enterprise-VLAN-Lab (lab 4be558aa): /state "STARTED", converged true,
# all 10 nodes BOOTED, links/interfaces STARTED.
# -----------------------------------------------------------------------------

_PROBE_ELEMENT_STATE = {
    "nodes": {
        "209abc66-5886-4e12-9ef8-e025500bec67": "BOOTED",
        "23ae2cc3-73e8-4d5f-90f0-d58b5e9165cd": "BOOTED",
        "7a4a2f76-e4c8-4a58-b131-c70ed349c892": "BOOTED",
        "960a00e3-dc51-42cd-a120-54ac4a34820a": "BOOTED",
        "9aafc9b5-fb6d-444a-96fe-697e57a8a8cd": "BOOTED",
        "b678885b-cc62-44a7-8304-ec4e88716f40": "BOOTED",
        "bf24050a-df79-4aaf-8680-650779621c9d": "BOOTED",
        "c26597b2-3c1c-45b2-93ab-d67d195242d8": "BOOTED",
        "c4700f21-53ce-4e78-9bee-042858da7869": "BOOTED",
        "f751289a-3cc4-4596-924f-b90cc59cab8e": "BOOTED",
    },
    "links": {
        "1eab3ab8-f18b-47e8-965f-877efffb1153": "STARTED",
        "e918b8e9-4ffe-448d-a3be-8eb5e44621fb": "STARTED",
    },
    "interfaces": {
        "01624b6c-ab20-4371-bc03-06462af012f8": "STARTED",
        "fee4bc9d-f358-4592-9203-ea3da02b5c75": "STARTED",
    },
}


def _dict_get_json(routes):
    def _get(path):
        if path not in routes:
            raise RuntimeError(f"unexpected GET {path}")
        outcome = routes[path]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    return _get


def test_probe_element_shape_parses_booted_and_active():
    from cliq.labs.discovery import parse_element_nodes

    assert parse_element_nodes(_PROBE_ELEMENT_STATE) == (10, 10, 10)


def test_probe_snapshot_is_running_evidence():
    from cliq.labs.routes import _lab_snapshot

    get_json = _dict_get_json({
        "/labs/lab-9/state": "STARTED",
        "/labs/lab-9/check_if_converged": True,
        "/labs/lab-9/lab_element_state": _PROBE_ELEMENT_STATE,
    })
    snap = _lab_snapshot(get_json, "lab-9")
    assert snap.raw_state == "STARTED"
    assert snap.converged is True
    assert (snap.booted, snap.active, snap.total) == (10, 10, 10)
    assert snap.evidence_running() is True
    assert display_state(snap.raw_state, None, converged=snap.converged,
                         booted=snap.booted, active=snap.active,
                         total=snap.total) == "Running"


def _scripted_snapshots(scripts):
    """Build a get_snapshot callable replaying LabSnapshot scripts."""
    from cliq.labs.lifecycle import LabSnapshot

    pending = [LabSnapshot(**s) if isinstance(s, dict) else s
               for s in scripts]

    def _next():
        if not pending:
            raise AssertionError("snapshot script exhausted")
        outcome = pending.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    return _next


def _refresh_bounded(token, op_id, get_snapshot, timeout_s=10.0):
    """Run refresh_operation off-thread so a locking regression fails fast
    (a wedged refresh must never stall the suite silently)."""
    import threading

    from cliq.labs.lifecycle import refresh_operation

    box: dict = {}

    def _run():
        try:
            box["op"] = refresh_operation(token, op_id, get_snapshot)
        except Exception as exc:  # propagate real failures
            box["error"] = exc

    worker = threading.Thread(target=_run, daemon=True)
    worker.start()
    worker.join(timeout=timeout_s)
    if worker.is_alive():
        raise AssertionError(
            "refresh_operation deadlocked (still holding the op lock after "
            f"{timeout_s}s)")
    if "error" in box:
        raise box["error"]
    return box.get("op")


def test_frozen_modal_scenario_never_completes():
    """DEFINED_ON_CORE + converged + 0/10 booted/0/10 active (the reported
    stuck modal) must keep polling — never complete, never read Running —
    and the PUT is issued exactly once."""
    from cliq.labs.lifecycle import get_operation, start_lab

    frozen = {"raw_state": "DEFINED_ON_CORE", "converged": True,
              "booted": 0, "active": 0, "total": 10}
    token = f"lc-direct-{uuid.uuid4().hex}"
    calls: list = []
    op, created = start_lab(
        token, "lab-9", "Demo9",
        client_factory=lambda h, u, p: _FakeStartClient(
            lab=_FakeStartLab(calls), calls=calls),
        session_host="h", session_username="u", session_password="p",
        get_snapshot=_scripted_snapshots([dict(frozen)]),
    )
    assert created is True and op.status == "starting"
    poll = _scripted_snapshots([dict(frozen)] * 3)
    for _ in range(3):
        current = _refresh_bounded(token, op.op_id, poll)
        assert current is not None and current.status == "starting"
    final = get_operation(token, op.op_id)
    assert final is not None
    assert display_state(final.raw_state, None, converged=final.converged,
                         booted=final.booted, active=final.active,
                         total=final.total) == "Defined"
    assert [c for c in calls if c == ("lab.start",)] == [("lab.start",)]


def test_start_completion_needs_full_active_not_partial():
    """Converged + partially active keeps polling; converged + fully
    active completes — convergence alone never decides."""
    from cliq.labs.lifecycle import start_lab

    token = f"lc-direct-{uuid.uuid4().hex}"
    op, _ = start_lab(
        token, "lab-9", "Demo9",
        client_factory=lambda h, u, p: _FakeStartClient(
            lab=_FakeStartLab([])),
        session_host="h", session_username="u", session_password="p",
        get_snapshot=_scripted_snapshots([
            {"raw_state": "DEFINED_ON_CORE", "converged": False,
             "booted": 0, "active": 0, "total": 2},
        ]),
    )
    assert op.status == "starting"
    partial = _refresh_bounded(token, op.op_id, _scripted_snapshots([
        {"raw_state": "DEFINED_ON_CORE", "converged": True,
         "booted": 1, "active": 1, "total": 2},
    ]))
    assert partial is not None and partial.status == "starting"
    full = _refresh_bounded(token, op.op_id, _scripted_snapshots([
        {"raw_state": "DEFINED_ON_CORE", "converged": True,
         "booted": 1, "active": 2, "total": 2},
    ]))
    assert full is not None and full.status == "running"


def test_start_short_circuits_on_started_snapshot_direct():
    """Pre-check on a STARTED snapshot: no client, no PUT, immediate done."""
    from cliq.labs.lifecycle import start_lab

    token = f"lc-direct-{uuid.uuid4().hex}"
    calls: list = []
    op, created = start_lab(
        token, "lab-9", "Demo9",
        client_factory=lambda h, u, p: calls.append("factory"),
        session_host="h", session_username="u", session_password="p",
        get_snapshot=_scripted_snapshots([
            {"raw_state": "STARTED", "converged": True,
             "booted": 10, "active": 10, "total": 10},
        ]),
    )
    assert created is True and op.status == "running"
    assert calls == []


def test_refresh_blip_then_success_completes():
    """Two failed snapshot reads ride through; the third (STARTED) wins."""
    from cliq.labs.lifecycle import start_lab

    token = f"lc-direct-{uuid.uuid4().hex}"
    op, _ = start_lab(
        token, "lab-9", "Demo9",
        client_factory=lambda h, u, p: _FakeStartClient(
            lab=_FakeStartLab([])),
        session_host="h", session_username="u", session_password="p",
        get_snapshot=_scripted_snapshots([
            {"raw_state": "DEFINED", "converged": False,
             "booted": 0, "active": 0, "total": 1},
        ]),
    )
    poll = _scripted_snapshots([
        RuntimeError("boom-1"), RuntimeError("boom-2"),
        {"raw_state": "STARTED", "converged": True,
         "booted": 1, "active": 1, "total": 1},
    ])
    assert _refresh_bounded(token, op.op_id, poll).status == "starting"
    assert _refresh_bounded(token, op.op_id, poll).status == "starting"
    assert _refresh_bounded(token, op.op_id, poll).status == "running"


def test_fetch_bundle_prefers_state_endpoint():
    """/state is authoritative; details.state is only the fallback."""
    from cliq.labs.discovery import _fetch_lab_bundle

    bundle = _fetch_lab_bundle(_dict_get_json({
        "/labs/lab-9": {"lab_title": "Demo9", "state": "DEFINED_ON_CORE",
                        "owner_username": "u"},
        "/labs/lab-9/state": "STARTED",
        "/labs/lab-9/nodes": ["n1"],
        "/labs/lab-9/check_if_converged": True,
        "/labs/lab-9/lab_element_state": _elements("BOOTED"),
    }), "lab-9")
    assert bundle.state == "STARTED"
    assert bundle.error == ""
    assert (bundle.nodes_booted, bundle.nodes_active,
            bundle.nodes_total) == (1, 1, 1)

    fallback = _fetch_lab_bundle(_dict_get_json({
        "/labs/lab-9": {"lab_title": "Demo9", "state": "DEFINED_ON_CORE",
                        "owner_username": "u"},
        "/labs/lab-9/nodes": ["n1"],
        "/labs/lab-9/check_if_converged": True,
        "/labs/lab-9/lab_element_state": _elements("DEFINED_ON_CORE"),
    }), "lab-9")
    assert fallback.state == "DEFINED_ON_CORE"
    assert fallback.error == ""


def test_fetch_bundle_failures_stay_unknown_with_error():
    from cliq.labs.discovery import _fetch_lab_bundle

    gone = _fetch_lab_bundle(_dict_get_json(
        {"/labs/lab-9": RuntimeError("gone")}), "lab-9")
    assert gone.state == "" and "gone" in gone.error
    assert display_state(gone.state, None) == "Unknown"

    bad = _fetch_lab_bundle(_dict_get_json({}), "../evil")
    assert bad.state == "" and bad.error != ""
    assert display_state(bad.state, None) == "Unknown"


def test_available_slow_lab_degraded_without_stalling():
    """One hung lab cannot stall /available: it degrades to Unknown WITH
    its timeout error while healthy labs resolve normally."""
    import time as _time

    from cliq.labs.discovery import list_labs_available

    def _slow_get_json(path):
        if path.startswith("/labs/slow"):
            _time.sleep(2)
            raise AssertionError("slow lab must time out first")
        routes = {
            "/labs": ["slow", "fast"],
            "/labs/fast": {"lab_title": "Fast", "state": "STARTED",
                           "owner_username": "u"},
            "/labs/fast/state": "STARTED",
            "/labs/fast/nodes": ["n1", "n2"],
            "/labs/fast/check_if_converged": True,
            "/labs/fast/lab_element_state": _elements("BOOTED", "BOOTED"),
        }
        if path not in routes:
            raise RuntimeError(f"unexpected GET {path}")
        return routes[path]

    started = _time.time()
    labs = list_labs_available(_slow_get_json, per_lab_timeout_s=0.3,
                               overall_timeout_s=5)
    elapsed = _time.time() - started
    assert elapsed < 5, elapsed
    by_id = {lab.id: lab for lab in labs}
    assert set(by_id) == {"slow", "fast"}
    assert by_id["slow"].state == ""
    assert "timed out" in by_id["slow"].error
    assert display_state(by_id["slow"].state, None) == "Unknown"
    assert by_id["fast"].state == "STARTED"
    assert (by_id["fast"].nodes_booted, by_id["fast"].nodes_active,
            by_id["fast"].nodes_total) == (2, 2, 2)


# -----------------------------------------------------------------------------
# Operation recovery endpoint (TestClient — socket-approval gated in sandbox)
# -----------------------------------------------------------------------------

def test_lab_operation_returns_latest_op(cml_session, monkeypatch):
    token_a, headers_a = cml_session(auth=_FakeCMLAuth(_start_routes(
        ["DEFINED", "DEFINED"])))
    token_b, headers_b = cml_session(auth=_FakeCMLAuth(_start_routes(
        ["DEFINED", "DEFINED"])))
    monkeypatch.setattr("cliq.labs.deploy.build_client_library",
                        lambda h, u, p: _FakeStartClient(
                            lab=_FakeStartLab([])))
    client = _client()
    op_id = client.post("/api/labs/lab-9/start",
                        headers=headers_a).json()["op_id"]
    assert token_a != token_b
    res = client.get("/api/labs/lab-9/operation", headers=headers_a)
    assert res.status_code == 200, res.text
    assert res.json()["op_id"] == op_id
    # Foreign session sees nothing (ownership-scoped, no leak).
    assert client.get("/api/labs/lab-9/operation",
                      headers=headers_b).status_code == 404


def test_lab_operation_missing_is_404(cml_session):
    _, headers = cml_session(auth=_FakeCMLAuth(_labs_routes()))
    res = _client().get("/api/labs/lab-9/operation", headers=headers)
    assert res.status_code == 404
    assert res.json() == {"detail": "no lifecycle operation for this lab"}


def test_available_running_without_any_operation(cml_session):
    """Post-restart dashboard: with zero tracked operations, fresh live
    reads alone must render a STARTED lab as Running."""
    _, headers = cml_session(auth=_FakeCMLAuth(_labs_routes({
        "/labs/lab-1/state": "STARTED",
        "/labs/lab-1/check_if_converged": True,
        "/labs/lab-1/lab_element_state":
            _elements("BOOTED", "BOOTED", "BOOTED"),
    })))
    res = _client().get("/api/labs/available", headers=headers)
    assert res.status_code == 200, res.text
    by_id = {lab["id"]: lab for lab in res.json()["labs"]}
    assert by_id["lab-1"]["state"] == "STARTED"
    assert by_id["lab-1"]["state_display"] == "Running"
    assert by_id["lab-1"]["nodes_booted"] == 3
    assert by_id["lab-1"]["nodes_active"] == 3
