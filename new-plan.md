# Skeleton Plan: Pi-Hosted Database Repo

## Context

You want a new single-repo project that runs on both your Raspberry Pi and your local computer. The Pi hosts a SQLite database + FastAPI server (reachable over Tailscale), and your local machine runs a CLI client that talks to that server. A Joplin-based loading system ingests structured text into the database. This plan describes the architecture and skeleton, modeled directly on the patterns in `personal-crm`.

## Architecture Overview

```
[Pi]                                         [Local Machine]
  crm-style local CLI (argparse)               Remote CLI client (httpx + rich)
        |                                            |
  FastAPI server (uvicorn)  <--- Tailscale --->  HTTP calls
        |
  SQLite DB (WAL mode)
        |
  Joplin loader (fetch note -> parse -> POST to API)
```

Single repo, two entry points:
- `python -m <pkg>` — local CLI on the Pi (serve, load, query, etc.)
- `<pkg>q` — remote CLI client on either machine (installed via `pip install -e .`)

---

## 1. Project Structure

```
src/<pkg>/
  __main__.py          # Entry: python -m <pkg> -> cli.main()
  cli.py               # Local CLI (argparse nested subcommands)
  client.py            # Remote CLI client (<pkg>q)
  api.py               # FastAPI server (CRUD + query endpoints)
  models.py            # Dataclasses / Pydantic models for DB records
  state.py             # Atomic JSON read/write (tmp + os.replace)
  settings.py          # Config loading with deep-merge defaults
  joplin.py            # Joplin Server note fetcher + parser + loader
data/                  # .gitignored runtime directory
  credentials.json     # Joplin config (url, email, password, note_path)
  db.sqlite            # SQLite database
  configs/
    api.json           # {"host": "...", "port": ...} for client
    settings.json      # User-overridable settings
pyproject.toml         # setuptools, console_scripts for <pkg>q
```

**Follows from personal-crm:**
- `src/` layout with setuptools (`[tool.setuptools.packages.find] where = ["src"]`)
- Local CLI via `__main__.py` (`python -m <pkg>`)
- Remote CLI registered as console script in `pyproject.toml` (`<pkg>q = "<pkg>.client:main"`)
- `data/` directory `.gitignored`, all runtime state lives there

---

## 2. Database (SQLite on Pi)

**Pattern from personal-crm** (`api.py` lines 66-88):

```python
# Module-level path, set by CLI before uvicorn.run()
DB_PATH = Path("data/db.sqlite")

def _get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            -- your schema columns here --
            date TEXT NOT NULL DEFAULT '',
            ...
        );
    """)
    return conn
```

- Schema auto-creates on first connection (`CREATE TABLE IF NOT EXISTS`)
- WAL mode for concurrent reads during API serving
- `sqlite3.Row` factory for dict-like access
- Caller closes connection in try/finally

---

## 3. FastAPI Server

**Pattern from personal-crm** (`api.py`):

```python
app = FastAPI(title="<Your App>")

# Module-level vars set by CLI before starting uvicorn
DB_PATH: Path = Path("data/db.sqlite")
DATA_DIR: Path = Path("data")

# CRUD endpoints
@app.get("/items")                    # List all
@app.get("/items/{id}")               # Get by ID
@app.get("/items/by-date")            # Query by date range (?from=&to=)
@app.put("/items/{id}")               # Edit/update item
@app.post("/items")                   # Create item
@app.delete("/items/{id}")            # Delete item

# Config endpoints
@app.get("/configs/{name}")           # Read a config file
@app.put("/configs/{name}")           # Update a config file

# Pipeline trigger endpoints
@app.post("/run/load")                # Trigger Joplin load
```

**Date-based retrieval** (follows the `follow-ups` pattern):
```python
@app.get("/items/by-date")
def get_items_by_date(from_date: str = "", to_date: str = ""):
    conn = _get_db()
    try:
        query = "SELECT * FROM items WHERE date >= ? AND date <= ? ORDER BY date"
        rows = conn.execute(query, (from_date, to_date)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()
```

