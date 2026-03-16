"""Generate recurring tasks from a JSON config file."""
from __future__ import annotations

import json
import urllib.request
from datetime import date, timedelta
from pathlib import Path


def generate_recurring_tasks(data_dir: Path, api_base: str) -> None:
    config_path = data_dir / "recurring.json"
    if not config_path.exists():
        return

    definitions = json.loads(config_path.read_text())
    if not definitions:
        return

    # Fetch existing tasks for dedup
    resp = urllib.request.urlopen(f"{api_base}/tasks", timeout=10)
    existing_tasks = json.loads(resp.read())
    existing_set = {(t["headline"], t["date"]) for t in existing_tasks}

    today = date.today()
    horizon = today + timedelta(days=60)
    created = 0

    for defn in definitions:
        headline = defn["headline"]
        context = defn.get("context", "")
        start = date.fromisoformat(defn["start_date"])
        every = defn["every_days"]

        # Generate all occurrence dates from start_date up to horizon
        current = start
        while current <= horizon:
            if current > today and (headline, current.isoformat()) not in existing_set:
                body = json.dumps({
                    "date": current.isoformat(),
                    "headline": headline,
                    "context": context,
                }).encode()
                req = urllib.request.Request(
                    f"{api_base}/tasks",
                    data=body,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                urllib.request.urlopen(req, timeout=10)
                created += 1
            current += timedelta(days=every)

    if created:
        print(f"Recurring: created {created} task(s)")
