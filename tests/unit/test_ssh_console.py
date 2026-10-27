#!/usr/bin/env python3
"""Unit tests for the SSH terminal-server console backend (real-HW transport).

These tests are pure and offline: netmiko is replaced with a scripted fake
channel so the whole SSH path (connect → identify → execute → ?-help) is
exercised without any device. They lock in the release-blocking guarantees for
real hardware:

  * PID → capability (ISR4331/K9 → router-only; VLAN/switching gated OFF).
  * Default-hostname handling (``Router>`` / ``Router#`` mode detection).
  * Input integrity (Bug N): typed bytes == sent bytes, logged/redacted.
  * Never-execute: ``?``-help is sent with NO submit newline.
  * Transport parity: SSHConsoleDevice satisfies the DeviceTransport protocol.
"""

from __future__ import annotations

import time

import pytest

from cliq.connection import ssh_console_device as sshmod

@pytest.fixture(autouse=True)
def _mock_netmiko_ok(monkeypatch):
    monkeypatch.setattr(sshmod, "_NETMIKO_OK", True)
    yield
from cliq.connection.ssh_console_device import (
    SSHConsoleDevice,
    build_console_username,
)
from cliq.connection.live_device import mode_from_prompt
from cliq.connection.output_cleaner import clean
from cliq.connection.transport import DeviceTransport
from cliq.validation.platform_rules import (
    capability_from_pid,
    node_type_from_pid,
    parse_pid_from_running_config,
    parse_pid_from_show_version,
    platform_capabilities,
)


# --- Real ISR4331 'show version' banner (trimmed to the identifying lines) ---
ISR4331_SHOW_VERSION = """\
Cisco IOS XE Software, Version 16.06.04
Cisco IOS Software [Everest], ISR Software (X86_64_LINUX_IOSD-UNIVERSALK9-M), Version 16.6.4

cisco ISR4331/K9 (1RU) processor with 1795979K/6147K bytes of memory.
Processor board ID FDO21120AAA
3 Gigabit Ethernet interfaces

License Level: appxk9
"""

ISR4331_SHOW_VERSION_PID_LINE = """\
cisco router (1RU) processor
PID: ISR4331/K9
"""


class FakeNetmikoConn:
    """Scripted stand-in for a netmiko ConnectHandler channel.

    Models the terminal-server flow: a bare newline written to the channel wakes
    the line and makes it emit ``wake_response`` (default: its base prompt), which
    the connect path reads to detect the prompt before redispatching to cisco_ios.
    """

    def __init__(self, show_version: str = ISR4331_SHOW_VERSION,
                 prompt: str = "Router#", wake_response: str | None = None,
                 run_config: str = "license udi pid ISR4331/K9 sn FDO21120AAA") -> None:
        self.prompt = prompt
        self.wake_response = wake_response
        self._enabled = False
        self.sent_commands: list[str] = []          # send_command args (typed==sent proof)
        self.timing_commands: list[str] = []
        self.channel_writes: list[str] = []          # raw write_channel bytes
        self._read_queue: list[str] = []
        self.disconnected = False
        self._show_version = show_version
        self._run_config = run_config

    # -- lifecycle / prompt --------------------------------------------------
    def is_alive(self) -> bool:
        return not self.disconnected

    def check_enable_mode(self) -> bool:
        return self._enabled

    def enable(self) -> None:
        self._enabled = True
        self.prompt = "Router#"

    def find_prompt(self) -> str:
        return self.prompt

    def set_base_prompt(self, *_a, **_k) -> str:
        return self.prompt.rstrip("#>")

    def disconnect(self) -> None:
        self.disconnected = True

    # -- command execution ---------------------------------------------------
    def send_command_timing(self, command: str, **_kw) -> str:
        self.timing_commands.append(command)
        return ""

    def _apply_mode(self, cmd: str) -> None:
        """Track CLI-mode transitions so the dynamic prompt reflects them."""
        low = cmd.lower().strip()
        if low in ("configure terminal", "conf t", "config t", "configure"):
            self.prompt = "Router(config)#"
        elif low.startswith("interface "):
            self.prompt = "Router(config-if)#"
        elif low.startswith("router "):
            self.prompt = "Router(config-router)#"
        elif low == "end":
            self.prompt = "Router#"
        elif low == "exit":
            if self.prompt in ("Router(config-if)#", "Router(config-router)#"):
                self.prompt = "Router(config)#"
            elif self.prompt == "Router(config)#":
                self.prompt = "Router#"
        elif low == "disable":
            self.prompt = "Router>"
        elif low == "enable":
            self.prompt = "Router#"
            self._enabled = True

    def _response_for(self, cmd: str) -> str:
        low = cmd.lower()
        if low.startswith("show version"):
            return self._show_version
        if "running-config" in low and "license udi" in low:
            return self._run_config
        return ""

    # -- raw channel (used by console wake + command exec + ?-help) ---------
    def write_channel(self, data: str) -> None:
        self.channel_writes.append(data)
        # A bare newline wakes the console line: emit the wake response so the
        # connect path can detect the prompt (or a busy/occupied message).
        if data in ("\r\n", "\n", "\r"):
            self._read_queue.append(
                self.wake_response if self.wake_response is not None
                else f"\r\n{self.prompt} "
            )
            return
        # A newline-terminated non-empty write is a SUBMITTED command: record it
        # (typed==sent proof), apply any mode change, then emit the echo + body +
        # the (possibly new) prompt so the dynamic-prompt read completes.
        if data.endswith(("\r\n", "\n", "\r")) and data.strip():
            cmd = data.strip()
            self.sent_commands.append(cmd)
            body = self._response_for(cmd)
            self._apply_mode(cmd)
            self._read_queue.append(f"{cmd}\r\n{body}\r\n{self.prompt} ")
            return
        # Otherwise a raw partial write (?-help query, pager space, Ctrl-U): the
        # test drives the read queue explicitly via queue_read().

    def queue_read(self, *chunks: str) -> None:
        self._read_queue.extend(chunks)

    def read_channel(self) -> str:
        return self._read_queue.pop(0) if self._read_queue else ""