**Edit endpoint** (follows the `PUT /persons/by-name/{name}` pattern):
```python
@app.put("/items/{item_id}")
def update_item(item_id: int, body: dict):
    conn = _get_db()
    try:
        # Update fields from body, commit
        conn.execute("UPDATE items SET ... WHERE id = ?", (..., item_id))
        conn.commit()
        return {"status": "updated"}
    finally:
        conn.close()
```

---

## 4. Local CLI on Pi

**Pattern from personal-crm** (`cli.py`):

```python
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    sub = parser.add_subparsers(dest="group")

    # Group: serve
    serve_p = sub.add_parser("serve")
    serve_p.add_argument("action", nargs="?", default="start",
                         choices=["start", "stop", "restart", "status"])
    serve_p.add_argument("--host", default="0.0.0.0")
    serve_p.add_argument("--port", type=int, default=8000)

    # Group: run
    run_p = sub.add_parser("run")
    run_sub = run_p.add_subparsers(dest="command")
    run_sub.add_parser("load")        # Load from Joplin

    # Group: query (local DB access, no server needed)
    query_p = sub.add_parser("query")
    query_sub = query_p.add_subparsers(dest="command")
    query_sub.add_parser("all")
    query_sub.add_parser("by-date")

    args = parser.parse_args()
    # dispatch...
```

**Server daemon management** (follows personal-crm exactly):
- `serve start`: Set `api.DB_PATH` and `api.DATA_DIR`, then `uvicorn.run(api.app, ...)`
- PID file at `data/api.pid`
- `serve stop`: Read PID, SIGTERM with retry, then SIGKILL
- `serve status`: Check PID file + `ss -tlnp` port lookup
- `serve restart`: stop + sleep + start

---

## 5. Remote CLI Client

**Pattern from personal-crm** (`client.py`):

```python
import httpx
from rich.console import Console
from rich.table import Table

def _load_config(data_dir: Path) -> dict:
    path = data_dir / "configs" / "api.json"
    if path.exists():
        return json.loads(path.read_text())
    return {"host": "localhost", "port": 8000}

def _get(url, params=None):
    resp = httpx.get(url, params=params, timeout=10)
    resp.raise_for_status()
    return resp

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    sub = parser.add_subparsers(dest="group")

    # retrieve group
    ret_p = sub.add_parser("retrieve")
    ret_sub = ret_p.add_subparsers(dest="command")
    ret_sub.add_parser("all")
    by_date = ret_sub.add_parser("by-date")
    by_date.add_argument("--from", dest="from_date")
    by_date.add_argument("--to", dest="to_date")

    # edit group
    edit_p = sub.add_parser("edit")
    edit_sub = edit_p.add_subparsers(dest="command")
    edit_item = edit_sub.add_parser("item")
    edit_item.add_argument("id", type=int)

    # run group (trigger server-side operations)
    run_p = sub.add_parser("run")
    run_sub = run_p.add_subparsers(dest="command")
    run_sub.add_parser("load")

    args = parser.parse_args()
    cfg = _load_config(args.data_dir)
    base = f"http://{cfg['host']}:{cfg['port']}"
    # dispatch to cmd_retrieve_all(base), cmd_edit_item(base, id), etc.
```

**Edit-in-$EDITOR pattern** (from personal-crm `client.py`):
```python
def _edit_json_in_editor(data: dict) -> dict:
    tmp = tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False)
    json.dump(data, tmp, indent=2)
    tmp.close()
    subprocess.run([os.environ.get("EDITOR", "vim"), tmp.name])
    with open(tmp.name) as f:
        return json.load(f)
```

**`api.json` on local machine** points to Pi's Tailscale IP:
```json
{"host": "100.x.x.x", "port": 8000}
```

**`api.json` on Pi** (for local use) can point to localhost:
```json
{"host": "localhost", "port": 8000}
```

---

## 6. Joplin Loading System

**Pattern from personal-crm** (`joplin.py`):

### Joplin Server Access

The Pi runs Joplin Server (or connects to one). Credentials in `data/credentials.json`:
```json
{
  "joplin": {
    "url": "http://localhost:22300",
    "email": "user@example.com",
    "password": "...",
    "note_path": "notebook/note_title"
  }
}
```

