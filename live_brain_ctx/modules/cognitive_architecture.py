"""ATLANTIS Cognitive Architecture — Recursive Adversarial Reasoning.

Injects structured reasoning instructions into the LLM context based on
query complexity (tiered activation). Manages ruled_out state across turns
for constraint propagation.

Integration points:
  - _pre_llm_call: call get_cognitive_context() to get prompt injection
  - _post_tool_call: call record_ruled_out() when tool fails
"""
from __future__ import annotations

import re
import sqlite3
import time
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Cognitive Prompts (tiered)
# ---------------------------------------------------------------------------

COGNITIVE_PROMPT_TIER2 = """\
REASONING PROTOCOL (follow for this response):
1. <decompose> Break the problem into atomic sub-problems. </decompose>
2. <verify> For each sub-problem, check: do I have a verified fact from KNOWN FACTS above? Mark [VERIFIED] or [UNVERIFIED]. </verify>
3. <answer> Synthesize ONLY from verified facts. For unverified parts, use brain_epistemic or state uncertainty explicitly. </answer>"""

COGNITIVE_PROMPT_TIER3 = """\
REASONING PROTOCOL (mandatory for this response):
1. <decompose> Break into atomic sub-problems from MULTIPLE perspectives:
   - Structural: what are the components?
   - Causal: what causes what?
   - Temporal: what sequence matters?
   - Analogical: where have I seen a similar pattern in a DIFFERENT domain? </decompose>
2. <verify> For EACH sub-problem: do I have a verified fact? Mark [VERIFIED] or [UNVERIFIED].
   If more than 1 sub-problem is [UNVERIFIED] → you MUST research before answering. </verify>
3. <synthesize> Combine ONLY verified facts + research results into a candidate answer. </synthesize>
4. <attack> Act as an adversarial critic. Try to DESTROY your own answer:
   - What assumption could be wrong?
   - What edge case breaks it?
   - Is there a simpler explanation I missed?
   - MANDATORY: List at least one concrete flaw OR explicitly state "No critical flaws found — answer is sound."
   - DO NOT skip this step. DO NOT write generic praise. Be brutal. </attack>
5. <final> If your answer survives the attack → deliver it.
   If not → state what failed, mark it as ruled_out, and try a different approach that avoids the flaw. </final>

VERIFICATION: After <attack>, your response MUST contain the exact string:
ATTACK_COMPLETED: followed by at least one sentence summarising the attack result.
If this string is missing, the system will reject the response and prompt you to retry."""

CONFIDENCE_GATE_MARKER = """\
⚠️ RESEARCH REQUIRED: Less than 2 verified facts available for this query.
You MUST use brain_epistemic(action=search_web) or explicitly state "I don't have verified information on this" rather than answering from training memory."""

# ---------------------------------------------------------------------------
# Complexity classification
# ---------------------------------------------------------------------------

_COMPLEX_SIGNALS = re.compile(
    r'\b(zašto|why|kako|how|debug|fix|error|ne radi|doesn.t work|fails?|broke|implement|architect|design|compare|analyze|explain why|root cause|difference between|почему|як|чому|как|не работает|не працює|ошибка|помилка|pregledaj|analiziraj|proveri|popravi|review|examine|inspect|investigate|refactor|optimize|upgrade|enhance|migrate|redesign|objasni|explain|razliku|difference|odradi|uradi|sredi|napravi|build|create|make|write|add|put)\b',
    re.IGNORECASE,
)

_TRIVIAL_SIGNALS = re.compile(
    r'^(da|ne|ok|hvala|thanks|yes|no|got it|važi|aha|razumem|nastavi|continue|skip|next|да|нет|ні|так|добре|дякую|спасибо|продолжай|далі)\s*[.!?]?$',
    re.IGNORECASE,
)

_REFLECTIVE_SIGNALS = re.compile(
    r'\b(misliš|mislis|šta misliš|šta mislis|kako ti se|cini|oceni|proceni|reci mi|tvoje mišljenje|tvoje misljenje|evaluation|review|assess|thoughts?|opinion|what do you think|how do you feel about|rate|grade|your take)\b',
    re.IGNORECASE,
)


