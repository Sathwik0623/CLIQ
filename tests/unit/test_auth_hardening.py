#!/usr/bin/env python3
"""Regression tests for the login / auth-token / session-integrity hardening.

Covers, all offline:

1. ``normalize_cml_host`` — a pasted URL (scheme + trailing slash + path) is
   reduced to a bare ``host`` / ``host:port`` so it can never be double-prefixed
   into the API base (the old ``host='https'`` → "Failed to resolve 'https'").
2. ``CMLAuthManager.login`` FAILS CLOSED — a wrong password / auth rejection
   raises (never returns True), and a cached token is NEVER used as a fallback
   when live auth fails; the stale token is discarded.
3. A cached token is reused ONLY after live revalidation succeeds; a token that
   fails revalidation (expired reservation / server rejection) is discarded and
   the call fails closed.
4. ``/api/auth`` returns a NON-2xx on every auth failure (wrong password, DNS
   failure, empty host) with the real cause — never 200 OK, never the misleading
   NetSim-key text for a DNS problem. The happy path (scheme-normalized host)
   still returns 200 with a token.
"""

from __future__ import annotations

import json
import logging

import pytest
import requests
from fastapi.testclient import TestClient

from cliq.connection import cml_auth
from cliq.connection.cml_auth import CMLAuthManager, normalize_cml_host
from cliq.utils import observability as obs
from cliq.web import server


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    # Redirect the token cache + obs logs to a temp dir; clear the NetSim key so
    # diagnose_cml_auth_failure stays fully offline.
    monkeypatch.setattr(cml_auth, "_TOKEN_CACHE", str(tmp_path / "token_cache.json"))
    monkeypatch.setattr(obs, "CLIQ_HOME", tmp_path)
    monkeypatch.delenv("CLIQ_NETSIM_API_KEY", raising=False)
    monkeypatch.delenv("CLIQ_RESERVATION_POLICY", raising=False)
    monkeypatch.delenv("CLIQ_ALLOW_UNVERIFIED_RESERVATION", raising=False)
    yield


# ── host normalization ──────────────────────────────────────────────────────
class TestNormalizeHost:
    def test_strips_scheme_and_trailing_slash(self):
        assert (
            normalize_cml_host("https://cml.example.com/")
            == "cml.example.com"
        )

    def test_strips_http_scheme_and_path(self):
        assert (
            normalize_cml_host("http://cml.example.com/lab/abc/")
            == "cml.example.com"
        )

    def test_bare_host_passthrough(self):
        assert normalize_cml_host("cml.example.com") == "cml.example.com"

    def test_host_port_preserved(self):
        assert normalize_cml_host("cml.example.com:8443") == "cml.example.com:8443"

    def test_whitespace_and_empty(self):
        assert normalize_cml_host("  cml.example.com  ") == "cml.example.com"
        assert normalize_cml_host("https://") == ""
        assert normalize_cml_host("") == ""


# ── fake requests.Session for the auth manager ──────────────────────────────
class _Resp:
    def __init__(self, *, status=200, payload=None):
        self.status_code = status
        self.ok = status < 400
        self._payload = payload or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} Client Error")

    def json(self):
        return self._payload


class _FakeSession:
    """Scriptable stand-in for ``requests.Session`` on a CMLAuthManager."""

    def __init__(self, *, post=None, get=None):
        self.verify = False
        self._post = post
        self._get = get
        self.post_calls = 0
        self.get_calls = 0

    def post(self, *_a, **_kw):
        self.post_calls += 1
        return self._post(self)

    def get(self, *_a, **_kw):
        self.get_calls += 1
        return self._get(self)


def _mgr(session):
    auth = CMLAuthManager()
    auth._session = session
    return auth


def _seed_cache(host, *, token="cached-tok", expiry=None):
    import time

    expiry = expiry if expiry is not None else time.time() + 3600
    with open(cml_auth._TOKEN_CACHE, "w") as f:
        json.dump({host: {"token": token, "expiry": expiry}}, f)


def _cache_has(host):
    if not __import__("os").path.exists(cml_auth._TOKEN_CACHE):
        return False
    with open(cml_auth._TOKEN_CACHE) as f:
        return host in json.load(f)


