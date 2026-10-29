#!/usr/bin/env python3
"""
live_device.py - Real device connections for CLIQ.

Two connection modes:
  1. CMLConsoleDevice -- CML SSH console-proxy (most common for lab use)
       SSH to CML host -> "open <path>" in the consoles> shell -> IOS console
       Usage: python -m cliq.ui.app --live
                --gateway cml.example.com
                --user cli_test --password <pw>
                --path /cli_test1/iosv-0/0

  2. LiveDevice (Netmiko) -- direct SSH to any IOS device
       Usage: python -m cliq.ui.app --live
                --host 192.168.1.1 --username cisco --password cisco

Both expose the same execute(cmd) -> str interface as MockDevice.
"""

from __future__ import annotations

import logging
import os
import re
import socket
import threading
import time

log = logging.getLogger("cliq.connection.live")

# Per-byte hex tracing (added during the ^@ diagnostics phase) is OFF by
# default; set CLIQ_TRACE_BYTES=1 to re-enable the verbose byte dumps at DEBUG.
# Never gates transport behaviour — logging only.
_TRACE_BYTES = os.getenv("CLIQ_TRACE_BYTES", "").strip().lower() in ("1", "true", "yes", "on")

from cliq.connection.output_cleaner import clean
from cliq.utils import observability as obs

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

class ConnectionError(Exception):
    """Raised when a device cannot be reached or authentication fails."""


class ConnectionLostError(ConnectionError):
    """Raised when an established SSH channel drops mid-session."""


class InteractiveAuthRequired(ConnectionError):
    """Raised by connect() when the device console demands interactive
    username/password login (NX-OS, or an IOS device with console AAA).

    The SSH channel is deliberately KEPT OPEN so the caller can drive the
    credential exchange via :meth:`CMLConsoleDevice.complete_login` after
    collecting credentials from the operator. ``stage`` indicates which prompt
    the device is currently showing ("username" or "password").
    """

    def __init__(self, stage: str = "username") -> None:
        super().__init__("interactive authentication required")
        self.stage = stage


# IOS prompt:  RouterName>  /  RouterName#  /  RouterName(config-if)#
# Excludes 'consoles>' (CML proxy prompt) via negative lookbehind on 'consoles'
_PROMPT_RE = re.compile(r"(?<!consoles)[\w\-]+(\([^\)]+\))?[#>]\s*$", re.MULTILINE)
# Same prompt token anchored at the START of a line. A help body line always
# begins with a keyword/description, never with the device prompt — so any line
# starting 'R1#...'/'R1>...'/'R1(config-if)#...' is a command echo or prompt
# re-display the device interleaves around the help, and must be dropped
# (Bug B: stray intermediate 'R1#' line after the '?' query).
_PROMPT_START_RE = re.compile(r"^(?!consoles>)[\w\-]+(\([^\)]+\))?[#>]")
_PRIV_PROMPT_RE = re.compile(r"[\w\-]+#\s*$", re.MULTILINE)
_RETURN_RE = re.compile(r"RETURN|return|Press.*get started", re.IGNORECASE)
# Console EXEC-level logout banner. `exit` / `logout` from user or privileged
# EXEC drops the console session and parks the line at this banner; a bare
# RETURN then re-opens the user EXEC ('>') prompt. Detected so execute()
# re-wakes the line promptly instead of blocking for the full read timeout
# waiting for a '#'/'>' prompt that never comes (slow exit-to-user-EXEC bug).
_PRESS_RETURN_RE = re.compile(r"Press\s+RETURN\s+to\s+get\s+started", re.IGNORECASE)
# Interactive IOS question prompt (e.g. bare `configure` -> "Configuring from
# terminal, memory, or network [terminal]? ", or "Destination filename
# [startup-config]? "). These lines end with a bracketed default followed by
# '?' or ':' — never a device '#'/'>' prompt — so the device is WAITING for the
# operator to type an answer. Requiring the trailing '?'/':' after the bracket
# keeps normal show-output (e.g. lines ending '[up]') from ever matching.
_INTERACTIVE_PROMPT_RE = re.compile(r"\[[^\]]{1,60}\]\s*[?:]\s*$")
_PASSWORD_RE = re.compile(r"Password:\s*$", re.IGNORECASE | re.MULTILINE)
# Interactive console LOGIN prompt — NX-OS ("login:" / "Username:") and IOS
# devices with console AAA. Case-insensitive; matched at the tail of a line so
# a banner mentioning "username" in prose never trips it. Bug 7.
_USERNAME_PROMPT_RE = re.compile(r"(?im)(?:^|\s)(?:login|username)\s*:\s*$")
# CML console connection established string
_CML_CONNECTED_RE = re.compile(r"Connected to CML terminalserver", re.IGNORECASE)
# CML proxy rejected the open command
_CML_UNKNOWN_RE = re.compile(r"unknown command", re.IGNORECASE)
# Strip ANSI escape sequences (CML proxy character-echoes with ANSI codes)
_ANSI_STRIP = re.compile(r"\x1b\[[0-9;]*[A-Za-z]|\x1b\][^\x07]*\x07|\r")

# Cisco pager prompt ('--More--' / '---(MORE)---'). Detected at the tail of a
# page so CLIQ can route raw keys (Space/Enter/q) to the device.
_MORE_RE = re.compile(r"--+\s*More\s*--+|---\(MORE\)---", re.IGNORECASE)

# A device error line in a '?'-help response. Both IOSv 15.9 and IOS XE 17.18
# answer an invalid help path with a '%'-prefixed error instead of an options
# list (e.g. '% Unrecognized command', "% Invalid input detected at '^' marker.",
# '% Incomplete command.', '% Ambiguous command:'). We match any '%'-leading
# line generally so new wordings on either platform still pass through.
_HELP_ERROR_RE = re.compile(r"^\s*%\s*\S", re.MULTILINE)