@pytest.fixture(autouse=True)
def _patch_redispatch(monkeypatch):
    """netmiko.redispatch reconfigures a live socket; stub it for offline tests.

    The connect path calls ``redispatch(conn, device_type=..., session_prep=False)``
    to upgrade the raw terminal_server session to cisco_ios. Against the fake
    channel this is a no-op — the fake already behaves as a cisco_ios session.
    """
    monkeypatch.setattr(sshmod, "redispatch", lambda *a, **k: None)


@pytest.fixture
def fake_conn():
    return FakeNetmikoConn()


@pytest.fixture
def connected_device(monkeypatch, fake_conn):
    """An SSHConsoleDevice connected against the fake netmiko channel."""
    monkeypatch.setattr(sshmod, "ConnectHandler", lambda **kw: fake_conn)
    dev = SSHConsoleDevice(
        host="ts01.example.org",
        username="operator",
        password="secret-pw",
        port_user="19",
    )
    dev.connect()
    return dev, fake_conn


# ── PID → capability (pure functions) ────────────────────────────────────────
class TestPidCapability:
    def test_parse_pid_from_banner(self):
        assert parse_pid_from_show_version(ISR4331_SHOW_VERSION) == "ISR4331/K9"

    def test_parse_pid_from_explicit_pid_line(self):
        assert parse_pid_from_show_version(ISR4331_SHOW_VERSION_PID_LINE) == "ISR4331/K9"

    def test_parse_pid_missing(self):
        assert parse_pid_from_show_version("no model here") == ""

    def test_parse_pid_from_license_udi_in_show_version(self):
        # A trimmed 'show version' lacking the banner still yields the PID via
        # its 'license udi pid ...' line.
        text = "Cisco IOS XE Software\nlicense udi pid ISR4331/K9 sn FDO21120AAA\n"
        assert parse_pid_from_show_version(text) == "ISR4331/K9"

    def test_parse_pid_from_running_config(self):
        cfg = "!\nlicense udi pid ISR4331/K9 sn FDO21120AAA\n!\n"
        assert parse_pid_from_running_config(cfg) == "ISR4331/K9"

    def test_parse_pid_from_running_config_missing(self):
        assert parse_pid_from_running_config("hostname Router") == ""

    def test_isr4331_is_router_only(self):
        caps = capability_from_pid("ISR4331/K9")
        assert caps == frozenset({"routing"})
        assert "switching" not in caps  # VLAN/switching gated OFF on real router

    def test_switch_pid_is_switching(self):
        assert capability_from_pid("C9300-48P") == frozenset({"switching"})
        assert capability_from_pid("WS-C2960X-48") == frozenset({"switching"})

    def test_unknown_pid_is_permissive(self):
        # Unknown → both, so the device (not the table) decides.
        assert capability_from_pid("MYSTERY-9000") == frozenset({"routing", "switching"})
        assert capability_from_pid("") == frozenset({"routing", "switching"})

    def test_node_type_from_pid_strips_suffix(self):
        assert node_type_from_pid("ISR4331/K9") == "isr4331"
        assert node_type_from_pid("") == ""


# ── username composition ─────────────────────────────────────────────────────
class TestConsoleUsername:
    def test_builds_port_mapped_username(self):
        assert build_console_username("operator", "19") == "operator:19"
        assert build_console_username("operator", 18) == "operator:18"

    def test_preserves_already_suffixed_username(self):
        assert build_console_username("operator:19", "99") == "operator:19"

    def test_empty_port(self):
        assert build_console_username("operator", "") == "operator"


