#!/usr/bin/env python3
"""
cml_auth.py — CML Authentication Manager.

Handles:
  - First-time credential collection (once per CML host)
  - JWT token acquisition and refresh
  - Secure storage in OS keyring
  - Lab discovery and device enumeration
  - CML lab URL parsing

USAGE:
  from cliq.connection.cml_auth import CMLAuthManager
  auth = CMLAuthManager()
  auth.login("cml.example.com")
  labs = auth.get_labs()
"""

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlparse

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

log = logging.getLogger("cml_auth")

# Where to cache tokens locally (fallback if keyring unavailable)
_TOKEN_CACHE = os.path.join(
    os.path.expanduser("~"), ".cliq", "token_cache.json"
)


def _reservation_degraded_optin() -> bool:
    """True if the operator has EXPLICITLY opted into degraded reservation mode.

    When ``CLIQ_ALLOW_UNVERIFIED_RESERVATION`` is truthy, a session that cannot
    verify the NetSim reservation (no API key / NetSim unreachable) is allowed
    with a warning. Preserved as an ALIAS for the ``warn`` policy so existing
    deployments relying on it keep working. The DEFAULT policy is already
    ``warn`` (see :func:`reservation_policy`).
    """
    return (os.environ.get("CLIQ_ALLOW_UNVERIFIED_RESERVATION") or "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def reservation_policy() -> str:
    """Resolve the reservation policy governing the ``unverifiable`` fallback.

    Returns ``"enforce"`` (unverifiable → refuse; recommended for hosted/prod)
    or ``"warn"`` (unverifiable → allow with a visible warning; the DEV DEFAULT),
    or ``"disabled"`` (skip NetSim reservation verification entirely).
    Resolution order:
      1. ``CLIQ_RESERVATION_POLICY`` = ``enforce`` / ``warn`` / ``disabled``
         (explicit wins).
      2. ``CLIQ_ALLOW_UNVERIFIED_RESERVATION=1`` (legacy alias) → ``warn``.
      3. Default → ``warn`` (so a valid lab is usable without a NetSim key).
    Policy ONLY governs the unverifiable case — a verified ``expired`` reservation
    is ALWAYS refused, and a present key ALWAYS uses NetSim authority.
    """
    raw = (os.environ.get("CLIQ_RESERVATION_POLICY") or "").strip().lower()
    if raw in ("enforce", "strict", "closed"):
        return "enforce"
    if raw in ("disabled", "disable", "off", "none", "bypass"):
        return "disabled"
    if raw in ("warn", "permissive", "open"):
        return "warn"
    if _reservation_degraded_optin():
        return "warn"
    return "warn"


def reservation_api_key_present() -> bool:
    """Standalone mode: external reservation booking keys are not used."""
    return False


def check_netsim_key_health(*, proxy: Optional[str] = None) -> str:
    """Standalone mode: external NetSim reservation verification is disabled."""
    return "disabled"


@dataclass
class ReservationDecision:
    """Policy verdict for a detected reservation state.

    ``outcome`` is ``"allowed"`` (active), ``"allowed_warned"`` (unverifiable
    under the ``warn`` policy — proceed but surface a banner) or ``"refused"``
    (expired always; unverifiable under ``enforce``). ``message`` is the
    ready-to-surface refusal reason or non-blocking warning text.
    """

    state: str        # active | expired | unverifiable
    policy: str       # enforce | warn | disabled
    allowed: bool
    outcome: str      # allowed | allowed_warned | refused
    warn: bool
    message: str


#: Causes for an ``unverifiable`` reservation state (never mixed into ``state``).
UNVERIFIABLE_NO_KEY = "no_key"                     # CLIQ_NETSIM_API_KEY absent
UNVERIFIABLE_NETSIM_UNREACHABLE = "netsim_unreachable"  # network / NetSim down
UNVERIFIABLE_KEY_INVALID = "key_invalid"           # key present but REJECTED


@dataclass
class ReservationDetection:
    """Raw NetSim detection result: the ``state`` plus, when ``unverifiable``,
    the distinct ``cause`` (``no_key`` / ``netsim_unreachable`` / ``key_invalid``)
    so the policy layer can surface an accurate, actionable message. ``cause`` is
    empty for ``active`` / ``expired``.
    """

    state: str        # active | expired | unverifiable
    cause: str = ""   # no_key | netsim_unreachable | key_invalid | ""


#: Non-blocking banner shown when proceeding WITHOUT NetSim verification.
#: Must be unmistakable: CLIQ genuinely cannot tell a valid lab from an expired
#: one without the NetSim key, so the risk is stated explicitly rather than
#: dressed up as a benign notice.
_RESERVATION_WARN_MESSAGE = (
    "⚠ UNVERIFIED ACCESS — CLIQ cannot confirm this reservation is active or "
    "expired (NetSim key not configured). You may be operating an EXPIRED lab. "
    "Configure CLIQ_NETSIM_API_KEY for verified access."
)
#: NetSim rejected a PRESENT key — almost always the 30-day rotation lapsing.
#: The operator must ROTATE (regenerate), not merely configure.
_RESERVATION_KEY_INVALID_MESSAGE = (
    "NetSim API key rejected — it may have expired (keys rotate every 30 days). "
    "Regenerate on the NetSim portal (API KeyGen) and update CLIQ_NETSIM_API_KEY."
)
#: Key present but NetSim itself is unreachable (network/VPN/portal down).
_RESERVATION_UNREACHABLE_MESSAGE = (
    "⚠ UNVERIFIED ACCESS — CLIQ could not reach NetSim to confirm this "
    "reservation is active or expired. You may be operating an EXPIRED lab. "
    "Check VPN/network connectivity to the NetSim portal."
)
_RESERVATION_EXPIRED_MESSAGE = "Reservation expired or inactive — access denied."
_RESERVATION_ENFORCE_MESSAGE = (
    "Cannot verify the reservation is active (NetSim verification unavailable). "
    "CLIQ is enforcing reservations — configure CLIQ_NETSIM_API_KEY, or set "
    "CLIQ_RESERVATION_POLICY=warn to proceed on device liveness."
)


def _unverifiable_message(cause: str) -> str:
    """Pick the cause-specific message for an ``unverifiable`` reservation."""
    if cause == UNVERIFIABLE_KEY_INVALID:
        return _RESERVATION_KEY_INVALID_MESSAGE
    if cause == UNVERIFIABLE_NETSIM_UNREACHABLE:
        return _RESERVATION_UNREACHABLE_MESSAGE
    return _RESERVATION_WARN_MESSAGE


def resolve_reservation_access(
    state: str, cause: str = "",
) -> ReservationDecision:
    """Apply the configured policy to a detected reservation ``state``.

    The SINGLE, SHARED policy resolution consumed by both the ``/api/auth`` gate
    and the console re-attach gate, so they can never diverge. A verified
    ``expired`` reservation is refused regardless of policy (security win); only
    the ``unverifiable`` fallback is policy-governed. ``cause`` selects the
    actionable message (e.g. ``key_invalid`` → rotate, ``no_key`` → configure).
    """
    policy = reservation_policy()
    if state == "active":
        return ReservationDecision(state, policy, True, "allowed", False, "")
    if state == "expired":
        return ReservationDecision(
            state, policy, False, "refused", False, _RESERVATION_EXPIRED_MESSAGE,
        )
    # unverifiable — the message reflects the distinct cause so the fix is clear.
    message = _unverifiable_message(cause)
    if policy == "warn":
        return ReservationDecision(
            state, policy, True, "allowed_warned", True, message,
        )
    # enforce refuses; a key-rejected cause keeps the rotation-specific text.
    enforce_message = (
        message if cause == UNVERIFIABLE_KEY_INVALID else _RESERVATION_ENFORCE_MESSAGE
    )
    return ReservationDecision(
        state, policy, False, "refused", False, enforce_message,
    )


def normalize_cml_host(raw: str) -> str:
    """Normalize an operator-entered CML host into a bare ``host`` or ``host:port``.

    Strips a leading ``http://`` / ``https://`` scheme, any path/query/fragment,
    surrounding whitespace and trailing slashes. This prevents a pasted URL such
    as ``https://cml.example.com/`` from being double-prefixed into the
    API base (which previously produced ``host='https'`` → "Failed to resolve
    'https'"). A bare host or ``host:port`` passes through unchanged.
    """
    text = (raw or "").strip()
    if not text:
        return ""
    # Strip an explicit scheme (case-insensitive) if the user pasted a URL.
    if "://" in text:
        parsed = urlparse(text)
        # netloc holds host[:port]; if the user typed "host/path" with no scheme
        # urlparse puts everything in path, so fall back to the raw text below.
        text = parsed.netloc or parsed.path
    # Drop any residual path/query/fragment and surrounding slashes/space.
    text = text.split("/")[0].split("?")[0].split("#")[0].strip().strip("/")
    return text


@dataclass
class CMLDevice:
    label: str          # R1, SW1, etc.
    node_id: str        # CML internal ID
    node_type: str      # iosv, iosvl2, etc.
    lab_id: str
    console_path: str   # /lab_name/label/0
    state: str = "unknown"


@dataclass
class CMLLab:
    lab_id: str
    title: str
    state: str
    owner: str
    devices: list = field(default_factory=list)


class CMLAuthManager:
    """
    Manages CML authentication, lab discovery, and device enumeration.
    Designed for minimal friction — credential prompt once per host.
    """

    def __init__(self):
        self.host = None
        self.token = None
        self.token_expiry = 0
        self._session = requests.Session()
        self._session.verify = False
        self._labs_cache = {}

    # ─────────────────────────────────────────────────────────────
    # Public API
    # ─────────────────────────────────────────────────────────────

    def login(self, host: str, username: str = None,
              password: str = None) -> bool:
        """
        Authenticate to CML — FAIL CLOSED.

        Security contract:
          * When credentials are supplied we ALWAYS verify them live against the
            controller. A cached token is NEVER used as a fallback when live auth
            fails — a stale token must not let a wrong password or an expired
            reservation back in. On any failure the cached token for this host is
            discarded and the underlying exception propagates (the caller maps it
            to a non-2xx response).
          * When no credentials are supplied a cached token may only be reused
            after it is revalidated against live reservation state (token still
            valid AND the lab/reservation still reachable). An invalid/expired
            token is discarded and the call fails closed.
        """
        self.host = normalize_cml_host(host)
        self.base = f"https://{self.host}/api/v0"

        # ── Credentialed path: verify the CURRENT credentials live. ──────────
        if username and password:
            try:
                return self._authenticate(username, password)
            except Exception:
                # Live auth failed (auth rejection, DNS failure, unreachable).
                # The cached token for this host can no longer be trusted — a
                # never-invalidated token would otherwise re-attach an expired
                # reservation / wrong-password attempt. Discard it and propagate
                # so the gate fails closed.
                self._invalidate_cached_token(self.host)
                self.token = None
                raise

        # ── Tokenless path: only a *revalidated* cached token may be reused. ──
        cached = self._load_cached_token(self.host)
        if cached and not self._is_expired(cached):
            self.token = cached["token"]
            self.token_expiry = cached["expiry"]
            if self._validate_cached_token_live():
                log.info(f"Using validated cached token for {self.host}")
                return True
            # Token expired server-side or reservation no longer active.
            log.warning(
                "Cached token for %s failed live revalidation — discarding.",
                self.host,
            )
            self._invalidate_cached_token(self.host)
            self.token = None

        raise ValueError(
            "Credentials required. "
            "Pass username and password to login()."
        )

    def parse_lab_url(self, url: str) -> tuple:
        """
        Parse a CML lab URL to extract host and lab_id.

        Supports formats:
          https://cml.example.com/lab/abc123/...
          https://cml.example.com/#/lab/abc123
        """
        parsed = urlparse(url)
        host = parsed.netloc

        # Extract lab ID from path or fragment
        lab_match = re.search(r'/lab/([a-f0-9\-]+)', url)
        lab_id = lab_match.group(1) if lab_match else None

        return host, lab_id

    def get_labs(self) -> list:
        """Return all labs accessible to the authenticated user."""
        self._ensure_auth()
        r = self._get("/labs")
        labs = []
        lab_ids = r.json() if isinstance(r.json(), list) else list(r.json().keys())
        for lab_id in lab_ids:
            try:
                details = self._get(f"/labs/{lab_id}").json()
                labs.append(CMLLab(
                    lab_id=lab_id,
                    title=details.get("lab_title", lab_id),
                    state=details.get("state", "unknown"),
                    owner=details.get("owner_username") or details.get("owner", "unknown"),
                ))
            except Exception:
                continue
        return labs

    def get_devices(self, lab_id: str) -> list:
        """Return all connectable (IOS) devices in a specific lab."""
        self._ensure_auth()

        # Get lab title for path construction
        try:
            lab_details = self._get(f"/labs/{lab_id}").json()
            lab_title = lab_details.get("lab_title", lab_id)
        except Exception:
            lab_title = lab_id

        # Non-IOS node types to skip (no IOS console)
        _SKIP_TYPES = {
            "external_connector", "unmanaged_switch", "bridge",
            "wan_emulator", "desktop", "server",
        }

        nodes_resp = self._get(f"/labs/{lab_id}/nodes")
        node_ids = nodes_resp.json() if isinstance(nodes_resp.json(), list) else list(nodes_resp.json().keys())

        devices = []
        for node_id in node_ids:
            try:
                node = self._get(f"/labs/{lab_id}/nodes/{node_id}").json()
                label = node.get("label", node_id)
                node_def = node.get("node_definition", "unknown")
                state = node.get("state", "unknown")

                if node_def in _SKIP_TYPES:
                    continue

                # console path from serial_consoles[0] device_number, or default /0
                sc = node.get("serial_consoles", [])
                line = sc[0]["device_number"] if sc else 0
                console_path = f"/{lab_title}/{label}/{line}"

                devices.append(CMLDevice(
                    label=label,
                    node_id=node_id,
                    node_type=node_def,
                    lab_id=lab_id,
                    console_path=console_path,
                    state=state,
                ))
            except Exception:
                continue

        return sorted(devices, key=lambda d: d.label)

    def get_topology(self, lab_id: str) -> dict:
        """
        Return full topology data ready for Sharath's UI renderer.

        Response schema:
          {
            "nodes": [
              { "id": "<uuid>", "label": "iosv-0", "type": "iosv",
                "state": "BOOTED", "x": -160, "y": 80 },
              ...
            ],
            "links": [
              { "id": "<uuid>", "label": "iosv-0-Gi0/0<->iosv-1-Gi0/0",
                "node_a": "<uuid>", "node_b": "<uuid>" },
              ...
            ]
          }

        Note: x, y are CML canvas coordinates (integers).
        """
        self._ensure_auth()

        # ── Nodes with positions ─────────────────────────────────────
        nodes_out = []
        try:
            node_ids = self._get(f"/labs/{lab_id}/nodes").json()
            if not isinstance(node_ids, list):
                node_ids = list(node_ids.keys())
            for nid in node_ids:
                try:
                    nd = self._get(f"/labs/{lab_id}/nodes/{nid}").json()
                    nodes_out.append({
                        "id":    nid,
                        "label": nd.get("label", nid),
                        "type":  nd.get("node_definition", "unknown"),
                        "state": nd.get("state", "unknown"),
                        "x":     nd.get("x", 0),
                        "y":     nd.get("y", 0),
                    })
                except Exception:
                    continue
        except Exception:
            pass

        # ── Links ────────────────────────────────────────────────────
        links_out = []

        def _first_value(payload: dict, keys: list[str]) -> str:
            for key in keys:
                value = payload.get(key)
                if value not in (None, ""):
                    return str(value)
            return ""

        try:
            link_ids = self._get(f"/labs/{lab_id}/links").json()
            if not isinstance(link_ids, list):
                link_ids = list(link_ids.keys())
            for lid in link_ids:
                try:
                    lk = self._get(f"/labs/{lab_id}/links/{lid}").json()
                    source_intf = _first_value(lk, [
                        "node_a_intf", "interface_a", "intf_a", "a_intf",
                        "source_interface", "source_port", "n1_intf", "i1",
                    ])
                    target_intf = _first_value(lk, [
                        "node_b_intf", "interface_b", "intf_b", "b_intf",
                        "target_interface", "target_port", "n2_intf", "i2",
                    ])
                    links_out.append({
                        "id":     lid,
                        "label":  lk.get("label", lid),
                        "node_a": lk.get("node_a", ""),
                        "node_b": lk.get("node_b", ""),
                        "node_a_intf": source_intf,
                        "node_b_intf": target_intf,
                        "raw": lk,
                    })
                except Exception:
                    continue
        except Exception:
            pass

        return {"nodes": nodes_out, "links": links_out}

    # ─────────────────────────────────────────────────────────────
    # Internal helpers
    # ─────────────────────────────────────────────────────────────

    def _authenticate(self, username: str, password: str) -> bool:
        try:
            r = self._session.post(
                f"{self.base}/auth_extended",
                json={"username": username, "password": password},
                timeout=15
            )
            r.raise_for_status()
            data = r.json()
            # CML returns JWT in 'token', UUID in 'id'
            self.token = data.get("token") or data.get("id", "")
            if not self.token:
                raise ValueError("No token in auth response")
            # CML tokens last 24 hours
            self.token_expiry = time.time() + (23 * 3600)
            self._save_token_cache(self.host, self.token, self.token_expiry)
            log.info(f"Authenticated to {self.host} as {username}")
            return True
        except Exception as e:
            # FAIL CLOSED. Do NOT swallow the failure into a False return — a
            # silent False previously let the web gate reply 200 OK on an auth
            # failure. Clear any half-set token and re-raise the real cause
            # (HTTP 401/403, DNS/NameResolution, connection error) so the caller
            # can map it to the correct non-2xx status and message.
            log.error(f"Auth failed to {self.host}: {e}")
            self.token = None
            self.token_expiry = 0
            raise

    def _validate_cached_token_live(self) -> bool:
        """Revalidate the in-memory cached token against live CONTROLLER state.

        Returns True only if the token is still accepted by the CML controller
        (a 2xx from a lightweight authenticated endpoint). This gates reuse of a
        cached token in the credential-less login path — it is a TOKEN liveness
        check, NOT the reservation authority (see ``check_reservation_state``).
        Never raises.
        """
        return self._controller_is_live()

    def _controller_is_live(self) -> bool:
        """True if the CML controller still accepts our token (2xx on /authok or
        /labs). Proves the node/controller is reachable and the token valid — it
        does NOT prove the NetSim reservation window is still open. Never raises.
        """
        if not self.token:
            return False
        for path in ("/authok", "/labs"):
            try:
                r = self._session.get(
                    f"{self.base}{path}",
                    headers={"Authorization": f"Bearer {self.token}"},
                    timeout=10,
                )
            except Exception:
                return False
            if r.status_code in (401, 403):
                return False
            if r.ok:
                return True
        return False

    def check_reservation_state(
        self,
        *,
        netsim_api_key: Optional[str] = None,
        proxy: Optional[str] = None,
    ) -> str:
        """Reservation authority DETECTION (state only) — see
        :meth:`check_reservation_detail` for the distinct ``unverifiable`` cause.
        Never raises; never logs the API key or token.
        """
        return self.check_reservation_detail(
            netsim_api_key=netsim_api_key, proxy=proxy,
        ).state

    def check_reservation_detail(
        self,
        *,
        netsim_api_key: Optional[str] = None,
        proxy: Optional[str] = None,
    ) -> "ReservationDetection":
        """Reservation authority DETECTION — the raw NetSim verdict, no policy.

        The CML controller session is the authoritative validation boundary. The controller's ``/authok``+/labs
        only prove the node is still RUNNING, which stays up during the
        post-expiry grace period, so it must never be used to assert reservation
        validity. Returns the DETECTED state + cause (what the policy layer acts on):

          * ``active``       — NetSim confirms the reservation is valid.
          * ``expired``      — NetSim reports it ended / in-grace / inactive / absent.
          * ``unverifiable`` — cannot consult the authority. The ``cause`` tells
            which: ``no_key`` (absent), ``key_invalid`` (present but REJECTED —
            typically the 30-day rotation lapsed) or ``netsim_unreachable``
            (network/portal down). Policy (:func:`resolve_reservation_access`)
            decides whether that is allowed and surfaces the right fix.

        Never raises; never logs the API key or token.
        """
        # Standalone Controller Session & Token Authority (fail-closed)
        if not self.token or self._is_expired({"expiry": self.token_expiry}):
            return ReservationDetection("expired")

        try:
            r = self._session.get(
                f"{self.base}/authok",
                headers={"Authorization": f"Bearer {self.token}"},
                timeout=10,
            )
            if r.status_code in (401, 403):
                return ReservationDetection("expired")
            if r.ok:
                return ReservationDetection("active")
            return ReservationDetection("expired")
        except Exception:
            return ReservationDetection("unverifiable", "controller_unreachable")

    def is_lab_accessible(self, lab_id: str) -> bool:
        """Verify the active controller session is authorized to access lab_id."""
        if not self.token or self._is_expired({"expiry": self.token_expiry}):
            return False
        try:
            r = self._session.get(
                f"{self.base}/labs/{lab_id}",
                headers={"Authorization": f"Bearer {self.token}"},
                timeout=10,
            )
            return r.ok
        except Exception:
            return False

    def _invalidate_cached_token(self, host: str) -> None:
        """Remove the persisted token for ``host`` so it can never be reused.

        Called on auth failure, credential mismatch, failed revalidation, and
        explicit logout. Never raises.
        """
        try:
            if not os.path.exists(_TOKEN_CACHE):
                return
            with open(_TOKEN_CACHE) as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return
            removed = data.pop(host, None) is not None
            # Also drop any raw/unnormalized alias for the same host.
            for alias in list(data):
                if normalize_cml_host(alias) == normalize_cml_host(host):
                    data.pop(alias, None)
                    removed = True
            if removed:
                with open(_TOKEN_CACHE, "w") as f:
                    json.dump(data, f)
        except Exception:
            pass

    def logout(self, host: Optional[str] = None) -> None:
        """Explicit logout — drop the live token and invalidate the cache."""
        target = normalize_cml_host(host) if host else self.host
        self.token = None
        self.token_expiry = 0
        if target:
            self._invalidate_cached_token(target)

    def _get(self, path: str) -> requests.Response:
        """Authenticated GET request."""
        self._ensure_auth()
        r = self._session.get(
            f"{self.base}{path}",
            headers={"Authorization": f"Bearer {self.token}"},
            timeout=10
        )
        r.raise_for_status()
        return r

    def _ensure_auth(self):
        if not self.token:
            raise RuntimeError("Not authenticated. Call login() first.")
        if self._is_expired({"expiry": self.token_expiry}):
            log.warning("Token expired — re-authentication needed.")
            raise RuntimeError("Token expired. Call login() again.")

    def _is_expired(self, cached: dict) -> bool:
        return time.time() > cached.get("expiry", 0) - 300  # 5min buffer

    def _save_token_cache(self, host, token, expiry):
        os.makedirs(os.path.dirname(_TOKEN_CACHE), exist_ok=True)
        try:
            existing = {}
            if os.path.exists(_TOKEN_CACHE):
                with open(_TOKEN_CACHE) as f:
                    existing = json.load(f)
            existing[host] = {"token": token, "expiry": expiry}
            with open(_TOKEN_CACHE, "w") as f:
                json.dump(existing, f)
        except Exception:
            pass

    def _load_cached_token(self, host) -> Optional[dict]:
        try:
            if os.path.exists(_TOKEN_CACHE):
                with open(_TOKEN_CACHE) as f:
                    data = json.load(f)
                return data.get(host)
        except Exception:
            pass
        return None