**How it works** (from personal-crm's `joplin.py`):
1. **Authenticate**: `POST /api/sessions` with email+password -> get `sessionId` cookie
2. **Resolve note path**: List all items via `GET /api/items/root:/:/children` (paginated), match by notebook name (type=2) then note title (type=1) within that parent
3. **Fetch note content**: `GET /api/items/root:/{item_name}:/content` -> parse Joplin sync format (title, blank line, body, blank line, metadata lines)
4. **Parse body**: Line-by-line structured text (your format TBD)
5. **Load into DB**: For each parsed item, `POST /items` to the FastAPI server
6. **Clear note**: Rebuild the Joplin item with empty body, update timestamps, `PUT` it back — so items aren't re-ingested on next run

### Loading Flow

```python
def load_from_joplin(data_dir: Path, base_url: str):
    cfg = load_joplin_config(data_dir)
    token = fetch_session_token(cfg["url"], cfg["email"], cfg["password"])
    item_name = resolve_note_path(cfg["url"], token, cfg["note_path"])
    body = fetch_note_body(cfg["url"], token, item_name)
    if not body.strip():
        return  # Nothing to load
    entries = parse_entries(body)  # Your line format
    for entry in entries:
        httpx.post(f"{base_url}/items", json=entry, timeout=10)
    clear_note(cfg["url"], token, item_name)
```

Triggered via:
- Local CLI: `python -m <pkg> run load`
- Remote CLI: `<pkg>q run load` -> `POST /run/load` on the server

---

## 7. Atomic File I/O

**Pattern from personal-crm** (`state.py`):

```python
def save_json(data: Any, path: Path) -> None:
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)

def load_json(path: Path, default: Any = None) -> Any:
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return default
```

Used for all config files, state, and any JSON persistence.

---

## 8. Config / Settings with Deep-Merge Defaults

**Pattern from personal-crm** (`settings.py`):

```python
DEFAULTS = {
    "server": {"host": "0.0.0.0", "port": 8000},
    # ...
}

def load_settings(path: Path) -> dict:
    settings = copy.deepcopy(DEFAULTS)
    if path.exists():
        with open(path) as f:
            _deep_merge(settings, json.load(f))
    return settings

def _deep_merge(base: dict, override: dict) -> None:
    for k, v in override.items():
        if k in base and isinstance(base[k], dict) and isinstance(v, dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
```

---

## 9. Packaging (`pyproject.toml`)

```toml
[build-system]
requires = ["setuptools>=68.0"]
build-backend = "setuptools.build_meta"

[project]
name = "<project-name>"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
    "fastapi",
    "uvicorn[standard]",
    "httpx",
    "rich",
]

[project.scripts]
<pkg>q = "<pkg>.client:main"

[tool.setuptools.packages.find]
where = ["src"]
```

- Install on both machines: `pip install -e .`
- Pi uses `python -m <pkg>` for local CLI + server
- Local machine uses `<pkg>q` for remote access
- `<pkg>q` also works on Pi (talks to localhost)

---

## 10. Deployment Summary

| Machine | What runs | Entry point | `api.json` host |
|---------|-----------|-------------|-----------------|
| Pi | Server + local CLI + Joplin loader | `python -m <pkg> serve` | `localhost` |
| Local | Remote CLI client only | `<pkg>q retrieve all` | Pi's Tailscale IP |

Both machines clone the same repo and run `pip install -e .`.

---

## Verification

1. On Pi: `python -m <pkg> serve start` — server starts, DB auto-creates
2. On Pi: `python -m <pkg> run load` — fetches Joplin note, parses, loads into DB
3. On local: `<pkg>q retrieve all` — lists all items from Pi over Tailscale
4. On local: `<pkg>q retrieve by-date --from 2026-01-01 --to 2026-02-20` — date filtering
5. On local: `<pkg>q edit item 1` — opens in $EDITOR, PUTs back to API
6. On Pi: `<pkg>q retrieve all` — same command works locally too
