#!/usr/bin/env python3
"""
cml_events.py — optional real-time CML state via ``virl2_client`` EventListener.

Phase A #4. This is the ONE architectural addition of Phase A and it is kept
strictly **additive and feature-flagged**:

  * Default OFF. Enable with ``CLIQ_CML_EVENTS=1`` (see
    :func:`cliq.utils.observability.event_listener_enabled`).
  * When OFF, nothing here runs — CLIQ keeps using its existing polling /
    ``sync_*_if_outdated`` refresh and the validated terminal path is untouched.
  * When ON, it subscribes to the controller's event stream and pushes
    node/link/lab state changes into a callback (so CLIQ can update its cached
    topology model without polling). Every event is timed in the unified
    observability schema (``layer="event"``).
  * It NEVER touches the terminal/console path. It only updates an in-memory
    state cache via the supplied callback.

The exact ``virl2_client`` event API surface has varied across 2.x releases, so
this wrapper discovers the available entry points at runtime and degrades to a
clean no-op (logging ``unavailable``) if the installed client does not expose
them. A misbehaving listener can be stopped instantly via :meth:`stop` or by
unsetting the flag — protecting the polling baseline for A/B comparison.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Optional

from cliq.utils import observability as obs

log = logging.getLogger("cliq.connection.cml_events")

#: Signature of the state-change callback CLIQ supplies. Receives a normalized
#: dict: {"element": "node"|"link"|"lab", "id": str, "state": str, "raw": Any}.
StateChangeCallback = Callable[[dict[str, Any]], None]


def _lazy_event_api():
    """Import the virl2_client event API lazily. Returns module or None."""
    try:
        import virl2_client.event_listening as ev  # type: ignore
        return ev
    except Exception:  # noqa: BLE001 - optional dependency / API absent
        return None


class CMLEventMonitor:
    """Feature-flagged real-time CML state monitor.

    Parameters
    ----------
    client:
        A connected ``virl2_client.ClientLibrary`` instance (from
        :class:`cliq.connection.cml_client.CMLClient`).
    on_state_change:
        Callback invoked for each normalized node/link/lab state event. CLIQ
        uses it to update its cached topology model. Exceptions in the callback
        are swallowed so a bad consumer can never crash the listener.
    enabled:
        Override the feature flag (defaults to the ``CLIQ_CML_EVENTS`` flag).
    """

    def __init__(
        self,
        client: Any,
        on_state_change: Optional[StateChangeCallback] = None,
        *,
        enabled: Optional[bool] = None,
    ):
        self._client = client
        self._on_state_change = on_state_change
        self._enabled = obs.event_listener_enabled() if enabled is None else bool(enabled)
        self._listener: Any = None
        self._running = False
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def running(self) -> bool:
        return self._running

    # ── lifecycle ────────────────────────────────────────────────────────
    def start(self) -> bool:
        """Start listening. Returns True if the listener is active.

        Non-fatal: any failure logs ``unavailable``/``error`` and returns False,
        leaving CLIQ on its polling path (no regression).
        """
        if not self._enabled:
            obs.log_event("event", "listener", "start", "disabled",
                          detail="CLIQ_CML_EVENTS not set")
            return False
        with self._lock:
            if self._running:
                return True
            ev = _lazy_event_api()
            if ev is None:
                obs.log_event("event", "listener", "start", "unavailable",
                              detail="virl2_client event API not importable")
                return False
            t0 = time.monotonic()
            try:
                self._listener = self._build_listener(ev)
                if self._listener is None:
                    obs.log_event("event", "listener", "start", "unavailable",
                                  detail="no compatible EventListener entry point")
                    return False
                self._start_listener(self._listener)
                self._running = True
                obs.log_event(
                    "event", "listener", "start", "success",
                    elapsed_ms=(time.monotonic() - t0) * 1000.0,
                )
                return True
            except Exception as exc:  # noqa: BLE001 - never fatal
                obs.log_event(
                    "event", "listener", "start", "error",
                    elapsed_ms=(time.monotonic() - t0) * 1000.0,
                    detail=type(exc).__name__,
                )
                self._listener = None
                self._running = False
                return False

    def stop(self) -> None:
        """Stop listening cleanly and idempotently (restores polling baseline)."""
        with self._lock:
            listener = self._listener
            self._listener = None
            self._running = False
        if listener is None:
            return
        for name in ("stop", "stop_listening", "disconnect", "close"):
            fn = getattr(listener, name, None)
            if callable(fn):
                try:
                    fn()
                    break
                except Exception:  # noqa: BLE001 - teardown must not raise
                    pass
        obs.log_event("event", "listener", "stop", "success")

    def __enter__(self) -> "CMLEventMonitor":
        self.start()
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.stop()

    # ── event handling ───────────────────────────────────────────────────
    def _handle_raw_event(self, event: Any) -> None:
        """Normalize one raw event, time it, and forward to the callback.

        ``elapsed_ms`` measures listener-side handling latency (receipt →
        callback complete), giving a real-time-vs-polling comparison signal.
        """
        t0 = time.monotonic()
        try:
            norm = _normalize_event(event)
            if self._on_state_change is not None and norm is not None:
                try:
                    self._on_state_change(norm)
                except Exception:  # noqa: BLE001 - bad consumer must not crash us
                    pass
            obs.log_event(
                "event", "state_change",
                (norm or {}).get("element", "unknown"),
                "success",
                elapsed_ms=(time.monotonic() - t0) * 1000.0,
                detail=(norm or {}).get("state", ""),
            )
        except Exception as exc:  # noqa: BLE001
            obs.log_event("event", "state_change", "handle", "error",
                          detail=type(exc).__name__)

    # ── virl2_client API discovery (defensive) ───────────────────────────
    def _build_listener(self, ev: Any) -> Any:
        """Construct an EventListener wired to ``_handle_raw_event``.

        Tries the documented entry points in order; returns None if none work.
        """
        # 1) Subclass EventHandler if available and register it on the client.
        handler = self._build_handler(ev)

        # 2a) Client exposes a ready-made event_listener.
        listener = getattr(self._client, "event_listener", None)
        if listener is not None and handler is not None:
            for add in ("add_handler", "add_listener", "subscribe", "register"):
                fn = getattr(listener, add, None)
                if callable(fn):
                    fn(handler)
                    return listener

        # 2b) Construct EventListener directly.
        EventListener = getattr(ev, "EventListener", None)
        if EventListener is not None:
            try:
                inst = EventListener(self._client)
            except Exception:  # noqa: BLE001 - try the no-arg form
                inst = EventListener()
            if handler is not None:
                for add in ("add_handler", "add_listener", "subscribe", "register"):
                    fn = getattr(inst, add, None)
                    if callable(fn):
                        fn(handler)
                        break
            return inst
        return None

    def _build_handler(self, ev: Any) -> Any:
        """Subclass the client's EventHandler so callbacks reach us."""
        base = getattr(ev, "EventHandler", None) or getattr(ev, "EventHandlerBase", None)
        if base is None:
            return None
        monitor = self

        class _CliqEventHandler(base):  # type: ignore[misc, valid-type]
            def parse_event(self, event: Any) -> Any:  # noqa: D401
                monitor._handle_raw_event(event)
                try:
                    return super().parse_event(event)
                except Exception:  # noqa: BLE001
                    return None

        try:
            return _CliqEventHandler()
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _start_listener(listener: Any) -> None:
        for name in ("start", "start_listening", "connect", "run"):
            fn = getattr(listener, name, None)
            if callable(fn):
                fn()
                return
        # Some implementations begin listening on construction; that's fine.


def _normalize_event(event: Any) -> Optional[dict[str, Any]]:
    """Best-effort normalization of a virl2_client event into CLIQ's shape."""
    if event is None:
        return None

    def g(*names: str) -> Any:
        for n in names:
            v = getattr(event, n, None)
            if v is None and isinstance(event, dict):
                v = event.get(n)
            if v is not None:
                return v
        return None

    element_type = str(g("element_type", "type", "event_type") or "unknown").lower()
    if "node" in element_type:
        element = "node"
    elif "link" in element_type:
        element = "link"
    elif "lab" in element_type:
        element = "lab"
    else:
        element = element_type or "unknown"

    return {
        "element": element,
        "id": str(g("element_id", "id", "uuid") or ""),
        "state": str(g("state", "new_state", "status") or ""),
        "raw": None,  # raw object deliberately dropped (avoid logging payloads)
    }


__all__ = ["CMLEventMonitor", "StateChangeCallback"]
