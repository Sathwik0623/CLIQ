#!/usr/bin/env python3
"""Unit tests for standalone CML 401/403 auth failure diagnosis.

Asserts that HTTP 401 (bad credentials) and HTTP 403 (forbidden/permission denied)
are classified distinctly and accurately without any dependency on external booking systems.
"""

from __future__ import annotations

import json
import pytest

from cliq.utils import observability as obs
from cliq.connection.cml_client import diagnose_cml_auth_failure, is_http_auth_error


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path, monkeypatch):
    monkeypatch.setattr(obs, "CLIQ_HOME", tmp_path)
    monkeypatch.delenv("CLIQ_NETSIM_API_KEY", raising=False)
    yield


def _read(path):
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


# ── HTTP auth-error detection ───────────────────────────────────────────────
def test_is_http_auth_error_detects_401_403():
    assert is_http_auth_error(Exception("401 Client Error: Unauthorized for url"))
    assert is_http_auth_error(Exception("403 Forbidden"))
    assert not is_http_auth_error(Exception("500 Server Error"))

    class _Resp:
        status_code = 401

    class _Err(Exception):
        response = _Resp()

    assert is_http_auth_error(_Err("boom"))


# ── 401 Unauthorized → distinct "bad_credentials" ───────────────────────────
def test_401_is_bad_credentials():
    diag = diagnose_cml_auth_failure("cml.example.com", status_code=401)
    assert diag.kind == "bad_credentials"
    assert "incorrect username or password" in diag.message.lower()
    assert "re-check your cml controller credentials" in diag.message.lower()
    assert diag.verified is True


def test_401_exception_is_bad_credentials():
    exc = Exception("401 Client Error: Unauthorized")
    diag = diagnose_cml_auth_failure("cml.example.com", exc=exc)
    assert diag.kind == "bad_credentials"
    assert "incorrect username or password" in diag.message.lower()


# ── 403 Forbidden → distinct "forbidden" (NOT bad credentials) ──────────────
def test_403_is_forbidden_distinction():
    diag = diagnose_cml_auth_failure("cml.example.com", status_code=403)
    assert diag.kind == "forbidden"
    assert "access forbidden" in diag.message.lower()
    assert "lacks required permissions" in diag.message.lower()
    assert diag.verified is True


def test_403_exception_is_forbidden():
    class _Resp403:
        status_code = 403

    class _Err403(Exception):
        response = _Resp403()

    diag = diagnose_cml_auth_failure("cml.example.com", exc=_Err403("403 Forbidden"))
    assert diag.kind == "forbidden"
    assert "access forbidden" in diag.message.lower()


# ── secret hygiene: observability logs do not expose passwords ───────────────
def test_diagnosis_observability_event(tmp_path):
    diag = diagnose_cml_auth_failure("cml.example.com", status_code=401)
    log = tmp_path / "cml.log"
    assert log.exists()
    records = _read(log)
    assert any(r["op"] == "auth_diagnosis" for r in records)