# ── connect → identify (default hostname + capability gating) ────────────────
class TestConnectIdentify:
    def test_connect_lands_enables_and_identifies(self, connected_device):
        dev, conn = connected_device
        assert dev.is_connected is True
        # Default hostname 'Router' handled: privileged prompt detected.
        assert dev.current_prompt == "Router#"
        # PID captured from show version → router capability, VLAN gated off.
        assert dev.pid == "ISR4331/K9"
        assert dev.node_type == "isr4331"
        assert dev.capability == frozenset({"routing"})
        assert "switching" not in dev.capability
        # 'show version' really was executed on the fake channel.
        assert any(c.startswith("show version") for c in conn.sent_commands)

    def test_enable_called_when_in_user_exec(self, monkeypatch):
        fake = FakeNetmikoConn(prompt="Router>")
        monkeypatch.setattr(sshmod, "ConnectHandler", lambda **kw: fake)
        dev = SSHConsoleDevice("h", "u", "p", port_user="19")
        dev.connect()
        assert fake._enabled is True  # escalated from user EXEC to privileged

    def test_connect_emits_ssh_console_event_with_redaction(self, monkeypatch, fake_conn):
        events: list[dict] = []
        monkeypatch.setattr(
            sshmod.obs, "log_event",
            lambda *a, **k: events.append((a, k)) or {},
        )
        monkeypatch.setattr(sshmod, "ConnectHandler", lambda **kw: fake_conn)
        dev = SSHConsoleDevice("ts01.example.org", "operator",
                               "secret-pw", port_user="19")
        dev.connect()
        # The required connect summary event fired with transport + capability.
        summary = [k for (a, k) in events
                   if a[:2] == ("connection", "ssh_console_connect")]
        assert summary, "ssh_console_connect event not emitted"
        last = summary[-1]
        assert last["transport"] == "ssh_console"
        assert last["port_user"] == "operator:19"
        assert last["capability_class"] == "routing"
        # The password never appears anywhere in the emitted event kwargs.
        blob = repr(events)
        assert "secret-pw" not in blob


# ── terminal-server connect strategy (redispatch, busy console) ──────────────
class TestTerminalServerConnect:
    def test_connects_as_terminal_server_then_redispatches_cisco_ios(self, monkeypatch):
        """Connect uses device_type='terminal_server' (skips the failing auto
        session-setup) then redispatches to cisco_ios with session_prep OFF."""
        captured: dict = {}
        fake = FakeNetmikoConn()

        def fake_ch(**kw):
            captured.update(kw)
            return fake

        redis: dict = {}

        def fake_redispatch(conn, device_type, session_prep=True):
            redis["device_type"] = device_type
            redis["session_prep"] = session_prep

        monkeypatch.setattr(sshmod, "ConnectHandler", fake_ch)
        monkeypatch.setattr(sshmod, "redispatch", fake_redispatch)

        dev = SSHConsoleDevice("h", "u", "p", port_user="19", device_type="cisco_ios")
        dev.connect()

        # Raw terminal-server connect avoids the 'terminal width 511' probe.
        assert captured["device_type"] == "terminal_server"
        # Console echo verification is relaxed to tolerate console latency.
        assert captured["global_cmd_verify"] is False
        # Upgraded to full IOS-XE handling, skipping the auto session-prep.
        assert redis["device_type"] == "cisco_ios"
        assert redis["session_prep"] is False

    def test_wake_sends_newline_before_prompt_detect(self, monkeypatch):
        """The console is woken with a CRLF newline before the prompt is read."""
        fake = FakeNetmikoConn()
        monkeypatch.setattr(sshmod, "ConnectHandler", lambda **kw: fake)
        dev = SSHConsoleDevice("h", "u", "p", port_user="19")
        dev.connect()
        assert any(w in ("\r\n", "\n", "\r") for w in fake.channel_writes)

    def test_busy_console_raises_distinct_error(self, monkeypatch):
        """A console occupied by another session is a ConsoleBusyError, NOT an
        auth failure — with a message that guides the operator to free the line."""
        fake = FakeNetmikoConn(wake_response="\r\nline 19 is in use\r\n")
        monkeypatch.setattr(sshmod, "ConnectHandler", lambda **kw: fake)
        dev = SSHConsoleDevice("ts01.example.org", "operator",
                               "pw", port_user="19")
        with pytest.raises(sshmod.ConsoleBusyError) as ei:
            dev.connect()
        msg = str(ei.value).lower()
        assert "in use" in msg
        assert "no other session" in msg

    def test_busy_error_is_a_connection_error(self):
        """ConsoleBusyError stays a ConnectionError so generic handling catches it."""
        assert issubclass(sshmod.ConsoleBusyError, sshmod.ConnectionError)


