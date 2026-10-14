"""Tests for the CLI's AnkiWeb sync (specs/00-overview.md#ankiweb-sync), on a temporary
collection. No test reaches AnkiWeb: the sync itself is replaced."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest
from anki.collection import Collection

from anki_assistant import cli
from anki_assistant.client import AnkiClient, SyncStatus
from tests.anki_fixture import TempAnki


@pytest.fixture
def card_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> int:
    """A card in a temporary collection, with an AnkiWeb key set and no real .env read."""
    anki = TempAnki(tmp_path)
    anki.add("Deck", fields={"Text": "{{c1::a}}"})
    monkeypatch.setattr(cli, "load_dotenv", lambda: None)
    monkeypatch.setenv("ANKIWEB_HKEY", "key")
    monkeypatch.setattr(cli, "SYNC_WAIT_SECONDS", 0.5)
    return anki.find_cards("")[0].card_id


def run(tmp_path: Path, *argv: str) -> int:
    return cli.main(["--collection", str(tmp_path / "collection.anki2"), *argv])


def fake_sync(
    monkeypatch: pytest.MonkeyPatch, status: SyncStatus, gate: threading.Event | None = None
) -> list[bool]:
    synced: list[bool] = []

    def _sync(self: AnkiClient, col: Collection, schema_changed_at_open: bool) -> SyncStatus:
        if gate is not None:
            gate.wait(5)
        synced.append(True)
        return status

    monkeypatch.setattr(AnkiClient, "_sync", _sync)
    return synced


def test_a_write_waits_for_its_sync(
    tmp_path: Path, card_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    synced = fake_sync(monkeypatch, SyncStatus(ok=True, message="synced"))
    assert run(tmp_path, "flag", "1", str(card_id)) == 0
    assert synced == [True], "--collection keeps the AnkiWeb key, and main waits for the sync"


def test_a_read_does_not_sync(
    tmp_path: Path, card_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    synced = fake_sync(monkeypatch, SyncStatus(ok=True, message="synced"))
    assert run(tmp_path, "card", str(card_id)) == 0
    assert synced == []


def test_a_failed_sync_is_reported(
    tmp_path: Path, card_id: int, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    fake_sync(monkeypatch, SyncStatus(ok=False, message="AnkiWeb sync failed: no network"))
    assert run(tmp_path, "flag", "1", str(card_id)) == 0
    assert "AnkiWeb sync failed: no network" in capsys.readouterr().err


def test_a_slow_sync_is_left_for_the_next_one(
    tmp_path: Path, card_id: int, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    gate = threading.Event()
    synced = fake_sync(monkeypatch, SyncStatus(ok=True, message="synced"), gate)
    assert run(tmp_path, "flag", "1", str(card_id)) == 0
    assert "still running" in capsys.readouterr().err
    assert synced == []
    gate.set()  # let the background thread end and close the file
