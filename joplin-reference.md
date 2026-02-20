# Joplin Server Integration — Code Reference

All code from `src/crm/joplin.py` in the personal-crm repo. Uses only stdlib (`urllib.request`).

---

## 1. Config Loading

Reads Joplin credentials from `data/credentials.json`:

```json
{
  "joplin": {
    "url": "http://localhost:22300",
    "email": "user@example.com",
    "password": "...",
    "note_path": "notebook/note_title",
    "server_host": "optional-host-header"
  }
}
```

```python
def _load_joplin_config(data_dir: Path) -> dict | None:
    creds_path = data_dir / "credentials.json"
    if not creds_path.exists():
        return None
    creds = json.loads(creds_path.read_text())
    return creds.get("joplin")
```

---

## 2. HTTP Helper

All Joplin Server calls go through one function. The `host` param overrides the `Host` header so Joplin Server's origin check passes when connecting to localhost (important on Pi).

```python
def _api_request(url: str, *, method: str = "GET", data: bytes | None = None,
                 headers: dict[str, str] | None = None,
                 host: str = "") -> bytes:
    h = dict(headers) if headers else {}
    if host:
        h["Host"] = host
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.read()
```

---

## 3. Authentication

Cookie-based session. POST email+password, get back a `sessionId` used as a cookie on all subsequent requests.

```python
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
```

---

## 4. Joplin Sync-Item Format

Joplin Server stores notes as text files in its own serialization format. Every note looks like:

```
Title
<blank line>
Body text here...
<blank line>
id: a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4
parent_id: f6e5d4c3b2a1f6e5d4c3b2a1f6e5d4c3
created_time: 2026-01-15T10:00:00.000Z
updated_time: 2026-01-15T10:00:00.000Z
...
type_: 1
```

`type_: 1` = note, `type_: 2` = notebook/folder.

### Parsing

```python
def _parse_joplin_item(content: str) -> dict:
    lines = content.rstrip('\n').split('\n')
    title = lines[0] if lines else ''

    # Find metadata start: the `id: <32-hex>` line
    metadata_start = len(lines)
    for i in range(1, len(lines)):
        if re.match(r'^id: [a-f0-9]{32}\s*$', lines[i]):
            metadata_start = i
            break

    # Extract metadata key-value pairs
    metadata: dict[str, str] = {}
    for i in range(metadata_start, len(lines)):
        line = lines[i]
        if ':' in line:
            key, value = line.split(':', 1)
            metadata[key.strip()] = value.strip()

    # Body sits between title and metadata, trimmed of blank lines
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
```

### Rebuilding (for clearing)

```python
def _rebuild_joplin_item(title: str, body: str, metadata_lines: list[str]) -> str:
    if body:
        return '\n'.join([title, '', body, ''] + metadata_lines) + '\n'
    return '\n'.join([title, '', ''] + metadata_lines) + '\n'
```

---

## 5. Note Path Resolution

Joplin Server stores items by hex ID (e.g. `a1b2c3d4.md`), not by human paths. To find `"ingest/crm.txt"`, you must list all items and match by title and parent notebook.

### List all items (paginated)

```python
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
```

### Find note by notebook name + note title

```python
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

        # type 2 = notebook/folder
        if parsed['type'] == 2 and parsed['title'] == notebook_name:
            folder_id = parsed['id']

        # type 1 = note
        if parsed['type'] == 1 and parsed['title'] == note_title:
            candidate_notes.append((name, parsed))

    if notebook_name and not folder_id:
        return None

    # Match notes that belong to the target notebook
    matches = [(n, p) for n, p in candidate_notes
               if not notebook_name or p['parent_id'] == folder_id]
    # Prefer notes with content over empty ones
    matches.sort(key=lambda x: (x[1]['body'] == '', x[0]))
    for item_name, parsed in matches:
        return item_name

    return None
```

### Resolve a "notebook/note" path string

```python
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
```

---

## 6. Fetch Note Body

```python
def fetch_note(base_url: str, token: str, item_name: str,
               host: str = "") -> str:
    url = f"{base_url}/api/items/root:/{item_name}:/content"
    resp = _api_request(url, headers=_cookie_headers(token), host=host)
    parsed = _parse_joplin_item(resp.decode("utf-8"))
    return parsed['body']
```

---

## 7. Clear Note (After Processing)

Removes the body while preserving title + metadata. Updates timestamps so Joplin desktop picks up the change on next sync.

