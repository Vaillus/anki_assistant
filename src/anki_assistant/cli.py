"""Command-line interface: anki decks | flagged <deck> | card <id> | edit <note_id> F=V."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import textwrap
from collections.abc import Iterable

from anki_assistant.client import AnkiClient, AnkiConnectError
from anki_assistant.models import Card, Note
from anki_assistant.sources import SourceStore


def _print_card(card: Card, width: int = 100) -> None:
    header = f"[{card.card_id}] {card.deck_name}  ·  {card.model_name}  ·  flag={card.flag_name}"
    if card.is_suspended:
        header += "  ·  SUSPENDED"
    print(header)
    for name, value in card.plain_fields().items():
        wrapped = textwrap.fill(
            value or "∅", width=width, initial_indent="    ", subsequent_indent="    "
        )
        print(f"  {name}:")
        print(wrapped)
    print()


def _print_note(note: Note) -> None:
    print(f"[note {note.note_id}] {note.model_name}  ·  tags: {' '.join(note.tags) or '∅'}")
    for name, value in note.plain_fields().items():
        print(f"  {name}: {value or '∅'}")
    print()


def _print_cards(cards: Iterable[Card], as_json: bool) -> None:
    cards = list(cards)
    if as_json:
        print(json.dumps([c.__dict__ for c in cards], ensure_ascii=False, indent=2))
        return
    if not cards:
        print("(no cards)")
        return
    for card in cards:
        _print_card(card)
    print(f"{len(cards)} card(s)")


def cmd_decks(client: AnkiClient, args: argparse.Namespace) -> None:
    names = client.deck_names()
    if args.json:
        print(json.dumps(names, ensure_ascii=False, indent=2))
        return
    for name in names:
        print(name)


def _print_deck_source(deck: str | None, as_json: bool) -> None:
    if not deck or as_json:
        return
    store = SourceStore()
    corpus = store.corpus(deck)
    if not corpus:
        print(f"source: (none) — anki source set {deck!r} <pdf|note>\n")
        return
    for source in corpus:
        inherited = "" if source.deck == deck else f"  (inherited from {source.deck})"
        print(f"source: {source.describe(store.vault)}{inherited}")
        print(f"        {source.uri(store.vault)}")
    print()


def cmd_cards(client: AnkiClient, args: argparse.Namespace) -> None:
    _print_deck_source(args.deck, args.json)
    cards = client.cards_in_deck(args.deck, args.query or "")
    if args.limit:
        cards = cards[: args.limit]
    _print_cards(cards, args.json)


def cmd_flagged(client: AnkiClient, args: argparse.Namespace) -> None:
    _print_deck_source(args.deck, args.json)
    cards = client.flagged_cards(deck=args.deck, flag=args.flag)
    _print_cards(cards, args.json)


def cmd_search(client: AnkiClient, args: argparse.Namespace) -> None:
    cards = client.find_cards(args.query)
    if args.limit:
        cards = cards[: args.limit]
    _print_cards(cards, args.json)


def cmd_card(client: AnkiClient, args: argparse.Namespace) -> None:
    card = client.card(args.card_id)
    if args.json:
        print(json.dumps(card.__dict__, ensure_ascii=False, indent=2))
    else:
        _print_card(card)


def cmd_note(client: AnkiClient, args: argparse.Namespace) -> None:
    note = client.note(args.note_id)
    if args.json:
        print(json.dumps(note.__dict__, ensure_ascii=False, indent=2))
    else:
        _print_note(note)


def _parse_assignments(items: list[str]) -> dict[str, str]:
    fields: dict[str, str] = {}
    for item in items:
        if "=" not in item:
            raise SystemExit(f"Expected FIELD=VALUE, got {item!r}")
        name, value = item.split("=", 1)
        fields[name] = value
    return fields


def cmd_edit(client: AnkiClient, args: argparse.Namespace) -> None:
    fields = _parse_assignments(args.assignments)
    client.update_note_fields(args.note_id, fields)
    _print_note(client.note(args.note_id))


def cmd_flag(client: AnkiClient, args: argparse.Namespace) -> None:
    client.set_flag(args.card_ids, args.flag)
    _print_cards(client.cards_info(args.card_ids), as_json=False)


def cmd_tag(client: AnkiClient, args: argparse.Namespace) -> None:
    if args.remove:
        client.remove_tags(args.note_ids, " ".join(args.tags))
    else:
        client.add_tags(args.note_ids, " ".join(args.tags))
    for note in client.notes_info(args.note_ids):
        _print_note(note)


def cmd_source(client: AnkiClient, args: argparse.Namespace) -> None:
    store = SourceStore()
    action = args.source_action

    if action == "list":
        if args.json:
            print(
                json.dumps(
                    {
                        d: [s.to_dict() for s in sources]
                        for d, sources in sorted(store.corpora.items())
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return
        if not store.corpora:
            print(f"(no source mapped yet — file: {store.path})")
            return
        for deck, sources in sorted(store.corpora.items()):
            print(deck)
            for source in sources:
                print(f"    {source.describe(store.vault)}")
        print(f"\n{len(store.corpora)} deck(s) with a corpus in {store.path}")

    elif action == "get":
        corpus = store.corpus(args.deck)
        if not corpus:
            print("(no source)")
            return
        for source in corpus:
            inherited = "" if source.deck == args.deck else f"  (inherited from {source.deck})"
            print(f"{source.describe(store.vault)}{inherited}")
            print(source.uri(store.vault))

    elif action == "set":
        source = store.set(
            args.deck, args.target, kind=args.kind, pages=args.pages or "", note=args.note or ""
        )
        print(f"{args.deck} -> {source.describe(store.vault)}")

    elif action == "unset":
        print("removed" if store.unset(args.deck) else "(nothing mapped on this deck exactly)")

    elif action == "open":
        source = store.get(args.deck)
        if source is None:
            raise SystemExit(f"No source mapped for {args.deck!r}")
        subprocess.run(["open", source.uri(store.vault)], check=True)

    elif action == "missing":
        unmapped = store.unmapped(client.deck_names())
        for deck in unmapped:
            print(deck)
        broken = store.broken()
        if broken:
            print("\nmapped but target not found on disk:")
            for source in broken:
                print(f"  {source.deck} -> {source.target}")
        print(f"\n{len(unmapped)} deck(s) without a source")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="anki", description="Talk to Anki through AnkiConnect.")
    p.add_argument("--url", default="http://localhost:8765", help="AnkiConnect URL")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("decks", help="list deck names")
    sp.set_defaults(func=cmd_decks)

    sp = sub.add_parser("cards", help="list cards of a deck")
    sp.add_argument("deck")
    sp.add_argument("-q", "--query", help="extra Anki search terms, e.g. 'is:due'")
    sp.add_argument("-n", "--limit", type=int)
    sp.set_defaults(func=cmd_cards)

    sp = sub.add_parser("flagged", help="list flagged cards (optionally within one deck)")
    sp.add_argument("deck", nargs="?")
    sp.add_argument("--flag", type=int, choices=range(1, 8), help="only this flag colour (1-7)")
    sp.set_defaults(func=cmd_flagged)

    sp = sub.add_parser("search", help="raw Anki search query")
    sp.add_argument("query")
    sp.add_argument("-n", "--limit", type=int)
    sp.set_defaults(func=cmd_search)

    sp = sub.add_parser("card", help="show one card")
    sp.add_argument("card_id", type=int)
    sp.set_defaults(func=cmd_card)

    sp = sub.add_parser("note", help="show one note")
    sp.add_argument("note_id", type=int)
    sp.set_defaults(func=cmd_note)

    sp = sub.add_parser("edit", help="set note fields: anki edit NOTE_ID Front='...' Back='...'")
    sp.add_argument("note_id", type=int)
    sp.add_argument("assignments", nargs="+", metavar="FIELD=VALUE")
    sp.set_defaults(func=cmd_edit)

    sp = sub.add_parser("flag", help="set flag on cards (0 clears)")
    sp.add_argument("flag", type=int, choices=range(0, 8))
    sp.add_argument("card_ids", type=int, nargs="+")
    sp.set_defaults(func=cmd_flag)

    sp = sub.add_parser("source", help="associate decks with a PDF or an Obsidian note")
    ssub = sp.add_subparsers(dest="source_action", required=True)
    sp.set_defaults(func=cmd_source)

    q = ssub.add_parser("list", help="show every mapping")
    q.set_defaults(func=cmd_source)
    q = ssub.add_parser("get", help="effective source of a deck (parents included)")
    q.add_argument("deck")
    q.set_defaults(func=cmd_source)
    q = ssub.add_parser("set", help="map a deck to a .pdf path, an Obsidian note name or a URL")
    q.add_argument("deck")
    q.add_argument("target")
    q.add_argument("--kind", choices=["pdf", "obsidian", "web"], help="override auto-detection")
    q.add_argument("--pages", help="pdf only: '12-19', '7', '3-5,9' (1-based, inclusive)")
    q.add_argument("--note", help="free-text comment stored with the mapping")
    q.set_defaults(func=cmd_source)
    q = ssub.add_parser("unset", help="remove the mapping written on this exact deck")
    q.add_argument("deck")
    q.set_defaults(func=cmd_source)
    q = ssub.add_parser("open", help="open the source in Preview or Obsidian")
    q.add_argument("deck")
    q.set_defaults(func=cmd_source)
    q = ssub.add_parser("missing", help="decks with no source, and broken targets")
    q.set_defaults(func=cmd_source)

    sp = sub.add_parser("tag", help="add (or --remove) tags on notes")
    sp.add_argument("note_ids", type=int, nargs="+")
    sp.add_argument("-t", "--tags", nargs="+", required=True)
    sp.add_argument("--remove", action="store_true")
    sp.set_defaults(func=cmd_tag)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    client = AnkiClient(url=args.url)
    try:
        args.func(client, args)
    except AnkiConnectError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except BrokenPipeError:  # output piped into `head` & co
        sys.stderr.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
