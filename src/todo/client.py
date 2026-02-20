"""todoq — CLI client for the Todo Tracker API."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

import httpx
from rich.console import Console
from rich.table import Table

console = Console()


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def _load_config(data_dir: Path) -> dict:
    path = data_dir / "configs" / "api.json"
    if path.exists():
        return json.loads(path.read_text())
    return {"host": "localhost", "port": 8000}


def _base_url(cfg: dict) -> str:
    return f"http://{cfg['host']}:{cfg['port']}"


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _get(url: str, params: dict | None = None) -> httpx.Response:
    try:
        resp = httpx.get(url, params=params, timeout=10)
    except httpx.ConnectError:
        console.print(f"[red]Cannot reach server at {url}[/red]")
        raise SystemExit(1)
    if resp.status_code == 404:
        detail = resp.json().get("detail", "Not found")
        console.print(f"[yellow]{detail}[/yellow]")
        raise SystemExit(1)
    if resp.status_code != 200:
        detail = resp.json().get("detail", resp.text)
        console.print(f"[red]HTTP {resp.status_code}: {detail}[/red]")
        raise SystemExit(1)
    return resp


def _post(url: str, body: dict | None = None, timeout: int = 10) -> httpx.Response:
    try:
        resp = httpx.post(url, json=body, timeout=timeout)
    except httpx.ConnectError:
        console.print(f"[red]Cannot reach server at {url}[/red]")
        raise SystemExit(1)
    if resp.status_code not in (200, 201):
        detail = resp.json().get("detail", resp.text)
        console.print(f"[red]HTTP {resp.status_code}: {detail}[/red]")
        raise SystemExit(1)
    return resp


def _put(url: str, body: dict) -> httpx.Response:
    try:
        resp = httpx.put(url, json=body, timeout=10)
    except httpx.ConnectError:
        console.print(f"[red]Cannot reach server at {url}[/red]")
        raise SystemExit(1)
    if resp.status_code not in (200, 201):
        detail = resp.json().get("detail", resp.text)
        console.print(f"[red]HTTP {resp.status_code}: {detail}[/red]")
        raise SystemExit(1)
    return resp


def _delete(url: str) -> httpx.Response:
    try:
        resp = httpx.delete(url, timeout=10)
    except httpx.ConnectError:
        console.print(f"[red]Cannot reach server at {url}[/red]")
        raise SystemExit(1)
    if resp.status_code == 404:
        detail = resp.json().get("detail", "Not found")
        console.print(f"[yellow]{detail}[/yellow]")
        raise SystemExit(1)
    if resp.status_code != 200:
        detail = resp.json().get("detail", resp.text)
        console.print(f"[red]HTTP {resp.status_code}: {detail}[/red]")
        raise SystemExit(1)
    return resp


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------

def _relative_date(date_str: str) -> tuple[str, str]:
    """Return (label, style) for a date relative to today."""
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return (date_str, "")
    delta = (dt - datetime.now().date()).days
    if delta == 0:
        return ("today", "bold green")
    elif delta > 0:
        return (f"in {delta} day{'s' if delta != 1 else ''}", "cyan")
    else:
        n = abs(delta)
        return (f"{n} day{'s' if n != 1 else ''} ago", "dim yellow")


def _show_tasks_table(tasks: list[dict], title: str | None = None) -> None:
    if not tasks:
        console.print("[yellow]No tasks found.[/yellow]")
        return
    table = Table(title=title, show_lines=True)
    table.add_column("ID", style="dim", justify="right")
    table.add_column("Date", justify="right")
    table.add_column("Headline", style="bold")
    table.add_column("Context")
    for t in tasks:
        label, style = _relative_date(t["date"])
        date_cell = f"[{style}]{label}[/{style}]" if style else label
        table.add_row(str(t["id"]), date_cell, t["headline"], t.get("context", ""))
    console.print(table)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_retrieve_all(base: str) -> None:
    resp = _get(f"{base}/tasks")
    _show_tasks_table(resp.json(), title="All tasks")


def cmd_retrieve_tasks(base: str, days: int) -> None:
    resp = _get(f"{base}/tasks/window", params={"days": days})
    _show_tasks_table(resp.json(), title=f"Tasks within {days} days")


def cmd_run_load(base: str) -> None:
    console.print("[dim]Running Joplin load on server...[/dim]")
    resp = _post(f"{base}/run/load", timeout=120)
    data = resp.json()
    if data.get("output"):
        console.print(data["output"].rstrip())
    console.print("[green]Load completed.[/green]")


def cmd_edit(base: str, task_id: int) -> None:
    resp = _get(f"{base}/tasks")
    tasks = resp.json()
    task = next((t for t in tasks if t["id"] == task_id), None)
    if not task:
        console.print(f"[yellow]Task {task_id} not found.[/yellow]")
        raise SystemExit(1)

    editor = os.environ.get("EDITOR", "vi")
    edit_data = {"date": task["date"], "headline": task["headline"], "context": task["context"]}

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", prefix="todoq_", delete=False
    ) as tmp:
        json.dump(edit_data, tmp, indent=2)
        tmp.write("\n")
        tmp_path = tmp.name

    try:
        result = subprocess.run([editor, tmp_path])
        if result.returncode != 0:
            console.print("[red]Editor exited with non-zero status, aborting.[/red]")
            raise SystemExit(1)
        edited = json.loads(Path(tmp_path).read_text())
    except json.JSONDecodeError as exc:
        console.print(f"[red]Invalid JSON: {exc}[/red]")
        raise SystemExit(1)
    finally:
        os.unlink(tmp_path)

    resp = _put(f"{base}/tasks/{task_id}", edited)
    console.print("[green]Updated:[/green]")
    t = resp.json()
    console.print(f"  [{t['id']}] {t['date']}  {t['headline']}"
                  + (f"  ({t['context']})" if t['context'] else ""))


def cmd_delete(base: str, task_id: int) -> None:
    resp = _delete(f"{base}/tasks/{task_id}")
    console.print(f"[green]Deleted task {task_id}.[/green]")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="todoq",
        description="Query and manage the Todo Tracker via its API.",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path("data"),
        help="Directory containing configs/api.json (default: ./data)",
    )

    sub = parser.add_subparsers(dest="group", required=True)

    # ── todoq retrieve ──
    ret_parser = sub.add_parser("retrieve", help="Query tasks")
    ret_sub = ret_parser.add_subparsers(dest="command", required=True)
    ret_sub.add_parser("all", help="List all tasks")
    tasks_parser = ret_sub.add_parser("tasks", help="Tasks within N days of today")
    tasks_parser.add_argument("days", type=int, help="Number of days (bidirectional)")

    # ── todoq run ──
    run_parser = sub.add_parser("run", help="Trigger server-side operations")
    run_sub = run_parser.add_subparsers(dest="command", required=True)
    run_sub.add_parser("load", help="Load tasks from Joplin")

    # ── todoq edit ──
    edit_parser = sub.add_parser("edit", help="Edit a task in $EDITOR")
    edit_parser.add_argument("id", type=int, help="Task ID to edit")

    # ── todoq delete ──
    delete_parser = sub.add_parser("delete", help="Delete a task")
    delete_parser.add_argument("id", type=int, help="Task ID to delete")

    args = parser.parse_args(argv)

    cfg = _load_config(args.data_dir.resolve())
    base = _base_url(cfg)

    if args.group == "retrieve":
        if args.command == "all":
            cmd_retrieve_all(base)
        elif args.command == "tasks":
            cmd_retrieve_tasks(base, args.days)

    elif args.group == "run":
        if args.command == "load":
            cmd_run_load(base)

    elif args.group == "edit":
        cmd_edit(base, args.id)

    elif args.group == "delete":
        cmd_delete(base, args.id)


if __name__ == "__main__":
    main()
