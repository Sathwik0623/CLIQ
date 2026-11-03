"""
Unit tests for CLIQ M1 — Lab Copilot planning, read-only validation, preview.

Covers:
1. Strict Pydantic blueprint models (forbid extras, reference integrity).
2. Natural-language planning via the AI provider abstraction (+ offline fallback).
3. Read-only CML validation (definitions, capacity; never writes, never dials out).
4. ReactFlow-compatible preview data.
5. FastAPI labs routes (POST /api/labs/plan|validate|preview).

Conventions: no pytest-asyncio — async code is driven via ``asyncio.run``.
"""

import asyncio
import os
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from cliq.ai.provider import ChatResponse
from cliq.labs.models import (
    ControllerSnapshot,
    LabBlueprint,
    ValidationResult,
)
from cliq.labs.planner import (
    PlannerError,
    parse_blueprint_json,
    parse_lab_prompt_fallback,
    plan_lab,
)
from cliq.labs.preview import blueprint_to_reactflow
from cliq.labs.validator import validate_blueprint


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
        "notes": "Two routers with one access switch.",
    }


# -----------------------------------------------------------------------------
# 1. Strict blueprint models
# -----------------------------------------------------------------------------

def test_blueprint_accepts_valid():
    bp = LabBlueprint.model_validate(_valid_blueprint_dict())
    assert bp.version == "1.0"
    assert len(bp.nodes) == 3
    assert len(bp.links) == 2


def test_blueprint_rejects_extra_fields():
    payload = _valid_blueprint_dict()
    payload["nodes"][0]["rogue"] = "x"
    with pytest.raises(ValidationError):
        LabBlueprint.model_validate(payload)

    payload = _valid_blueprint_dict()
    payload["unexpected_top_level"] = 1
    with pytest.raises(ValidationError):
        LabBlueprint.model_validate(payload)


def test_blueprint_rejects_duplicate_node_ids():
    payload = _valid_blueprint_dict()
    payload["nodes"][1]["id"] = "R1"
    payload["nodes"][1]["label"] = "R1b"
    with pytest.raises(ValidationError, match="duplicate node id"):
        LabBlueprint.model_validate(payload)


def test_blueprint_rejects_duplicate_labels():
    payload = _valid_blueprint_dict()
    payload["nodes"][1]["label"] = "R1"
    with pytest.raises(ValidationError, match="duplicate node label"):
        LabBlueprint.model_validate(payload)


def test_blueprint_rejects_dangling_link():
    payload = _valid_blueprint_dict()
    payload["links"][0]["node_b"] = "R99"
    with pytest.raises(ValidationError, match="unknown node"):
        LabBlueprint.model_validate(payload)


def test_blueprint_rejects_self_link():
    payload = _valid_blueprint_dict()
    payload["links"][0]["node_b"] = "R1"
    with pytest.raises(ValidationError, match="self-link"):
        LabBlueprint.model_validate(payload)


def test_blueprint_rejects_duplicate_interface_use():
    payload = _valid_blueprint_dict()
    payload["links"][1]["interface_a"] = "Gi0/0"  # R1 Gi0/0 already used by L1
    with pytest.raises(ValidationError, match="used more than once"):
        LabBlueprint.model_validate(payload)


def test_blueprint_rejects_empty_nodes():
    payload = _valid_blueprint_dict()
    payload["nodes"] = []
    with pytest.raises(ValidationError):
        LabBlueprint.model_validate(payload)


def test_blueprint_normalizes_node_definition():
    payload = _valid_blueprint_dict()
    payload["nodes"][0]["node_definition"] = "  IOSv "
    bp = LabBlueprint.model_validate(payload)
    assert bp.nodes[0].node_definition == "iosv"


# -----------------------------------------------------------------------------
# 2. Read-only validation
# -----------------------------------------------------------------------------

def test_validator_accepts_valid_blueprint():
    bp = LabBlueprint.model_validate(_valid_blueprint_dict())
    result = validate_blueprint(bp)
    assert isinstance(result, ValidationResult)
    assert result.valid is True
    assert result.errors == []
    est = result.resource_estimate
    assert est.total_nodes == 3
    assert est.total_links == 2
    assert est.total_vcpus == 3
    assert est.total_ram_mb == 3 * 2048


def test_validator_rejects_unknown_node_definition():
    payload = _valid_blueprint_dict()
    payload["nodes"][0]["node_definition"] = "quantum-router-9000"
    bp = LabBlueprint.model_validate(payload)
    result = validate_blueprint(bp)
    assert result.valid is False
    assert any(e.code == "unknown_node_definition" and e.node_id == "R1"
               for e in result.errors)


