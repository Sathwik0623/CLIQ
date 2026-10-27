"""Unit tests for the dependency-free ``.env`` loader (cliq.config)."""

from __future__ import annotations

import os

from cliq.config import _parse_line, load_env_file


class TestParseLine:
    def test_skips_blank_and_comment(self):
        assert _parse_line("") is None
        assert _parse_line("   ") is None
        assert _parse_line("# comment") is None

    def test_basic_key_value(self):
        assert _parse_line("FOO=bar") == ("FOO", "bar")

    def test_trims_whitespace(self):
        assert _parse_line("  FOO = bar ") == ("FOO", "bar")

    def test_strips_matching_quotes(self):
        assert _parse_line('FOO="bar baz"') == ("FOO", "bar baz")
        assert _parse_line("FOO='bar baz'") == ("FOO", "bar baz")

    def test_keeps_inner_equals(self):
        # A base64-ish key with '=' padding must survive intact.
        assert _parse_line("K=abc==") == ("K", "abc==")

    def test_supports_export_prefix(self):
        assert _parse_line("export FOO=bar") == ("FOO", "bar")

    def test_missing_equals_skipped(self):
        assert _parse_line("NOTAVAR") is None


class TestLoadEnvFile:
    def test_loads_values(self, tmp_path, monkeypatch):
        env = tmp_path / ".env"
        env.write_text("CLIQ_NETSIM_API_KEY=abc123==\n# note\nCLIQ_X=1\n")
        monkeypatch.delenv("CLIQ_NETSIM_API_KEY", raising=False)
        monkeypatch.delenv("CLIQ_X", raising=False)
        assert load_env_file(env) is True
        assert os.environ["CLIQ_NETSIM_API_KEY"] == "abc123=="
        assert os.environ["CLIQ_X"] == "1"

    def test_real_env_wins_by_default(self, tmp_path, monkeypatch):
        env = tmp_path / ".env"
        env.write_text("CLIQ_NETSIM_API_KEY=from_file\n")
        monkeypatch.setenv("CLIQ_NETSIM_API_KEY", "from_shell")
        load_env_file(env)
        assert os.environ["CLIQ_NETSIM_API_KEY"] == "from_shell"

    def test_override_true_replaces(self, tmp_path, monkeypatch):
        env = tmp_path / ".env"
        env.write_text("CLIQ_NETSIM_API_KEY=from_file\n")
        monkeypatch.setenv("CLIQ_NETSIM_API_KEY", "from_shell")
        load_env_file(env, override=True)
        assert os.environ["CLIQ_NETSIM_API_KEY"] == "from_file"

    def test_missing_file_is_noop(self, tmp_path):
        assert load_env_file(tmp_path / ".env") is False

    def test_malformed_lines_do_not_crash(self, tmp_path, monkeypatch):
        env = tmp_path / ".env"
        env.write_text("garbage line\n=noname\nGOOD=ok\n")
        monkeypatch.delenv("GOOD", raising=False)
        assert load_env_file(env) is True
        assert os.environ["GOOD"] == "ok"