def classify_complexity(user_message: str, fact_count: int, ruled_out_count: int = 0) -> int:
    """Return complexity tier: 1 (trivial), 2 (medium), 3 (complex)."""
    msg = (user_message or "").strip()
    if not msg or _TRIVIAL_SIGNALS.match(msg) or len(msg) < 15:
        return 1
    words = msg.split()
    word_count = len(words)
    complex_matches = len(_COMPLEX_SIGNALS.findall(msg))
    reflective_matches = len(_REFLECTIVE_SIGNALS.findall(msg))
    has_multi_part = bool(re.search(r'[,;—•]|\d+\.|\b(i\s+|and\s+|or\s+|ili\s+|ili)\b', msg, re.I))
    # Purely reflective/evaluative queries cap at Tier 2 (verification needed, not full decomposition)
    if reflective_matches >= 1 and complex_matches < 2:
        return 2
    # Tier 3: strongly complex, blocked by prior failures, or substantive with no facts
    if complex_matches >= 2 or ruled_out_count > 0:
        return 3
    if (complex_matches >= 1 and fact_count == 0) or (word_count > 25 and fact_count == 0):
        return 3
    # Tier 2: some complexity, substantial length, multi-part, or moderately long message
    if (complex_matches >= 1 or word_count > 20 or (word_count > 12 and has_multi_part)
            or len(msg) > 40):
        return 2
    return 1


# ---------------------------------------------------------------------------
# Fact counting (accurate, not newline proxy)
# ---------------------------------------------------------------------------

_FACT_SECTIONS = ('KNOWN FACTS', 'VERIFIED ARTIFACTS', 'PROVEN FIX', 'OPEN BUG',
                  'NEXT REQUIRED ACTION', 'MUST FOLLOW', 'ACTIVE TASK')


def _count_facts_in_context(context: str) -> int:
    """Count actual bullet items in fact-bearing sections."""
    if not context:
        return 0
    count = 0
    in_section = False
    for line in context.splitlines():
        stripped = line.strip()
        if any(stripped.startswith(s) for s in _FACT_SECTIONS):
            in_section = True
            continue
        if stripped and stripped[0].isupper() and stripped.endswith(':') and not stripped.startswith('- '):
            in_section = False
            continue
        if in_section and stripped.startswith('- '):
            count += 1
    return count


# ---------------------------------------------------------------------------
# Session-tier cache (for post-LLM attack verification)
# ---------------------------------------------------------------------------

_tier_cache_lock = threading.Lock()
_session_tier_cache: Dict[str, Tuple[int, float]] = {}
_TIER_CACHE_TTL = 300  # 5 minutes


def get_last_tier(session_id: str) -> int:
    """Return the tier used in the last pre_llm_call for this session, or 0 if unknown/expired."""
    if not session_id:
        return 0
    with _tier_cache_lock:
        tier, ts = _session_tier_cache.get(session_id, (0, 0))
    if time.time() - ts > _TIER_CACHE_TTL:
        return 0
    return tier


def _set_last_tier(session_id: str, tier: int) -> None:
    """Record tier for attack verification in post_llm_call."""
    if not session_id:
        return
    with _tier_cache_lock:
        _session_tier_cache[session_id] = (tier, time.time())


# ---------------------------------------------------------------------------
# Ruled-out state (in-memory, persisted to SQLite)
# ---------------------------------------------------------------------------

_ruled_out_lock = threading.Lock()
_ruled_out_state: Dict[str, List[Dict[str, Any]]] = {}
_ruled_out_table_ensured = False
MAX_RULED_OUT = 5


# ---------------------------------------------------------------------------
# Ruled-out helpers
# ---------------------------------------------------------------------------

def record_ruled_out(session_id: str, approach: str, reason: str, db_conn: Optional[sqlite3.Connection] = None, category: str = "reasoning") -> None:
    """Record a failed approach for constraint propagation.

    Categories:
      reasoning   — failed reasoning/logic (injected into cognitive context)
      development — code/tool execution failures (NOT injected)
      attack      — skipped or weak attack step (injected)
    """
    global _ruled_out_table_ensured
    if not session_id or not approach:
        return
    entry = {"approach": approach[:200], "reason": reason[:200], "ts": time.time(), "category": category}
    with _ruled_out_lock:
        lst = _ruled_out_state.setdefault(session_id, [])
        lst.append(entry)
        if len(lst) > MAX_RULED_OUT:
            lst[:] = lst[-MAX_RULED_OUT:]
    # SQLite persistence (if connection provided)
    if db_conn:
        try:
            if not _ruled_out_table_ensured:
                ensure_ruled_out_table(db_conn)
                _ruled_out_table_ensured = True
            _persist_ruled_out(db_conn, session_id, entry)
        except Exception:
            pass


