#!/usr/bin/env python3
"""
transport.py — Common device-transport interface for CLIQ.

CLIQ's intelligence stack (``?``-help capture, ghost-text, device-over-KB
reconciliation, learning, capability gating, pager, abbreviation expansion) is
transport-agnostic: it only ever talks to a *console device object* through a
small, stable surface. This module documents that surface as a runtime-checkable
:class:`DeviceTransport` protocol so any backend can plug in **without changing
a single line of the intelligence stack**.

Two backends conform to it today (structurally — no inheritance, so each
backend stays independent):

* :class:`cliq.connection.live_device.CMLConsoleDevice`
      — SSH to a CML terminal-server console proxy (existing, UNCHANGED).
* :class:`cliq.connection.ssh_console_device.SSHConsoleDevice`
      — netmiko SSH to a lab terminal server with a port-mapped username, for
        REAL hardware (e.g. an ISR4331 console).

The cardinal rule is identical across every transport: **the live device is the
source of truth.** A transport MUST send exactly the bytes CLIQ hands it
(input integrity), MUST never submit a command during a ``?``-help capture
(never-execute), and MUST surface the device's own output/errors faithfully so
device > cache > KB reconciliation holds on any wire.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class DeviceTransport(Protocol):
    """The console-device surface every CLIQ transport backend must expose.

    Any object providing these members works unchanged with the WebSocket
    command loop and the whole advisory/ghost/learning stack. The protocol is
    ``runtime_checkable`` so a backend can be asserted to conform in tests
    (``isinstance(dev, DeviceTransport)``) without subclassing.
    """

    # ── Identity / capability (populated on connect) ──────────────────────
    #: Normalized device/node type token used to key the persistent command
    #: cache and did-you-mean platform reconciliation (e.g. ``"isr4331"`` for
    #: real HW, ``"iosv"`` for CML). Empty until known.
    node_type: str

    # ── State ─────────────────────────────────────────────────────────────
    @property
    def is_connected(self) -> bool:
        """True once a live session is established and ready for commands."""
        ...

    @property
    def current_prompt(self) -> str:
        """The last device prompt seen, e.g. ``"Router#"`` / ``"R1(config-if)#"``."""
        ...

    @property
    def awaiting_auth(self) -> bool:
        """True while parked at an interactive login prompt awaiting creds."""
        ...

    # ── Lifecycle ─────────────────────────────────────────────────────────
    def connect(self) -> None:
        """Establish the session and leave the device at a known clean prompt.

        May raise :class:`cliq.connection.live_device.InteractiveAuthRequired`
        when the device parks at an interactive login prompt; the caller then
        drives :meth:`complete_login`.
        """
        ...

    def reattach(self) -> str:
        """Re-establish a dropped session and return the resumed CLI mode."""
        ...

    def complete_login(self, username: str, password: str) -> None:
        """Finish a parked interactive login with operator-supplied creds.

        Credentials are used in-memory only and MUST never be logged/persisted.
        """
        ...

    def disconnect(self) -> None:
        """Tear the session down; best-effort, never raises."""
        ...

    # ── Command execution (input integrity + never-execute apply) ─────────
    def execute(self, command: str) -> str:
        """Run ``command`` and return the cleaned device output."""
        ...

    def execute_paged(self, command: str, on_chunk=None) -> tuple[str, bool]:
        """Run ``command``; return ``(page_text, more)`` where ``more`` marks a
        pending ``--More--`` pager. ``on_chunk`` streams cleaned deltas (UX)."""
        ...

    def pager_send_key(self, key: str) -> tuple[str, bool]:
        """Send a single raw pager key (space/enter/q); return ``(page, more)``."""
        ...

    # ── Context ('?') help — advisory, never submits a command ────────────
    def context_help_start(self, partial: str) -> tuple[str, bool]:
        """Capture the FIRST ``?``-help page for ``partial``; ``(page, more)``."""
        ...

    def context_help_more(self, key: str = " ") -> tuple[str, bool]:
        """Stream the NEXT ``?``-help page; ``(page, more)``."""
        ...

    # ── Health ────────────────────────────────────────────────────────────
    def _check_connection(self) -> None:
        """Raise ``ConnectionLostError`` if the channel appears dead."""
        ...
