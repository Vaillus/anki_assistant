"""Sync `todo.md` with a TickTick project.

The local file is a plain Markdown checklist. Each line already known to TickTick
carries its task id in a trailing HTML comment, which is how the two sides are
matched:

    - [ ] review the specs <!--tt:6a9d847a3302cf9468487e2d-->

Running the script does, in order:

1. **Push** local edits. A checked box completes the task; a line with no id
   creates one; a line whose text no longer matches renames the task.
2. **Pull** the project back down, rewriting `todo.md` from what TickTick holds
   and appending anything just completed to the `## Done` section.

Deleting a line locally does not delete the task -- the next pull brings it
back. Remove it in TickTick instead. When a title changed on both sides, the
local text wins.

Credentials come from the `tt` CLI's config (`~/.config/tt/.env`), so there is
no second set of secrets to manage. Run `tt auth` if the token has expired.

Usage:
    uv run python scripts/sync_todos.py [--dry-run] [--pull-only]
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values, set_key

PROJECT_NAME = "anki assistant"
TODO_FILE = Path(__file__).resolve().parent.parent / "todo.md"

API_BASE = "https://api.ticktick.com/open/v1"
TOKEN_URL = "https://ticktick.com/oauth/token"
CONFIG_DIR = Path(os.environ.get("TT_CONFIG_DIR", Path.home() / ".config" / "tt"))
ENV_FILE = CONFIG_DIR / ".env"

HEADER = f"""# Todos -- {PROJECT_NAME}

