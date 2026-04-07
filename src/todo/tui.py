"""Interactive TUI for managing tasks."""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import httpx
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import (
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    Static,
)


# ---------------------------------------------------------------------------
# HTTP helpers (reuse logic from client.py but return data directly)
# ---------------------------------------------------------------------------

def _load_config(data_dir: Path) -> dict:
    path = data_dir / "configs" / "api.json"
    if path.exists():
        return json.loads(path.read_text())
    return {"host": "localhost", "port": 8001}


def _base_url(cfg: dict) -> str:
    return f"http://{cfg['host']}:{cfg['port']}"


def _fetch_tasks(base: str, days: int) -> list[dict]:
    resp = httpx.get(f"{base}/tasks/window", params={"days": days}, timeout=10)
    resp.raise_for_status()
    return resp.json()


def _delete_task(base: str, task_id: int) -> None:
    resp = httpx.delete(f"{base}/tasks/{task_id}", timeout=10)
    resp.raise_for_status()


def _postpone_task(base: str, task: dict, days: int) -> None:
    old_date = task["date"]
    new_date = (
        datetime.strptime(old_date, "%Y-%m-%d").date() + timedelta(days=days)
    ).isoformat()
    resp = httpx.put(
        f"{base}/tasks/{task['id']}",
        json={"date": new_date, "headline": task["headline"], "context": task.get("context", "")},
        timeout=10,
    )
    resp.raise_for_status()


# ---------------------------------------------------------------------------
# Relative date display
# ---------------------------------------------------------------------------

def _relative_date(date_str: str) -> str:
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return date_str
    delta = (dt - datetime.now().date()).days
    if delta == 0:
        return "today"
    elif delta > 0:
        return f"in {delta} day{'s' if delta != 1 else ''}"
    else:
        n = abs(delta)
        return f"{n} day{'s' if n != 1 else ''} ago"


# ---------------------------------------------------------------------------
# Postpone modal
# ---------------------------------------------------------------------------

class PostponeModal(ModalScreen[int | None]):
    """Ask user how many days to postpone."""

    DEFAULT_CSS = """
    PostponeModal {
        align: center middle;
    }
    #postpone-dialog {
        width: 40;
        height: 10;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
    }
    #postpone-dialog Label {
        margin-bottom: 1;
    }
    #postpone-input {
        width: 100%;
    }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="postpone-dialog"):
            yield Label("Postpone by how many days?")
            yield Input(id="postpone-input", placeholder="e.g. 3", type="integer")

    def on_mount(self) -> None:
        self.query_one("#postpone-input", Input).focus()

    @on(Input.Submitted, "#postpone-input")
    def on_submit(self, event: Input.Submitted) -> None:
        val = event.value.strip()
        if val and val.isdigit() and int(val) > 0:
            self.dismiss(int(val))
        else:
            self.dismiss(None)

    def key_escape(self) -> None:
        self.dismiss(None)


# ---------------------------------------------------------------------------
# Lookforward modal
# ---------------------------------------------------------------------------

class LookforwardModal(ModalScreen[int | None]):
    """Ask user for lookforward days."""

    DEFAULT_CSS = """
    LookforwardModal {
        align: center middle;
    }
    #lookforward-dialog {
        width: 40;
        height: 10;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
    }
    #lookforward-dialog Label {
        margin-bottom: 1;
    }
    #lookforward-input {
        width: 100%;
    }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="lookforward-dialog"):
            yield Label("Lookforward days (0 = up to today):")
            yield Input(id="lookforward-input", placeholder="e.g. 7", type="integer")

    def on_mount(self) -> None:
        self.query_one("#lookforward-input", Input).focus()

    @on(Input.Submitted, "#lookforward-input")
    def on_submit(self, event: Input.Submitted) -> None:
        val = event.value.strip()
        if val and val.isdigit():
            self.dismiss(int(val))
        else:
            self.dismiss(None)

    def key_escape(self) -> None:
        self.dismiss(None)


# ---------------------------------------------------------------------------
# Main TUI app
# ---------------------------------------------------------------------------

class TodoTUI(App):
    """Interactive task manager."""

    TITLE = "Todo Tracker"

    CSS = """
    #status {
        dock: top;
        height: 1;
        background: $accent;
        color: $text;
        padding: 0 1;
    }
    DataTable {
        height: 1fr;
    }
    """

    BINDINGS = [
        Binding("d", "delete_task", "Delete"),
        Binding("p", "postpone_task", "Postpone"),
        Binding("c", "complete_task", "Complete"),
        Binding("l", "set_lookforward", "Lookforward"),
        Binding("r", "refresh", "Refresh"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, base_url: str, days: int = 0) -> None:
        super().__init__()
        self.base_url = base_url
        self.days = days
        self.tasks: list[dict] = []

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("", id="status")
        yield DataTable()
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.cursor_type = "row"
        table.add_columns("ID", "Date", "Headline", "Context")
        self._load_tasks()

    def _load_tasks(self) -> None:
        try:
            self.tasks = _fetch_tasks(self.base_url, self.days)
        except (httpx.ConnectError, httpx.HTTPStatusError) as e:
            self.query_one("#status", Static).update(f"[red]Error: {e}[/red]")
            self.tasks = []
            return

        table = self.query_one(DataTable)
        table.clear()
        for t in self.tasks:
            table.add_row(
                str(t["id"]),
                _relative_date(t["date"]),
                t["headline"],
                t.get("context", ""),
                key=str(t["id"]),
            )
        self.query_one("#status", Static).update(
            f" Lookforward: [bold]{self.days}[/bold] days  |  [bold]{len(self.tasks)}[/bold] tasks"
        )

    def _get_selected_task(self) -> dict | None:
        table = self.query_one(DataTable)
        if table.row_count == 0:
            return None
        row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
        task_id = int(str(row_key.value))
        return next((t for t in self.tasks if t["id"] == task_id), None)

    def action_refresh(self) -> None:
        self._load_tasks()

    def action_delete_task(self) -> None:
        task = self._get_selected_task()
        if not task:
            return
        try:
            _delete_task(self.base_url, task["id"])
            self.notify(f"Deleted: {task['headline']}")
        except (httpx.ConnectError, httpx.HTTPStatusError) as e:
            self.notify(f"Error: {e}", severity="error")
            return
        self._load_tasks()

    def action_complete_task(self) -> None:
        task = self._get_selected_task()
        if not task:
            return
        try:
            _delete_task(self.base_url, task["id"])
            self.notify(f"Completed: {task['headline']}", severity="information")
        except (httpx.ConnectError, httpx.HTTPStatusError) as e:
            self.notify(f"Error: {e}", severity="error")
            return
        self._load_tasks()

    def action_postpone_task(self) -> None:
        task = self._get_selected_task()
        if not task:
            return

        def on_postpone(days: int | None) -> None:
            if days is None:
                return
            try:
                _postpone_task(self.base_url, task, days)
                self.notify(f"Postponed '{task['headline']}' by {days} day(s)")
            except (httpx.ConnectError, httpx.HTTPStatusError) as e:
                self.notify(f"Error: {e}", severity="error")
                return
            self._load_tasks()

        self.push_screen(PostponeModal(), on_postpone)

    def action_set_lookforward(self) -> None:
        def on_lookforward(days: int | None) -> None:
            if days is None:
                return
            self.days = days
            self._load_tasks()

        self.push_screen(LookforwardModal(), on_lookforward)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run_tui(data_dir: Path, days: int = 0) -> None:
    cfg = _load_config(data_dir.resolve())
    base = _base_url(cfg)
    app = TodoTUI(base, days)
    app.run()