# ── fail-closed login ───────────────────────────────────────────────────────
class TestLoginFailsClosed:
    def test_wrong_password_raises_and_no_cache_fallback(self):
        host = "cml.example.com"
        # A previous correct login left a live, non-expired cached token.
        _seed_cache(host)

        def _post(_s):
            return _Resp(status=401)

        auth = _mgr(_FakeSession(post=_post))
        # Wrong password → live auth 401. Must RAISE (fail closed), never return
        # True by falling back to the still-valid cached token.
        with pytest.raises(requests.HTTPError):
            auth.login(host, "user", "wrong")
        assert auth.token is None
        # The stale token was invalidated so it can never be reused.
        assert not _cache_has(host)

    def test_dns_failure_raises_and_discards_token(self):
        host = "cml.example.com"
        _seed_cache(host)

        def _post(_s):
            raise requests.exceptions.ConnectionError(
                "Failed to resolve 'cml.example.com' (NameResolutionError)"
            )

        auth = _mgr(_FakeSession(post=_post))
        with pytest.raises(requests.exceptions.ConnectionError):
            auth.login(host, "user", "pw")
        assert not _cache_has(host)

    def test_success_caches_token(self):
        host = "cml.example.com"

        def _post(_s):
            return _Resp(status=200, payload={"token": "fresh-jwt"})

        auth = _mgr(_FakeSession(post=_post))
        assert auth.login(host, "user", "right") is True
        assert auth.token == "fresh-jwt"
        assert _cache_has(host)

    def test_scheme_prefixed_host_is_normalized_before_auth(self):
        def _post(_s):
            return _Resp(status=200, payload={"token": "jwt"})

        auth = _mgr(_FakeSession(post=_post))
        assert auth.login("https://cml.example.com/", "u", "p") is True
        # Never 'https' — the base URL uses the bare host.
        assert auth.host == "cml.example.com"
        assert auth.base == "https://cml.example.com/api/v0"


# ── cached-token revalidation ───────────────────────────────────────────────
class TestCachedTokenRevalidation:
    def test_stale_token_discarded_when_revalidation_fails(self):
        host = "cml.example.com"
        _seed_cache(host)

        def _get(_s):
            return _Resp(status=401)  # token rejected / reservation gone

        auth = _mgr(_FakeSession(get=_get))
        # No credentials + a cached token that no longer validates → fail closed.
        with pytest.raises(ValueError):
            auth.login(host)
        assert auth.token is None
        assert not _cache_has(host)

    def test_validated_token_reused(self):
        host = "cml.example.com"
        _seed_cache(host, token="good-tok")

        def _get(_s):
            return _Resp(status=200)  # /authok accepts the token

        auth = _mgr(_FakeSession(get=_get))
        assert auth.login(host) is True
        assert auth.token == "good-tok"
        assert _cache_has(host)


# ── /api/auth endpoint fails closed with a non-2xx ──────────────────────────
class _EndpointAuth:
    """Fake CMLAuthManager wired into the endpoint under test."""

    def login(self, host, username, password):
        self.host = host
        if password == "wrong":
            raise requests.HTTPError("401 Unauthorized")
        if host == "baddns":
            raise requests.exceptions.ConnectionError(
                "Failed to resolve 'baddns' (NameResolutionError)"
            )
        if host == "unreachable":
            raise requests.exceptions.ConnectionError(
                "HTTPSConnectionPool(host='unreachable', port=443): "
                "Max retries exceeded (Connection refused)"
            )
        return True

    def check_reservation_state(self, *, proxy=None):
        # NetSim reservation authority DETECTION — keyed off the host so tests
        # can drive each outcome without a live NetSim call. The endpoint applies
        # the shared policy to the returned state.
        return self.check_reservation_detail(proxy=proxy).state

    def check_reservation_detail(self, *, proxy=None):
        # Richer DETECTION including the distinct ``unverifiable`` cause so tests
        # can drive key_invalid (rotated/expired key) vs no_key vs unreachable.
        from cliq.connection.cml_auth import ReservationDetection

        host = getattr(self, "host", "")
        if "expired-res" in host:
            return ReservationDetection("expired")
        if "expired-key" in host:
            return ReservationDetection("unverifiable", "key_invalid")
        if "netsim-down" in host:
            return ReservationDetection("unverifiable", "netsim_unreachable")
        if "noverify" in host:
            return ReservationDetection("unverifiable", "no_key")
        return ReservationDetection("active")

    def get_labs(self):
        return []