# ── dynamic prompt tracking (Bug 1) + output capture (Bug 4) ─────────────────
class TestDynamicPromptTracking:
    def test_config_mode_prompt_tracked(self, connected_device):
        """'configure terminal' → prompt tracked to Router(config)#, no static-
        prompt "pattern not detected" failure."""
        dev, _ = connected_device
        dev.execute("configure terminal")
        assert dev.current_prompt == "Router(config)#"
        assert mode_from_prompt(dev.current_prompt) == "config"

    def test_interface_submode_prompt_tracked(self, connected_device):
        dev, _ = connected_device
        dev.execute("configure terminal")
        dev.execute("interface GigabitEthernet0/0/0")
        assert dev.current_prompt == "Router(config-if)#"
        assert mode_from_prompt(dev.current_prompt) == "config-if"

    def test_end_returns_to_privileged(self, connected_device):
        dev, _ = connected_device
        dev.execute("configure terminal")
        dev.execute("end")
        assert dev.current_prompt == "Router#"
        assert mode_from_prompt(dev.current_prompt) == "enable"

    def test_exit_steps_out_one_submode(self, connected_device):
        dev, _ = connected_device
        dev.execute("configure terminal")
        dev.execute("interface GigabitEthernet0/0/0")
        dev.execute("exit")
        assert dev.current_prompt == "Router(config)#"

    def test_command_writes_carry_single_submit_newline(self, connected_device):
        """Dynamic-prompt exec writes command + exactly one CRLF (input integrity)."""
        dev, conn = connected_device
        conn.channel_writes.clear()
        dev.execute("show ip route")
        cmd_writes = [w for w in conn.channel_writes if w.strip() == "show ip route"]
        assert cmd_writes == ["show ip route\r\n"]

    def test_output_captured_between_echo_and_prompt(self, connected_device):
        """Full output (not empty) is captured between the echo and next prompt."""
        dev, _ = connected_device
        page = dev.execute("show version")
        assert "ISR4331/K9" in page  # the interface/version body really came back

# ── ?-help isolation + NUL (^@) contamination + PID fallback (real HW bugs) ──
class TestHelpIsolationAndNulls:
    def test_stream_command_strips_null_bytes(self, connected_device):
        """NUL (^@) bytes from a console are stripped at the source (Bug N)."""
        dev, conn = connected_device
        conn.queue_read("show clock\r\n\x00\x0012:00:00.000 UTC\r\nRouter# ")
        _raw, out = dev._stream_command("show clock", timeout=2.0)
        assert "\x00" not in out
        assert "12:00:00" in out

    def test_flush_channel_drains_residual_and_strips_nulls(self, connected_device):
        """Residual bytes (incl. ^@) are fully drained so nothing leaks forward."""
        dev, conn = connected_device
        conn.queue_read("\x00\x00", "leftover echo", "Router# ")
        drained = dev._flush_channel(idle=0.2, timeout=1.0)
        assert "\x00" not in drained
        # The channel is empty afterwards — the next command starts clean.
        assert conn.read_channel() == ""

    def test_command_after_help_is_uncontaminated(self, connected_device):
        """The first command AFTER a ?-help is clean: typed==sent, no ^@, output."""
        dev, conn = connected_device
        # Help text, then residual (incl. NUL) that WOULD leak into the next cmd.
        conn.queue_read("  ip  Interior protocol\n", "", "\x00\x00Router# ")
        dev.context_help_start("sh ")

        conn.sent_commands.clear()
        conn.channel_writes.clear()
        out = dev.execute("show ip interface brief")
        # Exact bytes reached the device (no ghost/^@ drift), single submit newline.
        assert conn.sent_commands == ["show ip interface brief"]
        assert conn.channel_writes[0] == "show ip interface brief\r\n"
        assert "\x00" not in out

    def test_help_flushes_channel_before_returning(self, connected_device):
        """context_help_start drains the channel so no residual remains queued."""
        dev, conn = connected_device
        conn.queue_read("  ip  proto\n", "", "\x00residual", "Router# ")
        dev.context_help_start("sh ")
        # Everything the fake queued for the help interaction was consumed.
        assert conn.read_channel() == ""

    def test_identify_falls_back_to_running_config_for_pid(self, monkeypatch):
        """When 'show version' lacks a PID, identify parses 'license udi pid'."""
        no_pid_ver = "Cisco IOS XE Software, Version 16.06.04\nbanner without a pid\n"
        fake = FakeNetmikoConn(show_version=no_pid_ver)
        monkeypatch.setattr(sshmod, "ConnectHandler", lambda **kw: fake)
        dev = SSHConsoleDevice("h", "u", "p", port_user="19")
        dev.connect()
        assert dev.pid == "ISR4331/K9"
        assert dev.capability == frozenset({"routing"})
        # The fallback command really was issued.
        assert any("running-config" in c and "license udi" in c
                   for c in fake.sent_commands)


