#!/usr/bin/env python3
"""Regression tests for the SSH terminal-server connect flow (Phase 2 wiring).

These cover the REST surface that lets the browser pick the SSH transport and
have it drive the SAME WebSocket terminal as the CML path:

  * ``POST /api/ssh/connect`` mints a session token + a single synthetic device
    and registers both in the in-memory maps the WS loop reads.
  * The registered device meta carries the ``connection_type="ssh"`` marker and
    the SSH coordinates (host/user/port) the transport branch consumes.
  * The password is used in-memory only and is NEVER echoed back in the JSON
    response (it lives only inside the AuthSession for the live session).
  * Missing required coordinates are rejected with 422.

All I/O is offline: the endpoint does not reach the device (the live attach
happens later on the WS ``connect_device``), so no netmiko/SSH is exercised.
"""

from fastapi.testclient import TestClient

from cliq.web import server


def _client() -> TestClient:
    return TestClient(server.app)


def _clear_state() -> None:
    server.SESSIONS.clear()
    server.DEVICES_BY_TOKEN.clear()


class TestSshConnectEndpoint:
    def test_connect_registers_session_and_device(self):
        _clear_state()
        client = _client()
        resp = client.post(
            "/api/ssh/connect",
            json={
                "host": "ts01.example.org",
                "console_user": "operator",
                "console_port": "19",
                "password": "s3cr3t-should-not-leak",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True
        assert body["connection_type"] == "ssh"
        token = body["token"]
        device = body["device"]

        # Session registered as an SSH session with NO CML auth manager.
        session = server.SESSIONS[token]
        assert session.connection_type == "ssh"
        assert session.auth is None
        assert session.host == "ts01.example.org"
        assert session.username == "operator"
        # The password IS held in-memory for the live session...
        assert session.password == "s3cr3t-should-not-leak"

        # ...but is NEVER echoed back to the client anywhere in the response.
        assert "s3cr3t-should-not-leak" not in resp.text

        # A single synthetic device is registered under this token.
        metas = server.DEVICES_BY_TOKEN[token]
        assert device["id"] in metas
        meta = metas[device["id"]]
        assert meta["connection_type"] == "ssh"
        assert meta["ssh_host"] == "ts01.example.org"
        assert meta["ssh_console_user"] == "operator"
        assert meta["ssh_console_port"] == "19"
        assert meta["ssh_device_type"] == "cisco_ios"
        # console_path is a globally-unique lock key for exclusive attach.
        assert meta["console_path"].startswith("ssh://")
        # type is unknown until 'show version' on connect.
        assert meta["type"] == ""

    def test_label_reflects_user_port_host(self):
        _clear_state()
        client = _client()
        resp = client.post(
            "/api/ssh/connect",
            json={
                "host": "ts.example.com",
                "console_user": "opuser",
                "console_port": "7",
                "password": "pw",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["device"]["label"] == "opuser:7@ts.example.com"

    def test_two_connects_get_distinct_tokens_and_locks(self):
        _clear_state()
        client = _client()
        first = client.post(
            "/api/ssh/connect",
            json={"host": "h1", "console_user": "u", "console_port": "1", "password": "p"},
        ).json()
        second = client.post(
            "/api/ssh/connect",
            json={"host": "h2", "console_user": "u", "console_port": "2", "password": "p"},
        ).json()
        assert first["token"] != second["token"]
        m1 = server.DEVICES_BY_TOKEN[first["token"]][first["device"]["id"]]
        m2 = server.DEVICES_BY_TOKEN[second["token"]][second["device"]["id"]]
        assert m1["console_path"] != m2["console_path"]

    def test_missing_console_user_rejected(self):
        _clear_state()
        client = _client()
        resp = client.post(
            "/api/ssh/connect",
            json={"host": "ts.example.com", "console_user": "  ", "console_port": "9", "password": "p"},
        )
        assert resp.status_code == 422

    def test_missing_host_rejected(self):
        _clear_state()
        client = _client()
        resp = client.post(
            "/api/ssh/connect",
            json={"host": "   ", "console_user": "u", "console_port": "9", "password": "p"},
        )
        assert resp.status_code == 422


class TestSshTransportSelection:
    def test_ssh_meta_selects_ssh_backend(self):
        """The connect branch keys off meta['connection_type'] to pick backend."""
        _clear_state()
        client = _client()
        body = client.post(
            "/api/ssh/connect",
            json={"host": "h", "console_user": "u", "console_port": "5", "password": "p"},
        ).json()
        meta = server.DEVICES_BY_TOKEN[body["token"]][body["device"]["id"]]
        # This is exactly the discriminator the server's connect_device reads.
        assert meta.get("connection_type", "cml") == "ssh"


class TestDisplayBoundaryGuard:
    """Phase 10 — the single display-boundary guard that stops control-char
    artifacts (``^@``/NUL, ``^C``/ETX) from ever reaching the browser.

    The ``^@`` symptom appeared during terminal-server LATENCY: a delayed/empty
    channel read carrying ``\\x00`` was streamed as ``device_output``/
    ``device_stream`` before it was stripped. The guard runs at the boundary
    where SSH bytes are handed to the frontend and (a) strips every control
    artifact and (b) drops the chunk entirely if nothing real remains.
    """

    def test_nul_only_read_sanitizes_to_empty(self):
        # A delayed/empty read that is nothing but NUL collapses to "" — the
        # on_chunk boundary then skips the emit (never a device_stream/output).
        assert server._sanitize_console_display("\x00") == ""
        assert server._sanitize_console_display("\x00\x00\x00") == ""

    def test_etx_ctrlc_artifact_stripped(self):
        # The real log showed 'Async0/2/7 ... up ^C' — the ETX must vanish.
        assert server._sanitize_console_display("Async0/2/7 up\x03") == "Async0/2/7 up"
        assert server._sanitize_console_display("\x03") == ""

    def test_mixed_control_artifacts_stripped_real_text_kept(self):
        assert server._sanitize_console_display("a\x00b\x03c") == "abc"

    def test_literal_caret_at_stripped(self):
        # PROVEN by byte trace: the terminal server emits the LITERAL two-char
        # sequence '^' (0x5e) + '@' (0x40) as visible text — NOT a NUL byte.
        # The display boundary must strip that exact pairing.
        assert [ord(c) for c in "^@"] == [0x5E, 0x40]
        assert server._sanitize_console_display("^@") == ""
        assert server._sanitize_console_display("^@\nRouter#") == "\nRouter#"
        assert (
            server._sanitize_console_display("*12:00:00 UTC^@")
            == "*12:00:00 UTC"
        )

    def test_real_caret_error_marker_preserved(self):
        # A lone '^' error marker ('% Invalid input ... ^') is never followed by
        # '@', so it must survive the literal-'^@' strip (no over-stripping).
        text = "                   ^\n% Invalid input detected at '^' marker."
        assert server._sanitize_console_display(text) == text

    def test_whitespace_only_after_strip_is_droppable(self):
        # NUL-then-space is whitespace-only after strip -> boundary drops it
        # (the on_chunk guard checks `.strip()` and returns without emitting).
        cleaned = server._sanitize_console_display("\x00 \x00")
        assert cleaned.strip() == ""

    def test_layout_whitespace_preserved(self):
        # \t \n \r are legitimate table/layout bytes and MUST survive so real
        # output (e.g. 'sh ip interface brief') renders correctly.
        text = "Interface\tIP\r\n  Gig0/0/0\t1.1.1.1\n"
        assert server._sanitize_console_display(text) == text

    def test_clean_output_is_noop(self):
        # No-op for already-clean text (the CML transport's cleaned deltas),
        # guaranteeing the CML display path is unchanged.
        text = "Router#\nGigabitEthernet0/0/0 up up"
        assert server._sanitize_console_display(text) == text

    def test_empty_input_returned_unchanged(self):
        assert server._sanitize_console_display("") == ""

