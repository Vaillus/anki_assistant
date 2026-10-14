"""Tests for client.py's holding policy and AnkiWeb sync (specs/00-overview.md#anki), on a
temporary collection. No test reaches AnkiWeb: the sync itself is replaced."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest
from anki.collection import Collection
from anki.sync import SyncAuth
from anki.sync_pb2 import SyncCollectionResponse

from anki_assistant.client import (
    FULL_SYNC_MESSAGE,
    AnkiClient,
    CollectionLocked,
    SyncStatus,
    anki_client_from_env,
    full_sync_choice,
)
from tests.anki_fixture import new_collection

REQ = SyncCollectionResponse.ChangesRequired


class SyncSpy(AnkiClient):
    """A client whose AnkiWeb sync is recorded instead of sent; `gate` holds it until set."""

    def __init__(self, path: Path) -> None:
        super().__init__(path, auth=SyncAuth(hkey="key"))
        self.synced: list[bool] = []
        self.gate = threading.Event()
        self.gate.set()
        self.fail = False

    def _sync(self, col: Collection, schema_changed_at_open: bool) -> SyncStatus:
        self.gate.wait(5)
        assert col.db is not None, "the file is still open during the sync"
        self.synced.append(schema_changed_at_open)
        if self.fail:
            raise RuntimeError("no network")
        return SyncStatus(ok=True, message="synced")


@pytest.fixture
def spy(tmp_path: Path) -> SyncSpy:
    return SyncSpy(new_collection(tmp_path / "collection.anki2"))


def wait_idle(client: AnkiClient) -> None:
    """Returns once the client holds the file no more (its background sync is done)."""
    with client.action():
        pass


def test_a_read_action_closes_without_syncing(spy: SyncSpy) -> None:
    spy.deck_names()
    wait_idle(spy)
    assert spy.synced == []


def test_a_write_action_syncs_once_then_closes(spy: SyncSpy) -> None:
    with spy.action():
        spy.create_deck("a")
        spy.create_deck("b")
    wait_idle(spy)
    assert len(spy.synced) == 1, "one sync for the whole action"
    assert spy.path is not None
    Collection(str(spy.path)).close()  # the file is free again: Anki desktop could open it


def test_another_action_waits_for_the_sync(spy: SyncSpy) -> None:
    spy.gate.clear()
    spy.create_deck("a")  # returns at once: the sync runs in the background
    assert spy.synced == []
    done: list[list[str]] = []
    reader = threading.Thread(target=lambda: done.append(spy.deck_names()))
    reader.start()
    reader.join(0.2)
    assert done == [], "waits for the file"
    spy.gate.set()
    reader.join(5)
    assert len(spy.synced) == 1
    assert done == [["a"]]


def test_a_failed_sync_is_recorded_and_releases_the_file(spy: SyncSpy) -> None:
    spy.fail = True
    spy.create_deck("a")
    wait_idle(spy)
    assert spy.last_sync is not None and not spy.last_sync.ok
    assert "no network" in spy.last_sync.message
    assert spy.deck_names() == ["a"], "the write stays"


def test_a_failure_right_after_open_releases_the_file(
    spy: SyncSpy, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(self: Collection) -> bool:
        raise RuntimeError("schema unreadable")

    monkeypatch.setattr(Collection, "schema_changed", broken)
    with pytest.raises(RuntimeError):
        spy.deck_names()
    monkeypatch.undo()
    later = threading.Thread(target=spy.deck_names, daemon=True)
    later.start()
    later.join(2)
    assert not later.is_alive(), "the next action got the file"


def test_no_login_no_sync(tmp_path: Path) -> None:
    client = AnkiClient(new_collection(tmp_path / "collection.anki2"))
    client.create_deck("a")
    assert client.last_sync is None


def test_the_lock_refuses_while_another_process_holds_the_file(tmp_path: Path) -> None:
    path = new_collection(tmp_path / "collection.anki2")
    desktop = Collection(str(path))
    try:
        with pytest.raises(CollectionLocked, match="Close Anki desktop first"):
            AnkiClient(path).deck_names()
    finally:
        desktop.close()


def test_a_missing_collection_is_never_created(tmp_path: Path) -> None:
    with pytest.raises(Exception, match="No Anki collection"):
        AnkiClient(tmp_path / "none.anki2").deck_names()
    assert not (tmp_path / "none.anki2").exists()


@pytest.mark.parametrize(
    ("required", "at_open", "now", "choice"),
    [
        (REQ.NO_CHANGES, False, False, "done"),
        (REQ.NORMAL_SYNC, True, True, "done"),
        (REQ.FULL_UPLOAD, True, True, "upload"),
        (REQ.FULL_SYNC, False, True, "upload"),  # the app's own write changed the schema
        (REQ.FULL_SYNC, True, True, "leave"),  # Anki desktop changed it and did not sync
        (REQ.FULL_SYNC, False, False, "leave"),  # another device forced a full sync
        (REQ.FULL_DOWNLOAD, False, True, "leave"),
    ],
)
def test_full_sync_choice(required: int, at_open: bool, now: bool, choice: str) -> None:
    assert full_sync_choice(required, at_open, now) == choice


def test_full_sync_message() -> None:
    assert "open Anki desktop" in FULL_SYNC_MESSAGE


def test_client_from_env(tmp_path: Path) -> None:
    path = str(tmp_path / "c.anki2")
    plain = anki_client_from_env({"ANKI_COLLECTION": path})
    assert plain.path == Path(path) and plain.auth is None
    synced = anki_client_from_env({"ANKI_COLLECTION": path, "ANKIWEB_HKEY": "k"})
    assert synced.auth is not None and synced.auth.hkey == "k"
