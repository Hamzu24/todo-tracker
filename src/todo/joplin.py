"""Joplin Server integration — fetch tasks from a Joplin note."""
from __future__ import annotations

import datetime
import json
import re
import urllib.error
import urllib.request
from pathlib import Path


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def _load_joplin_config(data_dir: Path) -> dict | None:
    creds_path = data_dir / "credentials.json"
    if not creds_path.exists():
        return None
    creds = json.loads(creds_path.read_text())
    return creds.get("joplin")


# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------

def _api_request(url: str, *, method: str = "GET", data: bytes | None = None,
                 headers: dict[str, str] | None = None,
                 host: str = "") -> bytes:
    h = dict(headers) if headers else {}
    if host:
        h["Host"] = host
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.read()


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

def _cookie_headers(token: str) -> dict[str, str]:
    return {"Cookie": f"sessionId={token}"}


def fetch_session_token(base_url: str, email: str, password: str,
                        host: str = "") -> str:
    url = f"{base_url}/api/sessions"
    body = json.dumps({"email": email, "password": password}).encode()
    resp = _api_request(url, method="POST", data=body,
                        headers={"Content-Type": "application/json"},
                        host=host)
    return json.loads(resp)["id"]


# ---------------------------------------------------------------------------
# Joplin sync-item format
# ---------------------------------------------------------------------------

def _parse_joplin_item(content: str) -> dict:
    lines = content.rstrip('\n').split('\n')
    title = lines[0] if lines else ''

    metadata_start = len(lines)
    for i in range(1, len(lines)):
        if re.match(r'^id: [a-f0-9]{32}\s*$', lines[i]):
            metadata_start = i
            break

    metadata: dict[str, str] = {}
    for i in range(metadata_start, len(lines)):
        line = lines[i]
        if ':' in line:
            key, value = line.split(':', 1)
            metadata[key.strip()] = value.strip()

    body_start = 1
    while body_start < metadata_start and not lines[body_start].strip():
        body_start += 1
    body_end = metadata_start
    while body_end > body_start and not lines[body_end - 1].strip():
        body_end -= 1
    body = '\n'.join(lines[body_start:body_end]) if body_start < body_end else ''

    return {
        'title': title,
        'body': body,
        'type': int(metadata.get('type_', '0') or '0'),
        'id': metadata.get('id', ''),
        'parent_id': metadata.get('parent_id', ''),
        'metadata_lines': lines[metadata_start:],
    }


def _rebuild_joplin_item(title: str, body: str, metadata_lines: list[str]) -> str:
    if body:
        return '\n'.join([title, '', body, ''] + metadata_lines) + '\n'
    return '\n'.join([title, '', ''] + metadata_lines) + '\n'


# ---------------------------------------------------------------------------
# Note path resolution
# ---------------------------------------------------------------------------

def _list_all_items(base_url: str, token: str, host: str = "") -> list[dict]:
    items: list[dict] = []
    cursor = ""
    while True:
        url = f"{base_url}/api/items/root:/:/children"
        if cursor:
            url += f"?cursor={cursor}"
        resp = _api_request(url, headers=_cookie_headers(token), host=host)
        data = json.loads(resp)
        items.extend(data.get("items", []))
        if not data.get("has_more"):
            break
        cursor = data.get("cursor", "")
    return items


def _find_note_item(base_url: str, token: str,
                    notebook_name: str, note_title: str,
                    host: str = "") -> str | None:
    items = _list_all_items(base_url, token, host=host)

    folder_id: str | None = None
    candidate_notes: list[tuple[str, dict]] = []

    for item in items:
        name = item.get("name", "")
        if not name.endswith(".md"):
            continue
        try:
            raw = _api_request(
                f"{base_url}/api/items/root:/{name}:/content",
                headers=_cookie_headers(token), host=host,
            )
            parsed = _parse_joplin_item(raw.decode("utf-8"))
        except Exception:
            continue

        # type 2 = notebook/folder — use startswith for emoji suffixes
        if parsed['type'] == 2 and parsed['title'].startswith(notebook_name):
            folder_id = parsed['id']

        # type 1 = note
        if parsed['type'] == 1 and parsed['title'] == note_title:
            candidate_notes.append((name, parsed))

    if notebook_name and not folder_id:
        return None

    matches = [(n, p) for n, p in candidate_notes
               if not notebook_name or p['parent_id'] == folder_id]
    matches.sort(key=lambda x: (x[1]['body'] == '', x[0]))
    for item_name, parsed in matches:
        return item_name

    return None