def get_ruled_out(session_id: str, db_conn: Optional[sqlite3.Connection] = None) -> List[Dict[str, Any]]:
    """Get ruled_out list for session. Falls back to SQLite if memory is empty."""
    with _ruled_out_lock:
        entries = _ruled_out_state.get(session_id, [])
    if not entries and db_conn:
        try:
            entries = _load_ruled_out(db_conn, session_id)
            if entries:
                with _ruled_out_lock:
                    _ruled_out_state[session_id] = entries
        except Exception:
            pass
    return entries


# ---------------------------------------------------------------------------
# Cross-domain hints (SQLite query, 0 LLM calls)
# ---------------------------------------------------------------------------

def _cross_domain_hints(db_conn: sqlite3.Connection, query_words: List[str], scope_key: str) -> List[str]:
    """Find facts from OTHER domains that share structural patterns with query."""
    if not query_words or not db_conn:
        return []
    try:
        # Get facts NOT in current scope but matching query words
        fts_query = ' OR '.join(query_words[:4])
        rows = db_conn.execute(
            """SELECT fact_text FROM facts
               WHERE status='active' AND confidence >= 0.8
               AND scope_key != ? AND fact_text IS NOT NULL
               AND rowid IN (SELECT rowid FROM facts_fts WHERE facts_fts MATCH ?)
               ORDER BY confidence DESC LIMIT 3""",
            (scope_key, fts_query),
        ).fetchall()
        return [f"[cross-domain] {row[0][:150]}" for row in rows if row[0]]
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Main entry point — called from _pre_llm_call
# ---------------------------------------------------------------------------

def get_cognitive_context(
    user_message: str,
    session_id: str,
    fact_count: int,
    scope_key: str = "",
    query_words: Optional[List[str]] = None,
    db_conn: Optional[sqlite3.Connection] = None,
) -> str:
    """Build cognitive architecture context injection.

    Returns empty string for tier 1 (no overhead).
    """
    ruled_out = get_ruled_out(session_id, db_conn)
    tier = classify_complexity(user_message, fact_count, len(ruled_out))

    if tier == 1:
        _set_last_tier(session_id, 1)
        return ""

    parts: List[str] = []

    # Cognitive prompt based on tier
    if tier == 3:
        parts.append(COGNITIVE_PROMPT_TIER3)
    else:
        parts.append(COGNITIVE_PROMPT_TIER2)

    # Confidence gate
    if fact_count < 2:
        parts.append(CONFIDENCE_GATE_MARKER)

    # Ruled-out constraints (inter-turn propagation) — skip development noise
    user_facing_ruled_out = [e for e in ruled_out if e.get("category") != "development"]
    if user_facing_ruled_out:
        constraint_lines = ["RULED OUT (do NOT repeat these approaches):"]
        for entry in user_facing_ruled_out:
            constraint_lines.append(f"  ✗ {entry['approach']} — because: {entry['reason']}")
        constraint_lines.append("Any new approach MUST NOT depend on the same assumptions that caused the above failures.")
        parts.append("\n".join(constraint_lines))

    # Cross-domain hints (Tier 2+ gets analogies, not just Tier 3)
    if tier >= 2 and db_conn and query_words:
        hints = _cross_domain_hints(db_conn, query_words, scope_key)
        if hints:
            parts.append("CROSS-DOMAIN ANALOGIES (from other knowledge areas):\n" + "\n".join(hints))

    _set_last_tier(session_id, tier)
    return "COGNITIVE FRAMEWORK\n" + "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Attack content parser (rule-based quality check)
# ---------------------------------------------------------------------------

_ATTACK_BLOCK_RE = re.compile(r'<attack>(.*?)</attack>', re.DOTALL | re.IGNORECASE)

# Concrete criticism signals — presence of ANY = likely valid attack
_ATTACK_CRITICISM_SIGNALS = re.compile(
    r'\b(flaws?|wrong|incorrect|invalid|breaks?|broken|edge case|missed|simpler|assumption|weakness|gap|hole|problems?|issues?|limitation|contradiction|errors?|mistakes?|fails?|failures?|risks?|concerns?|critiques?|criticism|overlooked|naive|fragile|brittle|incomplete|insufficient|unverified|untested|unreliable|bias|skewed)\b',
    re.IGNORECASE,
)

