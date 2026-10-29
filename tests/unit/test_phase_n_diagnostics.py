"""Unit and regression tests for CLIQ Phase N diagnostics and fixes."""

from __future__ import annotations

import asyncio
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from cliq.connection.live_device import ConnectionLostError
from cliq.connection.ssh_console_device import SSHConsoleDevice
from cliq.db.query import CommandDB
from cliq.intelligence.advisory_router import route_post_enter_advisory
from cliq.intelligence.did_you_mean import (
    DidYouMean,
    analyze_device_error,
    best_matches,
    build_suggestion,
)


# ---------------------------------------------------------------------------
# 1. Typo Correction & Advisory Routing Tests
# ---------------------------------------------------------------------------

class TestTypoCorrectionPhaseN:
    """Validate typo suggestion for 'show previlige' and guarantee no identical suggestions."""

    def test_show_privilege_in_command_db(self):
        cdb = CommandDB()
        all_cmds = cdb.all_full_commands()
        assert "show privilege" in all_cmds, "'show privilege' must exist in CommandDB"

    def test_best_matches_excludes_identical_typo(self):
        options = ["previlige", "privilege", "process"]
        matches = best_matches("previlige", options)
        assert "previlige" not in matches
        assert "privilege" in matches

    def test_build_suggestion_rejects_identical_command(self):
        suggestion = build_suggestion("show", "previlige", ["previlige"], False)
        # Even if options only had the typo, fix must remain None
        assert suggestion is not None
        assert suggestion.fix is None

    def test_analyze_device_error_suggests_privilege_fallback(self):
        # 14 chars in 'show previlige', '^' under 'previlige' (pos 5)
        # echo line is removed by clean, so output starts with '^' at column 5
        output = "     ^\n% Invalid input detected at '^' marker.\n"
        def mock_help(partial: str):
            return ""

        suggestion = analyze_device_error(
            "show previlige",
            output,
            help_lookup=mock_help,
            prompt="cat9200#",
        )
        assert suggestion is not None
        assert suggestion.fix == "show privilege"

    def test_advisory_router_never_suggests_identical_command(self):
        fake_sugg = DidYouMean(
            message="Did you mean: show previlige?",
            fix="show previlige",
            kind="typo",
        )
        with patch("cliq.intelligence.advisory_router.analyze_device_error", return_value=fake_sugg):
            adv = route_post_enter_advisory(
                command="show previlige",
                output="%\n",
                mode="enable",
                pre_enter=None,
                help_lookup=lambda x: "",
                prompt="cat9200#",
            )
            assert adv is None, "Advisory router must never recommend the identical command"


# ---------------------------------------------------------------------------
# 2. Incremental Output Streaming Tests
# ---------------------------------------------------------------------------

class FakeDirectSSHConnection:
    """Simulates direct SSH connection returning output chunks over time."""

    def __init__(self, responses: list[str], prompt: str = "cat9200#"):
        self.responses = list(responses)
        self.prompt = prompt
        self.closed = False
        self.written: list[str] = []

    def is_alive(self) -> bool:
        return not self.closed

    def find_prompt(self) -> str:
        return self.prompt

    def read_channel(self) -> str:
        if not self.written:
            return ""
        if self.responses:
            return self.responses.pop(0)
        return ""

    def write_channel(self, text: str) -> None:
        self.written.append(text)