def _resolve_note_item(base_url: str, token: str,
                       note_path: str, data_dir: Path,
                       host: str = "") -> str | None:
    parts = note_path.rsplit("/", 1)
    if len(parts) == 2:
        notebook_name, note_title = parts
    else:
        notebook_name, note_title = "", parts[0]

    return _find_note_item(base_url, token, notebook_name, note_title,
                           host=host)


# ---------------------------------------------------------------------------
# Fetch / clear note
# ---------------------------------------------------------------------------

def fetch_note(base_url: str, token: str, item_name: str,
               host: str = "") -> str:
    url = f"{base_url}/api/items/root:/{item_name}:/content"
    resp = _api_request(url, headers=_cookie_headers(token), host=host)
    parsed = _parse_joplin_item(resp.decode("utf-8"))
    return parsed['body']


def clear_note(base_url: str, token: str, item_name: str,
               host: str = "") -> None:
    content_url = f"{base_url}/api/items/root:/{item_name}:/content"
    raw = _api_request(content_url, headers=_cookie_headers(token), host=host)
    text = raw.decode("utf-8")

    title_end = text.index('\n\n') + 2
    meta_match = re.search(r'\n\nid: [a-f0-9]{32}', text)
    if not meta_match:
        raise ValueError("Cannot find metadata boundary in Joplin note")

    cleared = text[:title_end] + text[meta_match.start():]

    now = datetime.datetime.now(datetime.timezone.utc).strftime(
        '%Y-%m-%dT%H:%M:%S.000Z')
    cleared = re.sub(r'(?<=\nupdated_time: ).*', now, cleared)
    cleared = re.sub(r'(?<=\nuser_updated_time: ).*', now, cleared)

    _api_request(content_url, method="PUT", data=cleared.encode("utf-8"),
                 headers={**_cookie_headers(token),
                          "Content-Type": "application/octet-stream"},
                 host=host)


# ---------------------------------------------------------------------------
# Task parser
# ---------------------------------------------------------------------------

def parse_tasks(text: str) -> list[dict[str, str]]:
    """Parse lines of format: date;headline or date;headline;context"""
    entries: list[dict[str, str]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split(";")]
        if len(parts) < 2:
            continue
        date_str = parts[0]
        headline = parts[1]
        if not date_str or not headline:
            continue
        context = parts[2] if len(parts) >= 3 else ""
        entries.append({"date": date_str, "headline": headline, "context": context})
    return entries


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def load_from_joplin(data_dir: Path, api_base: str) -> int:
    """Fetch tasks from Joplin note and POST to the API. Returns count ingested."""
    config = _load_joplin_config(data_dir)
    if not config:
        print("No Joplin config found in credentials.json")
        return 0

    base_url = config.get("url", "")
    email = config.get("email", "")
    password = config.get("password", "")
    note_path = config.get("note_path", "ingest/todo.txt")
    server_host = config.get("server_host", "")

    if not base_url or not email or not password:
        print("Joplin config incomplete (need url, email, password)")
        return 0

    # Authenticate
    try:
        token = fetch_session_token(base_url, email, password, host=server_host)
    except (urllib.error.URLError, OSError, json.JSONDecodeError, KeyError) as exc:
        print(f"Joplin auth failed: {exc}")
        return 0

    # Resolve note path
    try:
        item_name = _resolve_note_item(base_url, token, note_path, data_dir,
                                       host=server_host)
    except (urllib.error.URLError, OSError) as exc:
        print(f"Failed to resolve note path: {exc}")
        return 0
    if not item_name:
        print(f"Note not found: {note_path}")
        return 0

    # Fetch note body
    try:
        text = fetch_note(base_url, token, item_name, host=server_host)
    except (urllib.error.URLError, OSError, json.JSONDecodeError, KeyError) as exc:
        print(f"Failed to fetch note: {exc}")
        return 0

    # Parse tasks
    tasks = parse_tasks(text)
    if not tasks:
        print("No tasks found in note")
        return 0

    # POST each task to the API
    count = 0
    for task in tasks:
        try:
            body = json.dumps(task).encode()
            req = urllib.request.Request(
                f"{api_base}/tasks",
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status == 201:
                    count += 1
        except Exception as exc:
            print(f"Failed to POST task: {exc}")

    # Clear the note
    try:
        clear_note(base_url, token, item_name, host=server_host)
    except (urllib.error.URLError, OSError):
        pass  # non-fatal

    print(f"Ingested {count} task(s) from Joplin")
    return count