class TestApiAuthGate:
    @pytest.fixture(autouse=True)
    def _client(self, monkeypatch):
        server.SESSIONS.clear()
        monkeypatch.setattr(server, "CMLAuthManager", _EndpointAuth)
        self.client = TestClient(server.app)
        yield

    def _post(self, host, password="pw"):
        return self.client.post(
            "/api/auth",
            json={"host": host, "username": "u", "password": password},
        )

    def test_wrong_password_distinct_message_and_401(self, monkeypatch):
        # No NetSim key → still correctly report wrong-password (needs no key),
        # never the API-key catch-all.
        monkeypatch.delenv("CLIQ_NETSIM_API_KEY", raising=False)
        resp = self._post("cml.example.com", password="wrong")
        assert resp.status_code == 401
        detail = resp.json()["detail"]
        assert detail["errorKind"] == "bad_credentials"
        assert "incorrect username or password" in detail["error"].lower()
        assert "netsim api key" not in detail["error"].lower()
        assert not server.SESSIONS  # no token minted → device list unreachable

    def test_forbidden_distinct_message_and_403(self, monkeypatch):
        # Controller 403 Forbidden is classified distinctly from 401 bad credentials
        class _FakeAuth403(server.CMLAuthManager):
            def login(self, host, username=None, password=None):
                import requests
                r = requests.Response()
                r.status_code = 403
                raise requests.HTTPError("403 Client Error: Forbidden", response=r)

        monkeypatch.setattr(server, "CMLAuthManager", _FakeAuth403)
        resp = self._post("cml.example.com", password="secret")
        assert resp.status_code == 403
        detail = resp.json()["detail"]
        assert detail["errorKind"] == "forbidden"
        assert "access forbidden" in detail["error"].lower()
        assert not server.SESSIONS

    def test_backend_unreachable_distinct_message(self):
        resp = self._post("unreachable")
        assert resp.status_code == 401
        detail = resp.json()["detail"]
        assert detail["errorKind"] == "backend_unreachable"
        assert "could not reach" in detail["error"].lower()

    def test_expired_netsim_reservation_refused_at_api_gate(self):
        # Login (credentials + controller) succeeds, but the NetSim reservation
        # authority reports expired → the API gate refuses too (any policy).
        resp = self._post("expired-res.example.com", password="ok")
        assert resp.status_code == 401
        detail = resp.json()["detail"]
        assert detail["errorKind"] == "reservation_expired"
        assert "expired or inactive" in detail["error"].lower()
        assert not server.SESSIONS

    def test_unverifiable_warn_default_logs_in_with_warning(self):
        # Default policy 'warn' → an unverifiable reservation LOGS IN (regression
        # fix) but surfaces an UNMISTAKABLE unverified-access warning + the
        # persistent-indicator flag.
        resp = self._post("noverify.example.com", password="ok")
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True and body["token"]
        warning = (body.get("warning") or "").lower()
        assert "unverified access" in warning
        assert "may be operating an expired lab" in warning
        # Persistent session indicator flag — honest state, never claims verified.
        assert body.get("unverified") is True

    def test_unverifiable_warn_never_reports_verified_or_active(self, monkeypatch):
        # Honest reporting: an unverifiable session must NOT masquerade as a
        # verified/active NetSim session in the response body or observability.
        events: list[tuple] = []
        real = server.obs.log_event

        def _capture(layer, op, stage, outcome, **extra):
            events.append((op, stage, outcome, extra))
            return real(layer, op, stage, outcome, **extra)

        monkeypatch.setattr(server.obs, "log_event", _capture)
        resp = self._post("noverify.example.com", password="ok")
        assert resp.status_code == 200
        body = resp.json()
        assert body.get("unverified") is True
        checks = [e for e in events if e[0] == "reservation_check"]
        assert checks, "expected a reservation_check event"
        _op, authority, outcome, extra = checks[-1]
        assert authority == "controller_fallback"
        assert outcome == "allowed_warned"
        assert extra["reservation_state"] == "unverifiable"
        assert extra["verified"] is False
        assert extra["api_key_present"] is False

    def test_unverifiable_enforce_fails_closed(self, monkeypatch):
        # Strict mode preserved: policy 'enforce' refuses the unverifiable login.
        monkeypatch.setenv("CLIQ_RESERVATION_POLICY", "enforce")
        resp = self._post("noverify.example.com", password="ok")
        assert resp.status_code == 401
        detail = resp.json()["detail"]
        assert detail["errorKind"] == "unverifiable"
        assert "cannot verify" in detail["error"].lower()
        assert not server.SESSIONS

    def test_expired_key_warn_shows_rotation_message(self, monkeypatch):
        # A PRESENT-but-REJECTED key (30-day rotation lapsed) → warn allows but
        # the banner tells the operator to ROTATE, not configure.
        monkeypatch.setenv("CLIQ_NETSIM_API_KEY", "STALEKEY12345")
        resp = self._post("expired-key.example.com", password="ok")
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True and body["token"]
        warning = (body.get("warning") or "").lower()
        assert "rejected" in warning and "rotate every 30 days" in warning
        assert "regenerate" in warning
        assert body.get("unverified") is True

    def test_expired_key_enforce_refuses_with_rotation_message(self, monkeypatch):
        # enforce refuses the rejected key, still with the rotation guidance.
        monkeypatch.setenv("CLIQ_NETSIM_API_KEY", "STALEKEY12345")
        monkeypatch.setenv("CLIQ_RESERVATION_POLICY", "enforce")
        resp = self._post("expired-key.example.com", password="ok")
        assert resp.status_code == 401
        detail = resp.json()["detail"]
        assert detail["errorKind"] == "unverifiable"
        assert "rotate every 30 days" in detail["error"].lower()
        assert not server.SESSIONS

    def test_key_invalid_distinct_from_no_key_message(self, monkeypatch):
        # no_key → 'configure'; key_invalid → 'rotate'. They must not be the same.
        monkeypatch.delenv("CLIQ_NETSIM_API_KEY", raising=False)
        no_key = (self._post("noverify.example.com", password="ok").json()
                  .get("warning") or "").lower()
        monkeypatch.setenv("CLIQ_NETSIM_API_KEY", "STALEKEY12345")
        key_invalid = (self._post("expired-key.example.com", password="ok").json()
                       .get("warning") or "").lower()
        assert "configure cliq_netsim_api_key" in no_key
        assert "rotate every 30 days" in key_invalid
        assert no_key != key_invalid

    def test_key_invalid_obs_cause_recorded(self, monkeypatch):
        # Observability carries the distinct cause; key/token never logged.
        monkeypatch.setenv("CLIQ_NETSIM_API_KEY", "STALEKEY12345")
        events: list[tuple] = []
        real = server.obs.log_event

        def _capture(layer, op, stage, outcome, **extra):
            events.append((op, extra))
            return real(layer, op, stage, outcome, **extra)

        monkeypatch.setattr(server.obs, "log_event", _capture)
        self._post("expired-key.example.com", password="ok")
        checks = [e for e in events if e[0] == "reservation_check"]
        assert checks and checks[-1][1]["unverifiable_cause"] == "key_invalid"
        assert checks[-1][1]["reservation_state"] == "unverifiable"
        assert "STALEKEY12345" not in str(checks[-1][1])

    def test_dns_failure_gets_real_cause_not_netsim_text(self):
        resp = self._post("https://baddns/")
        assert resp.status_code == 401
        detail = resp.json()["detail"]
        assert detail["errorKind"] == "dns_failure"
        assert "resolve" in detail["error"].lower()
        assert "netsim" not in detail["error"].lower()

    def test_empty_host_returns_400(self):
        resp = self._post("https://")
        assert resp.status_code == 400
        assert resp.json()["detail"]["errorKind"] == "invalid_host"

    def test_scheme_host_happy_path_returns_token(self):
        resp = self._post("https://cml.example.com/", password="ok")
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True
        assert body["token"]
        assert body["host"] == "cml.example.com"
        assert not body.get("warning")  # verified reservation → no warning
        assert not body.get("unverified")  # verified reservation → no indicator
        assert server.SESSIONS[body["token"]].host == "cml.example.com"


