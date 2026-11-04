"""
CLIQ Read-Only Network Context Service (Phase V).

Provides safe, read-only extraction and sanitization of network state from
active CLIQ sessions and live device runtimes. Secret-redacted by default.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

from cliq.utils.observability import redact

log = logging.getLogger("cliq.ai.context")

# Regex to redact Cisco enable secrets, user passwords, and community strings
_SECRET_PATTERNS = [
    re.compile(r"(enable\s+secret(?:\s+\d+)?\s+)\S+", re.IGNORECASE),
    re.compile(r"(password(?:\s+\d+)?\s+)\S+", re.IGNORECASE),
    re.compile(r"(username\s+\S+\s+(?:privilege\s+\d+\s+)?(?:secret|password)(?:\s+\d+)?\s+)\S+", re.IGNORECASE),
    re.compile(r"(snmp-server\s+community\s+)\S+", re.IGNORECASE),
    re.compile(r"(neighbor\s+\S+\s+password(?:\s+\d+)?\s+)\S+", re.IGNORECASE),
]


def redact_config_secrets(text: str) -> str:
    """Scrub passwords, enable secrets, and tokens from network device output."""
    if not text:
        return ""
    scrubbed = str(text)
    # Redact Cisco configuration secrets
    for pattern in _SECRET_PATTERNS:
        scrubbed = pattern.sub(r"\1[REDACTED_SECRET]", scrubbed)
    return str(redact(scrubbed))


class NetworkContextService:
    """Read-only service for extracting live device context for the AI Copilot."""

    @staticmethod
    def _get_server_module():
        """Lazy-import server module to prevent circular dependencies."""
        from cliq.web import server
        return server

    def list_devices(self, session_token: Optional[str] = None) -> List[Dict[str, Any]]:
        """List active devices accessible within the given session token."""
        server = self._get_server_module()
        if not session_token or session_token not in server.DEVICES_BY_TOKEN:
            # Fallback to any active devices or mock list if unauthenticated in dev
            all_devs: List[Dict[str, Any]] = []
            for token_devs in server.DEVICES_BY_TOKEN.values():
                for dev in token_devs.values():
                    all_devs.append({
                        "id": dev.get("id"),
                        "label": dev.get("label", "Device"),
                        "node_type": dev.get("type", "router"),
                        "status": dev.get("status", "connected"),
                        "mode": dev.get("mode", "exec"),
                    })
            return all_devs

        dev_map = server.DEVICES_BY_TOKEN.get(session_token, {})
        return [
            {
                "id": d.get("id"),
                "label": d.get("label", "Device"),
                "node_type": d.get("type", "router"),
                "status": d.get("status", "connected"),
                "mode": d.get("mode", "exec"),
            }
            for d in dev_map.values()
        ]

    def get_device_info(self, session_token: str, device_id: str) -> Dict[str, Any]:
        """Get high-level summary info for a device."""
        devices = self.list_devices(session_token)
        for d in devices:
            if d.get("id") == device_id:
                return d
        return {"id": device_id, "label": device_id, "status": "unknown"}

    def _resolve_runtime(self, session_token: Optional[str], device_id: str):
        """Locate active DeviceRuntime by token/id, or resolve by node_id / label across active sessions."""
        server = self._get_server_module()
        target_norm = (device_id or "").strip().lower()
        if not target_norm:
            return None

        # 1. Direct tuple match
        if session_token:
            rt = server._DEVICE_RUNTIMES.get((session_token, device_id))
            if rt and rt.device:
                return rt

        # 2. Match within the same session by label or case-insensitive node_id
        if session_token:
            for (tok, d_id), rt in list(server._DEVICE_RUNTIMES.items()):
                if tok == session_token:
                    if d_id.lower() == target_norm:
                        return rt
                    meta = server.DEVICES_BY_TOKEN.get(tok, {}).get(d_id, {})
                    if meta.get("label", "").lower() == target_norm:
                        return rt

        # 3. Global fallback across any active token
        for (tok, d_id), rt in list(server._DEVICE_RUNTIMES.items()):
            if d_id.lower() == target_norm:
                return rt
            meta = server.DEVICES_BY_TOKEN.get(tok, {}).get(d_id, {})
            if meta.get("label", "").lower() == target_norm:
                return rt

        return None

    def _node_type_for(self, session_token: Optional[str], device_id: str) -> str:
        """Inventory node type for a device id or label ("" when unknown)."""
        server = self._get_server_module()
        norm = (device_id or "").strip().lower()
        if not norm:
            return ""
        token_maps = []
        if session_token and session_token in server.DEVICES_BY_TOKEN:
            token_maps.append(server.DEVICES_BY_TOKEN[session_token])
        for tok, devs in server.DEVICES_BY_TOKEN.items():
            if tok != session_token:
                token_maps.append(devs)
        for devs in token_maps:
            for d_id, meta in (devs or {}).items():
                if (str(d_id).lower() == norm
                        or str((meta or {}).get("label") or "").lower() == norm):
                    return str((meta or {}).get("type") or "")
        return ""

    def _execute_read_only_command(self, session_token: Optional[str], device_id: str, command: str) -> str:
        """Safely execute a read-only verification command on the device runtime."""
        # Validate that the command is strictly read-only
        cmd_clean = command.strip().lower()
        if not cmd_clean.startswith("show "):
            from cliq.ai import device_exec as _device_exec
            platform = _device_exec.classify_platform(
                self._node_type_for(session_token, device_id))
            verdict = _device_exec.validate_command_for_platform(command, platform)
            if not verdict.ok or not verdict.read_only:
                raise ValueError(
                    "Only read-only 'show' commands (or platform-validated "
                    f"read-only diagnostics) are permitted by the AI context service: {command}")

        runtime = self._resolve_runtime(session_token, device_id)
        if not runtime or not runtime.device:
            return f"[Device {device_id} is not currently connected in this terminal session]"

        try:
            device = runtime.device
            # Execute command safely via device
            if hasattr(device, "execute"):
                raw = device.execute(command)
            elif hasattr(device, "send_command"):
                raw = device.send_command(command)
            elif hasattr(device, "execute_paged"):
                raw, _ = device.execute_paged(command)
            else:
                raw = "[Device driver does not support direct command query]"
            return redact_config_secrets(str(raw))
        except Exception as ex:
            log.warning("Failed to query read-only command '%s' on %s: %s", command, device_id, ex)
            return f"[Error querying device: {str(ex)[:80]}]"

    def get_interfaces(self, session_token: Optional[str] = None, device_id: str = "") -> str:
        """Retrieve sanitized interface status (platform-aware command)."""
        from cliq.ai import device_exec as _device_exec
        platform = _device_exec.classify_platform(
            self._node_type_for(session_token, device_id))
        command = ("ifconfig -a" if platform == _device_exec.PLATFORM_LINUX
                   else "show ip interface brief")
        return self._execute_read_only_command(session_token, device_id, command)

    def get_running_config(self, session_token: Optional[str] = None, device_id: str = "") -> str:
        """Retrieve sanitized running configuration with secrets redacted."""
        return self._execute_read_only_command(session_token, device_id, "show running-config")

    def get_routing_table(self, session_token: Optional[str] = None, device_id: str = "") -> str:
        """Retrieve routing table."""
        return self._execute_read_only_command(session_token, device_id, "show ip route")

    def get_arp_table(self, session_token: Optional[str] = None, device_id: str = "") -> str:
        """Retrieve ARP cache."""
        return self._execute_read_only_command(session_token, device_id, "show ip arp")

    def get_neighbors(self, session_token: Optional[str] = None, device_id: str = "") -> str:
        """Retrieve CDP / LLDP neighbor discovery."""
        cdp = self._execute_read_only_command(session_token, device_id, "show cdp neighbors")
        if "invalid" in cdp.lower() or "error" in cdp.lower():
            return self._execute_read_only_command(session_token, device_id, "show lldp neighbors")
        return cdp

    def get_recent_commands(self, session_token: Optional[str] = None, device_id: str = "") -> List[str]:
        """Fetch recently entered commands from session operational memory."""
        try:
            from cliq.memory.store import SessionLog
            s = SessionLog()
            # Try to match device key
            state = s.load_state(device_id)
            if state:
                return s.get_history(device_id, limit=20)
        except Exception as ex:
            log.debug("Could not read session log history: %s", ex)
        return []


# Global context service instance
_network_context_service = NetworkContextService()

def get_network_context_service() -> NetworkContextService:
    return _network_context_service
