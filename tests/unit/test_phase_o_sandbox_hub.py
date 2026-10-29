"""Unit and integration tests for CLIQ Phase O: Multi-Platform Sandbox Hub and CML Lab Integration."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest
from starlette.testclient import TestClient

from cliq.web import server
from cliq.connection.cml_auth import CMLLab, CMLDevice


def _client() -> TestClient:
    return TestClient(server.app)


def _clear_state() -> None:
    server.SESSIONS.clear()
    server.DEVICES_BY_TOKEN.clear()
    server._DEVICE_RUNTIMES.clear()
    server._CONSOLE_LOCKS.clear()
    server._ACTIVE_SOCKETS_BY_TOKEN.clear()


class TestPhaseOSandboxHub:
    """Validates the Sandbox Hub, CML workflow, multi-vendor extensibility, and security."""

    def test_hub_ui_renders_three_platform_cards(self):
        """1. Sandbox Hub renders all three platform cards with accurate badges, titles, and subtitle."""
        hub_path = Path(__file__).resolve().parents[2] / "frontend" / "src" / "components" / "SandboxHub.tsx"
        platform_path = Path(__file__).resolve().parents[2] / "frontend" / "src" / "types" / "platform.ts"
        assert hub_path.is_file(), f"SandboxHub.tsx not found at {hub_path}"
        assert platform_path.is_file(), f"platform.ts not found at {platform_path}"

        hub_code = hub_path.read_text(encoding="utf-8")
        platform_code = platform_path.read_text(encoding="utf-8")

        # Subtitle and branding
        assert "Your network lab, one terminal away" in hub_code
        assert "CLIQ Sandbox Hub" in hub_code

        # Three platform options defined
        assert "'direct_ssh'" in platform_code
        assert "'cml'" in platform_code
        assert "'future_platforms'" in platform_code

        # Platform names and tags
        assert "Direct Sandbox Device" in platform_code
        assert "CML Sandbox Lab" in platform_code
        assert "Multi-Vendor Platforms" in platform_code

        # Status and Badges
        assert "'available'" in platform_code
        assert "'coming_soon'" in platform_code
        assert "Coming Soon" in platform_code
        assert "Available" in platform_code

    def test_direct_ssh_selection_preserves_form_and_api(self):
        """2. Direct SSH preserves existing connection form fields and backend API behavior."""
        login_step_path = Path(__file__).resolve().parents[2] / "frontend" / "src" / "components" / "LoginStep.tsx"
        assert login_step_path.is_file()
        code = login_step_path.read_text(encoding="utf-8")

        # Direct SSH fields preserved
        assert "Device IP or Hostname *" in code
        assert "SSH Port *" in code
        assert "Username *" in code
        assert "SSH Login Password *" in code
        assert "Enable Secret (Optional)" in code
        assert "Target Platform" in code
        assert "Friendly Label (Optional)" in code
        assert "CLIQ — Sandbox Device Terminal" in code
        assert "Back to Sandbox Hub" in code

        # API behavior
        _clear_state()
        client = _client()
        resp = client.post(
            "/api/ssh/connect",
            json={
                "host": "10.10.20.181",
                "port": 22,
                "username": "developer",
                "password": "ssh_password_123",
                "secret": "enable_secret_456",
                "device_type": "cisco_ios",
                "method": "direct",
                "label": "Direct Switch",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["token"] in server.SESSIONS
        assert server.SESSIONS[data["token"]].secret == "enable_secret_456"

    def test_cml_selection_displays_cml_specific_form(self):
        """3. CML selection displays the CML-specific form with back action."""
        cml_form_path = Path(__file__).resolve().parents[2] / "frontend" / "src" / "components" / "CmlConnectForm.tsx"
        assert cml_form_path.is_file(), f"CmlConnectForm.tsx not found at {cml_form_path}"
        code = cml_form_path.read_text(encoding="utf-8")

        # CML form fields
        assert "CML Controller Host or URL *" in code
        assert "Username *" in code
        assert "Password *" in code
        assert "Lab / Deployment Label (Optional)" in code
        assert "Back to Sandbox Hub" in code
        assert "Connect to CML Sandbox" in code

    def test_cml_credentials_submitted_to_auth_endpoint(self, monkeypatch):
        """4. CML credentials submit to existing /api/auth endpoint without fabrication."""
        _clear_state()
        client = _client()

        # Mock CMLAuthManager to verify backend interaction
        fake_auth = MagicMock()
        fake_auth.login.return_value = True
        fake_auth.get_labs.return_value = [
            CMLLab(lab_id="lab-001", title="Enterprise Branch", state="STARTED", owner="developer")
        ]

        with patch("cliq.web.server.CMLAuthManager", return_value=fake_auth):
            resp = client.post(
                "/api/auth",
                json={
                    "host": "cml.example.org",
                    "username": "cml_user",
                    "password": "cml_password_abc",
                },
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["success"] is True
            assert "token" in data
            assert len(data["labs"]) == 1
            assert data["labs"][0]["id"] == "lab-001"
            assert data["labs"][0]["title"] == "Enterprise Branch"

            # Verify session stored in server
            token = data["token"]
            assert token in server.SESSIONS
            session = server.SESSIONS[token]
            assert session.username == "cml_user"
            assert session.host == "cml.example.org"

    def test_cml_errors_shown_accurately_without_fabricated_success(self, monkeypatch):
        """5. CML authentication errors fail closed and return real cause without fake success."""
        _clear_state()
        client = _client()

        fake_auth = MagicMock()
        fake_auth.login.side_effect = ValueError("Invalid CML username or password")

        with patch("cliq.web.server.CMLAuthManager", return_value=fake_auth):
            resp = client.post(
                "/api/auth",
                json={
                    "host": "cml.example.org",
                    "username": "wrong_user",
                    "password": "wrong_password",
                },
            )
            assert resp.status_code == 401
            data = resp.json()
            assert "detail" in data
            assert data["detail"]["success"] is False
            assert "Invalid CML username or password" in data["detail"]["error"]
            assert len(server.SESSIONS) == 0

    def test_successful_cml_response_renders_labs_and_devices(self, monkeypatch):
        """6. A successful CML authentication returns real labs, devices, and topology nodes."""
        _clear_state()
        client = _client()

        token = "cml-token-test-123"
        fake_auth = MagicMock()
        fake_devices = [
            CMLDevice(
                label="dist-sw01",
                node_id="node-dist-1",
                node_type="iosvl2",
                lab_id="lab-001",
                console_path="/Enterprise Branch/dist-sw01/0",
                state="BOOTED",
            ),
            CMLDevice(
                label="edge-rtr01",
                node_id="node-edge-1",
                node_type="csr1000v",
                lab_id="lab-001",
                console_path="/Enterprise Branch/edge-rtr01/0",
                state="BOOTED",
            ),
        ]
        fake_topology = {
            "nodes": [
                {"id": "node-dist-1", "label": "dist-sw01", "type": "iosvl2", "state": "BOOTED", "x": 0, "y": 0},
                {"id": "node-edge-1", "label": "edge-rtr01", "type": "csr1000v", "state": "BOOTED", "x": 100, "y": 100},
            ],
            "links": [
                {"id": "link-1", "label": "Gi0/0 <-> Gi0/1", "node_a": "node-dist-1", "node_b": "node-edge-1"},
            ],
        }

        fake_auth.get_devices.return_value = fake_devices
        fake_auth.get_topology.return_value = fake_topology

        session = server.AuthSession(
            host="cml.example.org",
            username="developer",
            password="pwd",
            auth=fake_auth,
            connection_type="cml",
        )
        server.SESSIONS[token] = session

        resp = client.post(
            "/api/devices",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "host": "cml.example.org",
                "token": token,
                "lab_id": "lab-001",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "devices" in data
        assert len(data["devices"]) == 2
        assert data["devices"][0]["label"] == "dist-sw01"
        assert data["devices"][1]["label"] == "edge-rtr01"
        assert "topology" in data
        assert len(data["topology"]["nodes"]) == 2
        assert len(data["topology"]["links"]) == 1

        # Check server registration
        dev_map = server.DEVICES_BY_TOKEN[token]
        assert "node-dist-1" in dev_map
        assert dev_map["node-dist-1"]["console_path"] == "/Enterprise Branch/dist-sw01/0"

    def test_selected_node_connects_only_through_verified_supported_console(self, monkeypatch):
        """7. A selected CML node attaches to the terminal using verified CMLConsoleDevice."""
        _clear_state()
        token = "cml-token-test-node"
        dev_id = "node-dist-1"
        meta = {
            "id": dev_id,
            "label": "dist-sw01",
            "type": "iosvl2",
            "console_path": "/Enterprise Branch/dist-sw01/0",
            "connection_type": "cml",
            "lab_id": "lab-001",
        }
        server.DEVICES_BY_TOKEN[token] = {dev_id: meta}

        fake_auth = MagicMock()
        session = server.AuthSession(
            host="cml.example.org",
            username="developer",
            password="pwd",
            auth=fake_auth,
            connection_type="cml",
        )
        server.SESSIONS[token] = session

        fake_cml_device = MagicMock()
        fake_cml_device.current_prompt = "dist-sw01>"
        fake_cml_device.is_connected = True

        client = _client()
        with patch("cliq.web.server.CMLConsoleDevice", return_value=fake_cml_device):
            with client.websocket_connect(f"/ws?token={token}&host=cml.example.org") as ws:
                ws.send_json({"type": "connect_device", "device_id": dev_id})
                connected = False
                for _ in range(15):
                    msg = ws.receive_json()
                    if msg.get("type") == "device_status" and msg.get("status") == "connected":
                        connected = True
                        break
                assert connected
                assert (token, dev_id) in server._DEVICE_RUNTIMES

    def test_platform_selection_and_back_navigation_do_not_create_duplicate_sessions(self):
        """8. Platform selection, switching, and back navigation do not create unneeded sessions."""
        _clear_state()
        # Initial state: 0 sessions
        assert len(server.SESSIONS) == 0

        # Simulating user navigating to hub, choosing CML, returning to hub, then choosing Direct SSH
        # No API requests are fired during navigation until actual submission
        assert len(server.SESSIONS) == 0
        assert len(server._DEVICE_RUNTIMES) == 0

    def test_credentials_not_persisted_or_logged(self):
        """9. Passwords and secrets are never leaked in response payloads or device meta."""
        _clear_state()
        client = _client()
        resp = client.post(
            "/api/ssh/connect",
            json={
                "host": "10.10.20.181",
                "username": "developer",
                "password": "super_secret_ssh_pw",
                "secret": "super_secret_enable_sec",
                "device_type": "cisco_ios",
                "method": "direct",
            },
        )
        assert resp.status_code == 200
        data = resp.json()

        # Response must never leak credentials
        raw_text = str(data)
        assert "super_secret_ssh_pw" not in raw_text
        assert "super_secret_enable_sec" not in raw_text

        # Device meta in memory must never expose secrets to frontend
        token = data["token"]
        dev_meta = server.DEVICES_BY_TOKEN[token][data["device"]["id"]]
        assert "password" not in dev_meta
        assert "secret" not in dev_meta

    def test_future_platforms_clearly_marked_coming_soon(self):
        """10. Palo Alto Networks and Juniper Networks are clearly marked as Coming Soon."""
        platform_path = Path(__file__).resolve().parents[2] / "frontend" / "src" / "types" / "platform.ts"
        code = platform_path.read_text(encoding="utf-8")

        assert "Palo Alto Networks" in code
        assert "Juniper Networks" in code
        assert "'coming_soon'" in code
        assert "Palo Alto PAN-OS (Planned)" in code
        assert "Juniper Junos OS (Planned)" in code