# Generic praise / empty attack signals — if ONLY these present = invalid
_ATTACK_PRAISE_SIGNALS = re.compile(
    r'\b(good|great|excellent|perfect|sound|solid|valid|correct|strong|robust|well-done|flawless|no issues|no problems|no flaws)\b',
    re.IGNORECASE,
)

# Numbered flaws = strong indicator of real attack
_ATTACK_NUMBERED_FLAW = re.compile(r'(?:flaw|issue|problem)\s*#?\s*\d+', re.IGNORECASE)


def check_attack_quality(response: str) -> Tuple[bool, str]:
    """Parse <attack> block and verify it contains genuine criticism.

    Returns (is_valid, reason).
    Rule-based: 0 LLM calls.
    """
    if not response:
        return False, "Empty response"

    match = _ATTACK_BLOCK_RE.search(response)
    if not match:
        return False, "Missing <attack> block"

    attack_text = match.group(1)
    if len(attack_text.strip()) < 20:
        return False, "Attack block too short (< 20 chars)"

    criticism_hits = len(_ATTACK_CRITICISM_SIGNALS.findall(attack_text))
    praise_hits = len(_ATTACK_PRAISE_SIGNALS.findall(attack_text))
    numbered_flaws = bool(_ATTACK_NUMBERED_FLAW.search(attack_text))

    # Strong signal: numbered flaws or multiple criticism words
    if numbered_flaws or criticism_hits >= 2:
        return True, f"Valid attack: {criticism_hits} criticism signals, numbered_flaws={numbered_flaws}"

    # Weak signal: some criticism but also praise — ambiguous
    if criticism_hits >= 1 and praise_hits == 0:
        return True, f"Valid attack: {criticism_hits} criticism signals, no praise"

    # Empty / praise-only attack
    if criticism_hits == 0 and praise_hits >= 1:
        return False, f"Praise-only attack: {praise_hits} praise signals, 0 criticism"

    return False, f"No concrete criticism found (crit={criticism_hits}, praise={praise_hits})"


# ---------------------------------------------------------------------------
# SQLite persistence helpers
# ---------------------------------------------------------------------------

def ensure_ruled_out_table(db_conn: sqlite3.Connection) -> None:
    """Create ruled_out_approaches table if not exists."""
    db_conn.execute("""
        CREATE TABLE IF NOT EXISTS ruled_out_approaches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            scope_key TEXT DEFAULT '',
            approach_text TEXT NOT NULL,
            reason TEXT NOT NULL,
            category TEXT DEFAULT 'reasoning',
            created_at REAL NOT NULL
        )
    """)
    db_conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_ruled_out_session
        ON ruled_out_approaches(session_id, created_at DESC)
    """)
    # Migration: add category column if table exists from older schema
    try:
        db_conn.execute("ALTER TABLE ruled_out_approaches ADD COLUMN category TEXT DEFAULT 'reasoning'")
    except Exception:
        pass
    db_conn.commit()


def _persist_ruled_out(db_conn: sqlite3.Connection, session_id: str, entry: Dict[str, Any]) -> None:
    """Write a ruled_out entry to SQLite."""
    db_conn.execute(
        "INSERT INTO ruled_out_approaches (session_id, approach_text, reason, category, created_at) VALUES (?, ?, ?, ?, ?)",
        (session_id, entry["approach"], entry["reason"], entry.get("category", "reasoning"), entry["ts"]),
    )
    # FIFO: keep max 20 per session
    db_conn.execute(
        """DELETE FROM ruled_out_approaches WHERE id IN (
            SELECT id FROM ruled_out_approaches WHERE session_id=?
            ORDER BY created_at DESC LIMIT -1 OFFSET 20
        )""",
        (session_id,),
    )
    db_conn.commit()


def _load_ruled_out(db_conn: sqlite3.Connection, session_id: str) -> List[Dict[str, Any]]:
    """Load ruled_out entries from SQLite."""
    rows = db_conn.execute(
        "SELECT approach_text, reason, category, created_at FROM ruled_out_approaches WHERE session_id=? ORDER BY created_at DESC LIMIT ?",
        (session_id, MAX_RULED_OUT),
    ).fetchall()
    return [{"approach": r[0], "reason": r[1], "category": r[2] or "reasoning", "ts": r[3]} for r in reversed(rows)]