def is_help_error(text: str) -> bool:
    """True when a captured '?'-help body is actually a device ERROR response.

    Used so the caller renders the error faithfully (like CML) instead of a
    blank prompt, and never feeds it to the ghost-text engine. A help error
    response is one whose first non-blank line is a '%'-prefixed device error.
    """
    for line in (text or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        return stripped.startswith("%")
    return False


def looks_like_auth_prompt(text: str) -> str | None:
    """Classify a device output tail as an interactive AUTH prompt (Bug 10).

    Returns ``"password"`` when the output ends at a ``Password:`` prompt,
    ``"username"`` for a ``login:`` / ``Username:`` prompt (NX-OS + IOS console
    AAA), or ``None`` otherwise. Case-insensitive and anchored at the line tail
    so a banner mentioning these words in prose never trips it.

    The backend uses this purely as a defensive signal to surface the secure
    credential dialog; it NEVER validates, echoes, or logs whatever follows.
    """
    tail = _ANSI_STRIP.sub("", text or "")
    if _PASSWORD_RE.search(tail):
        return "password"
    if _USERNAME_PROMPT_RE.search(tail):
        return "username"
    return None

# Map a live IOS prompt to a CLIQ mode. Submode in parentheses wins; a bare
# '#' is privileged EXEC (enable); a bare '>' is user EXEC.
_SUBMODE_RE = re.compile(r"\(([^)]+)\)\s*[#>]\s*$")


def mode_from_prompt(prompt: str) -> str:
    """Detect the CLI mode from a live device prompt string.

    e.g. 'R1>'              -> 'exec'
         'R1#'              -> 'enable'
         'R1(config)#'      -> 'config'
         'R1(config-if)#'   -> 'config-if'
         'SW1(config-line)#'-> 'config-line'
    """
    if not prompt:
        return "exec"
    p = _ANSI_STRIP.sub("", prompt).strip()
    m = _SUBMODE_RE.search(p)
    if m:
        sub = m.group(1).strip().lower()
        # 'config' alone, or 'config-if', 'config-line', 'config-router', ...
        return sub if sub.startswith("config") else "config"
    if p.endswith("#"):
        return "enable"
    return "exec"


def _extract_context_help(text: str, partial: str) -> str:
    """Clean a device context-help capture into just the help body.

    Removes the echoed ``partial?`` input, any re-displayed prompt line, pager
    artifacts (``--More--``, backspaces), stray ``% ...`` errors, and the
    trailing kill-line artifacts so the client sees only the device's help.
    """
    # Priority 2/4: strip pager artifacts BEFORE line parsing so the help list
    # is complete and free of stray '--More--' tokens / erase backspaces.
    text = _MORE_RE.sub("", text or "")
    text = text.replace("\x08", "")
    lines = text.replace("\r", "").split("\n")
    cleaned: list[str] = []
    error_lines: list[str] = []
    partial_norm = partial.strip().lower()
    last_token = partial_norm.split()[-1] if partial_norm.split() else ""
    for line in lines:
        stripped = line.rstrip()
        low = stripped.strip().lower()
        if not stripped.strip():
            continue
        # An invalid '?'-help path makes the device answer with a '%'-prefixed
        # error instead of an options list. Capture those lines separately: if
        # the response turns out to be ONLY an error (no options), we return it
        # faithfully so the terminal shows it exactly like CML. If the response
        # is a valid options list with a stray leaked '% ...', we still drop it.
        if low.startswith("%"):
            error_lines.append(stripped.strip())
            continue
        # A caret marker line ('        ^') points under the rejected token in
        # the echoed query (which we strip). On its own it is error context, not
        # a help option — never count it as a body line.
        if set(stripped.strip()) == {"^"}:
            continue
        # Bug B: drop ANY line that begins with the device prompt (hostname#/>,
        # optionally with a submode). These are command echoes or prompt
        # re-displays the device interleaves before/after the help body —
        # including the stray intermediate 'R1#' line that surfaced right after
        # the '?' query. Help body lines never start with the prompt, so this is
        # safe and catches every prompt artifact in one rule.
        if _PROMPT_START_RE.match(stripped.strip()):
            continue
        # Priority 4: drop a BARE prompt redisplay (e.g. 'R2#') with no command
        # text so no stray/duplicate prompt leaks into the help output.
        if _PROMPT_RE.search(stripped) and not _PROMPT_RE.sub("", stripped).strip():
            continue
        # Drop the echoed input line (prompt + partial?) and bare prompt redisplays.
        if partial_norm and partial_norm in low and low.endswith("?"):
            continue
        if _PROMPT_RE.search(stripped) and partial_norm and partial_norm in low:
            continue
        # Drop the command line IOS re-displays after help (e.g. 'R1#show ip bgp
        # s'): a prompt token (... '#'/'>') immediately followed by the partial.
        # Without this the re-display leaks into the help body and confuses the
        # client's ghost parsing.
        if partial_norm and partial_norm in low:
            head = low.split(partial_norm, 1)[0].rstrip()
            if head.endswith("#") or head.endswith(">"):
                continue
        # Drop the re-displayed command line for the mid-token case where the
        # device echoes 'R2#show ip bgp r' (ends with the partial's last token
        # after a prompt) — defensive against abbreviated heads.
        if last_token and _PROMPT_RE.search(stripped) and low.endswith(last_token):
            continue
        cleaned.append(stripped)
    # Faithful error passthrough: when the device returned ONLY an error (no
    # options survived cleaning), surface the device error verbatim so the
    # terminal mirrors CML instead of showing a blank prompt.
    if not cleaned and error_lines:
        return "\n".join(error_lines).strip()
    return "\n".join(cleaned).strip()


def _clean_page(raw: str, command: str | None = None) -> str:
    """Clean a single pager page for display.

    Strips ANSI codes, the ``--More--`` token, backspace artifacts left when the
    device erases the pager line, the echoed command (first page) and any
    trailing prompt line, while preserving the paged content itself.
    """
    text = _ANSI_STRIP.sub("", raw or "")
    text = _MORE_RE.sub("", text)
    text = text.replace("\x08", "")
    lines = text.split("\n")
    if command:
        cnorm = command.strip().lower()
        if lines and lines[0].strip().lower() == cnorm:
            lines = lines[1:]
    while lines and _PROMPT_RE.search(lines[-1].strip()):
        lines.pop()
    # Trim leading/trailing blank lines but keep internal blanks.
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CML console device  (paramiko, interactive consoles> shell)
# ---------------------------------------------------------------------------

class CMLConsoleDevice:
    """
    Connect to a CML device console via the CML SSH console-proxy.

    CML exposes an interactive SSH shell (prompt: consoles>).
    We authenticate to the gateway, then send:
        open /lab/node/line
    to attach to the IOS device's serial console.

    Parameters
    ----------
    gateway:  CML server hostname or IP
    username: CML SSH username
    password: CML SSH password
    path:     Console path, e.g. /cli_test1/iosv-0/0
    timeout:  SSH connect / recv timeout in seconds
    """

    def __init__(
        self,
        gateway: str,
        username: str,
        password: str,
        path: str,
        timeout: int = 20,
    ) -> None:
        try:
            import paramiko
            self._paramiko = paramiko
        except ImportError as exc:
            raise ImportError("paramiko is not installed. Run: pip install paramiko") from exc

        self.gateway = gateway
        self.username = username
        self.password = password
        self.path = path.strip()
        self.timeout = timeout

        # Serializes all SSH channel I/O (send + read_until). A single paramiko
        # channel is NOT safe for concurrent access: if two execute() calls run
        # on different threads they interleave bytes on the wire and the prompt
        # detection in _read_until reads another command's output. This lock
        # guarantees each send -> read-to-prompt cycle completes atomically.
        self._io_lock = threading.RLock()

        self._client = None
        self._chan = None
        self._connected = False
        self._current_prompt: str = ""  # last IOS prompt seen, e.g. "Switch#"
        # Bug 7: set while the console is parked at an interactive login/password
        # prompt waiting for operator-supplied credentials (driven via
        # complete_login). The StageTimer from connect() is stashed so the
        # resumed finalize can record prompt_ready against the same timeline.
        self._awaiting_auth = False
        self._auth_stage_timer = None

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    def connect(self) -> None:
        paramiko = self._paramiko

        # Phase A #2: live console cold-start breakdown. The ~10s cold-start is
        # the console-session open/attach, NOT the virl2 REST auth. Time each
        # stage (auth → console_open → probe_prompt → prompt_ready) into the
        # unified "cml" log so a real connection shows where the time goes.
        # Measurement only — connection behavior is unchanged.
        st = obs.StageTimer("cml", "console_attach", detail=self.path)

        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        log.info("SSH connect to CML gateway %s as %s ...", self.gateway, self.username)
        try:
            client.connect(
                self.gateway,
                username=self.username,
                password=self.password,
                timeout=self.timeout,
                look_for_keys=False,
                allow_agent=False,
            )
        except paramiko.AuthenticationException as exc:
            st.mark("auth", "error", detail="AuthenticationException")
            raise ConnectionError(
                f"Authentication failed for {self.username}@{self.gateway}"
            ) from exc
        except Exception as exc:
            st.mark("auth", "error", detail=type(exc).__name__)
            raise ConnectionError(
                f"Cannot reach CML gateway {self.gateway}: {exc}"
            ) from exc
        st.mark("auth", "success", detail="ssh gateway authenticated")

        transport = client.get_transport()
        chan = transport.open_session()
        chan.get_pty(term="vt100", width=200, height=50)
        # CML opens an interactive console-proxy shell (prompt: "consoles>").
        # We open the device console with "open <path>".
        chan.invoke_shell()
        # 0.2s recv granularity: the read loops poll for the prompt this often,
        # so a prompt change (e.g. '>' -> '#' on enable) is detected within
        # ~200 ms of the device emitting it instead of blocking on a longer
        # timeout — near-instant privilege transitions (Bug 4).
        chan.settimeout(0.2)

        self._client = client
        self._chan = chan

        # Wait for the CML banner and "consoles>" prompt
        time.sleep(0.5)
        banner = self._read_cml_prompt(timeout=8)
        log.debug("CML banner: %r", banner[:200])

        # Open the device console
        open_cmd = f"open {self.path}"
        log.info("Sending: %r to CML proxy ...", open_cmd)
        self._send(open_cmd)

        # Wait for the IOS console to initialise.
        # _read_until now waits for a real IOS prompt (no early CML-banner break),
        # so the 20s window covers: CML handshake + "Press RETURN" banner + prompt.
        buf = self._read_until(timeout=20)
        clean_open = _ANSI_STRIP.sub("", buf)
        log.debug("Post-open buf (stripped): %r", clean_open[:300])

        # Detect CML rejecting the path (e.g. missing /0 line number)
        if _CML_UNKNOWN_RE.search(clean_open) or (
            not _CML_CONNECTED_RE.search(clean_open) and "consoles>" in clean_open
        ):
            st.mark("console_open", "error", detail="CML rejected path")
            raise ConnectionError(
                f"CML rejected path '{self.path}'. "
                "Check format: /lab_name/node_name/line (e.g. /cli_test1/iosvl2-0/0). "
                "Run 'list' at the CML consoles> prompt to see valid paths."
            )
        st.mark("console_open", "success", detail="console proxy attached")

        # Drive the wake loop + prompt normalization. If the console is parked
        # at an interactive login/password prompt (NX-OS / console AAA), this
        # raises InteractiveAuthRequired with the channel kept open so the caller
        # can collect credentials and resume via complete_login() (Bug 7).
        self._wake_and_finalize(buf, st)

    def _wake_and_finalize(self, buf: str, st) -> None:
        """Wake the console, detect the prompt, normalize mode, mark ready.

        Shared by connect() (initial attach) and complete_login() (resume after
        interactive auth). Raises :class:`InteractiveAuthRequired` the moment a
        login/password prompt is seen so the credential exchange can be driven
        from the UI instead of silently failing or leaking a blank password.
        """
        # Wake loop: handle all device states by sending blank lines until we
        # see a real IOS prompt.  Uses exponential backoff for faster parallel wake.
        #   - Silent after CML attach → first blank wakes the device
        #   - "Press RETURN to get started." → blank dismisses it
        #   - "con0 is now available" → blank after CML connection wakes it
        #   - Already at a prompt → blank just echoes the prompt again
        last_line = self._current_prompt  # may already be set by _read_until
        _wake_intervals = [0.3, 0.3, 0.5, 0.5, 0.7, 0.7, 1.0, 1.0, 1.0, 1.5, 2.0, 3.0]
        for attempt, wait in enumerate(_wake_intervals):
            clean = _ANSI_STRIP.sub("", buf).rstrip()
            lines = [l.strip() for l in clean.split("\n") if l.strip()]
            last_line = lines[-1] if lines else ""
            log.debug("Wake attempt %d: last_line=%r", attempt, last_line[-60:])

            if _PROMPT_RE.search(last_line):
                log.debug("Got IOS prompt: %r", last_line)
                self._current_prompt = last_line
                break

            # Bug 7: an interactive login/password prompt means the device wants
            # credentials. Park the session (channel stays open) and signal the
            # caller to collect them — NEVER auto-send a blank password.
            if _USERNAME_PROMPT_RE.search(clean) or _PASSWORD_RE.search(clean):
                stage = (
                    "password"
                    if _PASSWORD_RE.search(clean) and not _USERNAME_PROMPT_RE.search(clean)
                    else "username"
                )
                self._awaiting_auth = True
                self._auth_stage_timer = st
                st.mark("probe_prompt", "auth_required", detail=f"interactive login ({stage})")
                log.info("Console at %s requires interactive login (stage=%s)", self.path, stage)
                raise InteractiveAuthRequired(stage)

            # "Press RETURN", silent console, or junk — send Enter and wait
            log.debug("No prompt yet (wait=%.1fs) — sending Enter to wake device", wait)
            self._send("")
            buf = self._read_until(timeout=wait * 2 + 1)
        else:
            st.mark("probe_prompt", "error", detail="no IOS prompt after attach")
            raise ConnectionError(
                f"Device at {self.path} did not return an IOS prompt after connection. "
                "It may be booting — wait 30 s and retry."
            )

        st.mark("probe_prompt", "success", detail="IOS prompt detected")
        last_line = self._current_prompt or last_line

        # If device is in a config sub-mode (left from previous session),
        # move back to enable mode so we start from a known clean state.
        if "(" in last_line and "#" in last_line:
            log.debug("Device in config sub-mode '%s' - sending 'end' to normalize", last_line)
            self._send("end")
            buf = self._read_until(timeout=5)
            last_line = _ANSI_STRIP.sub("", buf).rstrip().split("\n")[-1].strip()

        # User EXEC mode (>) preserved - elevation requires explicit enable command

        # Priority 2: do NOT disable paging. 'terminal length 0' force-streams
        # all output, which renders a premature newline/prompt before the body
        # (the stray 'R1#' before help). We KEEP native paging and drive it
        # CML-style: '?'-help auto-pages internally (context_help) and long
        # command output pages via execute_paged()/pager_send_key() where the
        # engineer presses Space (next page) / Tab (break) / q (quit). We still
        # set 'terminal width 0' to stop line-wrapping so output parses cleanly.
        self._send("terminal width 0")
        self._read_until(timeout=5)

        self._awaiting_auth = False
        self._auth_stage_timer = None
        self._connected = True
        st.mark("prompt_ready", "success", detail=self._current_prompt or "")
        log.info("CML console ready: %s -> %s (prompt: %r)", self.gateway, self.path, self._current_prompt)

    @property
    def awaiting_auth(self) -> bool:
        """True while the console is parked at an interactive login prompt."""
        return self._awaiting_auth

    def complete_login(self, username: str, password: str) -> None:
        """Resume a parked console login with operator-supplied credentials.

        Called after connect() raised :class:`InteractiveAuthRequired`. Sends the
        username (if the console is showing a login/username prompt) followed by
        the password, then re-drives the wake/prompt normalization so CLIQ owns
        a clean, known-mode session. The password is written through
        :meth:`_audit_send` with ``secret=True`` so it is NEVER echoed into the
        audit log (release-blocker redaction). Credentials are used in-memory
        only and never persisted.

        Re-raises :class:`InteractiveAuthRequired` when the device rejects the
        credentials (re-prompts for login), so the caller can ask again.
        """
        if not self._chan:
            raise ConnectionError("No open console channel to authenticate against.")
        st = self._auth_stage_timer or obs.StageTimer("cml", "console_attach", detail=self.path)

        # Read whatever the console is currently showing so we know which prompt
        # we're answering (username first, or straight to password).
        buf = self._read_until(timeout=3)
        clean = _ANSI_STRIP.sub("", buf)

        if _USERNAME_PROMPT_RE.search(clean) or (
            not _PASSWORD_RE.search(clean) and self._awaiting_auth
        ):
            self._audit_send(username + "\r\n", source="auth.username")
            buf = self._read_until(timeout=8)
            clean = _ANSI_STRIP.sub("", buf)

        if _PASSWORD_RE.search(clean):
            # secret=True → the audit chokepoint logs <redacted>, never the value.
            self._audit_send(password + "\r\n", source="auth.password", secret=True)
            buf = self._read_until(timeout=10)
            clean = _ANSI_STRIP.sub("", buf)

        # A re-displayed login/password prompt means the credentials were wrong.
        if _USERNAME_PROMPT_RE.search(clean) or _PASSWORD_RE.search(clean):
            self._awaiting_auth = True
            stage = "password" if _PASSWORD_RE.search(clean) else "username"
            log.info("Interactive login rejected at %s (re-prompt stage=%s)", self.path, stage)
            raise InteractiveAuthRequired(stage)

        # Credentials accepted — finish the normal wake/normalize/ready sequence.
        self._awaiting_auth = False
        self._wake_and_finalize(buf, st)


    def disconnect(self) -> None:
        for obj in (self._chan, self._client):
            if obj:
                try:
                    obj.close()
                except Exception:
                    pass
        self._connected = False
        self._chan = None
        self._client = None

    @property
    def is_connected(self) -> bool:
        return self._connected and self._chan is not None

    @property
    def current_prompt(self) -> str:
        """Last IOS prompt seen on the channel, e.g. 'Switch#' or 'Switch(config)#'."""
        return self._current_prompt

    def check_enable_mode(self) -> bool:
        """Check if the console is currently in privileged EXEC mode ('#')."""
        if self._current_prompt:
            p = _ANSI_STRIP.sub("", self._current_prompt).strip()
            return bool(_PRIV_PROMPT_RE.search(p) or p.endswith("#"))
        return False

    def enable(self, secret: str = "") -> bool:
        """Elevate the CML console session to privileged EXEC mode ('#').

        Sends 'enable' (if not already waiting at a password prompt), enters the
        provided secret with secret=True (redacted from audit logs), and verifies
        that the prompt ends with '#'.
        """
        io_lock = getattr(self, "_io_lock", None)
        if io_lock is None:
            io_lock = self._io_lock = threading.RLock()

        with io_lock:
            if not self.is_connected or not self._chan:
                raise ConnectionError("Not connected. Call connect() first.")

            # If already in privileged EXEC or sub-config mode ending with '#'
            if self.check_enable_mode():
                return True

            sec = secret or ""

            # Check if console is already displaying a Password prompt
            # (e.g. if 'enable' was sent earlier or parked at password)
            buf = self._drain(timeout=0.3)
            clean = _ANSI_STRIP.sub("", buf)
            is_at_password = bool(_PASSWORD_RE.search(clean))

            if not is_at_password:
                # Send 'enable' command
                self._audit_send("enable\r\n", source="enable.cmd")
                # Read response until password prompt or regular prompt
                buf = self._read_until(timeout=8.0)
                clean = _ANSI_STRIP.sub("", buf)
                is_at_password = bool(_PASSWORD_RE.search(clean))

            if is_at_password:
                # Send the secret with secret=True so it's NEVER logged or echoed
                self._audit_send(sec + "\r\n", source="enable.password", secret=True)
                buf_resp = self._read_until(timeout=10.0)
                clean_resp = _ANSI_STRIP.sub("", buf_resp).rstrip()
                last_line = clean_resp.split("\n")[-1].strip() if clean_resp else ""

                # Check if prompt now ends with '#'
                if _PROMPT_RE.search(last_line) and last_line.endswith("#"):
                    self._current_prompt = last_line
                    return True

                # If re-prompted with Password or error
                if (
                    _PASSWORD_RE.search(last_line)
                    or "% Bad secrets" in clean_resp
                    or "% Access denied" in clean_resp
                    or "% Error in authentication" in clean_resp
                ):
                    # Clear out the failed auth attempt so console returns to normal prompt
                    self._audit_send("\x03\r\n", source="enable.cancel")
                    time.sleep(0.2)
                    self._drain(timeout=1.0)
                    self.probe_prompt(timeout=3.0)
                    raise ConnectionError("Enable authentication rejected by device.")

                # Check if prompt is still '>'
                if last_line.endswith(">"):
                    self._current_prompt = last_line
                    raise ConnectionError("Enable authentication failed: returned to user EXEC mode.")
            else:
                # If IOS did not prompt for password (e.g. device with no enable secret)
                last_line = clean.split("\n")[-1].strip() if clean else ""
                if _PROMPT_RE.search(last_line) and last_line.endswith("#"):
                    self._current_prompt = last_line
                    return True
                if "% No password set" in clean:
                    raise ConnectionError("Enable password is not set on the device.")

            # Fallback probe
            p = self.probe_prompt(timeout=3.0)
            if p and p.endswith("#"):
                return True

            raise ConnectionError("Enable mode could not be verified.")

    def cancel_enable(self) -> None:
        """Cancel an in-progress enable prompt and return the console to user EXEC."""
        io_lock = getattr(self, "_io_lock", None)
        if io_lock is None:
            io_lock = self._io_lock = threading.RLock()
        with io_lock:
            try:
                self._audit_send("\x03\r\n", source="cancel_enable")
                time.sleep(0.1)
                self._drain(timeout=1.0)
                self.probe_prompt(timeout=3.0)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Command execution
    # ------------------------------------------------------------------

    MAX_RECONNECT_ATTEMPTS = 3
    RECONNECT_DELAY_SECONDS = 2

    def _is_alive(self) -> bool:
        """Non-blocking check — returns in <1 ms."""
        if not self._chan or self._chan.closed:
            return False
        try:
            self._chan.send_ready()  # non-blocking; raises if channel dead
            return not self._chan.closed
        except Exception:
            return False

    def _check_connection(self) -> None:
        """Raise ConnectionLostError if the channel appears dead."""
        if not self._is_alive():
            raise ConnectionLostError("SSH channel closed")
        try:
            old = self._chan.gettimeout()
            self._chan.settimeout(1.0)
            self._audit_send(b"", source="_check_connection.keepalive")  # IOS ignores empty
            self._chan.settimeout(old)
        except Exception:
            raise ConnectionLostError("SSH channel unresponsive")

    def _reconnect(self) -> None:
        """Re-establish the full SSH+console connection using stored credentials."""
        log.info("Reconnecting to %s ...", self.path)
        for obj in (self._chan, self._client):
            try:
                if obj:
                    obj.close()
            except Exception:
                pass
        self._chan = None
        self._client = None
        self._connected = False
        self.connect()
        log.info("Reconnected: %s", self.path)

    def probe_prompt(self, timeout: float = 5.0) -> str:
        """Send a harmless newline and read the live prompt off the channel.

        Used after a re-attach to discover the device's CURRENT hostname and
        mode without changing anything. Updates self._current_prompt and returns
        the detected prompt (empty string if none seen).
        """
        io_lock = getattr(self, "_io_lock", None)
        if io_lock is None:
            io_lock = self._io_lock = threading.RLock()
        with io_lock:
            try:
                # A bare CR just re-echoes the current prompt on IOS.
                self._audit_send("\r\n", source="probe_prompt")
            except Exception as exc:
                raise ConnectionLostError(f"probe failed: {exc}") from exc
            buf = self._read_until(timeout=timeout)
        clean_buf = _ANSI_STRIP.sub("", buf).rstrip()
        last = clean_buf.split("\n")[-1].strip() if clean_buf else ""
        if _PROMPT_RE.search(last):
            self._current_prompt = last
        return self._current_prompt

    def reattach(self) -> str:
        """Re-attach to the EXISTING CML console session after CLIQ restarts.

        Unlike connect(), this does NOT normalize the device (no forced 'end'/
        'enable'/'terminal length'): the console session on the CML side stays
        alive across SSH drops, so we simply re-open the console proxy path and
        probe the live prompt to resume in whatever mode the device was left in.

        Returns the detected mode (e.g. 'config-if'). Falls back to a full
        connect() if the console path cannot be re-opened.
        """
        paramiko = self._paramiko
        log.info("Re-attaching to existing console session %s ...", self.path)

        for obj in (self._chan, self._client):
            try:
                if obj:
                    obj.close()
            except Exception:
                pass
        self._chan = None
        self._client = None
        self._connected = False

        try:
            st = obs.StageTimer("cml", "console_reattach", detail=self.path)
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            client.connect(
                self.gateway, username=self.username, password=self.password,
                timeout=self.timeout, look_for_keys=False, allow_agent=False,
            )
            chan = client.get_transport().open_session()
            chan.get_pty(term="vt100", width=200, height=50)
            chan.invoke_shell()
            chan.settimeout(0.2)  # snappy prompt polling (Bug 4)
            self._client = client
            self._chan = chan
            st.mark("auth", "success", detail="ssh gateway authenticated")

            time.sleep(0.3)
            self._read_cml_prompt(timeout=6)
            self._send(f"open {self.path}")
            self._read_until(timeout=10)
            st.mark("console_open", "success", detail="console proxy re-attached")

            self._connected = True
            prompt = self.probe_prompt(timeout=6)
            if not prompt:
                # The console may be at a "Press RETURN" banner — nudge once.
                prompt = self.probe_prompt(timeout=6)
            mode = mode_from_prompt(prompt)
            st.mark("prompt_ready", "success", detail=prompt or "")
            log.info("Re-attached: %s (prompt=%r, mode=%s)", self.path, prompt, mode)
            return mode
        except Exception as exc:
            log.warning("Re-attach failed (%s); doing a full connect.", exc)
            self.connect()
            return mode_from_prompt(self._current_prompt)

    def execute(self, command: str) -> str:
        if not self.is_connected:
            raise ConnectionError("Not connected. Call connect() first.")

        command = command.strip()
        last_exc: Exception | None = None

        # Lazily ensure the I/O lock exists (devices built via __new__ in tests
        # skip __init__).
        io_lock = getattr(self, "_io_lock", None)
        if io_lock is None:
            io_lock = self._io_lock = threading.RLock()

        for attempt in range(self.MAX_RECONNECT_ATTEMPTS):
            try:
                if attempt > 0:
                    # Re-check after reconnect before sending
                    pass
                # Hold the I/O lock for the whole send -> read-to-prompt cycle so
                # no other thread can write to or read from the channel until this
                # command's full response (up to the prompt) has been consumed.
                with io_lock:
                    self._check_connection()
                    log.debug("SEND -> %r", command)
                    # Phase A latency: request → first_byte → complete. Reset the
                    # first-byte stamp, send, then read; _read_until records when
                    # the first byte arrives. Behavior is unchanged (timing only).
                    self._read_first_byte_ts = None
                    _t_request = time.monotonic()
                    self._send(command)
                    raw = self._read_until(timeout=60)
                    # EXEC-level logout ('exit'/'logout' from user or privileged
                    # EXEC) leaves the console parked at the "Press RETURN to get
                    # started." banner instead of a prompt. Send a bare RETURN to
                    # re-open the user EXEC ('>') prompt so mode detection lands on
                    # 'exec' immediately, rather than the caller waiting out the
                    # full read timeout for a prompt that never arrives.
                    _stripped = _ANSI_STRIP.sub("", raw).rstrip()
                    _last = _stripped.split("\n")[-1] if _stripped else ""
                    if _PRESS_RETURN_RE.search(_stripped) and not _PROMPT_RE.search(_last):
                        self._send("")
                        raw += self._read_until(timeout=8)
                    _t_complete = time.monotonic()
                output = clean(raw, command)
                log.debug("RECV <- %d chars", len(output))
                self._log_command_latency(
                    command, _t_request, self._read_first_byte_ts, _t_complete,
                    out_chars=len(output),
                )
                return output
            except ConnectionLostError as exc:
                last_exc = exc
                log.warning("Connection lost (attempt %d/%d): %s",
                             attempt + 1, self.MAX_RECONNECT_ATTEMPTS, exc)
                if attempt < self.MAX_RECONNECT_ATTEMPTS - 1:
                    try:
                        self._reconnect()
                        log.info("Reconnected — retrying command.")
                        time.sleep(0.2)
                    except Exception as reconnect_exc:
                        log.warning("Reconnect attempt %d failed: %s",
                                    attempt + 1, reconnect_exc)
                        time.sleep(self.RECONNECT_DELAY_SECONDS)

        raise ConnectionLostError(
            f"Failed to execute '{command}' after {self.MAX_RECONNECT_ATTEMPTS} "
            f"reconnect attempts: {last_exc}"
        )

    def _read_help_page(self, timeout: float = 8.0) -> tuple[str, str]:
        """Read ONE page of '?'-help output (CML-style streaming).

        Returns ``(buf, state)`` where ``state`` is:
          ``"more"``    — a ``--More--`` pager prompt appeared (the engineer
                          drives the next page),
          ``"prompt"``  — output settled: the help finished and the device
                          re-displayed the partial line (which is NOT a clean
                          ``Router#`` prompt, so we detect an idle gap instead),
          ``"timeout"`` — nothing arrived before the deadline.

        Unlike :meth:`_read_page`, '?'-help completion never ends in a bare
        prompt line, so an idle gap (no new bytes) means "page complete".
        """
        buf = ""
        deadline = time.time() + timeout
        last_data = time.time()
        while time.time() < deadline:
            try:
                if self._chan.recv_ready():
                    chunk = self._chan.recv(4096)
                    if chunk:
                        buf += chunk.decode("utf-8", errors="replace")
                        last_data = time.time()
                        if _MORE_RE.search(_ANSI_STRIP.sub("", buf)):
                            return buf, "more"
                        continue
                time.sleep(0.05)
                if buf and (time.time() - last_data) >= 0.5:
                    return buf, "prompt"
            except Exception:
                break
        return buf, ("prompt" if buf else "timeout")

    def _clear_help_line(self) -> None:
        """Clear the partial line IOS re-displays after a '?'-help capture.

        Priority 5: sends ONLY Ctrl-U (kill-line) — NEVER Ctrl-C. Ctrl-C at the
        prompt prints a FRESH prompt line, which leaked the wasteful extra
        'R1#' the engineer reported. Ctrl-U erases the re-typed partial in place
        WITHOUT emitting a new prompt. If the console swallowed Ctrl-U we fall
        back to sized backspaces. CLIQ never sends \\r/\\n here, so nothing is
        ever executed. The drained bytes are discarded (display-only), so no
        artifact reaches the client.
        """
        partial = getattr(self, "_help_partial", "") or ""
        try:
            self._audit_send("\x15", source="context_help.ctrl_u")
            time.sleep(0.1)
            echo = _ANSI_STRIP.sub("", self._drain(timeout=1.0))
            pnorm = partial.strip().lower()
            tail = echo.replace("\r", "").split("\n")[-1].strip().lower()
            if pnorm and pnorm in tail:
                self._audit_send(
                    "\x7f" * (len(partial) + 2),
                    source="context_help.backspace",
                )
                time.sleep(0.1)
                self._drain(timeout=1.0)
        except Exception:
            pass

    def context_help_start(self, partial: str) -> tuple[str, bool]:
        """Begin a CML-style streaming '?'-help capture.

        Sends ``partial?`` WITHOUT a newline so IOS returns its own context
        help, then reads ONLY the FIRST page. Returns ``(page_text, more)``:
        when ``more`` is True the device is sitting at a ``--More--`` prompt —
        call :meth:`context_help_more` (Space/Enter/q) to stream the next page.

        Priority 1+2: tokens from EACH page are returned immediately so ghost
        completion works before the full list is captured, and the engineer is
        never blocked waiting for the device to auto-page the entire list.
        Advisory — no newline is ever sent, so nothing can execute.
        """
        if not self.is_connected:
            raise ConnectionError("Not connected. Call connect() first.")
        io_lock = getattr(self, "_io_lock", None)
        if io_lock is None:
            io_lock = self._io_lock = threading.RLock()

        with io_lock:
            self._check_connection()
            # ── Priority: whitespace-normalize the help request ────────────
            # IOS distinguishes 'show?' (complete the TOKEN 'show') from
            # 'show ?' (list the SUBCOMMANDS of show). That intent is encoded
            # solely in trailing whitespace. Collapse internal runs so
            # 'show ?' and 'show   ?' send an IDENTICAL request.
            wants_subcommands = partial != partial.rstrip()
            body = re.sub(r"\s+", " ", partial).strip()
            help_query = body + (" ?" if wants_subcommands else "?")
            self._help_partial = partial
            log.info(
                "[HELP-CAPTURE] start partial=%r -> query=%r (subcommands=%s)",
                partial, help_query, wants_subcommands,
            )
            self._audit_send(help_query, source="context_help.query")
            raw, state = self._read_help_page(timeout=8.0)
            more = state == "more"
            if not more:
                self._clear_help_line()

        page = _extract_context_help(_ANSI_STRIP.sub("", raw), partial)
        log.info("[HELP-CAPTURE] start -> page=%d chars more=%s", len(page), more)
        return page, more

    def context_help_more(self, key: str = " ") -> tuple[str, bool]:
        """Stream the NEXT page of an active '?'-help pager (CML-style).

        ``key`` is a raw pager key: ``" "`` (next page), ``"\\r"`` (next line)
        or ``"q"`` (quit). Returns ``(page_text, more)``. When ``more`` is False
        the help is finished and the re-displayed partial line has been cleared.
        Advisory — never submits a command.
        """
        if not self.is_connected:
            raise ConnectionError("Not connected. Call connect() first.")
        io_lock = getattr(self, "_io_lock", None)
        if io_lock is None:
            io_lock = self._io_lock = threading.RLock()
        partial = getattr(self, "_help_partial", "") or ""

        with io_lock:
            self._check_connection()
            self._audit_send(key, source="context_help.more_key")
            if key == "q":
                # Quit the pager, then clear the re-displayed partial line.
                raw, _ = self._read_help_page(timeout=4.0)
                self._clear_help_line()
                page = _extract_context_help(_ANSI_STRIP.sub("", raw), partial)
                log.info("[HELP-CAPTURE] quit -> page=%d chars", len(page))
                return page, False
            raw, state = self._read_help_page(timeout=8.0)
            more = state == "more"
            if not more:
                self._clear_help_line()

        page = _extract_context_help(_ANSI_STRIP.sub("", raw), partial)
        log.info("[HELP-CAPTURE] more -> page=%d chars more=%s", len(page), more)
        return page, more

    def _drain(self, timeout: float = 3.0) -> str:
        """Read whatever is available until the channel goes idle."""
        buf = ""
        idle = 0.0
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if self._chan.recv_ready():
                    chunk = self._chan.recv(4096)
                    if chunk:
                        buf += chunk.decode("utf-8", errors="replace")
                        idle = 0.0
                        continue
                idle += 0.1
                time.sleep(0.1)
                if idle >= 0.5 and buf:
                    break
            except Exception:
                break
        return buf

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _send(self, cmd: str, submit: str = "\r\n") -> None:
        # Use \r\n — IOS consoles require CR to dismiss "Press RETURN" banners;
        # \r\n works for all IOS variants (IOSv, IOSvL2, IOS-XE).
        # `submit` lets an interactive caller submit with a bare CR ("\r") so a
        # trailing LF cannot auto-answer an IOS question prompt (e.g. a bare
        # `configure` whose "[terminal]?" default would otherwise be accepted by
        # the stray newline, skipping the interactive step).
        self._audit_send(cmd + submit, source="_send")

    def _log_command_latency(
        self,
        command: str,
        t_request: float,
        first_byte_ts,
        t_complete: float,
        *,
        out_chars: int = 0,
        paged: bool = False,
    ) -> None:
        """Emit unified per-command latency (request → first_byte → complete).

        Logged to the ``terminal`` layer (``terminal.log``) because this is the
        live interactive-terminal path the WebSocket UI drives. Measurement
        only. We log ONLY the command verb (first token) as detail — never the
        full command line — so credential-bearing commands (e.g.
        ``username … password …``) never leak into the log. op="run_command".
        """
        try:
            verb = (command.split() or ["?"])[0]
            complete_ms = (t_complete - t_request) * 1000.0
            if first_byte_ts is not None:
                first_byte_ms = (first_byte_ts - t_request) * 1000.0
                obs.log_event(
                    "terminal", "run_command", "first_byte", "success",
                    elapsed_ms=first_byte_ms, detail=verb, paged=paged,
                )
            obs.log_event(
                "terminal", "run_command", "complete", "success",
                elapsed_ms=complete_ms, detail=verb, out_chars=out_chars,
                paged=paged,
            )
        except Exception:  # noqa: BLE001 - latency logging must never break exec
            pass

    def _audit_send(self, data, source: str, secret: bool = False) -> None:
        """Single chokepoint for EVERY byte written to the device channel.

        Phase 9 (Bug 1) diagnostic: the phantom "auto-replay" after a '?' help
        request means a bare CR reached the device while a partial command was
        buffered. To find the exact culprit, ALL writes funnel through here and
        are logged with their trigger source and a repr of the bytes. Any write
        that contains a submit newline (\\r / \\n) is flagged so the offending
        path is obvious in the logs.

        Bug 7 (RELEASE BLOCKER): credential writes pass ``secret=True`` so the
        raw bytes are NEVER rendered into the diagnostic log — only the length
        and a ``<redacted>`` placeholder are recorded. This guarantees an
        interactively-entered password can never leak through the audit trail.

        This is intentionally verbose; downgrade/remove the log level once the
        live-CML trace has confirmed no rogue submit remains.
        """
        try:
            payload = data if isinstance(data, (bytes, bytearray)) else str(data)
            has_submit = (
                (b"\r" in payload or b"\n" in payload)
                if isinstance(payload, (bytes, bytearray))
                else ("\r" in payload or "\n" in payload)
            )
            if secret:
                # NEVER log the bytes of a credential — redact unconditionally.
                log.debug(
                    "DEVICE-WRITE source=%s submit=%s bytes=<redacted len=%d>",
                    source, has_submit, len(payload),
                )
            else:
                log.debug(
                    "DEVICE-WRITE source=%s submit=%s bytes=%r",
                    source, has_submit, payload,
                )
        except Exception:
            pass
        self._chan.send(data)


    def _read_cml_prompt(self, timeout: float = 8.0) -> str:
        """Read until the CML 'consoles>' prompt appears."""
        buf = ""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                chunk = self._chan.recv(4096)
                if not chunk:
                    time.sleep(0.05)
                    continue
                buf += chunk.decode("utf-8", errors="replace")
                if "consoles>" in buf:
                    break
            except socket.timeout:
                if "consoles>" in buf:
                    break
            except Exception:
                break
        return buf

    def _read_until(self, timeout: float = 10.0) -> str:
        """
        Read from the channel until an IOS prompt appears or timeout expires.
        Strips ANSI escape sequences before checking for prompts.
        NOTE: Does NOT break on 'Press RETURN' or CML banners — callers
        handle those explicitly so this method stays general-purpose.
        NOTE: Does NOT auto-page '--More--'. Native paging is handled by the
        higher-level execute_paged()/pager_send_key() flow so the engineer drives
        the pager with raw keys, exactly like a real console.
        """
        buf = ""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                chunk = self._chan.recv(4096)
                if not chunk:
                    time.sleep(0.05)
                    continue
                # Phase A latency: stamp the first byte of this read cycle so
                # execute() can report request→first_byte latency. Measurement
                # only — does not alter read/prompt behavior.
                if getattr(self, "_read_first_byte_ts", None) is None:
                    self._read_first_byte_ts = time.monotonic()
                buf += chunk.decode("utf-8", errors="replace")
                stripped = _ANSI_STRIP.sub("", buf).rstrip()
                last = stripped.split("\n")[-1] if stripped else ""
                if _PROMPT_RE.search(last):
                    log.debug("_read_until: prompt match on %r", last[-40:])
                    self._current_prompt = last.strip()
                    break
                if _PASSWORD_RE.search(last):
                    break
                # EXEC-level logout ('exit'/'logout') parks the line at the
                # "Press RETURN to get started." banner — never a prompt. Break
                # so execute() can re-wake instead of blocking for the timeout.
                if _PRESS_RETURN_RE.search(stripped):
                    log.debug("_read_until: logout banner detected")
                    self._current_prompt = ""
                    break
            except socket.timeout:
                stripped = _ANSI_STRIP.sub("", buf).rstrip()
                last = stripped.split("\n")[-1] if stripped else ""
                if _PROMPT_RE.search(last):
                    log.debug("_read_until(timeout): prompt match on %r", last[-40:])
                    self._current_prompt = last.strip()
                    break
                if _PRESS_RETURN_RE.search(stripped):
                    log.debug("_read_until(timeout): logout banner detected")
                    self._current_prompt = ""
                    break
            except Exception as exc:
                log.debug("_read_until exception: %s", exc)
                break
        return buf

    def _read_page(
        self,
        timeout: float = 60.0,
        *,
        on_chunk=None,
        command: str | None = None,
        detect_interactive: bool = False,
    ) -> tuple[str, str]:
        """Read one page of output.

        Returns ``(buf, state)`` where ``state`` is:
          ``"prompt"``  — an IOS prompt appeared (output complete),
          ``"more"``    — a ``--More--`` pager prompt appeared (more to come),
          ``"input"``   — an interactive IOS question appeared (the device is
                          waiting for the operator to type an answer); only
                          detected when ``detect_interactive`` is True so normal
                          command output can never be misread as a question,
          ``"timeout"`` — neither appeared before the deadline.

        When ``on_chunk`` is provided it is called with each newly-available
        *cleaned* delta of output as bytes arrive (latency-masking UX only).
        The delta is computed from :func:`_clean_page` so the streamed text is
        identical to the final cleaned page — it never includes ANSI codes, the
        echoed command, or the trailing prompt. ``on_chunk`` is best-effort:
        exceptions are swallowed so streaming can never break the read.
        """
        buf = ""
        streamed = ""
        deadline = time.time() + timeout
        cmd_norm = command.strip().lower() if command else ""
        echo_cleared = False if cmd_norm else True
        output_started = False
        stream_cursor = 0
        while time.time() < deadline:
            try:
                chunk = self._chan.recv(4096)
                if not chunk:
                    time.sleep(0.05)
                    continue
                # Phase A latency: stamp the first byte of this read cycle so
                # execute_paged() can report request→first_byte latency.
                # Measurement only — does not alter read/prompt behavior.
                if getattr(self, "_read_first_byte_ts", None) is None:
                    self._read_first_byte_ts = time.monotonic()
                buf += chunk.decode("utf-8", errors="replace")

                # Echo discard: wait until the command echo line has completed
                if not echo_cleared:
                    if "\n" in buf:
                        first_nl = buf.find("\n")
                        first_line = (
                            _ANSI_STRIP.sub("", buf[:first_nl])
                            .replace("\r", "")
                            .replace("\x08", "")
                            .strip()
                            .lower()
                        )
                        if first_line == cmd_norm or first_line.startswith(cmd_norm) or cmd_norm.startswith(first_line):
                            stream_cursor = first_nl + 1
                            echo_cleared = True
                        else:
                            stream_cursor = 0
                            echo_cleared = True

                # Real-time incremental streaming: emit complete output lines live as they arrive.
                # Trailing incomplete lines (e.g. prompt, --More--, password) are held until newline.
                if echo_cleared and on_chunk is not None:
                    unstreamed = buf[stream_cursor:]
                    if "\n" in unstreamed:
                        last_nl = unstreamed.rfind("\n")
                        delta_raw = unstreamed[:last_nl + 1]
                        stream_cursor += last_nl + 1

                        delta = _ANSI_STRIP.sub("", delta_raw).replace("\x08", "").replace("\r\r\n", "\r\n")
                        if not output_started:
                            lines = delta.split("\n")
                            while lines and not lines[0].strip():
                                lines.pop(0)
                            if lines:
                                output_started = True
                                delta = "\n".join(lines)
                            else:
                                delta = ""
                        if delta:
                            try:
                                on_chunk(delta)
                            except Exception:
                                pass

                stripped = _ANSI_STRIP.sub("", buf).rstrip()
                last = stripped.split("\n")[-1] if stripped else ""
                if _PROMPT_RE.search(last):
                    self._current_prompt = last.strip()
                    return buf, "prompt"
                if _PASSWORD_RE.search(last):
                    return buf, "prompt"
                if _MORE_RE.search(stripped):
                    return buf, "more"
                # EXEC-level logout ('exit'/'logout') parks at the "Press RETURN
                # to get started." banner — signal it so execute_paged() can
                # re-wake to the user EXEC prompt instead of blocking to timeout.
                if _PRESS_RETURN_RE.search(stripped):
                    self._current_prompt = ""
                    return buf, "logout"
                # Interactive IOS question (e.g. 'configure' -> "[terminal]?").
                # Only when the caller opted in, so ordinary output never trips
                # it. The device is waiting for a typed answer, not a prompt.
                if detect_interactive and _INTERACTIVE_PROMPT_RE.search(last):
                    return buf, "input"
            except socket.timeout:
                stripped = _ANSI_STRIP.sub("", buf).rstrip()
                last = stripped.split("\n")[-1] if stripped else ""
                if _PROMPT_RE.search(last):
                    self._current_prompt = last.strip()
                    return buf, "prompt"
                if _MORE_RE.search(stripped):
                    return buf, "more"
                if _PRESS_RETURN_RE.search(stripped):
                    self._current_prompt = ""
                    return buf, "logout"
                if detect_interactive and _INTERACTIVE_PROMPT_RE.search(last):
                    return buf, "input"
            except Exception as exc:
                log.debug("_read_page exception: %s", exc)
                break

        if echo_cleared and on_chunk is not None:
            tail = buf[stream_cursor:]
            stripped_tail = _ANSI_STRIP.sub("", tail).rstrip()
            last = stripped_tail.split("\n")[-1].strip() if stripped_tail else ""
            if (
                tail
                and not _PROMPT_RE.search(last)
                and not _PASSWORD_RE.search(last)
                and not _MORE_RE.search(stripped_tail)
                and not _PRESS_RETURN_RE.search(stripped_tail)
            ):
                delta = _ANSI_STRIP.sub("", tail).replace("\x08", "").replace("\r\r\n", "\r\n")
                if delta.strip():
                    try:
                        on_chunk(delta)
                    except Exception:
                        pass

        return buf, "timeout"

    def execute_paged(self, command: str, on_chunk=None) -> tuple[str, bool]:
        """Run a command and return ``(page_text, more)``.

        ``more`` is True when the device is now sitting at a ``--More--`` pager
        prompt; call :meth:`pager_send_key` to advance it. Unlike :meth:`execute`
        this never auto-pages — the engineer controls the pager.

        ``on_chunk`` (optional) is a callback invoked with each newly-available
        cleaned delta as output streams in — latency-masking UX only. When
        omitted the behavior is byte-for-byte identical to before.
        """
        if not self.is_connected:
            raise ConnectionError("Not connected. Call connect() first.")
        command = command.strip()
        io_lock = getattr(self, "_io_lock", None)
        if io_lock is None:
            io_lock = self._io_lock = threading.RLock()
        with io_lock:
            self._check_connection()
            log.debug("SEND (paged) -> %r", command)
            # Phase A latency: request → first_byte → complete on the LIVE path.
            # This is the method the WebSocket terminal actually drives, so the
            # timing here is what populates terminal.log. Reset the first-byte
            # stamp, send, read; _read_page records when the first byte arrives.
            self._read_first_byte_ts = None
            _t_request = time.monotonic()
            self._send(command)
            raw, state = self._read_page(timeout=60, on_chunk=on_chunk, command=command)
            # EXEC-level logout ('exit'/'logout' from user or privileged EXEC)
            # parks the line at the "Press RETURN to get started." banner. Send a
            # bare RETURN to re-open the user EXEC ('>') prompt so the terminal
            # lands back in 'exec' mode immediately instead of the caller waiting
            # out the read timeout for a prompt that never arrives.
            if state == "logout":
                self._current_prompt = ""
            _t_complete = time.monotonic()
        more = state == "more"
        page = _clean_page(raw, command)
        self._log_command_latency(
            command, _t_request, self._read_first_byte_ts, _t_complete,
            out_chars=len(page), paged=more,
        )
        return page, more

    def execute_interactive(self, command: str, on_chunk=None) -> tuple[str, str]:
        """Run a command that may trigger an interactive IOS question.

        Submits with a bare CR (never CR+LF) so a trailing newline cannot
        auto-answer the device's default (this is exactly why a bare
        ``configure`` used to skip straight into config mode). Returns
        ``(page_text, state)`` where ``state`` is:
          ``"input"``  — the device is waiting at an interactive question
                         (e.g. "Configuring from terminal, memory, or network
                         [terminal]? "); feed the operator's answer back through
                         this same method,
          ``"prompt"`` — the device settled at a normal prompt (it either
                         answered itself or the command completed),
          ``"more"``   — a ``--More--`` pager appeared,
          ``"timeout"``— nothing settled before the deadline.

        The interactive question is detected only on this path, so ordinary
        command output is never misread as a prompt for input.
        """
        if not self.is_connected:
            raise ConnectionError("Not connected. Call connect() first.")
        command = command.strip()
        io_lock = getattr(self, "_io_lock", None)
        if io_lock is None:
            io_lock = self._io_lock = threading.RLock()
        with io_lock:
            self._check_connection()
            log.debug("SEND (interactive) -> %r", command)
            self._read_first_byte_ts = None
            _t_request = time.monotonic()
            # Bare CR so a stray LF can't answer the "[terminal]?" default.
            self._send(command, submit="\r")
            raw, state = self._read_page(
                timeout=30, on_chunk=on_chunk, command=command,
                detect_interactive=True,
            )
            _t_complete = time.monotonic()
        page = _clean_page(raw, command)
        self._log_command_latency(
            command, _t_request, self._read_first_byte_ts, _t_complete,
            out_chars=len(page),
        )
        return page, state

    def pager_send_key(self, key: str) -> tuple[str, bool]:
        """Send a single raw key to an active pager; return ``(page_text, more)``.

        ``key`` is a raw character: ``" "`` (next page), ``"\\r"`` (next line) or
        ``"q"`` (quit). No newline is appended — the pager consumes single keys.
        """
        if not self.is_connected:
            raise ConnectionError("Not connected. Call connect() first.")
        io_lock = getattr(self, "_io_lock", None)
        if io_lock is None:
            io_lock = self._io_lock = threading.RLock()
        with io_lock:
            self._check_connection()
            log.debug("PAGER-KEY -> %r", key)
            self._audit_send(key, source="pager_send_key")
            raw, state = self._read_page(timeout=60)
        more = state == "more"
        return _clean_page(raw), more

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self) -> "CMLConsoleDevice":
        self.connect()
        return self

    def __exit__(self, *_) -> None:
        self.disconnect()

    def __repr__(self) -> str:
        state = "connected" if self.is_connected else "disconnected"
        return f"CMLConsoleDevice({self.gateway}{self.path}, {state})"


