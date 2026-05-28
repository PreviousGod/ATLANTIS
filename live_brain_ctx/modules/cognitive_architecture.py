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

FAILURE_DIAGNOSIS_PROTOCOL = """\
FAILURE DIAGNOSIS (mandatory when a tool call failed):
1. <diagnose> State EXACTLY what the error says. Quote it. What does this error ACTUALLY mean?
   - "command not found" → the tool binary is NOT installed. Find an alternative.
   - "INSTALL_FAILED_INVALID_APK: native libraries" → .so files compressed. Store uncompressed.
   - "ModuleNotFoundError: No module named X" → pip install X or use a different approach.
   - "Permission denied" → check file ownership, not a retry.
   - Do NOT retry with the same tool if the error is environmental (missing binary, wrong permissions). </diagnose>
2. <root_cause> What is the FUNDAMENTAL reason this failed? Is it:
   - Missing tool? → find alternative that exists, or install it
   - Wrong approach? → what assumption was wrong?
   - Environmental? → check what IS available before proceeding </root_cause>
3. <alternative> Propose a DIFFERENT approach that works with what IS available.
   - If Python is available but zip isn't → use zipfile module
   - If apksigner isn't installed → use jarsigner or uber-apk-signer
   - If nothing works → TELL THE USER what's missing instead of retrying </alternative>

CRITICAL: After 2 consecutive failures of the same type, you MUST NOT retry.
You MUST explain the root cause and ask the user for direction."""

CONFIDENCE_GATE_MARKER = """\
⚠️ RESEARCH REQUIRED: Less than 2 verified facts available for this query.
You MUST use brain_epistemic(action=search_web) or explicitly state "I don't have verified information on this" rather than answering from training memory."""

# ---------------------------------------------------------------------------
# Complexity classification
# ---------------------------------------------------------------------------

_COMPLEX_SIGNALS = re.compile(
    r'\b(?:'
    # Serbian / Croatian / Bosnian
    r'zašto|zasto|kako|ne radi|ne radi|ne valja|pregledaj|analiziraj|proveri|provjeri|'
    r'popravi|objasni|razliku|odradi|uradi|sredi|napravi|ispravi|greška|greska|bag|'
    # Russian
    r'почему|pochemu|как|kak|не работает|ne rabotaet|не запускается|ne zapuskaetsya|'
    r'ошибка|oshibka|баг|bag|сломалось|slomalos|поломалось|polomalos|'
    r'исправь|isprav|почини|pochini|сделай|sdelai|проверь|prover|анализируй|analizirui|'
    r'объясни|obyasni|разберись|razberis|напиши|napishi|добавь|dobav|'
    r'перепиши|perepishi|оптимизируй|optimizirui|рефакторинг|refaktoring|'
    r'спроектируй|sproektirui|архитектура|arkhitektura|сравни|sravni|'
    r'улучши|uluchshi|обнови|obnovi|мигрируй|migrirui|'
    # Ukrainian
    r'чому|chomu|як|iak|не працює|ne pratsiuie|не запускається|ne zapuskaietsia|'
    r'помилка|pomylka|баг|bag|зламалось|zlamalos|поламалось|polamalos|'
    r'виправ|vyprav|полагодь|polahod|зроби|zroby|перевір|perevir|аналізуй|analizui|'
    r'поясни|poiasny|розберись|rozberys|напиши|napyshy|додай|dodai|'
    r'перепиши|perepyshy|оптимізуй|optymizui|рефакторинг|refaktoryng|'
    r'спроектуй|sproektui|архітектура|arkhitektura|порівняй|porivniai|'
    r'покращ|pokrashch|онови|onovy|мігруй|mihrui|'
    # English
    r'why|how|debug|fix|error|fails?|broke|implement|architect|design|compare|'
    r'analyze|explain why|root cause|difference between|review|examine|inspect|'
    r'investigate|refactor|optimize|upgrade|enhance|migrate|redesign|explain|'
    r'difference|build|create|make|write|add|put'
    r')\b',
    re.IGNORECASE,
)