# ── execute read-timing: delayed terminal-server output (NOT ?-help) ────────
class _DelayedEchoConn(FakeNetmikoConn):
    """Terminal-server console whose echo + output LAG behind the send.

    ``read_channel`` returns nothing until the delay elapses, then delivers the
    full ``echo + output + fresh prompt`` in one go — modelling a laggy terminal
    server that dribbles the response late. There is NO stale prompt to skip:
    connect drains the channel (``set_base_prompt``/``find_prompt``) so, exactly
    like the CML path, each command reads only to its OWN prompt. The reader must
    simply wait for the bytes and forward them once — no retry, no grace, no
    echo gate.
    """

    def __init__(self, real_response: str, delay: float = 0.3, **kw) -> None:
        super().__init__(**kw)
        self._real = real_response
        self._delay = delay
        self._release_at: float | None = None

    def write_channel(self, data: str) -> None:  # type: ignore[override]
        self.channel_writes.append(data)
        if data.endswith(("\r\n", "\n", "\r")) and data.strip():
            self.sent_commands.append(data.strip())
            self._release_at = time.time() + self._delay  # withhold the echo

    def read_channel(self) -> str:  # type: ignore[override]
        if self._release_at is not None and time.time() >= self._release_at:
            self._release_at = None
            return self._real
        return self._read_queue.pop(0) if self._read_queue else ""


class TestExecuteReadTimingAndNulls:
    def test_stream_command_waits_for_delayed_output(self, connected_device):
        """Mirroring CML: the reader polls the channel and forwards the output
        the instant it arrives, breaking on the FIRST prompt — no echo gate, no
        grace, no stale-prompt re-read."""
        real = "show ip int br\r\nGi0/0/0  10.0.0.1  up  up\r\nRouter# "
        fake = _DelayedEchoConn(real_response=real, delay=0.3)
        dev = SSHConsoleDevice("h", "u", "p", port_user="19")
        dev._conn = fake
        dev._connected = True
        dev._current_prompt = "Router#"
        fake.write_channel("show ip int br\r\n")   # arm the delayed release
        _raw, out = dev._stream_command("show ip int br", timeout=3.0)
        assert "\x00" not in out
        assert "Gi0/0/0  10.0.0.1  up  up" in out

    def test_stream_command_times_out_when_no_prompt_arrives(self, connected_device):
        """If bytes never arrive the read simply times out (never hangs) and
        emits NOTHING — identical to CML's recv-loop deadline."""
        dev, conn = connected_device
        chunks: list[str] = []
        _raw, out = dev._stream_command("show running-config", on_chunk=chunks.append,
                                        timeout=0.5)
        assert "\x00" not in out
        assert chunks == []          # nothing to emit while the channel is silent

    def test_first_command_delayed_output_no_null_no_rerun(self):
        """FIRST command with a laggy terminal server returns the full output on
        the first pass with NO ^@ and NO re-run — the reader waits for the bytes
        like CML and the command is written exactly once."""
        real = "show ip int br\r\nGi0/0/0  10.0.0.1  YES  up  up\r\nRouter# "
        fake = _DelayedEchoConn(real_response=real, delay=0.3)
        dev = SSHConsoleDevice("h", "u", "p", port_user="19")
        dev._conn = fake
        dev._connected = True
        dev._current_prompt = "Router#"

        out = dev.execute("show ip int br")

        assert "\x00" not in out
        assert "Gi0/0/0  10.0.0.1  YES  up  up" in out
        # typed==sent: the command was written exactly once (never double-run).
        assert fake.sent_commands == ["show ip int br"]
        assert fake.channel_writes[0] == "show ip int br\r\n"

    def test_echo_line_with_space_stripped_from_output(self, connected_device):
        """A prompt-merged echo WITH a space ('Router# show ...') is dropped so
        neither the prompt nor the echoed command surfaces in the output."""
        dev, _ = connected_device
        merged = "Router# show ip int br\r\nGi0/0/0  10.0.0.1  up  up\r\nRouter# "
        out = clean(dev._normalize_console_output(merged, "show ip int br"),
                    "show ip int br")
        assert not out.lstrip().lower().startswith("router#")
        assert "show ip int br" not in out.splitlines()[0].lower()
        assert "Gi0/0/0  10.0.0.1  up  up" in out

    def test_echo_line_no_space_stripped_from_output(self, connected_device):
        """A prompt-merged echo with NO space ('Router#show running-config' —
        the real ISR4331 form) is still recognised and dropped."""
        dev, _ = connected_device
        raw = ("\x00\r\nRouter#show running-config\r\n"
               "Building configuration...\r\nhostname Router\r\nRouter# ")
        out = clean(dev._normalize_console_output(raw, "show running-config"),
                    "show running-config")
        assert "\x00" not in out
        assert "show running-config" not in out.lower()
        assert out.splitlines()[0] == "Building configuration..."
        assert "hostname Router" in out

    def test_bare_cr_repaint_collapses_no_duplicate(self, connected_device):
        """A bare-CR line repaint ('...up d\\r...up down') collapses to ONE
        complete line instead of duplicating as a partial + full line."""
        dev, _ = connected_device
        raw = ("show ip int br\r\n"
               "Async0/2/7 unassigned NO unset up d\r"
               "Async0/2/7 unassigned NO unset up down\r\n"
               "Router# ")
        out = clean(dev._normalize_console_output(raw, "show ip int br"),
                    "show ip int br")
        assert out.count("Async0/2/7") == 1
        assert "up down" in out
        assert "up d\n" not in out  # no dangling partial line

    def test_normalize_leaves_real_output_untouched(self, connected_device):
        """When the first line is genuine output (no echo), nothing is dropped."""
        dev, _ = connected_device
        raw = "Gi0/0/0  10.0.0.1  up  up\r\nRouter# "
        out = dev._normalize_console_output(raw, "show ip int br")
        assert "Gi0/0/0  10.0.0.1  up  up" in out

    def test_stream_forwards_each_line_once_across_chunks(self, connected_device):
        """Continuous streaming: output that arrives across MANY raw reads is
        forwarded byte-for-byte ONCE — the concatenated stream equals the final
        page and no line is ever repeated (the stale-buffer replay that looped
        'Async0/2/13 ... down' forever cannot happen with a monotonic cursor)."""
        dev, conn = connected_device
        conn.queue_read(
            "sh ip int br\r\n",                                   # echo
            "Async0/2/13  unassigned  NO  unset  down  down\r\n",
            "Async0/2/14  unassigned  NO  unset  down  down\r\n",
            "GigabitEthernet0/0/0  10.0.0.1  YES  up  up\r\n",
            "Router# ",                                           # trailing prompt
        )
        chunks: list[str] = []
        _raw, page = dev._stream_command("sh ip int br", on_chunk=chunks.append)
        streamed = "".join(chunks)
        # Each byte forwarded exactly once: the live stream == the final block.
        assert streamed == page
        # No line is ever emitted twice — not in the page, not in the stream.
        for line in ("Async0/2/13", "Async0/2/14", "GigabitEthernet0/0/0"):
            assert page.count(line) == 1
            assert streamed.count(line) == 1
        # The trailing prompt is never part of the streamed body.
        assert "Router#" not in page
        assert "Router#" not in streamed

    def test_stream_never_reemits_already_sent_bytes(self, connected_device):
        """A partial line delivered across reads is completed in place (like a
        real terminal) and NEVER re-sent from the start — deltas only ever
        append, so the display cannot show a duplicated/partial line."""
        dev, conn = connected_device
        conn.queue_read(
            "sh ip int br\r\n",                       # echo
            "GigabitEthernet0/0/0  10.0.0.1  YES  ",  # partial line
            "up  up\r\n",                             # completes the SAME line
            "Router# ",
        )
        chunks: list[str] = []
        _raw, page = dev._stream_command("sh ip int br", on_chunk=chunks.append)
        streamed = "".join(chunks)
        assert streamed == page
        # The interface line appears exactly once despite arriving in two reads.
        assert page.count("GigabitEthernet0/0/0") == 1
        # Every delta is a strict forward append (never a replay of the start).
        acc = ""
        for delta in chunks:
            assert page.startswith(acc + delta)
            acc += delta

    def test_output_free_command_returns_immediately(self, connected_device):
        """A genuinely output-less command (empty body) returns empty right away —
        the reader breaks on the first prompt, no retry loop, no re-read."""
        dev, conn = connected_device
        # The fake echoes the command with an empty body + a fresh prompt.
        out = dev.execute("configure terminal")
        assert out == ""
        assert conn.sent_commands[-1] == "configure terminal"


