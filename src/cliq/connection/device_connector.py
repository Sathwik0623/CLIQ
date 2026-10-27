#!/usr/bin/env python3
"""
device_connector.py - Live CML device connector used by the CLI shell.

Connects to a CML gateway, opens a device console path, and executes commands
against the live IOS prompt.
"""

from __future__ import annotations

import time

from netmiko import ConnectHandler


class DeviceConnector:
	def __init__(
		self,
		gateway: str,
		username: str,
		password: str,
		device_path: str,
		port: int = 22,
	):
		if not all([gateway, username, password, device_path]):
			raise ValueError("gateway, username, password, and device_path are required for live mode")

		self.gateway = gateway
		self.username = username
		self.password = password
		self.device_path = device_path
		self.port = port
		self.conn = None
		self._connect()

	def _connect(self) -> None:
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
		time.sleep(0.4)
		self._drain()

		try:
			self.conn.set_base_prompt()
		except Exception:
			pass

	def _clean(self, text: str) -> str:
		return text.replace("\x00", "").replace("^@", "").replace(" --More-- ", "")

	def _drain(self) -> str:
		time.sleep(0.2)
		return self._clean(self.conn.read_channel())

	def execute(self, command: str) -> str:
		if not self.conn:
			raise RuntimeError("Live session is not connected")
		output = self.conn.send_command_timing(
			command,
			strip_prompt=True,
			strip_command=True,
		)
		return self._clean(output).strip()

	def close(self) -> None:
		if self.conn:
			try:
				self.conn.disconnect()
			except Exception:
				pass
			self.conn = None

	def __del__(self):
		self.close()