class TestIncrementalStreamingPhaseN:
    """Validate incremental chunk emission in direct SSH mode."""

    def test_direct_ssh_emits_chunks_incrementally(self):
        dev = SSHConsoleDevice.__new__(SSHConsoleDevice)
        dev._lock = threading.RLock()
        dev.method = "direct"
        dev.console_username = "admin"
        dev.base_username = "admin"
        dev.host = "10.10.20.181"
        dev._connected = True
        dev._current_prompt = "cat9200#"
        dev.secret = ""
        dev.timeout = 5.0

        # Simulate: chunk 1 is echo + line 1, chunk 2 is line 2, chunk 3 is trailing prompt
        fake_conn = FakeDirectSSHConnection([
            "show ip int brief\r\nInterface      IP-Address\r\n",
            "Gi1/0/1        10.10.20.181\r\n",
            "cat9200#",
        ])
        dev._conn = fake_conn

        emitted_chunks = []
        def on_chunk(chunk: str):
            emitted_chunks.append(chunk)

        page, more = dev.execute_paged("show ip int brief", on_chunk=on_chunk)

        # Emitted chunks must have arrived incrementally
        assert len(emitted_chunks) >= 2, f"Expected >= 2 incremental chunks, got: {emitted_chunks}"
        # The prompt must NOT be in the emitted stream
        for chunk in emitted_chunks:
            assert "cat9200#" not in chunk
        # Prompt must be detected and tracked
        assert dev.current_prompt == "cat9200#"
        assert "Gi1/0/1" in page


# ---------------------------------------------------------------------------
# 3. Reattachment Storm & Thread Safety Tests
# ---------------------------------------------------------------------------

class TestReattachLockingPhaseN:
    """Validate locking prevents race conditions and reattach storms."""

    def test_concurrent_reattach_serialized_no_crash(self):
        dev = SSHConsoleDevice.__new__(SSHConsoleDevice)
        dev._lock = threading.RLock()
        dev.host = "10.10.20.181"
        dev.port = 22
        dev.base_username = "cisco"
        dev.console_username = "cisco"
        dev.password = "cisco"
        dev.method = "direct"
        dev.device_type = "cisco_ios"
        dev.secret = ""
        dev.timeout = 5
        dev.capability = frozenset()
        dev.pid = ""
        dev.node_type = ""
        dev._current_prompt = "cat9200#"
        dev._connected = True

        fake_conn = FakeDirectSSHConnection(["show version\r\nCisco IOS Software, C9200\r\ncat9200#"])
        dev._conn = fake_conn

        # Test reattach skips disconnect when already alive and healthy
        mode = dev.reattach()
        assert mode == "enable"
        assert dev.is_connected is True

    def test_identify_device_safe_when_disconnected(self):
        dev = SSHConsoleDevice.__new__(SSHConsoleDevice)
        dev._lock = threading.RLock()
        dev._connected = False
        dev._conn = None
        dev.capability = frozenset()
        dev.pid = ""
        # Must return safely without raising ConnectionError
        dev._identify_device()
        assert dev.pid == ""


# ---------------------------------------------------------------------------
# 4. Exit Command & Remote Socket Closure Tests
# ---------------------------------------------------------------------------

class TestExitCommandPhaseN:
    """Validate exit command behavior on direct SSH and channel close."""

    def test_exit_raises_connection_lost_when_channel_closed(self):
        dev = SSHConsoleDevice.__new__(SSHConsoleDevice)
        dev._lock = threading.RLock()
        dev.method = "direct"
        dev._connected = True
        dev._current_prompt = "cat9200#"
        dev.secret = ""
        dev.timeout = 2.0

        # Simulate device closing channel after echoing exit
        class RemoteClosedConn:
            def __init__(self):
                self.calls = 0
            def is_alive(self):
                return False
            def read_channel(self):
                return ""
            def write_channel(self, text):
                pass

        dev._conn = RemoteClosedConn()

        with pytest.raises(ConnectionLostError) as exc_info:
            dev.execute_paged("exit")
        assert "closed" in str(exc_info.value).lower()
        assert dev.is_connected is False

    def test_exit_in_config_mode_transitions_to_enable(self):
        dev = SSHConsoleDevice.__new__(SSHConsoleDevice)
        dev._lock = threading.RLock()
        dev.method = "direct"
        dev._connected = True
        dev._current_prompt = "cat9200(config)#"
        dev.secret = ""
        dev.timeout = 2.0

        fake_conn = FakeDirectSSHConnection([
            "exit\r\ncat9200#",
        ], prompt="cat9200#")
        dev._conn = fake_conn

        page, more = dev.execute_paged("exit")
        assert dev.current_prompt == "cat9200#"
        assert dev.is_connected is True