<!-- Mirror of the TickTick project "{PROJECT_NAME}". Do not edit the tt: ids. -->
<!-- Sync both ways with: uv run python scripts/sync_todos.py -->
"""

# - [ ] some title <!--tt:abc123 done:2026-09-10-->   (id and date both optional)
LINE_RE = re.compile(
    r"^- \[(?P<box>[ xX])\]\s+(?P<title>.*?)"
    r"(?:\s*<!--tt:(?P<id>[A-Za-z0-9]+)(?:\s+done:(?P<done>\d{4}-\d{2}-\d{2}))?-->)?"
    r"\s*$"
)


class SyncError(Exception):
    pass


@dataclass
class Entry:
    """One `- [ ]` line of `todo.md`."""

    title: str
    checked: bool
    task_id: str | None = None
    done_on: str | None = None


# --------------------------------------------------------------------------- api


class TickTick:
    """Minimal TickTick Open API client over the `tt` CLI's stored credentials."""

    def __init__(self) -> None:
        config = self._load_config()
        self._access_token = config.get("TICKTICK_ACCESS_TOKEN") or ""
        self._refresh_token = config.get("TICKTICK_REFRESH_TOKEN") or ""
        self._client_id = config.get("TICKTICK_CLIENT_ID") or ""
        self._client_secret = config.get("TICKTICK_CLIENT_SECRET") or ""
        if not self._access_token:
            raise SyncError(f"No access token in {ENV_FILE}. Run `tt auth`.")
        self._refreshed = False

    @staticmethod
    def _load_config() -> dict[str, str]:
        if not ENV_FILE.exists():
            raise SyncError(f"Config not found: {ENV_FILE}. Run `tt auth` first.")
        return {k: v for k, v in dotenv_values(ENV_FILE).items() if v is not None}

    def _request(self, method: str, endpoint: str, body: dict | None = None) -> object:
        payload = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(
            f"{API_BASE}{endpoint}",
            data=payload,
            method=method,
            headers={
                "Authorization": f"Bearer {self._access_token}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request) as response:
                raw = response.read()
        except urllib.error.HTTPError as error:
            if error.code == 401 and not self._refreshed:
                self._refresh()
                return self._request(method, endpoint, body)
            detail = error.read().decode(errors="replace")[:200]
            raise SyncError(f"{method} {endpoint} failed: HTTP {error.code} {detail}") from error
        return json.loads(raw) if raw else None

    def _refresh(self) -> None:
        if not self._refresh_token:
            raise SyncError(
                "Access token rejected and no refresh token is stored. Run `tt auth` "
                "to re-authorise, then run this script again."
            )
        credentials = base64.b64encode(f"{self._client_id}:{self._client_secret}".encode()).decode()
        data = urllib.parse.urlencode(
            {
                "grant_type": "refresh_token",
                "refresh_token": self._refresh_token,
                "scope": "tasks:read tasks:write",
            }
        ).encode()
        request = urllib.request.Request(
            TOKEN_URL,
            data=data,
            method="POST",
            headers={
                "Authorization": f"Basic {credentials}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        try:
            with urllib.request.urlopen(request) as response:
                token = json.loads(response.read())
        except urllib.error.HTTPError as error:
            raise SyncError(f"Token refresh failed (HTTP {error.code}). Run `tt auth`.") from error
        self._access_token = token["access_token"]
        self._refresh_token = token.get("refresh_token", self._refresh_token)
        set_key(str(ENV_FILE), "TICKTICK_ACCESS_TOKEN", self._access_token)
        set_key(str(ENV_FILE), "TICKTICK_REFRESH_TOKEN", self._refresh_token)
        self._refreshed = True

    def project_id(self, name: str) -> str:
        projects = self._request("GET", "/project")
        if not isinstance(projects, list):
            raise SyncError("Unexpected response listing projects.")
        for project in projects:
            if str(project.get("name", "")).casefold() == name.casefold():
                return str(project["id"])
        known = ", ".join(sorted(str(p.get("name", "?")) for p in projects))
        raise SyncError(f"No TickTick project named {name!r}. Found: {known}")

    def open_tasks(self, project_id: str) -> list[dict]:
        """Undone tasks, in the order TickTick returns them."""
        data = self._request("GET", f"/project/{project_id}/data")
        if not isinstance(data, dict):
            raise SyncError("Unexpected response fetching project tasks.")
        return [t for t in (data.get("tasks") or []) if t.get("status", 0) == 0]

    def create(self, project_id: str, title: str) -> str:
        created = self._request("POST", "/task", {"title": title, "projectId": project_id})
        if not isinstance(created, dict):
            raise SyncError(f"Unexpected response creating task {title!r}.")
        return str(created["id"])

    def rename(self, project_id: str, task_id: str, title: str) -> None:
        body = {"id": task_id, "projectId": project_id, "title": title}
        self._request("POST", f"/task/{task_id}", body)

    def complete(self, project_id: str, task_id: str) -> None:
        self._request("POST", f"/project/{project_id}/task/{task_id}/complete")


# -------------------------------------------------------------------------- file


def read_todo(path: Path) -> tuple[list[Entry], list[Entry]]:
    """Return the (open, done) entries of `todo.md`, both empty if it is absent."""
    if not path.exists():
        return [], []
    open_entries: list[Entry] = []
    done_entries: list[Entry] = []
    in_done = False
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            in_done = stripped.lstrip("# ").casefold().startswith("done")
            continue
        match = LINE_RE.match(line)
        if not match:
            continue
        title = (match["title"] or "").strip()
        if not title:
            continue
        entry = Entry(
            title=title,
            checked=match["box"].lower() == "x",
            task_id=match["id"],
            done_on=match["done"],
        )
        (done_entries if in_done else open_entries).append(entry)
    return open_entries, done_entries


def render(open_entries: list[Entry], done_entries: list[Entry]) -> str:
    lines = [HEADER, "## Open", ""]
    if open_entries:
        lines += [f"- [ ] {e.title} <!--tt:{e.task_id}-->" for e in open_entries]
    else:
        lines.append("_Nothing open._")
    lines += ["", "## Done", ""]
    if done_entries:
        for entry in done_entries:
            suffix = f" done:{entry.done_on}" if entry.done_on else ""
            lines.append(f"- [x] {entry.title} <!--tt:{entry.task_id}{suffix}-->")
    else:
        lines.append("_Nothing yet._")
    return "\n".join(lines) + "\n"


# -------------------------------------------------------------------------- sync


def push(
    client: TickTick,
    project_id: str,
    local_open: list[Entry],
    remote: dict[str, dict],
    *,
    dry_run: bool,
) -> tuple[list[str], dict[str, Entry]]:
    """Apply local edits to TickTick. Returns (log lines, entries completed now)."""
    actions: list[str] = []
    completed: dict[str, Entry] = {}
    today = dt.date.today().isoformat()

    for entry in local_open:
        if entry.task_id and entry.task_id not in remote:
            continue  # closed or deleted in TickTick already; the pull settles it

        if entry.task_id is None:
            verb = "create+done" if entry.checked else "create"
            actions.append(f"{verb}: {entry.title}")
            entry.task_id = "dry-run" if dry_run else client.create(project_id, entry.title)
        elif entry.checked:
            actions.append(f"complete:   {entry.title}")
        elif remote[entry.task_id]["title"] != entry.title:
            old = remote[entry.task_id]["title"]
            actions.append(f"rename:     {old} -> {entry.title}")
            if not dry_run:
                client.rename(project_id, entry.task_id, entry.title)

        if entry.checked:
            if not dry_run:
                client.complete(project_id, entry.task_id)
            entry.done_on = today
            completed[entry.task_id] = entry

    return actions, completed


def sync(*, dry_run: bool, pull_only: bool) -> int:
    client = TickTick()
    project_id = client.project_id(PROJECT_NAME)
    local_open, local_done = read_todo(TODO_FILE)
    remote = {task["id"]: task for task in client.open_tasks(project_id)}

    actions: list[str] = []
    completed: dict[str, Entry] = {}
    if not pull_only:
        actions, completed = push(client, project_id, local_open, remote, dry_run=dry_run)

    # Re-fetch only when writes landed, so the file picks up new ids and closures.
    if actions and not dry_run:
        remote = {task["id"]: task for task in client.open_tasks(project_id)}
    pulled = [Entry(title=t["title"], checked=False, task_id=t["id"]) for t in remote.values()]

    # A task reopened in TickTick leaves the Done section; anything just completed joins it.
    local_done = [e for e in local_done if e.task_id not in remote]
    already_done = {e.task_id for e in local_done}
    local_done += [e for task_id, e in completed.items() if task_id not in already_done]

    content = render(pulled, local_done)
    prefix = "[dry-run] " if dry_run else ""
    for action in actions:
        print(f"{prefix}{action}")

    if dry_run:
        print(f"{prefix}would write {len(pulled)} open / {len(local_done)} done to {TODO_FILE}")
        return 0
    if TODO_FILE.exists() and TODO_FILE.read_text(encoding="utf-8") == content:
        print(f"{TODO_FILE.name} already up to date ({len(pulled)} open).")
        return 0
    TODO_FILE.write_text(content, encoding="utf-8")
    print(f"Wrote {TODO_FILE.name}: {len(pulled)} open, {len(local_done)} done.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=f"Sync todo.md with TickTick '{PROJECT_NAME}'.")
    parser.add_argument("--dry-run", action="store_true", help="report changes, write nothing")
    parser.add_argument("--pull-only", action="store_true", help="never write to TickTick")
    args = parser.parse_args()
    try:
        return sync(dry_run=args.dry_run, pull_only=args.pull_only)
    except SyncError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
