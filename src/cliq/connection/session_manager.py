# src/cliq/connection/session_manager.py

import logging
import threading
import time
from dataclasses import dataclass, field
from collections import deque

log = logging.getLogger("session_manager")


@dataclass
class DeviceSession:
    label: str
    device: object                        # CMLConsoleDevice
    mode: str = "exec"
    connected: bool = False
    lab_id: str = ""
    node_id: str = ""
    command_history: list = field(default_factory=list)
    output_buffer: deque = field(
        default_factory=lambda: deque(maxlen=500)
    )
    _lock: threading.Lock = field(
        default_factory=threading.Lock
    )


class SessionManager:
    """
    Manages multiple simultaneous device connections.
    Each device is a DeviceSession with its own state.
    The IntelligenceEngine is SHARED (one instance, all devices).
    """

    def __init__(self, lab_id: str = ""):
        self.sessions: dict[str, DeviceSession] = {}
        self.active_label: str = None
        self.gateway_host: str = None
        self.gateway_username: str = None
        self.gateway_password: str = None
        self.lab_id = lab_id
        # Persistent operational memory (history, mode, typo stats) that
        # survives CLIQ restarts. Keyed by lab_id/node_id.
        from cliq.memory.store import SessionLog
        self.log = SessionLog()

    def add(self, label: str, host: str, username: str,
            password: str, path: str, node_id: str = ""):
        """Register a device (does not connect yet)."""
        from cliq.connection.live_device import CMLConsoleDevice
        device = CMLConsoleDevice(host, username, password, path)
        self.sessions[label] = DeviceSession(
            label=label, device=device,
            lab_id=self.lab_id, node_id=node_id or label,
        )
        log.info(f"Registered device: {label}")

    def _session_key(self, session: "DeviceSession") -> str:
        from cliq.memory.store import SessionLog
        return SessionLog.make_key(session.lab_id, session.node_id)

    def _restore_memory(self, session: "DeviceSession") -> None:
        """Reload command history + last mode for a device from session_log."""
        key = self._session_key(session)
        state = self.log.load_state(key)
        if state:
            session.mode = state.mode or session.mode
            session.command_history = self.log.get_history(key)
            log.info("Restored %d history entries for %s (mode=%s)",
                     len(session.command_history), session.label, session.mode)

    def connect(self, label: str):
        """Connect a specific device (lazy — called on first open)."""
        session = self.sessions.get(label)
        if not session:
            raise KeyError(f"Unknown device: {label}")
        from cliq.utils import observability as obs
        _t0 = time.monotonic()
        session.device.connect()
        session.connected = True
        obs.log_event(
            "terminal", "session_connect", "complete", "success",
            elapsed_ms=(time.monotonic() - _t0) * 1000.0,
        )
        # Resume CLIQ's operational memory and detect the device's live mode.
        from cliq.connection.live_device import mode_from_prompt
        session.mode = mode_from_prompt(session.device.current_prompt)
        self._restore_memory(session)
        self.log.save_state(
            self._session_key(session), session.lab_id, session.node_id,
            hostname=session.device.current_prompt, mode=session.mode,
        )
        if not self.active_label:
            self.active_label = label
        log.info(f"Connected: {label}")

    def reconnect(self, label: str) -> str:
        """Re-attach to an existing console session after a CLIQ restart/reload.

        Re-attaches via invoke_shell, probes the live prompt to detect the
        current hostname/mode, and restores command history + typo stats from
        session_log.sqlite. Returns the resumed mode.
        """
        session = self.sessions.get(label)
        if not session:
            raise KeyError(f"Unknown device: {label}")
        mode = session.device.reattach()
        session.mode = mode
        session.connected = True
        self._restore_memory(session)
        self.log.save_state(
            self._session_key(session), session.lab_id, session.node_id,
            hostname=session.device.current_prompt, mode=mode,
        )
        log.info("Reconnected %s — resumed in mode '%s'", label, mode)
        return mode

    def switch(self, label: str):
        """Switch active device (tab switch in UI)."""
        if label not in self.sessions:
            raise KeyError(f"Unknown device: {label}")
        self.active_label = label
        log.info(f"Active device: {label}")

    def execute(self, command: str) -> str:
        """Execute on active device."""
        session = self.active_session
        if not session:
            return "[error] No active device selected."
        if not session.connected:
            return f"[error] {session.label} not connected."
        with session._lock:
            output = session.device.execute(command)
            session.command_history.append(command)
            session.output_buffer.append(
                (command, output)
            )
            # Persist to durable memory and track the live mode after the cmd.
            from cliq.connection.live_device import mode_from_prompt
            session.mode = mode_from_prompt(session.device.current_prompt)
            key = self._session_key(session)
            self.log.append_command(key, command)
            self.log.save_state(
                key, session.lab_id, session.node_id,
                hostname=session.device.current_prompt, mode=session.mode,
            )
            return output

    def execute_on(self, label: str, command: str) -> str:
        """Execute on a specific device."""
        prev = self.active_label
        self.active_label = label
        result = self.execute(command)
        self.active_label = prev
        return result

    def get_mode(self, label: str = None) -> str:
        session = self.sessions.get(
            label or self.active_label
        )
        return session.mode if session else "exec"

    def set_mode(self, mode: str, label: str = None):
        session = self.sessions.get(
            label or self.active_label
        )
        if session:
            session.mode = mode

    def list_devices(self) -> list:
        """Returns [(label, connected), ...] sorted."""
        return sorted(
            [(label, s.connected)
             for label, s in self.sessions.items()],
            key=lambda x: x[0]
        )

    def disconnect_all(self):
        for label, session in self.sessions.items():
            if session.connected:
                try:
                    session.device.disconnect()
                except Exception:
                    pass

    @property
    def active_session(self) -> DeviceSession:
        return self.sessions.get(self.active_label)

    @property
    def device_labels(self) -> list:
        return list(self.sessions.keys())