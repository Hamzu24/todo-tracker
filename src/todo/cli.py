from __future__ import annotations

import argparse
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="todo", description="Todo Tracker")
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path("data"),
        help="Directory for database, credentials, and configs (default: ./data)",
    )

    sub = parser.add_subparsers(dest="group", required=True)

    # ── todo serve ──
    serve_parser = sub.add_parser("serve", help="Manage the API server")
    serve_parser.add_argument(
        "action", nargs="?", default="start",
        choices=["start", "stop", "restart", "status"],
        help="Server action (default: start)",
    )
    serve_parser.add_argument("--host", default="100.64.144.22")
    serve_parser.add_argument("--port", type=int, default=8001)

    # ── todo run ──
    run_parser = sub.add_parser("run", help="Run pipeline operations")
    run_sub = run_parser.add_subparsers(dest="command", required=True)
    run_sub.add_parser("load", help="Load tasks from Joplin note")

    # ── todo query ──
    query_parser = sub.add_parser("query", help="Query tasks directly from DB")
    query_sub = query_parser.add_subparsers(dest="command", required=True)
    query_sub.add_parser("all", help="List all tasks")
    window_parser = query_sub.add_parser("window", help="Tasks within N days of today")
    window_parser.add_argument("days", type=int, help="Number of days (bidirectional)")

    args = parser.parse_args(argv)

    data_dir: Path = args.data_dir.resolve()
    data_dir.mkdir(parents=True, exist_ok=True)

    # ── serve ──
    if args.group == "serve":
        _handle_serve(args, data_dir)

    # ── run ──
    elif args.group == "run":
        if args.command == "load":
            from todo.joplin import load_from_joplin
            load_from_joplin(data_dir, "http://100.64.144.22:8001")

    # ── query ──
    elif args.group == "query":
        import sqlite3
        from datetime import date, timedelta

        db_path = data_dir / "db.sqlite"
        if not db_path.exists():
            print(f"Database not found: {db_path}")
            raise SystemExit(1)

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        try:
            if args.command == "all":
                rows = conn.execute("SELECT * FROM tasks ORDER BY date").fetchall()
            elif args.command == "window":
                today = date.today()
                from_date = (today - timedelta(days=args.days)).isoformat()
                to_date = (today + timedelta(days=args.days)).isoformat()
                rows = conn.execute(
                    "SELECT * FROM tasks WHERE date >= ? AND date <= ? ORDER BY date",
                    (from_date, to_date),
                ).fetchall()
            else:
                rows = []

            for row in rows:
                print(f"[{row['id']}] {row['date']}  {row['headline']}"
                      + (f"  ({row['context']})" if row['context'] else ""))
        finally:
            conn.close()


def _handle_serve(args, data_dir: Path) -> None:
    pid_file = data_dir / "api.pid"

    def _pid_from_port(port: int) -> int | None:
        try:
            out = subprocess.check_output(
                ["ss", "-tlnp", f"sport = :{port}"],
                text=True, stderr=subprocess.DEVNULL,
            )
            m = re.search(r"pid=(\d+)", out)
            return int(m.group(1)) if m else None
        except (FileNotFoundError, subprocess.CalledProcessError):
            return None

    def _read_pid() -> int | None:
        if pid_file.exists():
            try:
                pid = int(pid_file.read_text().strip())
                os.kill(pid, 0)
                return pid
            except (ValueError, ProcessLookupError, PermissionError):
                pid_file.unlink(missing_ok=True)
        return None

    def _stop_server() -> bool:
        pid = _read_pid()
        if pid is None:
            pid = _pid_from_port(args.port)
            if pid is not None:
                print(f"PID file missing; found server on port {args.port} (pid {pid}).")
        if pid is None:
            print("No running API server found.")
            return False
        os.kill(pid, signal.SIGTERM)
        for _ in range(10):
            time.sleep(0.5)
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                pid_file.unlink(missing_ok=True)
                print(f"Stopped API server (pid {pid}).")
                return True
        os.kill(pid, signal.SIGKILL)
        pid_file.unlink(missing_ok=True)
        print(f"Killed API server (pid {pid}).")
        return True

    def _start_server() -> None:
        if _read_pid() is not None:
            print(f"API server already running (pid {_read_pid()}).")
            return
        log_path = data_dir / "api.log"
        proc = subprocess.Popen(
            [sys.executable, "-m", "todo", "--data-dir", str(data_dir),
             "serve", "--host", args.host, "--port", str(args.port)],
            stdout=open(log_path, "a"),
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        for _ in range(15):
            time.sleep(1)
            try:
                import httpx
                httpx.get(f"http://{args.host}:{args.port}/tasks", timeout=2)
                print(f"API server started (pid {proc.pid}, port {args.port}).")
                return
            except Exception:
                pass
        print(f"API server launched (pid {proc.pid}) but not yet responding. Check {log_path}")

    if args.action == "start":
        if _read_pid() is not None:
            print(f"API server already running (pid {_read_pid()}).")
        else:
            import todo.api as api
            import uvicorn
            api.DB_PATH = (data_dir / "db.sqlite").resolve()
            api.DATA_DIR = data_dir.resolve()
            pid_file.write_text(str(os.getpid()))
            try:
                uvicorn.run(api.app, host=args.host, port=args.port)
            finally:
                pid_file.unlink(missing_ok=True)

    elif args.action == "stop":
        _stop_server()

    elif args.action == "restart":
        _stop_server()
        time.sleep(1)
        _start_server()

    elif args.action == "status":
        pid = _read_pid() or _pid_from_port(args.port)
        if pid:
            print(f"API server running (pid {pid}, port {args.port}).")
        else:
            print("API server not running.")
