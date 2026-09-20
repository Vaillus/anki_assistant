"""Claude chat backend: prompt assembly, tools, streaming. Spec: specs/chat.md.

Split into four modules:

- **types** — protocols, dataclasses, type aliases, config constants.
- **prompt** — system prompt text, the four-block builder, read-tool output formatters.
- **tools** — tool schemas (proposal, read, web) and dispatch constants.
- **stream** — the async orchestrator (`stream_chat`), roster, citation tracker, web helpers.
"""

from .prompt import (
    STANDING_INSTRUCTIONS as STANDING_INSTRUCTIONS,
)
from .prompt import (
    build_system as build_system,
)
from .prompt import (
    format_decks as format_decks,
)
from .prompt import (
    format_note_type as format_note_type,
)
from .prompt import (
    format_notes as format_notes,
)
from .prompt import (
    format_notes_brief as format_notes_brief,
)
from .prompt import (
    format_source as format_source,
)
from .stream import stream_chat as stream_chat
from .tools import (
    NEW_SOURCE_KINDS as NEW_SOURCE_KINDS,
)
from .tools import (
    READ_TOOLS as READ_TOOLS,
)
from .tools import (
    TARGETED as TARGETED,
)
from .tools import (
    TOOL_KINDS as TOOL_KINDS,
)
from .tools import (
    WEB_RESULTS as WEB_RESULTS,
)
from .tools import (
    WEB_TOOLS as WEB_TOOLS,
)
from .tools import (
    proposal_tools as proposal_tools,
)
from .tools import (
    read_tool_defs as read_tool_defs,
)
from .tools import (
    tools as tools,
)
from .tools import (
    web_tool_defs as web_tool_defs,
)
from .types import (
    BRIEF_FIELD_CHARS as BRIEF_FIELD_CHARS,
)
from .types import (
    CITED_TEXT_CHARS as CITED_TEXT_CHARS,
)
from .types import (
    DEFAULT_MODEL as DEFAULT_MODEL,
)
from .types import (
    MAX_ATTACHED_CHARS as MAX_ATTACHED_CHARS,
)
from .types import (
    MAX_CARDS as MAX_CARDS,
)
from .types import (
    MAX_FULL_RESULT_CHARS as MAX_FULL_RESULT_CHARS,
)
from .types import (
    MAX_TOOL_LOOPS as MAX_TOOL_LOOPS,
)
from .types import (
    MAX_WEB_FETCHES as MAX_WEB_FETCHES,
)
from .types import (
    MAX_WEB_SEARCHES as MAX_WEB_SEARCHES,
)
from .types import (
    Attached as Attached,
)
from .types import (
    ChatEvent as ChatEvent,
)
from .types import (
    CorpusEntry as CorpusEntry,
)
from .types import (
    CorpusLoader as CorpusLoader,
)
from .types import (
    ReadTool as ReadTool,
)
from .types import (
    SourceLoader as SourceLoader,
)
from .types import (
    WorkspaceCard as WorkspaceCard,
)
from .types import (
    default_model as default_model,
)
