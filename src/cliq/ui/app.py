#!/usr/bin/env python3
"""
app.py — CLIQ interactive shell.

Driven by the real CommandDB + IntelligenceEngine, presented through a
Rich-decorated prompt-toolkit shell:

  * VS Code-style ghost text (Tab / → to accept)
  * Live bottom toolbar with status (✓/⏳/⚠/✗), latency, and stats/score
  * Cisco '?' help opens a context-aware Rich help panel
  * Wrong-mode → multi-step Auto-Fix with confirmation
  * Wrong-mode-from-config → 'do <cmd>' suggestion
  * Destructive command confirmation
  * Strict argument validation at execute-time (IPs, masks, interfaces, …)
  * Pipe (|) pass-through
  * `demo` runs a guided tour
  * `--live` swaps the mock device for a real CML device backend (when wired)

USAGE:
  python -m cliq.ui.app
  python -m cliq.ui.app --live
"""

from __future__ import annotations

import argparse
import html
import sys
import time

from prompt_toolkit import PromptSession
from prompt_toolkit.application import run_in_terminal
from prompt_toolkit.auto_suggest import AutoSuggest, Suggestion
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.styles import Style

from rich.console import Console
from rich.markup import escape

from cliq.db.query import CommandDB
from cliq.intelligence.engine import IntelligenceEngine
from cliq.ui import panels
from cliq.ui.mock_device import MockDevice
from cliq.connection.session_manager import SessionManager
from cliq.audit.audit_log import AuditLogger
from cliq.audit import safety_gates

console = Console()


MODE_PROMPTS = {
    "exec": "Router> ",
    "enable": "Router# ",
    "config": "Router(config)# ",
    "config-if": "Router(config-if)# ",
    "config-router": "Router(config-router)# ",
    "config-line": "Router(config-line)# ",
    "config-subif": "Router(config-subif)# ",
    "config-vlan": "Router(config-vlan)# ",
    "config-ext-nacl": "Router(config-ext-nacl)# ",
}


class Stats:
    def __init__(self):
        self.commands_run = 0
        self.typos_prevented = 0
        self.mode_errors_caught = 0
        self.invalid_args_caught = 0
        self.latencies: list[float] = []
        self.start_time = time.time()

    @property
    def avg_latency(self) -> float:
        sample = self.latencies[-20:]
        return sum(sample) / len(sample) if sample else 0.0

    @property
    def last_latency(self) -> float:
        return self.latencies[-1] if self.latencies else 0.0

    @property
    def time_saved_min(self) -> float:
        return self.typos_prevented * 0.5 + self.mode_errors_caught * 1.0 + self.invalid_args_caught * 0.5

    @property
    def score(self) -> int:
        return max(0, 100 - 2 * self.typos_prevented - 5 * self.mode_errors_caught - 3 * self.invalid_args_caught)

    def reset(self):
        self.__init__()


class OperationalMemory:
    def __init__(self, capacity: int = 5):
        self.cap = capacity
        self.values: dict[str, list[str]] = {}

    def remember(self, arg_name: str, value: str):
        if not value:
            return
        bucket = self.values.setdefault(arg_name, [])
        if value in bucket:
            bucket.remove(value)
        bucket.insert(0, value)
        if len(bucket) > self.cap:
            del bucket[self.cap:]

    def recent(self, arg_name: str) -> list[str]:
        return list(self.values.get(arg_name, []))


class EngineAutoSuggest(AutoSuggest):
    def __init__(self, engine: IntelligenceEngine, get_mode):
        self.engine = engine
        self.get_mode = get_mode

    def get_suggestion(self, buffer, document):
        text = document.text
        if not text.strip():
            return None
        analysis = self.engine.analyze(text, self.get_mode(), is_executing=False)
        if analysis.ghost_text:
            return Suggestion(analysis.ghost_text)
        return None


