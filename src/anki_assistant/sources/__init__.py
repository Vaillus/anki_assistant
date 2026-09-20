"""A deck's corpus: the documents it was made from. Spec: specs/sources.md.

This package is split into three modules:

- ``store`` — data model (``Source``, ``Vault``, ``SourceText``), persistence
  (``SourceStore``), kind detection, page parsing.
- ``web`` — web page fetching and HTML-to-text extraction.
- ``vault`` — vault filesystem helpers (note listing, PDF listing, Zotero URI).

Every public name is re-exported here so that ``from anki_assistant.sources import X``
keeps working unchanged.
"""

from .store import (
    DEFAULT_MAX_CHARS,
    DEFAULT_VAULT,
    KINDS,
    ZOTERO_DB,
    Kind,
    Source,
    SourceStore,
    SourceText,
    Vault,
    _page_numbers,
    default_store_path,
    detect_kind,
    is_url,
    is_valid_pages,
    new_source_id,
)
from .vault import vault_notes, vault_pdfs, zotero_open_uri
from .web import _WEB_TEXT_CACHE, html_to_text

__all__ = [
    "DEFAULT_MAX_CHARS",
    "DEFAULT_VAULT",
    "KINDS",
    "Kind",
    "Source",
    "SourceStore",
    "SourceText",
    "Vault",
    "ZOTERO_DB",
    "_WEB_TEXT_CACHE",
    "_page_numbers",
    "default_store_path",
    "detect_kind",
    "html_to_text",
    "is_url",
    "is_valid_pages",
    "new_source_id",
    "vault_notes",
    "vault_pdfs",
    "zotero_open_uri",
]