# ── single clean render per command: no ^@/empty stream, no partial re-emit ──
class TestSingleRenderDisplayPath:
    def _wire(self, real: str, delay: float = 0.3) -> tuple[SSHConsoleDevice, _DelayedEchoConn]:
        fake = _DelayedEchoConn(real_response=real, delay=delay)
        dev = SSHConsoleDevice("h", "u", "p", port_user="19")
        dev._conn = fake
        dev._connected = True
        dev._current_prompt = "Router#"
        return dev, fake

    def test_execute_paged_streams_final_output_once(self):
        """The full clean body is streamed EXACTLY once — no ^@, no per-read
        intermediate emits, and the streamed text equals the returned page."""
        real = "show ip int br\r\nGi0/0/0  10.0.0.1  up  up\r\nRouter# "
        dev, _ = self._wire(real)
        chunks: list[str] = []
        page, more = dev.execute_paged("show ip int br", on_chunk=chunks.append)
        assert more is False
        assert len(chunks) == 1                       # emitted once, not per-read
        assert "\x00" not in chunks[0]                # no ^@ leaked to display
        assert chunks[0] == page                      # streamed == final block
        assert "Gi0/0/0  10.0.0.1  up  up" in page
        assert page.count("Gi0/0/0  10.0.0.1  up  up") == 1  # no duplicate/partial

    def test_execute_paged_no_stream_for_empty_output(self, connected_device):
        """An output-less command streams NOTHING (no empty/^@ chunk)."""
        dev, _ = connected_device
        chunks: list[str] = []
        page, more = dev.execute_paged("configure terminal", on_chunk=chunks.append)
        assert page == ""
        assert chunks == []

    def test_delayed_output_no_duplicate_no_stale_prompt(self):
        """Laggy output is captured once and cleanly — the final body has no
        duplicate lines, no ^@ and no trailing prompt artifact."""
        real = "show clock\r\n*14:58:02.000 UTC Wed Jul 1 2026\r\nRouter# "
        dev, fake = self._wire(real)
        out = dev.execute("show clock")
        assert "\x00" not in out
        assert out.count("*14:58:02.000 UTC Wed Jul 1 2026") == 1
        assert "Router#" not in out                   # trailing prompt trimmed
        assert fake.sent_commands == ["show clock"]   # sent once (never re-run)