```python
def clear_note(base_url: str, token: str, item_name: str,
               host: str = "") -> None:
    content_url = f"{base_url}/api/items/root:/{item_name}:/content"
    raw = _api_request(content_url, headers=_cookie_headers(token), host=host)
    text = raw.decode("utf-8")

    # Locate title section end (title\n\n) and metadata start (\n\nid: <hex>)
    title_end = text.index('\n\n') + 2
    meta_match = re.search(r'\n\nid: [a-f0-9]{32}', text)
    if not meta_match:
        raise ValueError("Cannot find metadata boundary in Joplin note")

    # Stitch title + metadata, removing body
    cleared = text[:title_end] + text[meta_match.start():]

    # Update timestamps
    now = datetime.datetime.now(datetime.timezone.utc).strftime(
        '%Y-%m-%dT%H:%M:%S.000Z')
    cleared = re.sub(r'(?<=\nupdated_time: ).*', now, cleared)
    cleared = re.sub(r'(?<=\nuser_updated_time: ).*', now, cleared)

    _api_request(content_url, method="PUT", data=cleared.encode("utf-8"),
                 headers={**_cookie_headers(token),
                          "Content-Type": "application/octet-stream"},
                 host=host)
```

---

## 8. Full Orchestration (Entry Point)

This is the top-level function that ties everything together: authenticate, resolve, fetch, parse, clear, merge into output.

```python
def sync_joplin(data_dir: Path) -> None:
    config = _load_joplin_config(data_dir)
    if not config:
        return

    base_url = config.get("url", "")
    email = config.get("email", "")
    password = config.get("password", "")
    note_path = config.get("note_path", "ingest/crm.txt")
    server_host = config.get("server_host", "")

    if not base_url or not email or not password:
        return

    # --- Authenticate ---
    try:
        token = fetch_session_token(base_url, email, password, host=server_host)
    except (urllib.error.URLError, OSError, json.JSONDecodeError, KeyError):
        return

    # --- Resolve note path to item name ---
    try:
        item_name = _resolve_note_item(base_url, token, note_path, data_dir,
                                       host=server_host)
    except (urllib.error.URLError, OSError):
        return
    if not item_name:
        return

    # --- Fetch note body ---
    try:
        text = fetch_note(base_url, token, item_name, host=server_host)
    except (urllib.error.URLError, OSError, json.JSONDecodeError, KeyError):
        return

    # --- Parse entries ---
    entries = parse_notes(text)
    if not entries:
        return

    # --- Clear note so entries aren't re-ingested ---
    try:
        clear_note(base_url, token, item_name, host=server_host)
    except (urllib.error.URLError, OSError):
        pass  # non-fatal

    # --- Load entries into DB (your new project would POST to FastAPI here) ---
    for entry in entries:
        # In the new project, replace this with:
        # httpx.post(f"{base_url}/items", json=entry, timeout=10)
        pass
```

---

## 9. Note Text Format (Parser)

Each non-blank, non-comment line is semicolon-delimited:

```
name; context
name; context; follow_up_info
name; context; follow_up_info; follow_up_date
```

Example note:
```
Alice Smith; Met at conference, discussed project X
Bob Jones; Email intro; Schedule follow-up call; 2026-02-28
# This is a comment, ignored
```

```python
def parse_notes(text: str) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split(";")]
        if len(parts) < 2:
            continue
        name = parts[0]
        if not name:
            continue
        context = parts[1]
        follow_up_info = parts[2] if len(parts) >= 3 else ""
        follow_up_date = parts[3] if len(parts) >= 4 else ""
        if follow_up_info:
            if follow_up_date:
                notes = f"{context}. Follow up (by {follow_up_date}): {follow_up_info}"
            else:
                notes = f"{context}. Follow up: {follow_up_info}"
        else:
            notes = context
        entries.append({"name": name, "notes": notes})
    return entries
```

---

## Key Takeaways for a New Project

1. **Auth**: `POST /api/sessions` with email+password -> cookie `sessionId=<token>` on all requests
2. **Note resolution is the hard part**: Joplin Server uses hex IDs, not paths. You must list all items, fetch+parse each `.md` to find the right notebook (type=2) and note (type=1) by title
3. **Fetch**: `GET /api/items/root:/{item_name}:/content` -> parse the sync format to extract body
4. **Clear after processing**: Rebuild the item without the body, update timestamps, `PUT` it back as `application/octet-stream`
5. **Host header**: When Joplin Server is on localhost behind a reverse proxy or has origin checks, pass `server_host` as the `Host` header
6. **All stdlib**: Uses `urllib.request` only — no httpx/requests dependency needed for the Joplin side