_TRIVIAL_SIGNALS = re.compile(
    r'^(?:'
    # Serbian
    r'da|ne|ok|hvala|važi|vazi|aha|razumem|nastavi|'
    # Russian
    r'да|da|нет|net|ok|ок|ага|aga|угу|uhu|спасибо|spasibo|'
    r'хорошо|horosho|ладно|ladno|понятно|ponyatno|продолжай|prodolzhai|'
    r'давай|davai|дальше|dalshe|'
    # Ukrainian
    r'так|tak|ні|ni|добре|dobre|гаразд|harazd|дякую|diakuiu|'
    r'зрозуміло|zrozumilo|продовжуй|prodovzhui|далі|dali|'
    # English
    r'yes|no|ok|thanks|got it|continue|skip|next'
    r')\s*[.!?]?$',
    re.IGNORECASE,
)

_REFLECTIVE_SIGNALS = re.compile(
    r'\b(?:'
    # Serbian
    r'misliš|mislis|šta misliš|sta mislis|kako ti se|cini|oceni|proceni|'
    r'reci mi|tvoje mišljenje|tvoje misljenje|šta bi ti|sta bi ti|'
    # Russian
    r'что думаешь|chto dumaesh|как тебе|kak tebe|тво[её] мнение|tvoe mnenie|'
    r'оцени|otseni|проанализируй|proanalizirui|что скажешь|chto skazhesh|'
    r'как считаешь|kak schitaesh|твой вердикт|tvoi verdikt|'
    # Ukrainian
    r'що думаєш|shcho dumaiesh|як тобі|iak tobi|твоя думка|tvoia dumka|'
    r'оціни|otsiny|проаналізуй|proanalizui|що скажеш|shcho skazhesh|'
    r'як вважаєш|iak vvazhaiesh|твій вердикт|tvii verdikt|'
    # English
    r'evaluation|review|assess|thoughts?|opinion|'
    r'what do you think|how do you feel about|rate|grade|your take'
    r')\b',
    re.IGNORECASE,
)

_SIMPLE_FACTUAL_OR_STATUS_SIGNALS = re.compile(
    r'\b(weather|time|date|today|tomorrow|dokle\s+si|gde\s+si|where\s+are\s+you|status\s+check)\b',
    re.IGNORECASE,
)

# P2.9: praise/compliment detection. Messages that are purely complimentary
# should NOT trigger cognitive reasoning — they're chit-chat, not tasks.
# "kako mozes tako opasno" looks complex (contains "kako") but is a compliment.
_PRAISE_SIGNALS = re.compile(
    r'\b(?:'
    # Serbian
    r'ne razumem kako|neverovatno|neverovatno|svaka cast|svaka čast|bravo|'
    r'odlično|odlicno|super|extra|top|genijalan|genijalno|car|caru|kralju|'
    r'precizan|pametan|efikasn|efici|kreativan|opasan si|opasno|jebeno|jeben|'
    r'impresivno|fascinantno|neverovatan|nevjerojatan|predobar|predobro|'
    r'sjajan|sjajno|fantastičan|fantasticno|savrsen|savršen|perfektan|'
    r'najaci|najjači|najjaci| ubijaš|ubijas| razbijaš|razbijas|'
    # Russian
    r'не понимаю как|ne ponimayu kak|невероятно|neveroyatno|потрясающе|potryasayushche|'
    r'отлично|otlichno|супер|super|круто|kruto|гениально|genialno|'
    r'красава|krasava|молодец|molodets|шикарно|shikarno|офигенно|ofigenno|'
    r'точно|tochno|эффективно|effektivno|креативно|kreativno|'
    r'классно|klassno|здорово|zdorovo|пушка|pushka|бомба|bomba|'
    # Ukrainian
    r'не розумію як|ne rozumiiu iak|неймовірно|neimovirno|приголомшливо|pryholomshlyvo|'
    r'відмінно|vidminno|супер|super|круто|kruto|геніально|henialno|'
    r'красавчик|krasavchyk|молодець|molodets|шикарно|shykarno|офігенно|ofigenno|'
    r'точно|tochno|ефективно|efektyvno|креативно|kreatyvno|'
    r'класно|klasno|здорово|zdorovo|гармата|harmata|бомба|bomba|'
    # English
    r'amazing|incredible|impressive|wow|brilliant|genius|perfect|excellent|'
    r'fantastic|outstanding|remarkable|killing it|you.re (?:a|so|insanely|incredibly)'
    r')\b',
    re.IGNORECASE,
)


