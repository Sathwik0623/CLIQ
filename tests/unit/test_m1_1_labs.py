"""
Unit tests for CLIQ M1.1 — secured labs endpoints + read-only controller work.

Covers:
1. require_session authentication on all /api/labs/* endpoints (401 closed).
2. Session ownership (own controller session only; SSH sessions rejected).
3. Session isolation (no cross-session catalog/blueprint access).
4. Live read-only discovery parsing (SDK-verified GET endpoints only).
5. Live validation against controller data (definitions, images, capacity).
6. Controller failure mapping (transport/auth/shape errors → 502, no secrets).
7. Read-only guarantee (GET only; no CML write paths exist in labs code).
8. SDK contract presence (virl2_client methods this design relies on).

No test contacts a live controller: all CML I/O goes through scripted fakes.
"""

import os
import uuid
from unittest.mock import patch

import pytest

from cliq.labs.discovery import (
    DiscoveryError,
    ControllerCatalog,
    discover_controller_catalog,
    validate_definition_id,
)
from cliq.labs.models import LabBlueprint


def _valid_blueprint_dict() -> dict:
    return {
        "version": "1.0",
        "title": "OSPF backbone",
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


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _FakeCMLAuth:
    """Scripted stand-in for CMLAuthManager. Records paths; GET-only."""

    def __init__(self, routes: dict, token: str = "SECRET-TOKEN"):
        self.routes = routes
        self.token = token
        self.calls: list[str] = []

    def _get(self, path: str):
        self.calls.append(path)
        if path not in self.routes:
            raise RuntimeError(f"unexpected CML GET {path}")
        outcome = self.routes[path]
        if isinstance(outcome, Exception):
            raise outcome
        return _FakeResp(outcome)


def _sdk_shaped_routes(**overrides) -> dict:
    routes = {
        "/node_definitions": [
            {"id": "iosv", "description": "Cisco IOSv router"},
            {"id": "iosvl2", "description": "Cisco IOSvL2 switch"},
            {"id": "server", "description": "Server host"},
        ],
        "/image_definitions": [
            {"id": "iosv-159-3", "node_definition": "iosv"},
            {"id": "iosvl2-2020", "node_definition": "iosvl2"},
        ],
        "/system_stats": {"cpu_total": 32, "cpu_used": 4,
                          "ram_total_mb": 131072, "ram_used_mb": 8192},
        "/system_health": {"status": "healthy"},
        "/system_information": {"version": "2.9.0", "ready": True},
        "/node_definitions/iosv/image_definitions": [{"id": "iosv-159-3"}],
        "/node_definitions/iosvl2/image_definitions": [{"id": "iosvl2-2020"}],
    }
    routes.update(overrides)
    return routes


@pytest.fixture
def cml_session():
    """Register a CLIQ session with a fake CML controller behind it."""
    from cliq.web.server import SESSIONS, AuthSession

    created: list[str] = []

    def _make(routes: dict | None = None):
        token = f"m11-test-{uuid.uuid4().hex}"
        fake = _FakeCMLAuth(_sdk_shaped_routes(**(routes or {})))
        SESSIONS[token] = AuthSession(
            host="cml.test", username="u", password="p", auth=fake)
        created.append(token)
        return token, {"Authorization": f"Bearer {token}"}, fake

    yield _make
    for token in created:
        SESSIONS.pop(token, None)


@pytest.fixture
def ssh_only_session():
    """Register a CLIQ session with NO controller authentication."""
    from cliq.web.server import SESSIONS, AuthSession

    token = f"m11-ssh-{uuid.uuid4().hex}"
    SESSIONS[token] = AuthSession(
        host="term.test", username="u", password="p",
        auth=None, connection_type="ssh")
    yield token, {"Authorization": f"Bearer {token}"}
    SESSIONS.pop(token, None)


def _client():
    from fastapi.testclient import TestClient
    from cliq.web.server import app
    return TestClient(app)


# -----------------------------------------------------------------------------
# 1. Authentication (401 fail-closed on every labs endpoint)
# -----------------------------------------------------------------------------

def test_labs_endpoints_reject_missing_token():
    client = _client()
    bp = _valid_blueprint_dict()
    assert client.post("/api/labs/plan", json={"prompt": "x"}).status_code == 401
    assert client.post("/api/labs/validate", json={"blueprint": bp}).status_code == 401
    assert client.post("/api/labs/preview", json={"blueprint": bp}).status_code == 401
    assert client.get("/api/labs/catalog").status_code == 401


def test_labs_endpoints_reject_unknown_token():
    client = _client()
    headers = {"Authorization": "Bearer no-such-session"}
    bp = _valid_blueprint_dict()
    assert client.post("/api/labs/plan", json={"prompt": "x"},
                       headers=headers).status_code == 401
    assert client.post("/api/labs/validate", json={"blueprint": bp},
                       headers=headers).status_code == 401
    assert client.post("/api/labs/preview", json={"blueprint": bp},
                       headers=headers).status_code == 401
    assert client.get("/api/labs/catalog", headers=headers).status_code == 401


# -----------------------------------------------------------------------------
# 2. Session ownership
# -----------------------------------------------------------------------------

def test_catalog_rejects_session_without_cml_auth(ssh_only_session):
    _, headers = ssh_only_session
    res = _client().get("/api/labs/catalog", headers=headers)
    assert res.status_code == 409


def test_validate_live_rejects_session_without_cml_auth(ssh_only_session):
    _, headers = ssh_only_session
    res = _client().post("/api/labs/validate",
                         json={"blueprint": _valid_blueprint_dict(), "live": True},
                         headers=headers)
    assert res.status_code == 409


def test_validate_offline_needs_no_cml_auth(ssh_only_session):
    _, headers = ssh_only_session
    res = _client().post("/api/labs/validate",
                         json={"blueprint": _valid_blueprint_dict()},
                         headers=headers)
    assert res.status_code == 200, res.text
    assert res.json()["valid"] is True
    assert res.json()["live"] == {"live": False}


# -----------------------------------------------------------------------------
# 3. Session isolation
# -----------------------------------------------------------------------------

def test_catalog_isolation_between_sessions(cml_session):
    token_a, headers_a, fake_a = cml_session()
    token_b, headers_b, fake_b = cml_session(
        {"/node_definitions": [{"id": "other-def"}]})
    assert token_a != token_b

    client = _client()
    res_a = client.get("/api/labs/catalog", headers=headers_a)
    res_b = client.get("/api/labs/catalog", headers=headers_b)
    assert res_a.status_code == 200 and res_b.status_code == 200
    defs_a = [d["id"] for d in res_a.json()["node_definitions"]]
    defs_b = [d["id"] for d in res_b.json()["node_definitions"]]
    assert "iosv" in defs_a and "other-def" not in defs_a
    assert defs_b == ["other-def"]
    # Each token drove only its own controller session.
    assert fake_a.calls and fake_b.calls
    assert all(c.startswith("/") for c in fake_a.calls + fake_b.calls)


# -----------------------------------------------------------------------------
# 4. Discovery parsing (SDK-verified GET endpoints only)
# -----------------------------------------------------------------------------

def test_discover_catalog_sdk_shapes():
    routes = _sdk_shaped_routes()
    catalog = discover_controller_catalog(lambda p: _FakeResp(routes[p]).json())
    assert isinstance(catalog, ControllerCatalog)
    snap = catalog.to_snapshot()
    assert snap.node_definitions is not None
    assert set(snap.node_definitions) == {"iosv", "iosvl2", "server"}
    assert set(snap.image_definitions or []) == {"iosv-159-3", "iosvl2-2020"}
    assert (snap.cpu_total, snap.cpu_used) == (32, 4)
    assert (snap.ram_total_mb, snap.ram_used_mb) == (131072, 8192)
    assert catalog.summary()["controller_version"] == "2.9.0"
    assert catalog.summary()["controller_ready"] is True


def test_discover_unknown_capacity_stays_none():
    routes = _sdk_shaped_routes(**{"/system_stats": {"something_else": 1}})
    catalog = discover_controller_catalog(lambda p: _FakeResp(routes[p]).json())
    snap = catalog.to_snapshot()
    assert snap.cpu_total is None and snap.ram_total_mb is None


def test_definition_id_validation_blocks_traversal():
    assert validate_definition_id("iosv-159") == "iosv-159"
    for evil in ("../labs", "..", "a/b", "", "x" * 200, "a;b", "a b"):
        with pytest.raises(DiscoveryError):
            validate_definition_id(evil)


def test_discover_uses_get_only_paths():
    seen: list[str] = []

    def _get(path: str):
        seen.append(path)
        return {"ok": True} if path.startswith("/system") else []

    discover_controller_catalog(_get)
    assert seen == ["/node_definitions", "/image_definitions", "/system_stats",
                    "/system_health", "/system_information"]


# -----------------------------------------------------------------------------
# 5. Live validation against controller data
# -----------------------------------------------------------------------------

def test_validate_live_accepts_matching_blueprint(cml_session):
    _, headers, _ = cml_session()
    res = _client().post("/api/labs/validate",
                         json={"blueprint": _valid_blueprint_dict(), "live": True},
                         headers=headers)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["valid"] is True
    assert body["live"]["live"] is True
    assert body["live"]["controller_version"] == "2.9.0"


def test_validate_live_rejects_unknown_definition(cml_session):
    _, headers, _ = cml_session()
    bp = _valid_blueprint_dict()
    bp["nodes"][0]["node_definition"] = "quantum-router"
    res = _client().post("/api/labs/validate",
                         json={"blueprint": bp, "live": True}, headers=headers)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["valid"] is False
    assert any(e["code"] == "unknown_node_definition" for e in body["errors"])


def test_validate_live_rejects_unknown_image(cml_session):
    _, headers, _ = cml_session()
    bp = _valid_blueprint_dict()
    bp["nodes"][0]["image_definition"] = "no-such-image"
    res = _client().post("/api/labs/validate",
                         json={"blueprint": bp, "live": True}, headers=headers)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["valid"] is False
    assert any(e["code"] == "unknown_image_definition" for e in body["errors"])


def test_validate_live_rejects_image_not_valid_for_definition(cml_session):
    _, headers, _ = cml_session()
    bp = _valid_blueprint_dict()
    # Exists globally (iosvl2's image) but not valid for iosv per the mapping.
    bp["nodes"][0]["image_definition"] = "iosvl2-2020"
    res = _client().post("/api/labs/validate",
                         json={"blueprint": bp, "live": True}, headers=headers)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["valid"] is False
    assert any(e["code"] == "image_not_valid_for_definition" for e in body["errors"])


def test_validate_live_capacity_error(cml_session):
    _, headers, _ = cml_session(
        {"/system_stats": {"cpu_total": 1, "cpu_used": 0,
                           "ram_total_mb": 512, "ram_used_mb": 0}})
    res = _client().post("/api/labs/validate",
                         json={"blueprint": _valid_blueprint_dict(), "live": True},
                         headers=headers)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["valid"] is False
    assert {e["code"] for e in body["errors"]} >= {"insufficient_cpu",
                                                  "insufficient_ram"}


def test_validate_live_per_definition_outage_warns_not_silent(cml_session):
    _, headers, _ = cml_session(
        {"/node_definitions/iosv/image_definitions": RuntimeError("boom")})
    bp = _valid_blueprint_dict()
    bp["nodes"][0]["image_definition"] = "iosv-159-3"
    res = _client().post("/api/labs/validate",
                         json={"blueprint": bp, "live": True}, headers=headers)
    assert res.status_code == 200, res.text
    body = res.json()
    assert any(w["code"] == "per_definition_unavailable" for w in body["warnings"])


# -----------------------------------------------------------------------------
# 6. Controller failures → 502, no secrets
# -----------------------------------------------------------------------------

def test_catalog_controller_transport_failure_is_502(cml_session):
    _, headers, fake = cml_session(
        {"/node_definitions": ConnectionError("controller down")})
    res = _client().get("/api/labs/catalog", headers=headers)
    assert res.status_code == 502
    assert "SECRET-TOKEN" not in res.text
    assert "password" not in res.text.lower()


def test_catalog_controller_auth_failure_is_502(cml_session):
    class _AuthDenied(Exception):
        pass

    _, headers, _ = cml_session({"/node_definitions": _AuthDenied("401 denied")})
    res = _client().get("/api/labs/catalog", headers=headers)
    assert res.status_code == 502
    assert "SECRET-TOKEN" not in res.text


def test_validate_live_controller_failure_is_502(cml_session):
    _, headers, _ = cml_session(
        {"/image_definitions": TimeoutError("timed out")})
    res = _client().post("/api/labs/validate",
                         json={"blueprint": _valid_blueprint_dict(), "live": True},
                         headers=headers)
    assert res.status_code == 502


def test_catalog_shape(cml_session):
    _, headers, _ = cml_session()
    res = _client().get("/api/labs/catalog", headers=headers)
    assert res.status_code == 200, res.text
    body = res.json()
    assert set(body["snapshot"]["node_definitions"]) == {"iosv", "iosvl2", "server"}
    assert body["summary"]["node_definition_count"] == 3
    assert body["system_information"]["version"] == "2.9.0"


# -----------------------------------------------------------------------------
# 7. Read-only guarantee: no CML write paths in labs code
# -----------------------------------------------------------------------------

def test_labs_code_has_no_cml_write_calls():
    from pathlib import Path

    labs_dir = Path(__file__).resolve().parents[2] / "src" / "cliq" / "labs"
    # Read-only modules: no client HTTP verbs and no SDK usage. @router.post /
    # @router.get are FastAPI route declarations (inbound), not controller
    # calls, and docstring prose ("GET requests.", SDK citations) is
    # documentation, not use. deploy.py / routes.py are covered by the
    # sanctioned-surface test in test_m1_1_deploy.py.
    readonly_files = ("models.py", "planner.py", "validator.py", "preview.py",
                      "discovery.py")
    forbidden = ("import requests", "from requests", "requests.Session",
                 "requests.get(", "requests.post(", "requests.put(",
                 "requests.delete(", "requests.request(",
                 "import httpx", "from httpx", "httpx.Client", "httpx.Auth",
                 "import virl2_client",
                 "from virl2_client", "virl2_client.", "ClientLibrary(",
                 "session.post", "session.put", "session.delete",
                 "_session.post", "_session.put", "_session.delete",
                 ".post(", ".put(", ".delete(")
    for name in readonly_files:
        text = (labs_dir / name).read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), 1):
            for token in forbidden:
                assert token not in line, f"{name}:{lineno} has {token!r}"


# -----------------------------------------------------------------------------
# 8. SDK contract presence (skipped where the SDK is not installed)
# -----------------------------------------------------------------------------

def test_sdk_contract_presence():
    virl2_client = pytest.importorskip("virl2_client")
    from virl2_client import ClientLibrary
    from virl2_client.models.lab import Lab
    from virl2_client.models.node_image_definition import NodeImageDefinitions

    assert str(ClientLibrary.VERSION) == "2.9.0"
    for name in ("create_lab", "join_existing_lab", "get_system_health",
                 "get_system_stats", "system_info", "is_system_ready"):
        assert callable(getattr(ClientLibrary, name)), name
    for name in ("create_node", "create_link", "create_interface",
                 "has_converged", "wait_until_lab_converged", "state",
                 "start", "stop", "details"):
        assert callable(getattr(Lab, name)), name
    for name in ("node_definitions", "image_definitions",
                 "image_definitions_for_node_definition"):
        assert callable(getattr(NodeImageDefinitions, name)), name
    assert virl2_client is not None
