"""
Context Fencing - Feature 4
Prevents self-referential memory pollution and provides scope isolation.
"""
import re
import logging

logger = logging.getLogger(__name__)

# Patterns that indicate self-referential memory operations
SELF_REFERENTIAL_PATTERNS = [
    r'\b(?:brain_mark_|brain_recall|brain_state_debug|brain_reality_debug|brain_entity_graph|brain_synthesize|brain_user_profile|brain_compose_query)\b',
    r'\b(?:saved|stored|recorded|updated|marked)\s+(?:to|in)\s+(?:memory|live\s*brain|database)\b',
    r'\b(?:memory|live\s*brain)\s+(?:operation|update|write|sync)\b',
    r'\bI\s+(?:saved|stored|recorded|marked|updated)\s+(?:this|that|the)\s+(?:fact|belief|memory)\b',
    r'\b(?:extracting|extracted)\s+(?:fact|belief|entity|preference)\b',
    r'\b(?:auto[- ]?extracted|automatic\s+extraction)\b',
]

SELF_REFERENTIAL_RE = re.compile('|'.join(SELF_REFERENTIAL_PATTERNS), re.IGNORECASE)


def is_self_referential(text: str) -> bool:
    """
    Check if text describes a memory operation.

    Args:
        text: Text to check

    Returns:
        True if text is self-referential
    """
    if not text:
        return False
    return bool(SELF_REFERENTIAL_RE.search(text))


def should_fence(
    text: str,
    source_kind: str = '',
    extraction_method: str = 'manual'
) -> bool:
    """
    Determine if memory should be fenced (blocked).

    Rules:
    1. Block self-referential operations
    2. Block if source is 'tool_result' and mentions brain tools
    3. Allow manual extractions from user

    Args:
        text: Memory text to check
        source_kind: Source of the memory (e.g., 'tool_result', 'user', 'assistant')
        extraction_method: How memory was extracted ('manual' or 'auto')

    Returns:
        True if memory should be blocked
    """
    if not text:
        return True

    # Block self-referential operations
    if is_self_referential(text):
        logger.debug("[context_fence] blocking self-referential: %s", text[:100])
        return True

    # Block tool results that mention brain tools
    if source_kind == 'tool_result' and 'brain_' in text.lower():
        logger.debug("[context_fence] blocking tool result with brain mention: %s", text[:100])
        return True

    return False


def filter_memories(memories: list, scope_key: str) -> list:
    """
    Filter memories for context injection.

    Multi-container isolation: only return memories matching scope_key.
    Also filters out self-referential memories.

    Args:
        memories: List of memory dicts with 'scope_key' and 'text' fields
        scope_key: Current scope key to filter by

    Returns:
        Filtered list of memories
    """
    filtered = []
    for m in memories:
        # Scope isolation
        if m.get('scope_key') and m.get('scope_key') != scope_key:
            continue

        # Self-referential check
        text = m.get('text') or m.get('fact_text') or m.get('claim_text') or m.get('title') or ''
        if is_self_referential(text):
            continue

        filtered.append(m)

    return filtered
