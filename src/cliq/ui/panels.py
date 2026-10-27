#!/usr/bin/env python3
"""
panels.py — Rich panel builders for CLIQ shell error/intelligence output.

All shell-side rendering of error states (wrong-mode, destructive, syntax,
incomplete, typo, contextual help) lives here so app.py stays focused on
the prompt-toolkit event loop.
"""

from rich.panel import Panel
from rich.markup import escape


def typo_panel(raw_input: str, suggestions: list, confidence: float) -> Panel:
    """suggestions = list[str] of corrected commands."""
    if confidence > 0.85:
        conf_label = "[bold green]✓ HIGH CONFIDENCE[/bold green]"
    elif confidence > 0.6:
        conf_label = "[bold yellow]⚠ MEDIUM CONFIDENCE[/bold yellow]"
    else:
        conf_label = "[bold red]✗ LOW CONFIDENCE — please verify[/bold red]"

    sugg_text = "\n".join(
        f"  👉 [bold green]{escape(s)}[/bold green]" for s in suggestions
    )
    body = (
        f"[white]Command [bold cyan]'{escape(raw_input)}'[/bold cyan] was not recognized.[/white]\n\n"
        f"[bold yellow]💡 DID YOU MEAN:[/bold yellow]\n"
        f"{sugg_text}\n\n  {conf_label}"
    )
    return Panel(body, title="[bold yellow]⚠ CONTEXTUAL INTELLIGENCE[/bold yellow]",
                 border_style="yellow", expand=False)


def wrong_mode_panel(raw_input: str, current_prompt: str, target_prompt: str,
                     fix_path: list) -> Panel:
    """fix_path = list[(prompt, command_to_run)]."""
    lines = []
    for i, (prompt, cmd) in enumerate(fix_path, 1):
        lines.append(f"  {i}. {prompt} [cyan]{escape(cmd)}[/cyan]")
    fix_text = "\n".join(lines) if lines else "  [dim](no fix path)[/dim]"
    body = (
        f"[white]The command [bold cyan]'{escape(raw_input)}'[/bold cyan] is not valid in the current context.[/white]\n\n"
        f"[dim]CURRENT MODE:[/dim] [red]{current_prompt}[/red]\n"
        f"[dim]TARGET MODE:[/dim]  [green]{target_prompt}[/green]\n\n"
        f"[bold yellow]RECOMMENDED FIX PATH:[/bold yellow]\n{fix_text}"
    )
    return Panel(body, title="[bold red]❌ CONTEXTUAL ERROR: WRONG MODE[/bold red]",
                 border_style="red", expand=False)


def wrong_mode_do_panel(raw_input: str, do_suggestion: str) -> Panel:
    body = (
        f"[white]The command [bold cyan]'{escape(raw_input)}'[/bold cyan] is an exec command typed in a configuration mode.[/white]\n\n"
        f"[bold yellow]💡 DID YOU MEAN:[/bold yellow]\n"
        f"  👉 [bold green]{escape(do_suggestion)}[/bold green]\n\n"
        f"[dim]Tip: prefix exec commands with [bold]do[/bold] to run them from a config mode.[/dim]"
    )
    return Panel(body, title="[bold yellow]⚠ CONTEXTUAL INTELLIGENCE[/bold yellow]",
                 border_style="yellow", expand=False)


def syntax_error_panel(arg_error_msg: str, example: str = "") -> Panel:
    extra = f"\n\n[dim]Example:[/dim] [green]{escape(example)}[/green]" if example else ""
    body = (
        f"[white]Invalid argument provided.[/white]\n\n"
        f"[bold red]⚠ {escape(arg_error_msg)}[/bold red]{extra}"
    )
    return Panel(body, title="[bold red]❌ SYNTAX ERROR[/bold red]",
                 border_style="red", expand=False)


def incomplete_panel(raw_input: str, suggestions: list) -> Panel:
    sugg_text = "\n".join(
        f"  👉 [bold green]{escape(s)}[/bold green]" for s in suggestions
    ) if suggestions else "  [dim](no suggestions)[/dim]"
    body = (
        f"[white]Command [bold cyan]'{escape(raw_input)}'[/bold cyan] is incomplete.[/white]\n\n"
        f"{sugg_text}"
    )
    return Panel(body, title="[bold yellow]⏳ INCOMPLETE COMMAND[/bold yellow]",
                 border_style="yellow", expand=False)


def missing_arg_panel(what: str, how: str, example: str = "") -> Panel:
    extra = f"\n[dim]Example:[/dim] [green]{escape(example)}[/green]" if example else ""
    body = f"[white]{escape(what)}[/white]\n[bold yellow]→ {escape(how)}[/bold yellow]{extra}"
    return Panel(body, title="[bold yellow]⚠ MISSING ARGUMENT[/bold yellow]",
                 border_style="yellow", expand=False)


def destructive_panel(command: str) -> Panel:
    body = (
        f"[white]You are about to run a destructive command:[/white]\n"
        f"[bold red]{escape(command)}[/bold red]\n\n"
        f"[dim]This may disrupt service.[/dim]"
    )
    return Panel(body, title="[bold red]⚠ DESTRUCTIVE ACTION WARNING[/bold red]",
                 border_style="red", expand=False)


def wrong_device_panel(raw_input: str, what: str, why: str, how: str) -> Panel:
    body = (
        f"[white]Command [bold cyan]'{escape(raw_input)}'[/bold cyan] cannot run on this device.[/white]\n\n"
        f"[bold red]✖ {escape(what)}[/bold red]\n"
        f"[dim]{escape(why)}[/dim]\n\n"
        f"[bold yellow]→ {escape(how)}[/bold yellow]"
    )
    return Panel(body, title="[bold red]❌ WRONG DEVICE TYPE[/bold red]",
                 border_style="red", expand=False)


def help_panel(mode: str, prefix: str, completions: list, placeholders: list) -> Panel:
    """
    completions  = list[(token_or_full_cmd, description, destructive)]
    placeholders = list[(name, description, recent_values)]
    """
    lines = []
    if placeholders:
        lines.append("[bold cyan]EXPECTED ARGUMENTS:[/bold cyan]")
        for name, desc, recent in placeholders:
            lines.append(f"  [green]<{name}>[/green] - {desc}")
            if recent:
                lines.append(f"    [dim]Recent: {', '.join(recent[:3])}[/dim]")
        lines.append("")
    if completions:
        lines.append("[bold cyan]AVAILABLE NEXT TOKENS:[/bold cyan]")
        for token, desc, destructive in completions:
            flag = " [red]⚠[/red]" if destructive else ""
            lines.append(f"  [green]{token.ljust(24)}[/green] {desc}{flag}")
    if not lines:
        body = f"[red]No completions for '{escape(prefix)}' in '{mode}' mode.[/red]"
    else:
        body = "\n".join(lines).strip()
    title = f"[bold yellow]💡 HELP: {mode}" + (f" — '{escape(prefix)}'" if prefix else "") + "[/bold yellow]"
    return Panel(body, title=title, border_style="yellow", expand=False)
