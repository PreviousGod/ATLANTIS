"""live_brain_ctx — Context Engine for Live Brain.

Thin facade that registers the LiveBrainContextEngine and four hooks
(pre_llm_call, pre_tool_call, post_tool_call, post_llm_call) with the
Hermes plugin system. All implementation lives in ``modules/``.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from agent.context_compressor import ContextCompressor

# ---------------------------------------------------------------------------
# Public dataclasses (used by modules/hooks.py and modules/data_sources.py)
# ---------------------------------------------------------------------------

@dataclass
class QueryContext:
    """Query metadata extracted from user message."""
    scope_key: str
    query_lower: str
    intent: str
    query_words: List[str]
    active_tags: Dict[str, List[str]]
    continuation_query: bool
    now: float
    ttl_cutoff: float
    session_id: str = ""


@dataclass
class DataSources:
    """All data sources fetched from database."""
    binding_rules: List[sqlite3.Row]
    work_item_row: Optional[sqlite3.Row]
    continuity_work_rows: List[sqlite3.Row]
    episode_rows: List[sqlite3.Row]
    fact_rows: List[sqlite3.Row]
    belief_rows: List[sqlite3.Row]
    recipe_rows: List[sqlite3.Row]
    causal_rows: List[sqlite3.Row]
    knowledge_rows: List[sqlite3.Row]
    artifact_lines: List[str]
    recap_row: Optional[sqlite3.Row]
    pending_approval_rows: List[sqlite3.Row]
    should_surface_approval: bool
    approval_surface_reason: str


# ---------------------------------------------------------------------------
# Context engine class
# ---------------------------------------------------------------------------

class LiveBrainContextEngine(ContextCompressor):
    """Live Brain context engine registered with Hermes.

    Context injection happens via the ``pre_llm_call`` hook, not via
    ``compress()``. The base ContextCompressor handles token-budget
    compression of the message history unchanged.
    """

    @property
    def name(self) -> str:
        return "live_brain_ctx"


# ---------------------------------------------------------------------------
# Hook imports (deferred to avoid circular imports at module load time)
# ---------------------------------------------------------------------------

from .modules.hooks import (  # noqa: E402
    _pre_llm_call,
    _pre_tool_call,
    _post_tool_call,
    _post_llm_call,
    _load_live_brain_context,
    _prepare_query_context,
    _debug_live_brain_context,
    _perform_db_maintenance,
    _record_tool_result,
)

# Re-export state constants that existing tests reference directly on the
# module (e.g. live_brain_ctx._CONSTRAINT_TTL_DAYS).
from .modules.state import (  # noqa: E402
    CONSTRAINT_TTL_DAYS as _CONSTRAINT_TTL_DAYS,
    MAX_ACTIVE_EPISODES as _MAX_ACTIVE_EPISODES,
    CHIT_CHAT_PATTERNS as _CHIT_CHAT_PATTERNS,
    LOW_SIGNAL_WORDS as _LOW_SIGNAL_WORDS,
    SECTION_LIMITS as _SECTION_LIMITS,
)

# Re-export scoring/filter functions that tests and the monolith's callers
# reference directly on the module.
from .modules.query_filters import (  # noqa: E402
    _is_chit_chat,
    _is_review_only_query,
    _is_low_signal_episode,
    _is_noisy_memory,
    _meaningful_query_words,
    _expand_query_words,
    _is_continuation_query,
    _is_destructive_memory_text,
    _current_turn_allows_destructive_memory,
    _is_non_action_work_item_text,
    _is_local_stack_query,
    _is_question_like_memory,
)

from .modules.scoring import (  # noqa: E402
    _overlap_score,
    _has_overlap,
    _domain_conflicts,
    _marker_conflicts,
    _marker_tokens,
    _row_text,
    _row_noisy,
    _row_updated_at,
    _same_user_message,
    _causal_score,
    _visible_fact_matches,
    _specificity_score,
)

from .modules.tag_matching import (  # noqa: E402
    _active_tags,
    _row_tags,
    _matches,
    _causal_matches,
)

from .modules.approval import (  # noqa: E402
    _fetch_pending_approval_rows,
    _unsurfaced_pending_approval_rows,
    _mark_pending_approvals_surfaced,
    _approval_relevant_to_user_message,
    _should_surface_pending_approvals,
    _suppressed_approval_reminder_lines,
    _approval_context_lines,
)

from .modules.tool_context import (  # noqa: E402
    _artifact_required,
    _tool_relevant,
    _recipe_hint,
)

from .modules.formatting import (  # noqa: E402
    _append_section,
    _format_episodes,
    _format_fix_recipes,
    _format_binding_constraints,
    allowed_sections_for_intent,
    section_budget_for_intent,
)

from .modules.query_classification import (  # noqa: E402
    _classify_query_intent,
)

from .modules.integrations import (  # noqa: E402
    _load_reality_engine_class,
    _record_reality_event,
    _load_reality_brief,
    _load_epistemic_manager_class,
    _load_epistemic_brief,
    _load_epistemic_autonomous_context,
    _record_epistemic_answer_if_source_backed,
    _should_load_reality_brief,
    _should_isolate_epistemic_context,
    _epistemic_query_text,
)

from .modules.hooks import (  # noqa: E402
    _extract_scope_key,
    _hermes_home,
    _db_path,
    _get_connection,
    _get_active_session_evidence,
    _context_sections,
    _record_context_impression,
)

# Scopes imports (used by tag_matching and scoring)
try:
    from live_brain.scopes_config import (
        ARTIFACT_REQUIRED_TOOL_TOKENS,
        IMAGE_GENERATION_ALIASES,
        is_image_generation_tool,
    )
except Exception:
    ARTIFACT_REQUIRED_TOOL_TOKENS = ('image_generate', 'ffmpeg', 'tts', 'google_tts')
    IMAGE_GENERATION_ALIASES = ('seedream', 'bytedance-seed', 'image_generate')
    is_image_generation_tool = lambda tool_name: 'image_generate' in (tool_name or '').lower()

# Regex used by test_scoring.py
from .modules.state import (  # noqa: E402
    CONTINUATION_QUERY_RE as _CONTINUATION_QUERY_RE,
    OPEN_LOOP_QUERY_RE as _OPEN_LOOP_QUERY_RE,
    OPEN_LOOP_FACT_RE as _OPEN_LOOP_FACT_RE,
    PATH_CONFIG_RE as _PATH_CONFIG_RE,
    PATH_CONFIG_QUERY_RE as _PATH_CONFIG_QUERY_RE,
    MUSIC_DOMAIN_RE as _MUSIC_DOMAIN_RE,
    MEDIA_PROJECT_MEMORY_RE as _MEDIA_PROJECT_MEMORY_RE,
    MEDIA_PROJECT_QUERY_RE as _MEDIA_PROJECT_QUERY_RE,
    VOICE_TTS_DOMAIN_RE as _VOICE_TTS_DOMAIN_RE,
    RAW_TOOL_FACT_RE as _RAW_TOOL_FACT_RE,
    RAW_TOOL_QUERY_RE as _RAW_TOOL_QUERY_RE,
    REVIEW_ONLY_TERMS as _REVIEW_ONLY_TERMS,
    CHANGE_INTENT_TERMS as _CHANGE_INTENT_TERMS,
    META_WORK_ITEM_RE as _META_WORK_ITEM_RE,
    MUSIC_MEMORY_ALIASES as _MUSIC_MEMORY_ALIASES,
    SECRET_RE as _SECRET_RE,
)


# ---------------------------------------------------------------------------
# Plugin registration
# ---------------------------------------------------------------------------

def register(ctx) -> None:
    """Hermes plugin entry point."""
    ctx.register_context_engine(
        LiveBrainContextEngine(model="", quiet_mode=True, config_context_length=200000),
    )
    ctx.register_hook("pre_llm_call", _pre_llm_call)
    ctx.register_hook("pre_tool_call", _pre_tool_call)
    ctx.register_hook("post_tool_call", _post_tool_call)
    ctx.register_hook("post_llm_call", _post_llm_call)
    # P2.5 — memory block compression. Wraps core MemoryStore so the
    # frozen system-prompt snapshot collapses near-duplicate entries.
    from .modules.memory_compress import install as _install_memory_compress
    _install_memory_compress()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__ = [
    "register",
    "LiveBrainContextEngine",
    "QueryContext",
    "DataSources",
]
