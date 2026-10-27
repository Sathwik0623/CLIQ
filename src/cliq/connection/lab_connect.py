#!/usr/bin/env python3
"""
lab_connect.py — Interactive lab connection wizard.

Handles the full connect flow:
  1. Credential collection (once, cached)
  2. Lab selection
  3. Device selection
  4. Parallel connection to all selected devices

Returns a SessionManager ready to use.
"""

import getpass
import concurrent.futures
import logging

from rich.console import Console
from rich.table import Table
from rich.prompt import Prompt

from cliq.connection.cml_auth import CMLAuthManager, CMLLab, CMLDevice
from cliq.connection.session_manager import SessionManager
from cliq.connection.live_device import CMLConsoleDevice

log = logging.getLogger("lab_connect")
console = Console()


DEFAULT_HOST = "cml.example.com"


def connect_wizard(host: str = None) -> SessionManager:
    """
    Full interactive connection wizard.
    Returns a connected SessionManager.
    """
    console.print("\n[bold cyan]CLIQ — Connect to Lab[/bold cyan]\n")

    # Step 1: Get credentials
    host = host or Prompt.ask(
        "CML Host", default=DEFAULT_HOST
    )
    username = Prompt.ask("Username")
    password = getpass.getpass("Password: ")

    # Step 2: Authenticate
    auth = CMLAuthManager()
    with console.status("Authenticating..."):
        try:
            success = auth.login(host, username, password)
        except Exception as exc:
            # login() now fails closed by raising on any auth failure.
            success = False
            console.print(f"[red]✗ {exc}[/red]")

    if not success:
        console.print("[red]✗ Authentication failed.[/red]")
        raise RuntimeError("CML authentication failed.")

    console.print("[green]✓ Authenticated[/green]\n")

    # Step 3: Show labs
    with console.status("Fetching labs..."):
        labs = auth.get_labs()

    if not labs:
        console.print("[yellow]No labs found.[/yellow]")
        raise RuntimeError("No labs available.")

    _print_labs_table(labs)

    # Step 4: Select lab
    lab_input = Prompt.ask(
        "\nSelect lab [number] or paste lab URL"
    )
    selected_lab = _resolve_lab(lab_input, labs, auth)

    # Step 5: Get devices
    with console.status(f"Fetching devices for {selected_lab.title}..."):
        devices = auth.get_devices(selected_lab.lab_id)

    if not devices:
        console.print("[yellow]No devices found in lab.[/yellow]")
        raise RuntimeError("No devices in lab.")

    _print_devices_table(devices)

    # Step 6: Select devices
    device_input = Prompt.ask(
        "\nOpen which devices? [all / numbers e.g. 1,2 / range e.g. 1-3]",
        default="all"
    )
    selected_devices = _resolve_devices(device_input, devices)

    # Step 7: Connect (parallel for speed)
    console.print()
    manager = SessionManager()
    manager.gateway_host = host
    manager.gateway_username = username
    manager.gateway_password = password

    _connect_parallel(
        manager, selected_devices,
        host, username, password
    )

    connected = [
        label for label, ok in manager.list_devices()
        if ok
    ]
    console.print(
        f"\n[green]✓ {len(connected)} device(s) ready.[/green]"
    )

    return manager


def _connect_parallel(manager, devices, host, username, password):
    """Connect to multiple devices in parallel (much faster than serial)."""

    def connect_one(device):
        try:
            console.print(f"  → Connecting [cyan]{device.label}[/cyan]...")
            manager.add(
                label=device.label,
                host=host,
                username=username,
                password=password,
                path=device.console_path,
            )
            manager.connect(device.label)
            console.print(
                f"  [green]✓[/green] {device.label} connected"
            )
            return device.label, True
        except Exception as e:
            console.print(
                f"  [red]✗[/red] {device.label} failed: {e}"
            )
            return device.label, False

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as ex:
        list(ex.map(connect_one, devices))


def _resolve_lab(user_input, labs, auth):
    """Resolve lab from number, URL, or name."""
    # URL input
    if user_input.startswith("http"):
        host, lab_id = auth.parse_lab_url(user_input)
        for lab in labs:
            if lab.lab_id == lab_id:
                return lab
        # Build a minimal lab object from URL
        from cliq.connection.cml_auth import CMLLab
        return CMLLab(lab_id=lab_id, title=lab_id,
                      state="unknown", owner="")

    # Number input
    try:
        idx = int(user_input) - 1
        if 0 <= idx < len(labs):
            return labs[idx]
    except ValueError:
        pass

    # Name input
    for lab in labs:
        if lab.title.lower() == user_input.lower():
            return lab

    raise ValueError(f"Could not resolve lab: {user_input!r}")


def _resolve_devices(user_input, devices):
    """Parse device selection: all, 1,2,3, or 1-3."""
    if user_input.lower() == "all":
        return devices

    selected = []
    parts = user_input.split(",")
    for part in parts:
        part = part.strip()
        if "-" in part:
            start, end = part.split("-", 1)
            for i in range(int(start) - 1, int(end)):
                if 0 <= i < len(devices):
                    selected.append(devices[i])
        else:
            idx = int(part) - 1
            if 0 <= idx < len(devices):
                selected.append(devices[idx])

    return selected


def _print_labs_table(labs):
    table = Table(title="Your Labs", show_header=True,
                  header_style="bold cyan")
    table.add_column("#", width=4)
    table.add_column("Lab Name", min_width=20)
    table.add_column("State", width=12)
    table.add_column("Owner", width=15)

    for i, lab in enumerate(labs, 1):
        state_color = "green" if lab.state == "STARTED" else "yellow"
        table.add_row(
            str(i),
            lab.title,
            f"[{state_color}]{lab.state}[/{state_color}]",
            lab.owner,
        )
    console.print(table)


def _print_devices_table(devices):
    table = Table(title="Devices", show_header=True,
                  header_style="bold cyan")
    table.add_column("#", width=4)
    table.add_column("Label", min_width=12)
    table.add_column("Type", width=15)
    table.add_column("State", width=12)
    table.add_column("Console Path", min_width=25)

    for i, dev in enumerate(devices, 1):
        state_color = "green" if dev.state == "STARTED" else "yellow"
        table.add_row(
            str(i),
            f"[bold]{dev.label}[/bold]",
            dev.node_type,
            f"[{state_color}]{dev.state}[/{state_color}]",
            dev.console_path,
        )
    console.print(table)