def test_validator_honors_controller_snapshot_allowlist():
    payload = _valid_blueprint_dict()
    payload["nodes"][0]["node_definition"] = "custom-node"
    bp = LabBlueprint.model_validate(payload)

    # Absent from snapshot -> error.
    snap = ControllerSnapshot(node_definitions=["iosv", "iosvl2"])
    result = validate_blueprint(bp, snapshot=snap)
    assert result.valid is False

    # Present in snapshot -> no definition error.
    snap = ControllerSnapshot(node_definitions=["iosv", "iosvl2", "custom-node"])
    result = validate_blueprint(bp, snapshot=snap)
    assert all(e.code != "unknown_node_definition" for e in result.errors)


def test_validator_capacity_errors():
    bp = LabBlueprint.model_validate(_valid_blueprint_dict())
    snap = ControllerSnapshot(cpu_total=2, cpu_used=0, ram_total_mb=1024, ram_used_mb=0)
    result = validate_blueprint(bp, snapshot=snap)
    assert result.valid is False
    codes = {e.code for e in result.errors}
    assert "insufficient_cpu" in codes
    assert "insufficient_ram" in codes


def test_validator_warns_on_isolated_node():
    payload = _valid_blueprint_dict()
    payload["nodes"].append({"id": "R3", "label": "R3", "node_definition": "iosv"})
    bp = LabBlueprint.model_validate(payload)
    result = validate_blueprint(bp, snapshot=None)
    assert result.valid is True
    assert any(w.code == "isolated_node" and w.node_id == "R3"
               for w in result.warnings)


def test_validator_is_readonly_no_cml_imports():
    import sys
    from pathlib import Path

    labs_dir = Path(__file__).resolve().parents[2] / "src" / "cliq" / "labs"
    # The read-only modules must stay SDK-free forever. deploy.py (the single
    # sanctioned SDK user) and routes.py (confirm-time availability probe) are
    # covered by the precise sanctioned-surface test in test_m1_1_deploy.py.
    readonly_files = ("models.py", "planner.py", "validator.py", "preview.py",
                      "discovery.py")
    # Code usage is forbidden; docstring prose citing the SDK as the verified
    # contract source is documentation, not use.
    forbidden = ("import virl2_client", "from virl2_client", "virl2_client.",
                 "CMLClient", "create_lab", "delete_lab",
                 "import requests", "urllib", "socket", "subprocess")
    for name in readonly_files:
        text = (labs_dir / name).read_text(encoding="utf-8")
        for token in forbidden:
            assert token not in text, f"{name} contains forbidden {token!r}"

    bp = LabBlueprint.model_validate(_valid_blueprint_dict())
    validate_blueprint(bp)
    # No read-only labs module may bind the SDK, regardless of imports elsewhere.
    for name in readonly_files:
        mod = sys.modules.get(f"cliq.labs.{name[:-3]}")
        if mod is not None:
            assert "virl2_client" not in vars(mod), f"{name} binds virl2_client"


# -----------------------------------------------------------------------------
# 3. ReactFlow-compatible preview
# -----------------------------------------------------------------------------

def test_preview_reactflow_shape():
    bp = LabBlueprint.model_validate(_valid_blueprint_dict())
    preview = blueprint_to_reactflow(bp)

    assert preview["title"] == "OSPF backbone"
    assert len(preview["nodes"]) == 3
    assert len(preview["links"]) == 2

    node = preview["nodes"][0]
    assert set(node.keys()) == {"id", "position", "data"}
    assert set(node["position"].keys()) == {"x", "y"}
    assert node["data"]["label"] == "R1"
    assert node["data"]["type"] == "iosv"
    assert node["data"]["node_state"] == "PLANNED"

    link = preview["links"][0]
    assert link["source"] == "R1"
    assert link["target"] == "R2"
    assert link["sourceHandle"] == "Gi0/0"
    assert link["targetHandle"] == "Gi0/0"
    assert "↔" in link["label"]

    summary = preview["summary"]
    assert summary["total_nodes"] == 3
    assert summary["total_links"] == 2
    assert summary["node_definitions"] == {"iosv": 2, "iosvl2": 1}


def test_preview_uses_explicit_coordinates():
    payload = _valid_blueprint_dict()
    payload["nodes"][0]["x"] = 111
    payload["nodes"][0]["y"] = 222
    bp = LabBlueprint.model_validate(payload)
    preview = blueprint_to_reactflow(bp)
    by_id = {n["id"]: n for n in preview["nodes"]}
    assert by_id["R1"]["position"] == {"x": 111, "y": 222}


# -----------------------------------------------------------------------------
# 4. Natural-language planning
# -----------------------------------------------------------------------------

def test_planner_mock_fallback_counts():
    with patch.dict(os.environ, {"CLIQ_LABS_MOCK": "1"}):
        bp = parse_lab_prompt_fallback("Build 3 routers and 2 switches with OSPF")
        defs = sorted(n.node_definition for n in bp.nodes)
        assert defs.count("iosv") == 3
        assert defs.count("iosvl2") == 2
        node_ids = {n.id for n in bp.nodes}
        for link in bp.links:
            assert link.node_a in node_ids
            assert link.node_b in node_ids

        result = asyncio.run(plan_lab("Build 3 routers and 2 switches"))
        assert result.mock is True
        assert len(result.blueprint.nodes) == 5