# ── input integrity (Bug N) + never-execute ─────────────────────────────────
class TestInputIntegrityAndSafety:
    def test_typed_equals_sent(self, connected_device):
        dev, conn = connected_device
        conn.sent_commands.clear()
        dev.execute("show ip route")
        # Exactly the typed command reached the device (no ghost/echo drift).
        assert conn.sent_commands == ["show ip route"]

    def test_audit_logs_typed_command(self, connected_device, caplog):
        import logging
        dev, conn = connected_device
        with caplog.at_level(logging.DEBUG):
            dev.execute("show running-config")
        logged = " ".join(r.getMessage() for r in caplog.records)
        assert "show running-config" in logged

    def test_help_query_sends_no_newline(self, connected_device):
        dev, conn = connected_device
        conn.queue_read("  ip     Interior/exterior protocol\n  interface  Select\n", "")
        conn.channel_writes.clear()
        dev.context_help_start("sh ")
        # The '?'-help write must carry NO submit newline (never-execute).
        help_writes = [w for w in conn.channel_writes if "?" in w]
        assert help_writes, "no help query written"
        for w in help_writes:
            assert "\n" not in w and "\r" not in w

    def test_help_clears_line_with_ctrl_u(self, connected_device):
        dev, conn = connected_device
        conn.queue_read("  ip   proto\n", "")
        conn.channel_writes.clear()
        dev.context_help_start("sh ")
        # Ctrl-U (0x15) is used to clear the re-displayed line — never Ctrl-C.
        assert any("\x15" in w for w in conn.channel_writes)
        assert not any("\x03" in w for w in conn.channel_writes)


# ── transport parity ─────────────────────────────────────────────────────────
class TestTransportParity:
    def test_conforms_to_device_transport_protocol(self, connected_device):
        dev, _ = connected_device
        assert isinstance(dev, DeviceTransport)

    def test_execute_paged_returns_single_page(self, connected_device):
        dev, conn = connected_device
        page, more = dev.execute_paged("show ip interface brief")
        assert more is False
        assert isinstance(page, str)

    def test_pager_send_key_is_noop(self, connected_device):
        dev, _ = connected_device
        assert dev.pager_send_key(" ") == ("", False)

    def test_awaiting_auth_is_false(self, connected_device):
        dev, _ = connected_device
        assert dev.awaiting_auth is False

    def test_reattach_reconnects_and_returns_mode(self, monkeypatch):
        conns = [FakeNetmikoConn(), FakeNetmikoConn()]
        monkeypatch.setattr(sshmod, "ConnectHandler", lambda **kw: conns.pop(0))
        dev = SSHConsoleDevice("h", "u", "p", port_user="19")
        dev.connect()
        mode = dev.reattach()
        assert mode == "enable"  # 'Router#' → privileged EXEC
        assert dev.is_connected is True


# ── capability gating on a real router matches PID (regression guard) ─────────
class TestVlanGatedOnRealRouter:
    def test_router_capability_excludes_switching(self, connected_device):
        dev, _ = connected_device
        # 'switching' commands (vlan/switchport) are NOT in the capability set,
        # so the server's soft capability gate suppresses them on this router.
        assert "switching" not in dev.capability
        assert "routing" in dev.capability

    def test_platform_capabilities_untouched_for_cml(self):
        # CML node-type mapping is unchanged (iosvl2 stays switching-only).
        assert platform_capabilities("iosvl2") == frozenset({"switching"})


