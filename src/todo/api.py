from __future__ import annotations

import sqlite3
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

from fastapi import FastAPI, HTTPException

from todo.models import TaskIn, TaskOut

app = FastAPI(title="Todo Tracker")

# Module-level state, set by cli.py before uvicorn.run()
DB_PATH: Path = Path("data/db.sqlite")
DATA_DIR: Path = Path("data")


def _get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            headline TEXT NOT NULL,
            context TEXT DEFAULT ''
        );
    """)
    return conn


@app.get("/tasks", response_model=list[TaskOut])
def get_tasks():
    conn = _get_db()
    try:
        rows = conn.execute("SELECT * FROM tasks ORDER BY date").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@app.get("/tasks/window", response_model=list[TaskOut])
def get_tasks_window(days: int = 7):
    today = date.today()
    from_date = (today - timedelta(days=days)).isoformat()
    to_date = (today + timedelta(days=days)).isoformat()
    conn = _get_db()
    try:
        rows = conn.execute(
            "SELECT * FROM tasks WHERE date >= ? AND date <= ? ORDER BY date",
            (from_date, to_date),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@app.post("/tasks", response_model=TaskOut, status_code=201)
def create_task(task: TaskIn):
    conn = _get_db()
    try:
        cur = conn.execute(
            "INSERT INTO tasks (date, headline, context) VALUES (?, ?, ?)",
            (task.date, task.headline, task.context),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (cur.lastrowid,)).fetchone()
        return dict(row)
    finally:
        conn.close()


@app.put("/tasks/{task_id}", response_model=TaskOut)
def update_task(task_id: int, task: TaskIn):
    conn = _get_db()
    try:
        existing = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
        conn.execute(
            "UPDATE tasks SET date = ?, headline = ?, context = ? WHERE id = ?",
            (task.date, task.headline, task.context, task_id),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return dict(row)
    finally:
        conn.close()


@app.delete("/tasks/{task_id}")
def delete_task(task_id: int):
    conn = _get_db()
    try:
        existing = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
        conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        conn.commit()
        return {"status": "deleted", "id": task_id}
    finally:
        conn.close()


@app.post("/run/load")
def run_load():
    result = subprocess.run(
        [sys.executable, "-m", "todo", "--data-dir", str(DATA_DIR), "run", "load"],
        capture_output=True, text=True, timeout=120,
    )
    output = result.stdout + result.stderr
    if result.returncode != 0:
        raise HTTPException(status_code=500, detail=output or "Load failed")
    return {"status": "ok", "output": output}