def test_planner_rejects_short_prompt():
    with patch.dict(os.environ, {"CLIQ_LABS_MOCK": "1"}):
        with pytest.raises(PlannerError):
            asyncio.run(plan_lab("ab"))


def test_planner_rejects_invalid_json():
    with pytest.raises(PlannerError):
        parse_blueprint_json("not json at all {{{")


class _FakeConfiguredProvider:
    provider_id = "fake"
    display_name = "Fake"

    def __init__(self, content: str):
        self._content = content

    def is_configured(self) -> bool:
        return True

    def get_default_model(self) -> str:
        return "fake-model"

    async def chat(self, messages, model=None, temperature=0.2,
                   max_tokens=4096, timeout_seconds=60.0):
        return ChatResponse(content=self._content, model=model or "fake-model",
                            provider="fake")


class _FakeUnconfiguredProvider(_FakeConfiguredProvider):
    def is_configured(self) -> bool:
        return False


def _clear_mock_env(monkeypatch):
    monkeypatch.delenv("CLIQ_LABS_MOCK", raising=False)
    monkeypatch.delenv("CLIQ_AI_MOCK", raising=False)


def test_planner_uses_provider_abstraction(monkeypatch):
    import json as _json

    _clear_mock_env(monkeypatch)
    payload = _valid_blueprint_dict()
    fake = _FakeConfiguredProvider(_json.dumps(payload))
    with patch("cliq.ai.registry.get_provider", return_value=fake):
        result = asyncio.run(plan_lab("2 routers 1 switch", provider_id="fake"))
    assert result.mock is False
    assert result.provider == "fake"
    assert [n.id for n in result.blueprint.nodes] == ["R1", "R2", "SW1"]


def test_planner_falls_back_when_provider_unconfigured(monkeypatch):
    _clear_mock_env(monkeypatch)
    fake = _FakeUnconfiguredProvider("{}")
    with patch("cliq.ai.registry.get_provider", return_value=fake):
        result = asyncio.run(plan_lab("2 routers 1 switch", provider_id="fake"))
    assert result.mock is True
    assert "not configured" in " ".join(result.warnings)


def test_planner_surfaces_invalid_llm_output(monkeypatch):
    _clear_mock_env(monkeypatch)
    fake = _FakeConfiguredProvider("{\"version\": \"9.9\", \"nope\": true}")
    with patch("cliq.ai.registry.get_provider", return_value=fake):
        with pytest.raises(PlannerError):
            asyncio.run(plan_lab("2 routers 1 switch", provider_id="fake"))


# -----------------------------------------------------------------------------
# 5. FastAPI labs routes (M1.1: require_session authentication enforced)
# -----------------------------------------------------------------------------

@pytest.fixture
def _labs_auth_headers():
    """Register a throwaway CLIQ session and yield its auth headers."""
    import uuid as _uuid
    from cliq.web.server import SESSIONS, AuthSession

    token = f"m1-test-{_uuid.uuid4().hex}"
    SESSIONS[token] = AuthSession(host="cml.test", username="u", password="p")
    try:
        yield {"Authorization": f"Bearer {token}"}
    finally:
        SESSIONS.pop(token, None)


def test_labs_routes_plan_validate_preview(_labs_auth_headers):
    from fastapi.testclient import TestClient
    from cliq.web.server import app

    client = TestClient(app)
    with patch.dict(os.environ, {"CLIQ_LABS_MOCK": "1"}):
        res = client.post("/api/labs/plan", json={"prompt": "2 routers and 1 switch"},
                          headers=_labs_auth_headers)
    assert res.status_code == 200, res.text
    plan = res.json()
    assert "blueprint" in plan
    assert len(plan["blueprint"]["nodes"]) == 3

    blueprint = _valid_blueprint_dict()
    res = client.post("/api/labs/validate", json={"blueprint": blueprint},
                      headers=_labs_auth_headers)
    assert res.status_code == 200, res.text
    assert res.json()["valid"] is True

    res = client.post("/api/labs/preview", json={"blueprint": blueprint},
                      headers=_labs_auth_headers)
    assert res.status_code == 200, res.text
    preview = res.json()
    assert len(preview["nodes"]) == 3
    assert len(preview["links"]) == 2


def test_labs_routes_reject_bad_blueprint(_labs_auth_headers):
    from fastapi.testclient import TestClient
    from cliq.web.server import app

    client = TestClient(app)
    bad = _valid_blueprint_dict()
    bad["links"][0]["node_b"] = "GHOST"
    res = client.post("/api/labs/validate", json={"blueprint": bad},
                      headers=_labs_auth_headers)
    assert res.status_code == 422

    res = client.post("/api/labs/plan", json={"prompt": "x"},
                      headers=_labs_auth_headers)
    assert res.status_code == 422
