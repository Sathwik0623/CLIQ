#!/usr/bin/env python3
"""Unit tests for the Phase A unified observability/logging helper."""

from __future__ import annotations

import json

import pytest

from cliq.utils import observability as obs


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path, monkeypatch):
    """Point every log file at a temp dir so tests never touch ~/.cliq."""
    monkeypatch.setattr(obs, "CLIQ_HOME", tmp_path)
    yield


def _read_lines(path):
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def test_log_event_writes_unified_schema(tmp_path):
    obs.log_event("cml", "connect", "auth", "success", elapsed_ms=12.34, status_code=200)
    records = _read_lines(tmp_path / "cml.log")
    assert len(records) == 1
    rec = records[0]
    for key in ("ts", "layer", "op", "stage", "outcome", "status_code", "elapsed_ms", "detail"):
        assert key in rec
    assert rec["layer"] == "cml"
    assert rec["op"] == "connect"
    assert rec["stage"] == "auth"
    assert rec["outcome"] == "success"
    assert rec["elapsed_ms"] == 12.3  # rounded to 1 dp


def test_layer_routes_to_expected_files(tmp_path):
    obs.log_event("netsim", "get_projects", "request", "success")
    obs.log_event("terminal", "session_connect", "complete", "success")
    obs.log_event("command", "run_command", "complete", "success")
    obs.log_usage("session_start")
    assert (tmp_path / "netsim_api.log").exists()
    assert (tmp_path / "terminal.log").exists()
    assert (tmp_path / "command.log").exists()
    assert (tmp_path / "usage.log").exists()


@pytest.mark.parametrize(
    "secret_detail",
    [
        "connecting with api_key=ABCDEF1234567890",
        "Authorization: Token deadbeefdeadbeefdeadbeef",
        "password=hunter2 supplied",
        "x-api-key: 0123456789abcdef0123456789abcdef",
        "bearer aGVsbG8td29ybGQtdG9rZW4",
    ],
)
def test_no_secrets_reach_log(tmp_path, secret_detail):
    obs.log_event("netsim", "auth", "auth", "success", detail=secret_detail)
    raw = (tmp_path / "netsim_api.log").read_text(encoding="utf-8")
    assert "<redacted>" in raw
    # The obvious secret substrings must NOT appear verbatim.
    for leaked in ("hunter2", "ABCDEF1234567890", "deadbeefdeadbeefdeadbeef"):
        assert leaked not in raw


def test_secret_key_in_extra_is_redacted(tmp_path):
    obs.log_event("cml", "connect", "auth", "success", api_key="SUPERSECRET", auth_style="x-api-key")
    rec = _read_lines(tmp_path / "cml.log")[0]
    assert rec["api_key"] == "<redacted>"
    assert rec["auth_style"] == "x-api-key"  # non-secret extra preserved


def test_stage_timer_emits_per_stage(tmp_path):
    st = obs.StageTimer("cml", "connect_stages")
    st.mark("reservation_check")
    st.mark("auth")
    st.mark("complete")
    records = _read_lines(tmp_path / "cml.log")
    assert [r["stage"] for r in records] == ["reservation_check", "auth", "complete"]
    for r in records:
        assert r["elapsed_ms"] is not None
        assert "total_ms" in r


def test_timed_contextmanager_success_and_error(tmp_path):
    with obs.timed("command", "run_command", detail="show"):
        pass
    with pytest.raises(ValueError):
        with obs.timed("command", "run_command", detail="bad"):
            raise ValueError("boom")
    records = _read_lines(tmp_path / "command.log")
    assert records[0]["outcome"] == "success"
    assert records[1]["outcome"] == "error"


def test_rotation_caps_size(tmp_path, monkeypatch):
    monkeypatch.setattr(obs, "_MAX_BYTES", 500)
    monkeypatch.setattr(obs, "_MAX_BACKUPS", 2)
    for i in range(200):
        obs.log_event("command", "run_command", "complete", "success", detail=f"n{i}")
    # Primary file exists and at least one rotated backup was created.
    assert (tmp_path / "command.log").exists()
    assert (tmp_path / "command.log.1").exists()
    # Primary file stays within the cap (plus one line of slack).
    assert (tmp_path / "command.log").stat().st_size <= obs._MAX_BYTES + 256


def test_logging_never_raises(monkeypatch):
    # Even if serialization blows up, the caller must not see an exception.
    monkeypatch.setattr(obs, "_write_record", lambda *a, **k: (_ for _ in ()).throw(OSError()))
    # log_event swallows write failures internally; ensure no raise escapes.
    obs.log_event("cml", "connect", "auth", "success")


def test_event_listener_flag(monkeypatch):
    monkeypatch.delenv("CLIQ_CML_EVENTS", raising=False)
    assert obs.event_listener_enabled() is False
    monkeypatch.setenv("CLIQ_CML_EVENTS", "1")
    assert obs.event_listener_enabled() is True
    monkeypatch.setenv("CLIQ_CML_EVENTS", "off")
    assert obs.event_listener_enabled() is False
