"""Live Brain Context Modules

Shared helpers consumed by the live_brain_ctx plugin. Currently the monolithic
``live_brain_ctx/__init__.py`` imports from:
  - ``query_classification`` (query type detection)
  - ``text_processing`` (text filtering + redaction)
  - ``state`` (shared constants, TTLs, regex patterns)

Previously this package contained 10 additional modules with dependency-injected
duplicates of helpers that also live in the monolith. Those stayed out of sync
with the real implementation and have been removed. The remaining modules are
the ones that are actually imported from the monolith. Full migration of the
monolith into a thin facade + per-concern modules is tracked as a follow-up.
"""

# Query classification functions actually consumed by the monolith.
from .query_classification import (
    _is_recap_query,
    _is_diagnostic_query,
    _is_approval_query,
    _is_local_stack_query,
    _is_chit_chat,
    is_low_signal_thread_title,
)

# Text processing functions actually consumed by the monolith.
from .text_processing import (
    _truncate_fact,
    _redact,
    _expand_query_words,
    _meaningful_query_words,
    _is_low_signal_episode,
    _is_noisy_memory,
    is_noisy_episode_memory,
)

# Shared state / constants consumed by modules and re-exported for any future
# per-concern module that needs them.
from . import state  # noqa: F401

__all__ = [
    # query_classification
    '_is_recap_query',
    '_is_diagnostic_query',
    '_is_approval_query',
    '_is_local_stack_query',
    '_is_chit_chat',
    'is_low_signal_thread_title',
    # text_processing
    '_truncate_fact',
    '_redact',
    '_expand_query_words',
    '_meaningful_query_words',
    '_is_low_signal_episode',
    '_is_noisy_memory',
    'is_noisy_episode_memory',
    # state module
    'state',
]