# ── startup reservation posture warning ─────────────────────────────────────
class TestReservationPosture:
    def test_standalone_startup_posture_info(self, caplog):
        # Standalone mode logs controller session authority cleanly
        with caplog.at_level(logging.INFO, logger="cliq.web"):
            server._log_reservation_posture()
        assert "standalone mode active" in caplog.text.lower()
        assert "controller session authority" in caplog.text.lower()

    def test_standalone_startup_posture_no_warnings(self, caplog):
        with caplog.at_level(logging.WARNING, logger="cliq.web"):
            server._log_reservation_posture()
        assert caplog.text == ""


class TestUnreachableClassifier:
    def test_connection_refused_is_unreachable_not_dns(self):
        exc = requests.exceptions.ConnectionError("Connection refused")
        assert server._is_backend_unreachable_error(exc) is True
        assert server._is_host_resolution_error(exc) is False

    def test_dns_error_is_not_unreachable(self):
        exc = requests.exceptions.ConnectionError(
            "Failed to resolve 'h' (NameResolutionError)"
        )
        assert server._is_backend_unreachable_error(exc) is False


# ── DNS/host-resolution error classifier ────────────────────────────────────
class TestHostResolutionClassifier:
    def test_detects_name_resolution(self):
        exc = requests.exceptions.ConnectionError(
            "HTTPSConnectionPool: Failed to resolve 'h' (NameResolutionError)"
        )
        assert server._is_host_resolution_error(exc) is True

    def test_ignores_plain_http_auth_error(self):
        assert server._is_host_resolution_error(requests.HTTPError("401 Unauthorized")) is False