class CLIQShell:
    def __init__(
        self,
        live: bool = False,
        wizard: bool = False,
        # CML path mode
        gateway: str = "",
        path: str = "",
        # Direct SSH mode
        host: str = "",
        port: int = 22,
        # Shared credentials
        username: str = "",
        password: str = "",
        device_type: str = "cisco_ios",
        secret: str = "",
    ):
        self.db = CommandDB()
        self.engine = IntelligenceEngine(self.db)
        self.wizard = wizard
        self.manager: SessionManager | None = None

        if wizard:
            self.device, self.manager = self._build_wizard(host or gateway)
            self.live = True
        else:
            self.device = self._build_device(
                live=live,
                gateway=gateway, path=path,
                host=host, port=port,
                username=username, password=password,
                device_type=device_type, secret=secret,
            )
            self.live = live

        self.mode = "exec"
        self.hostname: str = "Router"
        if self.live:
            self._sync_from_device()
        self.stats = Stats()
        self.memory = OperationalMemory()
        self.session: PromptSession | None = None

        # Audit logger — starts immediately, writes to ~/.cliq/audit.log
        _device_label = (
            self.manager.active_label if self.manager else
            (gateway or host or "mock")
        )
        _lab_label = getattr(self.manager, "gateway_host", "local") if self.manager else "local"
        self.audit = AuditLogger(device=_device_label, lab=_lab_label)

    def _build_wizard(self, host: str = ""):
        """Launch the CML wizard, return (active_device, manager)."""
        from cliq.connection.lab_connect import connect_wizard
        mgr = connect_wizard(host)
        active = mgr.active_session.device if mgr.active_session else None
        if active is None:
            console.print("[red]\u2717 No device connected — falling back to mock.[/red]")
            return MockDevice(), None
        return active, mgr

    def _build_device(
        self,
        live: bool,
        gateway: str = "",
        path: str = "",
        host: str = "",
        port: int = 22,
        username: str = "",
        password: str = "",
        device_type: str = "cisco_ios",
        secret: str = "",
    ):
        if not live:
            return MockDevice()
        # Validate: need either (gateway+path) or (host)
        if not username:
            console.print("[red]✗ --live requires --user / --username.[/red]")
            sys.exit(1)
        if not gateway and not host:
            console.print(
                "[red]✗ --live requires either:[/red]\n"
                "  --gateway <cml-host> --path /lab/node/0    (CML console)\n"
                "  --host <ip>                                (direct SSH)"
            )
            sys.exit(1)
        try:
            from cliq.connection.live_device import connect_from_args, ConnectionError as DevConnError
        except ImportError as exc:
            console.print(f"[yellow]⚠ paramiko/netmiko not installed ({exc}). Falling back to mock.[/yellow]\n")
            return MockDevice()
        try:
            label = f"{gateway}{path}" if gateway else f"{host}:{port}"
            console.print(f"[cyan]Connecting to {label} …[/cyan]")
            dev = connect_from_args(
                gateway=gateway, path=path,
                host=host, port=port,
                username=username, password=password,
                device_type=device_type, secret=secret,
            )
            console.print(f"[green]✓ Connected to {label}[/green]\n")
            return dev
        except Exception as exc:
            console.print(f"[red]✗ Connection failed: {exc}[/red]\n[yellow]Falling back to mock device.[/yellow]\n")
            return MockDevice()

    # ---- prompt + toolbar ------------------------------------------------
    def _sync_from_device(self) -> None:
        """Read the live device's current prompt and sync hostname + mode."""
        prompt = getattr(self.device, "current_prompt", "").strip()
        if not prompt:
            # Fallback: send blank to get a fresh prompt
            try:
                self.device._send("")
                raw = self.device._read_until(timeout=5)
                from cliq.connection.live_device import _ANSI_STRIP
                prompt = _ANSI_STRIP.sub("", raw).rstrip().split("\n")[-1].strip()
            except Exception:
                pass
        if prompt:
            self._parse_ios_prompt(prompt)

    def _parse_ios_prompt(self, prompt: str) -> None:
        """Parse an IOS prompt string and update self.hostname and self.mode."""
        import re
        m = re.match(r'^([\w][\w\-\.]*)(\(([^\)]+)\))?([#>])\s*$', prompt.strip())
        if not m:
            return
        self.hostname = m.group(1)
        submode = m.group(3)   # e.g. "config", "config-if"
        marker  = m.group(4)   # "#" or ">"
        if marker == ">":
            self.mode = "exec"
        elif submode:
            _map = {
                "config":           "config",
                "config-if":        "config-if",
                "config-router":    "config-router",
                "config-line":      "config-line",
                "config-subif":     "config-subif",
                "config-vlan":      "config-vlan",
                "config-ext-nacl":  "config-ext-nacl",
            }
            self.mode = _map.get(submode, "config")
        else:
            self.mode = "enable"

    def _fetch_hostname(self) -> str:
        """Fallback: ask the live device for its configured hostname."""
        try:
            out = self.device.execute("show running-config | include hostname")
            for line in out.splitlines():
                parts = line.strip().split()
                if len(parts) >= 2 and parts[0].lower() == "hostname":
                    return parts[1]
        except Exception:
            pass
        return "Router"

    def _prompt_for(self, mode: str | None = None) -> str:
        h = self.hostname
        m = mode or self.mode
        _p = {
            "exec":             f"{h}> ",
            "enable":           f"{h}# ",
            "config":           f"{h}(config)# ",
            "config-if":        f"{h}(config-if)# ",
            "config-router":    f"{h}(config-router)# ",
            "config-line":      f"{h}(config-line)# ",
            "config-subif":     f"{h}(config-subif)# ",
            "config-vlan":      f"{h}(config-vlan)# ",
            "config-ext-nacl":  f"{h}(config-ext-nacl)# ",
        }
        return _p.get(m, f"{h}> ")

    def _bottom_toolbar(self):
        text = self.session.default_buffer.text if self.session else ""
        a = self.engine.analyze(text, self.mode, is_executing=False) if text.strip() else None

        if a is None:
            status = '<b><style fg="ansicyan">›</style></b> Type a command. Tab to accept ghost text · ? for help.'
        elif a.status == "ok":
            desc = a.matched_command.description if a.matched_command else ""
            status = f'<b><style fg="ansigreen">✓</style></b> {html.escape(desc)}'
        elif a.status == "partial":
            best = a.suggestions[0].full_command if a.suggestions else ""
            status = f'<b><style fg="ansiyellow">⏳</style></b> Keep typing… {html.escape(best)}'
        elif a.status == "missing_arg":
            status = f'<b><style fg="ansiyellow">⚠</style></b> Missing argument — {html.escape(a.explanation.how)}'
        elif a.status == "wrong_mode_do":
            status = (f'<b><style fg="ansiyellow">⚠</style></b> Exec command in config mode. '
                      f'Did you mean: <b>{html.escape(a.do_suggestion)}</b>?')
        elif a.status == "wrong_mode":
            req = a.matched_command.mode if a.matched_command else "?"
            status = f'<b><style fg="ansired">⚠</style></b> Wrong mode — requires <b>{html.escape(req)}</b>.'
        elif a.status == "typo":
            best = a.suggestions[0].full_command if a.suggestions else a.expanded_command
            # Preserve a 'do <cmd>' prefix so a config-mode exec typo suggests
            # the full runnable line (e.g. 'do show vlan'), not bare 'show vlan'.
            if (a.expanded_command or "").startswith("do ") and not (best or "").startswith("do "):
                best = a.expanded_command
            status = f'<b><style fg="ansiyellow">⚠</style></b> Typo. Did you mean: <b>{html.escape(best)}</b>?'
        elif a.status == "invalid_arg":
            why = a.explanation.why if a.explanation else ""
            status = f'<b><style fg="ansired">✗</style></b> Invalid argument — {html.escape(why)}'
        else:
            status = '<b><style fg="ansired">✗</style></b> Unknown command. Press ? for help.'

        s = self.stats
        bar = (
            f'\n┌──────────────────────────────────────────────────────────────────────┐\n'
            f'│  Session: {s.typos_prevented} typos prevented · {s.mode_errors_caught} mode errors · {s.invalid_args_caught} invalid args caught          │\n'
            f'│  Time saved: ~{s.time_saved_min:.1f} min · Avg latency: {s.avg_latency:.1f}ms · Score: {s.score:>3}/100        │\n'
            f'└──────────────────────────────────────────────────────────────────────┘'
        )
        return HTML(status + bar)

    # ---- run loop --------------------------------------------------------
    def run(self):
        self._banner()
        # Post-wizard startup: show all connected devices with live prompts
        if self.wizard and self.manager:
            self._show_device_startup_banner()

        kb = KeyBindings()

        @kb.add("tab")
        def _(event):
            buf = event.app.current_buffer
            if buf.suggestion and buf.suggestion.text:
                buf.insert_text(buf.suggestion.text)
            else:
                buf.insert_text("    ")

        @kb.add("?")
        def _(event):
            text = event.current_buffer.text
            mode = self.mode
            run_in_terminal(lambda: self._show_help(text, mode))

        style = Style.from_dict({
            "auto-suggestion": "fg:#666666 italic",
            "bottom-toolbar": "bg:#1e1e1e fg:#cccccc",
        })

        self.session = PromptSession(
            auto_suggest=EngineAutoSuggest(self.engine, lambda: self.mode),
            key_bindings=kb,
            bottom_toolbar=self._bottom_toolbar,
            style=style,
        )

        while True:
            try:
                user_input = self.session.prompt(
                    HTML(f"<ansicyan><b>{self._prompt_for()}</b></ansicyan>")
                )
            except (EOFError, KeyboardInterrupt):
                self._goodbye()
                return

            cmd = user_input.strip()
            if not cmd:
                continue

            # ── Multi-device built-ins (wizard mode) ──────────────────────
            if self.manager:
                if cmd.startswith("switch "):
                    label = cmd[7:].strip()
                    try:
                        self.manager.switch(label)
                        self.device = self.manager.active_session.device
                        # Immediately sync prompt from the device for snappy UX
                        synced = False
                        if hasattr(self.device, "_send") and hasattr(self.device, "_read_until"):
                            try:
                                from cliq.connection.live_device import _ANSI_STRIP
                                self.device._send("")
                                buf = self.device._read_until(timeout=3)
                                cleaned = _ANSI_STRIP.sub("", buf).rstrip()
                                last_line = cleaned.split("\n")[-1].strip() if cleaned else ""
                                if last_line:
                                    self._parse_ios_prompt(last_line)
                                    synced = True
                            except Exception:
                                pass
                        if not synced:
                            self._sync_from_device()
                        console.print(
                            f"[cyan]\u2022 Active device: [bold]{label}[/bold] "
                            f"({self._prompt_for().strip()})[/cyan]\n"
                        )
                    except KeyError:
                        console.print(f"[red]Unknown device: {label}[/red]\n")
                    continue

                if cmd == "devices":
                    from rich.table import Table as RTable
                    t = RTable(title="Connected Devices", show_header=True,
                               header_style="bold cyan", border_style="dim")
                    t.add_column("Label", min_width=14)
                    t.add_column("Status", width=10)
                    t.add_column("Mode", width=14)
                    t.add_column("Active", width=8, justify="center")
                    for lbl, connected in self.manager.list_devices():
                        is_active = (lbl == self.manager.active_label)
                        status = "[green]up[/green]" if connected else "[red]down[/red]"
                        mode = self.manager.get_mode(lbl)
                        active_marker = "[bold green]\u25cf[/bold green]" if is_active else ""
                        t.add_row(lbl, status, mode, active_marker)
                    console.print(t)
                    console.print()
                    continue

            # Built-in shell commands
            if cmd in ("exit", "quit"):
                if self.mode == "exec":
                    self._goodbye()
                    return
                self._mode_up()
                continue
            if cmd == "end":
                self.mode = "exec"
                continue
            if cmd == "stats":
                self._show_stats()
                continue
            if cmd == "reset stats":
                self.stats.reset()
                console.print("[green]✓ Stats reset.[/green]\n")
                continue
            if cmd == "clear":
                console.clear()
                self._banner()
                continue
            if cmd == "demo":
                self._run_demo()
                continue
            if cmd == "reconnect":
                self._manual_reconnect()
                continue
            if cmd.endswith("?"):
                self._show_help(cmd[:-1].rstrip(), self.mode)
                continue

            self._handle(cmd)

    # ---- main per-command dispatch ---------------------------------------
    def _get_active_device_type(self) -> str:
        """Return the node_type of the currently active device, or ''."""
        if self.manager and self.manager.active_session:
            return getattr(self.manager.active_session.device, "node_type", "")
        return getattr(self.device, "node_type", "")

    def _handle(self, raw: str):
        t0 = time.perf_counter()
        device_type = self._get_active_device_type()
        a = self.engine.analyze(raw, self.mode, is_executing=True,
                                device_type=device_type)
        latency_ms = (time.perf_counter() - t0) * 1000.0
        self.stats.latencies.append(latency_ms)

        status = a.status

        if status == "ok":
            self._execute_ok(a, raw)
            return

        if status == "missing_arg":
            e = a.explanation
            console.print(panels.missing_arg_panel(e.what, e.how, e.example))
            # Offer the full command completion as a runnable suggestion. The
            # ghost text is now word-by-word (display only), so prefer the
            # matched command's full name when we have it.
            if a.matched_command:
                self._offer_run(a.matched_command.full_command)
            elif a.ghost_text:
                full_suggestion = (a.expanded_command + a.ghost_text).strip()
                self._offer_run(full_suggestion)
            return

        if status == "invalid_arg":
            self.stats.invalid_args_caught += 1
            e = a.explanation
            console.print(panels.syntax_error_panel(e.why, e.example))
            return

        if status == "wrong_mode_do":
            self.stats.mode_errors_caught += 1
            console.print(panels.wrong_mode_do_panel(raw, a.do_suggestion))
            self._offer_run(a.do_suggestion + a.pipe_suffix)
            return

        if status == "wrong_mode":
            self.stats.mode_errors_caught += 1
            self._handle_wrong_mode(a, raw)
            return

        if status == "typo":
            self.stats.typos_prevented += 1
            sugg = [s.full_command for s in a.suggestions] if a.suggestions else [a.expanded_command]
            console.print(panels.typo_panel(raw, sugg, confidence=0.9))
            self._offer_run(sugg[0] + a.pipe_suffix)
            return

        if status == "partial":
            sugg = [s.full_command for s in a.suggestions] if a.suggestions else []
            console.print(panels.incomplete_panel(raw, sugg))
            return

        if status == "wrong_device":
            e = a.explanation
            console.print(panels.wrong_device_panel(
                raw,
                e.what if e else "Wrong device type",
                e.why if e else "",
                e.how if e else "",
            ))
            # Offer to switch to a compatible device if wizard mode
            if self.manager:
                compatible = self._find_compatible_device(a.matched_command)
                if compatible:
                    self._offer_device_switch(compatible, raw)
            return

        # unknown
        console.print("[red]✖ Unknown command.[/red] Type [bold]?[/bold] for help.")
        if a.explanation:
            console.print(f"[dim]{escape(a.explanation.how)}[/dim]\n")

    # ---- ok-path execution + safety gates --------------------------------
    def _execute_ok(self, analysis, raw: str):
        cmd = analysis.matched_command
        full_cmd = analysis.expanded_command + analysis.pipe_suffix

        # ── Safety gate check ──────────────────────────────────────────
        if self.live:
            gate = safety_gates.evaluate(analysis.expanded_command, self.mode)
            if gate.result == safety_gates.GateResult.BLOCK:
                console.print(f"[red bold]✗ Blocked:[/red bold] {gate.reason}")
                if gate.suggestion:
                    console.print(f"[dim]  {gate.suggestion}[/dim]")
                self.audit.log_blocked(full_cmd, self.mode, gate.reason)
                console.print()
                return
            if gate.result == safety_gates.GateResult.WARN:
                console.print(f"[yellow]⚠ Safety warning:[/yellow] {gate.reason}")
                if gate.suggestion:
                    console.print(f"[dim]  {gate.suggestion}[/dim]")
                try:
                    confirm = self.session.prompt("  Proceed? [y/N]: ").strip().lower()
                except (EOFError, KeyboardInterrupt):
                    confirm = "n"
                if confirm != "y":
                    self.audit.log_blocked(full_cmd, self.mode, "user declined at safety prompt")
                    console.print("[dim]Cancelled.[/dim]\n")
                    return

        new_mode = self._transition_for(cmd, analysis.expanded_command)
        if new_mode is not None:
            # For live devices, send the mode-transition command to the real device
            if self.live:
                _exec_device = self.manager.active_session.device if self.manager else self.device
                try:
                    out = _exec_device.execute(full_cmd)
                except Exception as exc:
                    self.audit.log_error(full_cmd, self.mode, str(exc))
                    console.print(f"[red]Device error:[/red] {exc}\n")
                    return
                # If IOS returned an error, don't change mode
                if out and "% " in out:
                    console.print(out)
                    console.print()
                    return
                if out:
                    console.print(out)
                # Sync hostname from updated device prompt (e.g. after hostname change)
                prompt = getattr(_exec_device, "current_prompt", "")
                if prompt:
                    self._parse_ios_prompt(prompt)
                else:
                    self.mode = new_mode
            else:
                self.mode = new_mode
            self.audit.log(full_cmd, self.mode, status="ok")
            console.print(f"[dim]→ entered {self.mode} mode[/dim]\n")
            return

        if cmd.is_destructive and not self.live:
            # Live commands already go through safety_gates above;
            # for mock mode keep the old destructive panel.
            console.print(panels.destructive_panel(full_cmd))
            try:
                confirm = self.session.prompt("  Proceed? [y/N]: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                confirm = "n"
            if confirm != "y":
                console.print("[dim]Cancelled.[/dim]\n")
                return

        self._remember_args(cmd, analysis.expanded_command)

        self.stats.commands_run += 1
        if raw.strip() != analysis.expanded_command.strip():
            console.print(f"[dim italic]Expanded to: {escape(full_cmd)}[/dim italic]")
        t0 = time.perf_counter()
        try:
            if self.manager:
                output = self.manager.execute(full_cmd)
                # sync mode from device prompt after execution
                prompt = getattr(self.manager.active_session.device, "current_prompt", "")
                if prompt:
                    self._parse_ios_prompt(prompt)
            else:
                output = self.device.execute(full_cmd)
        except Exception as exc:
            # Import here to avoid circular import at module load
            try:
                from cliq.connection.live_device import ConnectionLostError
                if isinstance(exc, ConnectionLostError):
                    console.print(
                        "[yellow]⚠ Connection lost — all reconnect attempts failed.[/yellow]"
                    )
                    console.print(
                        "[dim]  Run [bold]reconnect[/bold] to retry manually.[/dim]\n"
                    )
                    self.audit.log_error(full_cmd, self.mode, str(exc))
                    return
            except ImportError:
                pass
            self.audit.log_error(full_cmd, self.mode, str(exc))
            console.print(f"[red]Device error:[/red] {exc}\n")
            return
        latency_ms = (time.perf_counter() - t0) * 1000.0
        self.audit.log(full_cmd, self.mode, output=output or "", latency_ms=latency_ms)
        if output:
            device_type = self._get_active_device_type()
            output = self._post_process_output(output, full_cmd, device_type)
            console.print(output)
        # If user ran 'hostname <name>', update the shell prompt live
        if cmd.id == "hostname":
            words = analysis.expanded_command.split()
            if len(words) >= 2:
                self.hostname = words[1]
        console.print()

    def _transition_for(self, cmd, expanded: str) -> str | None:
        if not cmd:
            return None
        cid = cmd.id
        if cid == "enable":
            return "enable"
        if cid == "disable":
            return "exec"
        if cid == "configure_terminal":
            return "config"
        if cid == "interface":
            # Sub-interface (dotted) drops into config-subif, otherwise config-if.
            parts = expanded.split()
            target = parts[-1] if len(parts) > 1 else ""
            return "config-subif" if "." in target else "config-if"
        if cid == "router_ospf":
            return "config-router"
        if cid in ("line_vty", "line_console"):
            return "config-line"
        if cid == "vlan":
            return "config-vlan"
        if cid in ("ip_access_list_extended", "ip_access_list_standard"):
            return "config-ext-nacl"
        return None

    def _remember_args(self, cmd, expanded: str):
        if not cmd or not cmd.arguments:
            return
        words = expanded.split()
        for arg in cmd.arguments:
            pos = arg.position
            if pos is None or pos >= len(words):
                continue
            self.memory.remember(arg.name, words[pos])

    # ---- wrong-mode auto-fix ---------------------------------------------
    def _handle_wrong_mode(self, analysis, raw: str):
        e = analysis.explanation
        target_mode = analysis.matched_command.mode if analysis.matched_command else None
        target_prompt = self._prompt_for(target_mode).rstrip()

        fix_path = []
        cur_mode = self.mode
        for next_mode, enter_cmd in e.fix_mode_path:
            fix_path.append((self._prompt_for(cur_mode).rstrip(), enter_cmd))
            cur_mode = next_mode
        fix_path.append((target_prompt, analysis.expanded_command + analysis.pipe_suffix))

        console.print(panels.wrong_mode_panel(
            raw, self._prompt_for(self.mode).rstrip(), target_prompt, fix_path
        ))

        try:
            confirm = self.session.prompt("  Apply this fix automatically? [y/N]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            confirm = "n"
        if confirm != "y":
            console.print("[dim]Auto-fix cancelled.[/dim]\n")
            return

        for next_mode, enter_cmd in e.fix_mode_path:
            enter_cmd = self._fill_placeholders(enter_cmd)
            if enter_cmd is None:
                console.print("[dim]Auto-fix cancelled.[/dim]\n")
                return
            console.print(f"[dim]Executing:[/dim] [cyan]{escape(enter_cmd)}[/cyan]")
            inner = self.engine.analyze(enter_cmd, self.mode, is_executing=True)
            if inner.status != "ok":
                console.print(f"[red]Auto-fix step failed at '{enter_cmd}': {inner.status}[/red]\n")
                return
            transition = self._transition_for(inner.matched_command, inner.expanded_command)
            if transition:
                self.mode = transition
            time.sleep(0.15)

        final_input = analysis.expanded_command + analysis.pipe_suffix
        console.print(f"[dim]Executing:[/dim] [cyan]{escape(final_input)}[/cyan]")
        final = self.engine.analyze(final_input, self.mode, is_executing=True)
        if final.status == "ok":
            self._execute_ok(final, final_input)
        else:
            console.print(f"[red]Final command did not match in {self.mode}: {final.status}[/red]\n")

    def _fill_placeholders(self, command: str) -> str | None:
        """Replace <placeholder> tokens in an auto-fix step by prompting the user."""
        import re
        placeholders = re.findall(r"<([^>]+)>", command)
        if not placeholders:
            return command
        defaults = {
            "interface": "GigabitEthernet0/1",
            "protocol": "ospf",
            "type": "vty",
            "number": "0 4",
        }
        for name in placeholders:
            recent = self.memory.recent(name)
            default = (recent[0] if recent else defaults.get(name, ""))
            hint = f" [{default}]" if default else ""
            try:
                val = self.session.prompt(f"  Value for <{name}>{hint}: ").strip()
            except (EOFError, KeyboardInterrupt):
                return None
            if not val:
                if not default:
                    return None
                val = default
            command = command.replace(f"<{name}>", val, 1)
        return command

    # ---- offer to run a suggested command --------------------------------
    def _offer_run(self, suggestion: str):
        """Offer to run the corrected command.

        y       → execute immediately
        e/edit  → pre-populate the prompt so the user can edit before running
        n/Enter → do nothing (user retypes manually)
        """
        if not suggestion:
            return
        try:
            console.print(
                f"\n  [cyan]→ Suggested fix:[/cyan] [bold]{escape(suggestion)}[/bold]"
            )
            ans = self.session.prompt(
                f"  Run '{suggestion}'? [y/N/e(dit)]: "
            ).strip().lower()
        except (EOFError, KeyboardInterrupt):
            ans = "n"

        if ans == "y":
            a = self.engine.analyze(suggestion, self.mode, is_executing=True)
            if a.status == "ok":
                self._execute_ok(a, suggestion)
            else:
                console.print(f"[red]Suggestion did not validate: {a.status}[/red]\n")

        elif ans in ("e", "edit"):
            try:
                edited = self.session.prompt(
                    HTML(f"<ansicyan><b>{self._prompt_for()}</b></ansicyan>"),
                    default=suggestion,
                ).strip()
                if edited:
                    self._handle(edited)
            except (EOFError, KeyboardInterrupt):
                pass

    def _manual_reconnect(self):
        """'reconnect' shell command — try to re-establish a dropped connection."""
        try:
            from cliq.connection.live_device import ConnectionLostError
        except ImportError:
            console.print("[red]Live device module not available.[/red]\n")
            return
        try:
            console.print("[cyan]  Reconnecting...[/cyan]")
            if self.manager:
                active = self.manager.active_session
                if active and active.device and hasattr(active.device, "_reconnect"):
                    active.device._reconnect()
                    console.print(
                        f"[green]  ✓ Reconnected to {self.manager.active_label}[/green]\n"
                    )
                else:
                    console.print("[yellow]  No live device to reconnect.[/yellow]\n")
            elif hasattr(self.device, "_reconnect"):
                self.device._reconnect()
                console.print("[green]  ✓ Reconnected[/green]\n")
            else:
                console.print("[yellow]  Not running in live mode.[/yellow]\n")
        except Exception as exc:
            console.print(f"[red]  ✗ Reconnect failed: {exc}[/red]\n")

    def _find_compatible_device(self, matched_command) -> str:
        """Return the label of the first connected device that supports this command, or ''."""
        if not self.manager or not matched_command:
            return ""
        platforms = getattr(matched_command, "platforms", [])
        if not platforms:
            return ""
        for label, connected in self.manager.list_devices():
            if label == self.manager.active_label or not connected:
                continue
            dev = self.manager.sessions[label].device
            node_type = getattr(dev, "node_type", "").lower()
            if any(p.lower() == node_type or node_type.startswith(p.lower()) for p in platforms):
                return label
        return ""

    def _offer_device_switch(self, target_label: str, command: str):
        """Offer to switch to a compatible device and re-run the command."""
        try:
            console.print(
                f"\n  [cyan]→ '{command}' is available on [bold]{target_label}[/bold][/cyan]"
            )
            ans = self.session.prompt(
                f"  Switch to {target_label} and run it? [y/N]: "
            ).strip().lower()
        except (EOFError, KeyboardInterrupt):
            ans = "n"
        if ans == "y":
            self.manager.switch(target_label)
            self.device = self.manager.active_session.device
            self._sync_from_device()
            console.print(f"[cyan]  • Active device: {target_label}[/cyan]")
            self._handle(command)

    def _post_process_output(self, output: str, command: str, device_type: str = "") -> str:
        """Intercept IOS native errors and replace with CLIQ explanations."""
        if not output or "% Invalid input detected" not in output:
            return output
        # Check if platform issue
        if device_type:
            analysis = self.engine.analyze(command, self.mode, is_executing=True,
                                           device_type=device_type)
            if analysis.status == "wrong_device" and analysis.explanation:
                e = analysis.explanation
                return (
                    f"\n  [red]✖ Wrong device type[/red]\n"
                    f"  {e.what}\n"
                    f"  {e.why}\n"
                    f"  → {e.how}\n"
                )
        return (
            f"\n  [red]✖ IOS rejected this command[/red]\n"
            f"  {output.strip()}\n"
            f"  Type [bold]?[/bold] to see valid commands in this mode.\n"
        )

    # ---- '?' contextual help ---------------------------------------------
    def _show_help(self, prefix: str, mode: str):
        prefix = (prefix or "").strip()
        if prefix:
            from cliq.parser.tokenizer import Tokenizer
            tok = Tokenizer.tokenize(prefix)
            exp = self.engine.matcher.expander.expand(tok.tokens)
            prefix_tokens = exp.expanded_tokens
        else:
            prefix_tokens = []
        prefix_str = " ".join(prefix_tokens).lower()

        completions: dict[str, tuple[str, bool]] = {}
        placeholders: list[tuple[str, str, list[str]]] = []
        seen_args: set[str] = set()

        for cmd in self.db.commands_in_mode(mode):
            cmd_tokens = cmd.full_command.split()
            cmd_lower = cmd.full_command.lower()
            if not prefix_tokens:
                completions[cmd.full_command] = (cmd.description, cmd.is_destructive)
                continue
            if cmd_lower == prefix_str:
                for arg in cmd.arguments:
                    if arg.name in seen_args:
                        continue
                    seen_args.add(arg.name)
                    spec = self.db.get_arg_type(arg.type) or {}
                    desc = spec.get("description", arg.type)
                    placeholders.append((arg.name, desc, self.memory.recent(arg.name)))
                completions.setdefault(cmd.full_command, (cmd.description, cmd.is_destructive))
                continue
            if cmd_lower.startswith(prefix_str + " ") and len(cmd_tokens) > len(prefix_tokens):
                nxt = cmd_tokens[len(prefix_tokens)]
                completions.setdefault(nxt, (cmd.description, cmd.is_destructive))

        completion_rows = sorted(
            ((tok, desc, dest) for tok, (desc, dest) in completions.items()),
            key=lambda x: x[0],
        )
        console.print()
        console.print(panels.help_panel(mode, prefix, completion_rows, placeholders))
        console.print()

    # ---- house-keeping ---------------------------------------------------
    def _mode_up(self):
        info = self.db.get_mode(self.mode)
        parent = info.get("parent") if info else None
        self.mode = parent or "exec"

    def _show_stats(self):
        s = self.stats
        console.print()
        console.print("[bold cyan]── CLIQ Session Stats ──[/bold cyan]")
        console.print(f"  Commands run:        {s.commands_run}")
        console.print(f"  Typos prevented:     {s.typos_prevented}")
        console.print(f"  Mode errors caught:  {s.mode_errors_caught}")
        console.print(f"  Invalid args caught: {s.invalid_args_caught}")
        console.print(f"  Time saved:          ~{s.time_saved_min:.1f} min")
        console.print(f"  Avg latency:         {s.avg_latency:.1f} ms")
        console.print(f"  Last latency:        {s.last_latency:.1f} ms")
        console.print(f"  Score:               {s.score}/100\n")

    def _banner(self):
        if self.wizard and self.manager:
            labels = [lbl for lbl, _ in self.manager.list_devices()]
            backend = f"CML Wizard · {len(labels)} device(s)"
        elif self.live:
            backend = "Live CML"
        else:
            backend = "Mock Mode"
        banner = f"""
    ╔═══════════════════════════════════════════════════════════════╗
    ║                           CLIQ                                ║
    ║   Contextual Live Intelligence Quotient — {backend:<19} ║
    ║                                                               ║
    ║   ▸ Sub-50ms deterministic intelligence                       ║
    ║   ▸ Ghost text & autocomplete (Tab to accept)                 ║
    ║   ▸ Cross-mode typo correction & Auto-Fix                     ║
    ║   ▸ Live wrong-mode + arg validation while you type           ║
    ║                                                               ║
    ║   Type 'demo' for a guided tour · '?' for help · 'exit'       ║
    ╚═══════════════════════════════════════════════════════════════╝
"""
        console.print(banner, style="bold cyan")

    def _show_device_startup_banner(self):
        """Print a table of all wizard devices with their current live prompts."""
        from rich.table import Table as RTable
        t = RTable(show_header=True, header_style="bold cyan", border_style="dim",
                   title="[bold cyan]Connected Devices[/bold cyan]")
        t.add_column("Label", min_width=14)
        t.add_column("Type", width=12)
        t.add_column("Prompt", style="green")
        t.add_column("Active", width=8, justify="center")
        for lbl, connected in self.manager.list_devices():
            dev = self.manager.sessions[lbl].device
            is_active = (lbl == self.manager.active_label)
            active_marker = "[bold green]●[/bold green]" if is_active else ""
            node_type = getattr(dev, "node_type", "")
            prompt = getattr(dev, "current_prompt", lbl) or lbl
            status_str = f"[green]{prompt}[/green]" if connected else "[red]down[/red]"
            t.add_row(lbl, node_type, status_str, active_marker)
        console.print(t)
        console.print(
            f"[dim]  Active: [cyan]{self.manager.active_label}[/cyan] · "
            f"Use [bold]switch <label>[/bold] to change · [bold]devices[/bold] to list[/dim]\n"
        )
        console.print()
        self._show_stats()
        self.audit.close()
        console.print("[bold]Goodbye.[/bold]\n")

    # ---- demo mode -------------------------------------------------------
    def _run_demo(self):
        scenarios = [
            ("Word-Aware Typo Correction", "sh in br", "exec",
             "Common interface typo — watch the word-by-word correction land 'show ip interface brief'."),
            ("Cross-Mode Auto-Fix", "ip address 192.168.1.1 255.255.255.0", "exec",
             "Setting IP in user EXEC. Watch the multi-step Auto-Fix path appear."),
            ("'do' Command Intelligence", "show ip route", "config-if",
             "An exec command typed in config-if mode. The engine suggests 'do show ip route'."),
            ("Strict IP Validation", "ping 1.11.1", "exec",
             "Real-time validation catches malformed IPs before execution."),
            ("Destructive Action Warning", "shutdown", "config-if",
             "The engine intercepts dangerous commands and asks for confirmation."),
        ]
        console.print("\n[bold cyan]═══ GUIDED DEMO TOUR ═══[/bold cyan]\n")
        for i, (title, inp, mode, narrative) in enumerate(scenarios, 1):
            console.print(f"[bold cyan]► Scenario {i}: {title}[/bold cyan]")
            console.print(f"[dim]{narrative}[/dim]")
            console.print(f"[bold]Mode:[/bold] {mode}    [bold]Input:[/bold] [yellow]{inp}[/yellow]")
            time.sleep(0.6)

            a = self.engine.analyze(inp, mode, is_executing=True)
            console.print(f"[bold]Engine status:[/bold] [magenta]{a.status}[/magenta]")
            if a.status == "ok":
                console.print(f"  [green]✓ Matched:[/green] {a.expanded_command + a.pipe_suffix}")
            elif a.status == "invalid_arg":
                console.print(f"  [red]❌ Validation:[/red] {a.explanation.why}")
            elif a.status == "wrong_mode":
                steps = " → ".join(c for _, c in a.explanation.fix_mode_path) or "(none)"
                console.print(f"  [yellow]⚠ Wrong mode.[/yellow] Fix: {steps}")
            elif a.status == "wrong_mode_do":
                console.print(f"  [yellow]⚠ Suggestion:[/yellow] {a.do_suggestion}")
            elif a.status == "typo":
                best = a.suggestions[0].full_command if a.suggestions else a.expanded_command
                console.print(f"  [yellow]💡 Did you mean:[/yellow] {best}")
            elif a.status == "missing_arg":
                console.print(f"  [yellow]⚠ Missing:[/yellow] {a.explanation.how}")
            else:
                console.print(f"  [red]✗[/red] {a.status}")
            console.print()
            time.sleep(0.8)
        console.print("[bold green]✓ Tour complete.[/bold green]\n")


def main(argv: list[str] | None = None):
    import getpass
    parser = argparse.ArgumentParser(
        prog="cliq",
        description="CLIQ — Cisco CLI Intelligence Shell",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "CML wizard (recommended — shows labs, picks devices interactively):\n"
            "  cliq --wizard\n"
            "  cliq --wizard --gateway cml.example.com\n\n"
            "CML console (single device, manual path):\n"
            "  cliq --live --gateway cml.example.com \\\n"
            "            --user cli_test --password <pw> \\\n"
            "            --path /cli_test1/iosv-0/0\n\n"
            "Direct SSH to device:\n"
            "  cliq --live --host 192.168.1.1 --user cisco\n\n"
            "Multi-device shell commands (wizard mode):\n"
            "  devices             list all connected devices\n"
            "  switch <label>      switch active device\n"
        ),
    )
    parser.add_argument("--live", action="store_true",
                        help="Connect to a real device instead of mock")
    parser.add_argument("--wizard", "-w", action="store_true",
                        help="Interactive CML lab picker (auto-connects via REST API)")

    cml = parser.add_argument_group("CML console path (recommended for CML labs)")
    cml.add_argument("--gateway", default="",
                     help="CML server hostname or IP")
    cml.add_argument("--path", default="",
                     help="CML console path, e.g. /cli_test1/iosv-0/0")

    direct = parser.add_argument_group("Direct SSH (non-CML devices)")
    direct.add_argument("--host", default="",
                        help="Device IP or hostname")
    direct.add_argument("--port", type=int, default=22,
                        help="SSH port (default 22)")
    direct.add_argument("--device-type", default="cisco_ios",
                        help="Netmiko device type (default: cisco_ios)")
    direct.add_argument("--secret", default="",
                        help="Enable secret (defaults to password if omitted)")

    creds = parser.add_argument_group("Credentials")
    creds.add_argument("--user", "--username", "-u", dest="username", default="",
                       help="SSH / CML username")
    creds.add_argument("--password", "-p", default="",
                       help="SSH / CML password (omit to be prompted)")

    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    password = args.password
    if args.live and args.username and not password:
        host_label = args.gateway or args.host
        password = getpass.getpass(f"Password for {args.username}@{host_label}: ")

    shell = CLIQShell(
        live=args.live,
        wizard=args.wizard,
        gateway=args.gateway,
        path=args.path,
        host=args.host,
        port=args.port,
        username=args.username,
        password=password,
        device_type=args.device_type,
        secret=args.secret,
    )
    shell.run()


if __name__ == "__main__":
    main()
