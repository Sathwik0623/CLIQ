#!/usr/bin/env python3
"""
cml_discovery.py - Discover switch show commands from a live CML console.

The generated output is written as command_db YAML in the same shape already
used by the curated command files.
"""

from __future__ import annotations

import os
import re
import time
from typing import Iterable

import yaml
from netmiko import ConnectHandler


_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
_DEFAULT_OUTPUT = os.path.join(_PROJECT_ROOT, "command_db", "exec", "show_commands_switch_live.yaml")

_SEED_SHOW_KEYWORDS = {
	"interfaces",
	"ip",
	"vlan",
	"spanning-tree",
	"mac",
	"cdp",
	"lldp",
	"etherchannel",
	"logging",
	"inventory",
	"version",
	"running-config",
	"startup-config",
}


class CMLCommandDiscovery:
	def __init__(
		self,
		gateway: str,
		username: str,
		password: str,
		device_path: str,
		port: int = 22,
	):
		self.gateway = gateway
		self.username = username
		self.password = password
		self.device_path = device_path
		self.port = port
		self.conn = None

	def connect(self) -> None:
		self.conn = ConnectHandler(
			device_type="terminal_server",
			host=self.gateway,
			username=self.username,
			password=self.password,
			port=self.port,
			fast_cli=False,
		)

		try:
			self.conn.remote_conn.transport.set_keepalive(0)
		except Exception:
			pass

		self.conn.write_channel(f"open {self.device_path}\n")
		time.sleep(2.0)
		self.conn.write_channel("\r\n")
		time.sleep(1.0)
		self._drain()

		self.conn.write_channel("terminal length 0\n")
		time.sleep(0.3)
		self._drain()

	def disconnect(self) -> None:
		if self.conn:
			try:
				self.conn.disconnect()
			except Exception:
				pass
			self.conn = None

	def _clean(self, text: str) -> str:
		return text.replace("\x00", "").replace("^@", "")

	def _drain(self) -> str:
		time.sleep(0.2)
		return self._clean(self.conn.read_channel())

	def _help(self, command_prefix: str) -> str:
		out = self.conn.send_command_timing(
			f"{command_prefix} ?",
			strip_prompt=True,
			strip_command=True,
		)
		return self._clean(out)

	@staticmethod
	def _parse_help_tokens(raw_help: str) -> list[str]:
		tokens: list[str] = []
		for line in raw_help.splitlines():
			s = line.strip()
			if not s:
				continue
			if s.startswith("%") or s.startswith("^"):
				continue
			if s.startswith("|") or s.startswith("Output modifiers"):
				continue
			if "#" in s or ">" in s:
				continue
			first = s.split()[0]
			if first.startswith("<"):
				continue
			if first in {"WORD", "LINE", "A.B.C.D", "X:X:X:X::X"}:
				continue
			if first.startswith("(") or first.startswith("["):
				continue
			if re.match(r"^[a-zA-Z][a-zA-Z0-9_-]*$", first):
				tokens.append(first)
			elif re.match(r"^[a-zA-Z][a-zA-Z0-9_-]*/[a-zA-Z0-9_-]+$", first):
				tokens.append(first)
		return sorted(set(tokens))

	@staticmethod
	def _sanitize_id(command: str) -> str:
		base = re.sub(r"[^a-z0-9]+", "_", command.lower()).strip("_")
		return f"live_{base}"[:96]

	@staticmethod
	def _abbreviation(tokens: Iterable[str]) -> str:
		short = []
		for t in tokens:
			if len(t) <= 3:
				short.append(t)
			else:
				short.append(t[:3])
		return " ".join(short)

	def discover_show_commands(self, max_keywords: int = 40, max_subcommands: int = 25) -> list[str]:
		root_help = self._help("show")
		top = self._parse_help_tokens(root_help)

		prioritized = []
		for keyword in top:
			if keyword in _SEED_SHOW_KEYWORDS:
				prioritized.append(keyword)
		for keyword in top:
			if keyword not in prioritized and keyword.startswith(("ip", "vlan", "interface", "mac", "spanning")):
				prioritized.append(keyword)
		if not prioritized:
			prioritized = sorted(_SEED_SHOW_KEYWORDS)

		discovered: set[str] = set()
		for keyword in prioritized[:max_keywords]:
			discovered.add(f"show {keyword}")

			sub_help = self._help(f"show {keyword}")
			subs = self._parse_help_tokens(sub_help)
			for sub in subs[:max_subcommands]:
				discovered.add(f"show {keyword} {sub}")

		return sorted(discovered)

	@staticmethod
	def _guess_mode(command: str) -> str:
		if command in {"show running-config", "show startup-config", "show boot"}:
			return "enable"
		return "exec"

	@staticmethod
	def _guess_description(command: str) -> str:
		return f"Live-discovered switch command from CML: {command}"

	def write_yaml(self, commands: list[str], output_path: str = _DEFAULT_OUTPUT) -> dict:
		os.makedirs(os.path.dirname(output_path), exist_ok=True)
		rows = []
		for cmd in commands:
			toks = cmd.split()
			rows.append(
				{
					"id": self._sanitize_id(cmd),
					"full_command": cmd,
					"tokens": toks,
					"abbreviations": [self._abbreviation(toks)],
					"mode": self._guess_mode(cmd),
					"description": self._guess_description(cmd),
					"risk_class": "informational",
					"is_destructive": False,
					"arguments": [],
					"examples": [cmd],
					"related_commands": [],
				}
			)

		payload = {"commands": rows}
		with open(output_path, "w", encoding="utf-8") as f:
			yaml.safe_dump(payload, f, sort_keys=False)

		return {"output_path": output_path, "count": len(rows)}

	def discover_and_write(self, output_path: str = _DEFAULT_OUTPUT) -> dict:
		self.connect()
		try:
			commands = self.discover_show_commands()
			info = self.write_yaml(commands, output_path=output_path)
			info["commands"] = commands
			return info
		finally:
			self.disconnect()