def classify_complexity(user_message: str, fact_count: int, ruled_out_count: int = 0) -> int:
    """Return complexity tier: 1 (trivial), 2 (medium), 3 (complex)."""
    msg = (user_message or "").strip()
    # P2.9: praise/compliments are never complex — force Tier 1
    if _PRAISE_SIGNALS.search(msg):
        return 1
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
    # Simple factual/status checks should not trigger reasoning solely because no facts are available.
    if (complex_matches == 0 and not has_multi_part and word_count <= 6
            and _SIMPLE_FACTUAL_OR_STATUS_SIGNALS.search(msg)):
        return 1
    # Tier 3: strongly complex, blocked by prior failures, or substantive with no facts
    if complex_matches >= 2 or ruled_out_count > 0:
        return 3
    if (complex_matches >= 1 and fact_count == 0) or (word_count > 25 and fact_count == 0):
        return 3
    # Tier 2: some complexity, substantial length, multi-part, no facts, or moderately long message
    if (complex_matches >= 1 or word_count > 20 or (word_count > 12 and has_multi_part)
            or len(msg) > 40 or fact_count == 0):
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
        # Inject failure diagnosis protocol when there are prior failures
        parts.append(FAILURE_DIAGNOSIS_PROTOCOL)

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
    r'\b(?:'
    # English
    r'flaws?|wrong|incorrect|invalid|breaks?|broken|edge case|missed|simpler|'
    r'assumption|weakness|gap|hole|problems?|issues?|limitation|contradiction|'
    r'errors?|mistakes?|fails?|failures?|risks?|concerns?|critiques?|criticism|'
    r'overlooked|naive|fragile|brittle|incomplete|insufficient|unverified|'
    r'untested|unreliable|bias|skewed|'
    # Russian
    r'недостат|nedostat|ошибк|oshibk|проблем|problem|сломан|sloman|'
    r'не работает|ne rabotaet|неверн|nevern|неправильн|nepraviln|'
    r'не учтено|ne uchteno|упущен|upushchen|противореч|protivorech|'
    r'допущен|dopushchen|слаб|slab|хрупк|khrupk|наивн|naivn|'
    r'неполн|nepoln|недостаточн|nedostatochn|не проверен|ne proveren|'
    r'ненадёж|nenadezh|предвзят|predvzyat|искажён|iskazhen|'
    # Ukrainian
    r'недолік|nedolik|помилк|pomylk|проблем|problem|зламан|zlaman|'
    r'не працює|ne pratsiuie|невірн|nevirn|неправильн|nepravyln|'
    r'не врахован|ne vrakhovan|упущен|upushchen|супереч|superech|'
    r'допущен|dopushchen|слабк|slabk|крихк|krykhk|наївн|naivn|'
    r'неповн|nepovn|недостатн|nedostatn|не перевірен|ne pereviren|'
    r'ненадійн|nenadiin|упереджен|uperedzhen|спотворен|spotvoren'
    r')\b',
    re.IGNORECASE,
)

# Generic praise / empty attack signals — if ONLY these present = invalid
_ATTACK_PRAISE_SIGNALS = re.compile(
    r'\b(?:'
    # English
    r'good|great|excellent|perfect|sound|solid|valid|correct|strong|robust|'
    r'well-done|flawless|no issues|no problems|no flaws|'
    # Russian
    r'хорош|horosh|отличн|otlichn|прекрасн|prekrasn|идеальн|idealn|'
    r'правильн|praviln|верн|vern|надёжн|nadezhn|без ошибок|bez oshibok|'
    r'нет проблем|net problem|всё верно|vsyo verno|всё правильно|vsyo pravilno|'
    # Ukrainian
    r'добр|dobr|гарн|harn|чудов|chudov|відмінн|vidminn|прекрасн|prekrasn|'
    r'ідеальн|idealn|правильн|pravyln|вірн|virn|надійн|nadiin|'
    r'без помилок|bez pomylok|нема проблем|nema problem|все вірно|vse virno'
    r')\b',
    re.IGNORECASE,
)

# Numbered flaws = strong indicator of real attack
_ATTACK_NUMBERED_FLAW = re.compile(
    r'(?:flaw|issue|problem|'
    r'недостаток|nedostatok|проблема|problema|ошибка|oshibka|'
    r'недолік|nedolik|проблема|problema|помилка|pomylka'
    r')\s*#?\s*\d+',
    re.IGNORECASE,
)


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