# ---------------------------------------------------------------------------
# Direct SSH device  (Netmiko)
# ---------------------------------------------------------------------------

_IOS_TYPE_MAP: dict[str, str] = {
    "ios":   "cisco_ios",
    "iosxe": "cisco_ios",
    "iosxr": "cisco_xr",
    "nxos":  "cisco_nxos",
    "asa":   "cisco_asa",
    "auto":  "cisco_ios",
}

try:
    from netmiko import ConnectHandler, NetmikoTimeoutException, NetmikoAuthenticationException
    _NETMIKO_OK = True
except ImportError:
    _NETMIKO_OK = False


class LiveDevice:
    """Direct SSH connection via Netmiko (standalone IOS devices)."""

    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        port: int = 22,
        device_type: str = "cisco_ios",
        secret: str = "",
        timeout: int = 20,
    ) -> None:
        if not _NETMIKO_OK:
            raise ImportError("netmiko is not installed. Run: pip install netmiko")
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.device_type = _IOS_TYPE_MAP.get(device_type.lower(), device_type)
        self.secret = secret or password
        self.timeout = timeout
        self._conn = None
        self._connected = False

    def connect(self) -> None:
        log.info("Netmiko connect to %s:%d ...", self.host, self.port)
        try:
            self._conn = ConnectHandler(
                device_type=self.device_type,
                host=self.host,
                port=self.port,
                username=self.username,
                password=self.password,
                secret=self.secret,
                conn_timeout=self.timeout,
                banner_timeout=self.timeout,
                auth_timeout=self.timeout,
                global_delay_factor=1,
            )
            self._conn.enable()
            self._conn.send_command_timing("terminal length 0")
            self._conn.send_command_timing("terminal width 0")
            self._connected = True
            log.info("Connected to %s:%d", self.host, self.port)
        except NetmikoAuthenticationException as exc:
            raise ConnectionError(f"Auth failed for {self.username}@{self.host}:{self.port}") from exc
        except NetmikoTimeoutException as exc:
            raise ConnectionError(f"Timeout connecting to {self.host}:{self.port}") from exc
        except Exception as exc:
            raise ConnectionError(f"Failed to connect to {self.host}:{self.port}: {exc}") from exc

    def disconnect(self) -> None:
        if self._conn and self._connected:
            try:
                self._conn.disconnect()
            except Exception:
                pass
        self._connected = False
        self._conn = None

    @property
    def is_connected(self) -> bool:
        return self._connected and self._conn is not None

    def execute(self, command: str) -> str:
        if not self.is_connected:
            raise ConnectionError("Not connected.")
        command = command.strip()
        _t_request = time.monotonic()
        try:
            raw = self._conn.send_command(command, read_timeout=60)
        except Exception as exc:
            obs.log_event(
                "command", "run_command", "complete", "error",
                elapsed_ms=(time.monotonic() - _t_request) * 1000.0,
                detail=(command.split() or ["?"])[0],
            )
            raise ConnectionError(f"Command failed: {exc}") from exc
        output = clean(raw, command)
        obs.log_event(
            "command", "run_command", "complete", "success",
            elapsed_ms=(time.monotonic() - _t_request) * 1000.0,
            detail=(command.split() or ["?"])[0], out_chars=len(output),
        )
        return output

    def __enter__(self) -> "LiveDevice":
        self.connect()
        return self

    def __exit__(self, *_) -> None:
        self.disconnect()

    def __repr__(self) -> str:
        state = "connected" if self.is_connected else "disconnected"
        return f"LiveDevice({self.host}:{self.port}, {state})"


# ---------------------------------------------------------------------------
# Factory used by app.py
# ---------------------------------------------------------------------------

def connect_from_args(
    host: str = "",
    port: int = 22,
    username: str = "",
    password: str = "",
    device_type: str = "cisco_ios",
    secret: str = "",
    timeout: int = 20,
    # CML console path args
    gateway: str = "",
    path: str = "",
) -> "CMLConsoleDevice | LiveDevice":
    """
    Return a connected device object based on provided arguments.

    If gateway + path are given -> CMLConsoleDevice.
    If host is given             -> LiveDevice (Netmiko).
    """
    if gateway and path:
        dev: CMLConsoleDevice | LiveDevice = CMLConsoleDevice(
            gateway=gateway,
            username=username,
            password=password,
            path=path,
            timeout=timeout,
        )
    elif host:
        dev = LiveDevice(
            host=host,
            port=port,
            username=username,
            password=password,
            device_type=device_type,
            secret=secret,
            timeout=timeout,
        )
    else:
        raise ValueError("Provide either --gateway+--path (CML) or --host (direct SSH)")

    dev.connect()
    return dev
