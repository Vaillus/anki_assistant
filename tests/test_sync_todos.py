"""Tests for scripts/sync_todos.py, the todo.md <-> TickTick sync."""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from sync_todos import Entry, TickTick, push, read_todo, render  # noqa: E402


class FakeTickTick(TickTick):
    """A TickTick client that records calls instead of making them."""

    def __init__(self) -> None:  # noqa: D107 -- deliberately skips credential loading
        self.created: list[str] = []
        self.renamed: list[tuple[str, str]] = []
        self.completed: list[str] = []
        self._next_id = 0

    def create(self, project_id: str, title: str) -> str:
        self.created.append(title)
        self._next_id += 1
        return f"new{self._next_id}"

    def rename(self, project_id: str, task_id: str, title: str) -> None:
        self.renamed.append((task_id, title))

    def complete(self, project_id: str, task_id: str) -> None:
        self.completed.append(task_id)


def _remote(*pairs: tuple[str, str]) -> dict[str, dict]:
    return {task_id: {"id": task_id, "title": title} for task_id, title in pairs}


# --------------------------------------------------------------------- parsing


def test_parses_open_and_done_sections(tmp_path: Path) -> None:
    path = tmp_path / "todo.md"
    path.write_text(
        "# Todos\n\n## Open\n\n"
        "- [ ] with an id <!--tt:abc123-->\n"
        "- [ ] brand new line\n"
        "- [x] checked here <!--tt:def456-->\n"
        "\n## Done\n\n"
        "- [x] finished earlier <!--tt:ghi789 done:2026-09-01-->\n",
        encoding="utf-8",
    )
    open_entries, done_entries = read_todo(path)

    assert [(e.title, e.checked, e.task_id) for e in open_entries] == [
        ("with an id", False, "abc123"),
        ("brand new line", False, None),
        ("checked here", True, "def456"),
    ]
    assert [(e.title, e.task_id, e.done_on) for e in done_entries] == [
        ("finished earlier", "ghi789", "2026-09-01")
    ]


def test_missing_file_reads_as_empty(tmp_path: Path) -> None:
    assert read_todo(tmp_path / "absent.md") == ([], [])


def test_render_round_trips(tmp_path: Path) -> None:
    open_entries = [Entry(title="one", checked=False, task_id="a1")]
    done_entries = [Entry(title="two", checked=True, task_id="b2", done_on="2026-09-02")]
    path = tmp_path / "todo.md"
    path.write_text(render(open_entries, done_entries), encoding="utf-8")

    reparsed_open, reparsed_done = read_todo(path)
    assert [(e.title, e.task_id) for e in reparsed_open] == [("one", "a1")]
    assert [(e.title, e.task_id, e.done_on) for e in reparsed_done] == [("two", "b2", "2026-09-02")]


def test_placeholder_lines_are_not_entries(tmp_path: Path) -> None:
    path = tmp_path / "todo.md"
    path.write_text(render([], []), encoding="utf-8")
    assert read_todo(path) == ([], [])


# ------------------------------------------------------------------------ push


def test_unchanged_lines_produce_no_calls() -> None:
    client = FakeTickTick()
    local = [Entry(title="same", checked=False, task_id="a1")]
    actions, completed = push(client, "p", local, _remote(("a1", "same")), dry_run=False)

    assert actions == []
    assert completed == {}
    assert (client.created, client.renamed, client.completed) == ([], [], [])


def test_idless_line_is_created() -> None:
    client = FakeTickTick()
    local = [Entry(title="fresh", checked=False, task_id=None)]
    _, completed = push(client, "p", local, _remote(), dry_run=False)

    assert client.created == ["fresh"]
    assert local[0].task_id == "new1"
    assert completed == {}


def test_checked_line_is_completed_and_dated() -> None:
    client = FakeTickTick()
    local = [Entry(title="done now", checked=True, task_id="a1")]
    _, completed = push(client, "p", local, _remote(("a1", "done now")), dry_run=False)

    assert client.completed == ["a1"]
    assert set(completed) == {"a1"}
    assert completed["a1"].done_on is not None


def test_checked_idless_line_is_created_then_completed() -> None:
    client = FakeTickTick()
    local = [Entry(title="did it already", checked=True, task_id=None)]
    _, completed = push(client, "p", local, _remote(), dry_run=False)

    assert client.created == ["did it already"]
    assert client.completed == ["new1"]
    assert set(completed) == {"new1"}


def test_edited_text_renames_the_task() -> None:
    client = FakeTickTick()
    local = [Entry(title="new wording", checked=False, task_id="a1")]
    push(client, "p", local, _remote(("a1", "old wording")), dry_run=False)

    assert client.renamed == [("a1", "new wording")]


def test_task_gone_from_ticktick_is_left_alone() -> None:
    client = FakeTickTick()
    local = [Entry(title="stale", checked=True, task_id="gone")]
    actions, completed = push(client, "p", local, _remote(), dry_run=False)

    assert actions == []
    assert completed == {}
    assert client.completed == []


def test_dry_run_reports_without_calling() -> None:
    client = FakeTickTick()
    local = [
        Entry(title="fresh", checked=False, task_id=None),
        Entry(title="new wording", checked=False, task_id="a1"),
        Entry(title="finish me", checked=True, task_id="a2"),
    ]
    remote = _remote(("a1", "old wording"), ("a2", "finish me"))
    actions, _ = push(client, "p", local, remote, dry_run=True)

    assert len(actions) == 3
    assert (client.created, client.renamed, client.completed) == ([], [], [])