# ── literal '^@' (0x5e 0x40) strip + echo-synchronized read ──────────────────
class TestLiteralCaretAtAndDrain:
    """The terminal server emits the LITERAL two-char sequence '^' (0x5e) + '@'
    (0x40) as visible text — NOT a NUL byte. It also BUFFERS the PREVIOUS
    command's trailing bytes, which a naive read would return as THIS command's
    output (the off-by-one). These lock in the literal-strip and the
    echo-synchronized read (find this command's echo → capture only after it →
    discard prior/stale) while proving the real '^' error marker survives."""

    def test_normalize_strips_literal_caret_at(self, connected_device):
        # Exactly the trace: '^@' is literal text (0x5e 0x40), not '\x00'.
        dev, _ = connected_device
        assert [ord(c) for c in "^@"] == [0x5E, 0x40]  # proves the byte pattern
        raw = "^@\r\nRouter#show clock\r\n*12:00:00.000 UTC Mon\r\nRouter# "
        out = clean(dev._normalize_console_output(raw, "show clock"), "show clock")
        assert "^@" not in out
        assert "*12:00:00.000 UTC Mon" in out

    def test_clean_stream_strips_literal_caret_at(self, connected_device):
        dev, _ = connected_device
        raw = "show clock\r\n^@\r\n*12:00:00.000 UTC\r\nRouter#^@ "
        out = dev._clean_stream(raw, "show clock")
        assert "^@" not in out
        assert "*12:00:00" in out

    def test_real_caret_error_marker_preserved(self, connected_device):
        # A genuine '% Invalid input' caret marker: '^' is NOT followed by '@',
        # so it must survive the literal-'^@' strip (no over-stripping).
        dev, _ = connected_device
        raw = ("sh ip int bre\r\n"
               "                   ^\r\n"
               "% Invalid input detected at '^' marker.\r\n"
               "Router# ")
        out = dev._normalize_console_output(raw, "sh ip int bre")
        assert "^" in out
        assert "% Invalid input detected at '^' marker." in out

    def test_execute_paged_no_precommand_drain_preserves_fast_output(
        self, connected_device
    ):
        # Regression: a fast/small command's OWN output must NEVER be discarded
        # by a pre-command drain. 'show ip dhcp binding' returns its body, not ''
        # (the drain that ate quick responses is removed — plain send→read).
        dev, conn = connected_device
        conn.queue_read(
            "show ip dhcp binding\r\n"
            "Bindings from all pools not associated with VRF:\r\n"
            "IP address    Client-ID    Lease expiration    Type\r\n"
            "Router# "
        )
        page, more = dev.execute_paged("show ip dhcp binding")
        assert more is False
        assert "Bindings from all pools" in page   # real output preserved
        assert "IP address" in page
        # typed==sent: exactly one write, unaltered.
        assert conn.sent_commands[-1] == "show ip dhcp binding"
        assert "show ip dhcp binding\r\n" in conn.channel_writes

    def test_find_command_echo_matches_prompt_prefixed_line(self, connected_device):
        # The echo lands as '<prompt><command>' (often no space). _find_command_echo
        # returns the index of the FIRST byte AFTER that echo line so the caller
        # can slice off the stale prefix + echo. A prior command's echo must NOT
        # match (that is the whole point of the sync).
        dev, _ = connected_device
        buf = ("^@\r\nRouter#show ip int brief\r\nOUTPUT-A\r\n"
               "Router#show ip dhcp binding\r\nOUTPUT-B\r\nRouter# ")
        idx = dev._find_command_echo(buf, "show ip dhcp binding")
        assert idx is not None
        assert buf[idx:].startswith("OUTPUT-B")   # only THIS command's output
        assert "OUTPUT-A" not in buf[idx:]         # prior output discarded
        # A partial echo (no terminating newline) is NOT matched yet — its output
        # boundary is unknown, so the reader keeps waiting instead of stalling.
        assert dev._find_command_echo("Router#show ip dhcp binding", "show ip dhcp binding") is None

    def test_execute_paged_echo_sync_returns_current_not_previous(self, monkeypatch):
        # OFF-BY-ONE GUARD: the terminal server still holds command A's response
        # when command B is sent, so B's first read hands back A's stale block
        # (ending in a trailing 'Router#'). Echo sync must discard it and return
        # B's OWN output — never A's.
        class BodyConn(FakeNetmikoConn):
            def _response_for(self, cmd: str) -> str:
                low = cmd.lower()
                if "dhcp binding" in low:
                    return ("Bindings from all pools not associated with VRF:\r\n"
                            "IP address  Client-ID  Type")
                if "ip int brief" in low:
                    return "GigabitEthernet0/0/0  1.1.1.1  up  up"
                return super()._response_for(cmd)

        conn = BodyConn()
        monkeypatch.setattr(sshmod, "ConnectHandler", lambda **kw: conn)
        dev = SSHConsoleDevice("h", "u", "p", port_user="19")
        dev.connect()
        # Simulate A's response still buffered on the terminal server when B is
        # sent (note the literal '^@' idle marker leading the stale block).
        conn.queue_read(
            "^@\r\nRouter#show ip int brief\r\n"
            "GigabitEthernet0/0/0  1.1.1.1  up  up\r\n"
            "Router# "
        )
        page, more = dev.execute_paged("show ip dhcp binding")
        assert more is False
        assert "Bindings from all pools" in page      # B's OWN output returned
        assert "GigabitEthernet0/0/0" not in page     # A's stale output discarded
        assert "show ip int brief" not in page        # A's echo discarded
        assert "^@" not in page                       # literal caret-@ stripped


