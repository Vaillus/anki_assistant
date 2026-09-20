"""Vault filesystem helpers: note listing, PDF listing, Zotero URI lookup."""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from . import store as _store
from .store import Vault

log = logging.getLogger(__name__)


def vault_notes(vault: Vault, q: str = "", limit: int = 50) -> list[str]:
    """Note names (relative to the vault root, ".md" stripped) containing `q`, case-insensitive.

    Recursive, sorted, hidden directories (`.obsidian`, `.trash`, …) skipped.
    """
    root = vault.path
    if not root.exists():
        return []
    q_lower = q.lower()
    names = []
    for path in root.rglob("*.md"):
        rel = path.relative_to(root)
        if any(part.startswith(".") for part in rel.parts):
            continue
        name = rel.with_suffix("").as_posix()
        if q_lower in name.lower():
            names.append(name)
    names.sort(key=str.lower)
    return names[:limit]


def vault_pdfs(vault: Vault, q: str = "", limit: int = 50) -> list[str]:
    """PDF paths under the vault's Zotero folder whose filename contains `q`, case-insensitive.

    Paths under the user's home are returned with a `~/` prefix (what the user would type as a
    PDF source target); others as absolute paths. Recursive, sorted, hidden directories skipped.
    """
    pdf_root = vault.path / "Zotero"
    if not pdf_root.exists():
        return []
    home = Path.home()
    q_lower = q.lower()
    results: list[str] = []
    for path in pdf_root.rglob("*.pdf"):
        rel = path.relative_to(pdf_root)
        if any(part.startswith(".") for part in rel.parts):
            continue
        if q_lower in rel.name.lower():
            try:
                results.append("~/" + str(path.relative_to(home)))
            except ValueError:
                results.append(str(path))
    results.sort(key=str.lower)
    return results[:limit]


def zotero_open_uri(pdf_path: Path, vault: Vault) -> str | None:
    """Return a ``zotero://open-pdf/…`` URI for *pdf_path*, or *None* if the lookup fails.

    Queries the Zotero SQLite database for a linked attachment whose relative path (under the
    vault's ``Zotero/`` folder) matches the file.  The match is case-insensitive because macOS
    default filesystems (APFS) are case-insensitive and Zotero may store a different case than
    the actual filename.
    """
    if not _store.ZOTERO_DB.exists():
        return None
    zotero_root = vault.path / "Zotero"
    resolved = pdf_path.expanduser().resolve()
    try:
        relative = resolved.relative_to(zotero_root.resolve())
    except ValueError:
        return None
    db_path = f"attachments:{relative}"
    try:
        con = sqlite3.connect(f"file:{_store.ZOTERO_DB}?mode=ro&immutable=1", uri=True)
        try:
            row = con.execute(
                "SELECT i.key FROM itemAttachments ia"
                " JOIN items i ON i.itemID = ia.itemID"
                " WHERE ia.contentType = 'application/pdf'"
                "   AND ia.path = ? COLLATE NOCASE",
                (db_path,),
            ).fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        log.warning("failed to query Zotero database", exc_info=True)
        return None
    if row is None:
        return None
    return f"zotero://open-pdf/library/items/{row[0]}"
