from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from dataclasses import dataclass
from typing import Any, Dict, List

from .scopes import extract_scope_tags, tags_to_json
from .utils import is_noisy_episode_memory, is_low_signal_thread_title
from .audit import record_revision, row_to_dict
from .scopes_config import (
    ARTIFACT_REQUIRED_TOOL_TOKENS,
    IMAGE_GENERATION_ALIASES,
    RECIPE_TOOL_TOKENS,
    TOOL_SIGNAL_TERMS,
    tool_domain,
)


FILE_RE = re.compile(r'(?:^|\s)(/[\w./-]+)')
QUESTION_RE = re.compile(r'\?$')
USER_PREF_RE = [
    re.compile(r'\b(?:i|ja)\s+(?:prefer|like|want|need|volim|hocu|zelim)\b', re.IGNORECASE),
    re.compile(r'\b(?:uvek|always|never|nikad)\b', re.IGNORECASE),
]
# Intent classification patterns - determines how to handle user input
INTENT_PATTERNS = {
    'correction': re.compile(
        r'\b(zaboravio|zaboravila|vec (smo|si)|već (smo|si)|'
        r'rekao.*(si|nam)|rekla.*(si|nam)|(opet|ponovo) (samo |)(isti|isto|samo)|'
        r'(za)?sto (ponavljas|praviš|radis)|pogresno|nije (to|tacno)|'
        r'you (forgot|did this again|already did|same mistake)|'
        r'i told you|we already|we fixed)\b',
        re.IGNORECASE
    ),
    'binding': re.compile(
        r'\b(uvijek|uvek|nikad|nikada|obavezno|ne smijes|ne smes|'
        r'must not|do not|never|always|every time|'
        r'(ne |)(diraj|brisi|mjenjaj|mijenjaj|uklanjaj|dodaj)|'
        r'zapamti|remember this|never forget)\b',
        re.IGNORECASE
    ),
    'preference': re.compile(
        r'\b(i |ja )(preferiram|volim|hocu|zelim|like|want|need|prefer)\b',
        re.IGNORECASE
    ),
    'question': re.compile(r'\?$'),
    'command': re.compile(
        r'\b(napravi|uradi|make|do|create|fix|resolve|build|'
        r'generate|make|solve|execute|run|start|stop|delete|remove)\b',
        re.IGNORECASE
    ),
    'diagnostic': re.compile(
        r'\b(error|problem|bug|fails?|ne radi|greska|issue|'
        r'crash|broken|nicht|not working)\b',
        re.IGNORECASE
    ),
}

# Words removed when compressing a user request into a reusable recipe key.
# Keep generic task verbs here; keep domain words (image, video, api, tts) out so
# scope matching still has useful signal for weak LLM context retrieval.
TRIGGER_PATTERN_STOP_WORDS = {
    'problem', 'please', 'napravi', 'uradi', 'kako', 'sta', 'šta',
    'with', 'this', 'that', 'fix', 'issue', 'help', 'treba', 'moze', 'može',
}


def _atomize_fact(text: str) -> str:
    """Extract the core atomic claim from a long text.
    Removes conversational filler, summarises long passages into one crisp statement."""
    if not text:
        return ''
    # Strip quoted/attributed sections
    text = re.sub(r'^["""\'""\']', '', text).strip()
    text = re.sub(r'["""\'""\'"] $', '', text).strip()
    # Remove trailing meta-commentary
    text = re.sub(r'\s*[-–—]\s*.*(summary|evidently|basically|essentially|actually|in short).*$', '', text, flags=re.IGNORECASE)
    # If it's a list of items, take the first significant item
    lines = [l.strip() for l in text.split('\n') if l.strip() and not l.strip().startswith(('#', '- ', '•', '*'))]
    if lines:
        text = lines[0]
    # Truncate to reasonable length for a fact
    if len(text) > 200:
        # Try to find a sentence boundary near 200 chars
        match = re.search(r'.{0,200}[.!?]', text)
        text = match.group(0).strip() if match else text[:200].rsplit(' ', 1)[0] + '…'
    return text.strip()


def _canonical_fact_text(text: str) -> str:
    text = _atomize_fact(text)
    text = re.sub(r'\s+', ' ', text).strip()
    if len(text) > 200:
        text = text[:200].rsplit(' ', 1)[0].rstrip(' ,;:') + '…'
    return text


def _fact_id(fact_type: str, fact_text: str, scope_key: str = '') -> str:
    canonical = re.sub(r'\W+', ' ', (fact_text or '').lower()).strip()
    return f"fact:{fact_type}:{stable_hash(scope_key, canonical)[:24]}"


def _strip_system_notes(text: str) -> str:
    if not text:
        return ''
    cleaned = re.sub(r'^\[Note: model was just switched[^\]]*\]\s*', '', text, flags=re.IGNORECASE | re.DOTALL)
    cleaned = re.sub(r'^\[System Note:[^\]]*\]\s*', '', cleaned, flags=re.IGNORECASE | re.DOTALL)
    return cleaned.strip()


def _marker_tokens(text: str) -> set[str]:
    return {m.group(0).lower() for m in RUN_MARKER_RE.finditer(text or '')}


def _markers_conflict(left: str, right: str) -> bool:
    left_tokens = _marker_tokens(left)
    right_tokens = _marker_tokens(right)
    return bool(left_tokens and right_tokens and left_tokens.isdisjoint(right_tokens))


def _root_cause_relevant(user_text: str, claim_text: str) -> bool:
    lowered = (claim_text or '').lower()
    if re.search(r'\b(?:codename|run|lbcap)[-_ ][a-z0-9]+\b', lowered):
        return False
    if _markers_conflict(user_text, claim_text):
        return False
    return True


def _has_operational_signal(text: str) -> bool:
    return bool(OPERATIONAL_MEMORY_RE.search(text or '') or FILE_RE.search(text or ''))


def _first_useful_sentence(text: str, max_len: int = 220) -> str:
    cleaned = re.sub(r'\s+', ' ', (text or '')).strip()
    if not cleaned:
        return ''
    parts = re.split(r'(?<=[.!?])\s+', cleaned)
    for part in parts:
        part = part.strip(' -•*')
        if len(part) >= 12:
            cleaned = part
            break
    if len(cleaned) > max_len:
        cleaned = cleaned[:max_len].rsplit(' ', 1)[0].rstrip(' ,;:') + '…'
    return cleaned


def _is_question_or_recall_memory(text: str) -> bool:
    value = (text or '').strip()
    if not value:
        return False
    return bool(MEMORY_QUESTION_RE.search(value) or RECALL_MEMORY_RE.search(value))


def _implicit_workflow_fact(text: str) -> str:
    if not text or _is_question_or_recall_memory(text):
        return ''
    if not WORKFLOW_INSTRUCTION_RE.search(text) or not _has_operational_signal(text):
        return ''
    return _first_useful_sentence(text)


def _implicit_project_memory_fact(text: str) -> str:
    if not text or _is_question_or_recall_memory(text):
        return ''
    if not PROJECT_CONTINUITY_RE.search(text):
        return ''
    fact = _first_useful_sentence(text)
    if len(fact) < 12 or LOW_VALUE_THREAD_RE.match(fact):
        return ''
    return fact


def _assistant_action_fact(text: str) -> str:
    if not text or ASSISTANT_RECALL_META_RE.search(text):
        return ''
    if not ASSISTANT_ACTION_RE.search(text) or not _has_operational_signal(text):
        return ''
    return _first_useful_sentence(text)


def _extract_explicit_memory_facts(text: str) -> List[str]:
    if not text or not PERSISTENT_PREF_RE.search(text):
        return []
    cleaned = EXPLICIT_MEMORY_STOP_RE.sub('', text).strip()
    markers = sorted(_marker_tokens(text))
    marker_prefix = f'{markers[0]}: ' if markers else ''
    candidates: List[str] = []
    candidates.extend(match.group(1).strip() for match in EXPLICIT_MEMORY_NUMBERED_RE.finditer(cleaned))
    rule_match = EXPLICIT_MEMORY_RULE_RE.search(cleaned)
    if rule_match:
        candidates.append(rule_match.group(1).strip())

    facts: List[str] = []
    seen = set()
    for candidate in candidates:
        fact = re.sub(r'\s+', ' ', candidate).strip(' .;:-')
        if len(fact) < 12:
            continue
        if re.search(r'\b(?:ack|ack-seed|ack-infer)\b', fact, re.IGNORECASE):
            continue
        if marker_prefix and markers[0] not in fact.lower():
            fact = marker_prefix + fact
        canonical = _canonical_fact_text(fact)
        key = canonical.lower()
        if key and key not in seen:
            seen.add(key)
            facts.append(canonical)
    return facts


def _classify_intent(user_text: str) -> str:
    """Classify user intent from text. Returns primary intent category."""
    if not user_text:
        return 'unknown'
    lowered = user_text.lower()
    scores = {}
    for intent, pattern in INTENT_PATTERNS.items():
        if pattern.search(lowered):
            scores[intent] = scores.get(intent, 0) + 1
    if not scores:
        return 'conversation'
    # Return highest scoring intent, with tie-breaking priority
    priority = ['correction', 'binding', 'preference', 'command', 'diagnostic', 'question', 'conversation']
    for intent in priority:
        if intent in scores:
            return intent
    return max(scores, key=scores.get)
USER_STYLE_RE = [
    re.compile(r'\b(buraz|brate|stari|ceco|burazer)\b', re.IGNORECASE),
    re.compile(r'\b(pogresno|ponovo|opet|nije to|razumes|lupes)\b', re.IGNORECASE),
    re.compile(r'\b(tacno|da|to|tj|daaa)\b', re.IGNORECASE),
    re.compile(r'\b(samo|ne diraj|koristi isti|zapamti)\b', re.IGNORECASE),
]
# Explicit persistent preference markers
PERSISTENT_PREF_RE = re.compile(r'\b(nikad|uvijek|ne diraj|koristi isti|zapamti|nemoj|ostavi)\b', re.IGNORECASE)
PROBLEM_RE = [
    re.compile(r'\b(problem|error|bug|fails?|failed|crash|ne radi|greska|issue)\b', re.IGNORECASE),
]
CAUSE_CUE_RE = re.compile(r'\b(because|caused by|problem is|uzrok|razlog|zbog)\b', re.IGNORECASE)
RULED_OUT_RE = re.compile(r'\b(not the cause|nije uzrok|ruled out|iskljuceno)\b', re.IGNORECASE)
RUN_MARKER_RE = re.compile(r'\b(?:run|lbcap|codename)[-_][a-z0-9]+\b', re.IGNORECASE)
VALIDATION_RE = re.compile(r'\b(verified|confirmed|reproduced|works now|radi sada|potvrdjeno)\b', re.IGNORECASE)
EXPLICIT_MEMORY_STOP_RE = re.compile(
    r'\b(?:odgovori|respond|ne\s+izvodi|nemoj\s+zaklju[cč]|do\s+not\s+infer)\b.*$',
    re.IGNORECASE | re.DOTALL,
)
OPERATIONAL_MEMORY_RE = re.compile(
    r'\b(?:suno|brave|browser|remote\s+debugging|9222|cookies?|kolačić|kolacic|telegram|gateway|hermes|atlantis|live\s*brain|plugin|tool|api|login|oauth|session|sqlite|db|database|ffmpeg|image|video|audio|artifact|file|path)\b',
    re.IGNORECASE,
)
PROJECT_CONTINUITY_RE = re.compile(
    r'\b(?:pravimo|radimo|ho[cć]u|zelim|želim|probaj|pokušaj|pokusaj|isti\s+te(?:k|x)st|tekst|text|lyrics|'
    r'cover|pesm|pjesm|muzik|suno|flamenco|triler|trileri|trilerima|serbezovski|esmeralda|'
    r'referenc(?:a|e)|romska|romski|gitara|gitarom)\b',
    re.IGNORECASE,
)
MEMORY_QUESTION_RE = re.compile(
    r'(^\s*(?:kako|šta|sta|gde|gdje|koji|koja|what|which|where)\b|\?)',
    re.IGNORECASE,
)
RECALL_MEMORY_RE = re.compile(
    r'\b(?:gde|gdje|dje|dokle|where)\b.{0,80}\b(?:stali|stao|stala|ostali|left|off)\b|'
    r'\b(?:šta|sta|what)\b.{0,80}\b(?:rekao|rekla|rekli|told|radili|radimo)\b',
    re.IGNORECASE | re.DOTALL,
)
WORKFLOW_INSTRUCTION_RE = re.compile(
    r'\b(?:koristi(?:mo|ti)?|koristimo|use|using|radi(?:mo)?\s+(?:preko|sa)|workflow|proces|prijav(?:a|ili|ljuj)|login|connect|poveži|povezi)\b',
    re.IGNORECASE,
)
ASSISTANT_ACTION_RE = re.compile(
    r'\b(?:uradio sam|pokrenuo sam|otvorio sam|kliknuo sam|povezao sam|koristio sam|proverio sam|testirao sam|poslao sam|restartovao sam|instalirao sam|sačuvao sam|sacuvao sam|created|saved|opened|clicked|used|connected|tested|restarted|installed|sent|verified)\b',
    re.IGNORECASE,
)
ASSISTANT_RECALL_META_RE = re.compile(
    r'\b(?:prema pamćenju|prema pamcenju|iz sećanja|iz secanja|na osnovu memorije|from memory|according to memory)\b',
    re.IGNORECASE,
)
EXPLICIT_MEMORY_RULE_RE = re.compile(r'\b(?:pravilo|rule)[^:]*:\s*(.+)$', re.IGNORECASE | re.DOTALL)
EXPLICIT_MEMORY_NUMBERED_RE = re.compile(
    r'(?:^|[\s:])\d+\)\s*(.*?)(?=(?:\s+\d+\)\s*|\s+\b(?:pravilo|rule)\b[^:]*:|$))',
    re.IGNORECASE | re.DOTALL,
)
RECAP_QUERY_RE = re.compile(r'(sumarizuj|sta si radio|what did you do|recap|pregled)', re.IGNORECASE)
LOW_VALUE_THREAD_RE = re.compile(r'^(da|ne|ok|okej|hmm|hm|sve|yes|no|continue|nastavi|cekaj|čekaj|naravno|moze|može|vazi|važi|ajde|dobro)$', re.IGNORECASE)
FEEDBACK_CONTEXT_RE = re.compile(r'\b(recipe|fix|tool|output|artifact|slik|image|video|audio|file|fajl|ffmpeg|seedream|radi|works|fixed|resolved|ne radi|not working|wrong|pogresno|pogrešno)\b', re.IGNORECASE)
FEEDBACK_DIRECT_RE = re.compile(
    r'(^\s*(ne radi|nije radilo|not working|wrong|nope|pogresno|pogrešno|radi sada|works now|fixed|resolved|perfect|odlično|odlicno)\b|'
    r'\b(i dalje|still|sad|sada|now)\b.{0,80}\b(ne radi|not working|wrong|radi|works|fixed|resolved)\b|'
    r'\b(output|artifact|slik|image|video|audio|file|fajl|recipe|fix)\b.{0,100}\b(ne radi|not working|wrong|pogresno|pogrešno|radi|works|fixed|resolved)\b|'
    r'\b(ne radi|not working|wrong|pogresno|pogrešno|radi|works|fixed|resolved)\b.{0,100}\b(output|artifact|slik|image|video|audio|file|fajl|recipe|fix)\b)',
    re.IGNORECASE | re.DOTALL,
)
FEEDBACK_META_NOISE_RE = re.compile(
    r'\b(implementirano|implemented|arhitektur|architecture|precision ratio|attribution|context_impressions|'
    r'tool_results|causal_activations|fix_recipes|recipe_rejections|feedback loop|promotion gate|'
    r'candidate|needs_review|compiler|pipeline|metrics|metric|smoke ok|eval ok|live brain|hermes)\b',
    re.IGNORECASE,
)
MIN_ACTIVE_RECIPE_CONFIRMATIONS = 2



def stable_hash(*parts: str) -> str:
    h = hashlib.sha256()
    for part in parts:
        h.update((part or "").encode("utf-8", "ignore"))
        h.update(b"\x1f")
    return h.hexdigest()


@dataclass
class TurnArtifacts:
    current_thread: str
    entities: List[Dict[str, Any]]
    facts: List[Dict[str, Any]]
    beliefs: List[Dict[str, Any]]
    next_best_actions: List[str]
    episode_title: str
    intent: str = 'unknown'  # New: classified user intent


class Ingestor:
    def __init__(self, conn):
        self.conn = conn

    def store_fact(self, fact_type: str, fact_text: str, confidence: float, source_kind: str, created_at: float, subject_entity_id: str | None = None, evidence_count: int = 1, session_id: str = '', scope_key: str = '', evidence_packet_id: str = '') -> dict:
        fact = {
            "fact_id": _fact_id(fact_type, fact_text, scope_key),
            "subject_entity_id": subject_entity_id,
            "fact_type": fact_type,
            "fact_text": _canonical_fact_text(fact_text),
            "confidence": confidence,
            "source_kind": source_kind,
            "valid_from": created_at,
            "valid_to": None,
            "status": "active",
            "evidence_count": evidence_count,
            "session_id": session_id,
            "scope_key": scope_key,
            "evidence_packet_id": evidence_packet_id,
        }
        self._upsert_fact(fact)
        self.conn.commit()
        return fact

    def store_tool_result(self, tool_name: str, success: bool, error: str = None, artifact_verified: bool | None = None, artifact_path: str = '') -> None:
        """Backward-compatible wrapper for canonical tool result ingestion."""
        payload = {'success': bool(success)}
        if error:
            payload['error'] = error
        if artifact_path:
            payload['artifact_path'] = artifact_path
        result = self.store_tool_result_event(tool_name, {'paths': [artifact_path] if artifact_path else []}, payload)
        if artifact_verified is not None and bool(artifact_verified) != bool(result.get('artifact_verified')):
            self.conn.execute(
                "UPDATE tool_results SET artifact_verified=?, artifact_path=COALESCE(NULLIF(?, ''), artifact_path) WHERE result_id=?",
                (1 if artifact_verified else 0, artifact_path[:240], result['result_id']),
            )
            self.conn.commit()

    def _json_payload(self, result: Any) -> tuple[Any, str]:
        if isinstance(result, (dict, list)):
            return result, json.dumps(result, ensure_ascii=False)
        text = str(result or '')
        try:
            return json.loads(text), text
        except Exception:
            return None, text

    def _walk_artifact_values(self, value: Any):
        artifact_keys = {'image', 'path', 'output_path', 'artifact_path', 'file', 'filename', 'audio', 'video'}
        if isinstance(value, dict):
            for key, child in value.items():
                if isinstance(key, str) and key.lower() in artifact_keys and isinstance(child, str):
                    yield child
                yield from self._walk_artifact_values(child)
        elif isinstance(value, list):
            for child in value:
                yield from self._walk_artifact_values(child)

    def _tool_success_and_error(self, payload: Any, result_text: str) -> tuple[bool, str]:
        if isinstance(payload, dict):
            if payload.get('success') is False:
                return False, str(payload.get('error') or payload.get('message') or result_text)[:500]
            if payload.get('error'):
                return False, str(payload.get('error'))[:500]
            if payload.get('success') is True:
                return True, ''
        if re.search(r'\b(error|failed|failure|exception|traceback|unauthorized|forbidden|not found|timeout|timed out|invalid|cannot|could not)\b', result_text or '', re.IGNORECASE):
            return False, result_text[:500]
        if re.search(r'\b(success|saved|generated|created|done|ok|output)\b', result_text or '', re.IGNORECASE):
            return True, ''
        return True, ''

    def _payload_summary(self, payload: Any, result_text: str) -> str:
        if isinstance(payload, dict):
            parts = []
            for key in ('url', 'title', 'message', 'status', 'output', 'path', 'image', 'audio', 'video'):
                value = payload.get(key)
                if isinstance(value, str) and value.strip():
                    parts.append(f"{key}={value.strip()[:90]}")
                if len(parts) >= 3:
                    break
            if parts:
                return '; '.join(parts)
        return _first_useful_sentence(result_text, max_len=140)

    def _tool_action_fact_text(self, user_text: str, tool_name: str, args_template: Dict[str, Any], payload: Any, result_text: str, artifact_path: str = '') -> str:
        if not user_text or not tool_name:
            return ''
        combined = f"{user_text} {tool_name} {json.dumps(args_template, ensure_ascii=False, default=str)} {result_text}"
        if not _has_operational_signal(combined):
            return ''
        task = _first_useful_sentence(user_text, max_len=120)
        summary = self._payload_summary(payload, result_text)
        parts = [f"For task '{task}', agent successfully used tool {tool_name}"]
        if artifact_path:
            parts.append(f"artifact={artifact_path[:120]}")
        elif summary:
            parts.append(summary)
        return '; '.join(parts)

    def _args_template_from_event(self, tool_name: str, args: Any, payload: Any, result_text: str) -> Dict[str, Any]:
        template: Dict[str, Any] = {'tool': tool_name}
        if isinstance(args, dict):
            prompt = args.get('prompt') or args.get('text') or args.get('input')
            if isinstance(prompt, str) and prompt.strip():
                template['input_kind'] = 'prompt' if 'prompt' in args else 'text'
            model = args.get('model') or args.get('provider')
            if isinstance(model, str) and model.strip():
                template['model'] = model[:120]
        paths: List[str] = []
        for value in self._walk_artifact_values(args):
            if isinstance(value, str):
                paths.append(value)
        for value in self._walk_artifact_values(payload):
            if isinstance(value, str):
                paths.append(value)
        paths.extend(m.group(1).strip('.,;:) ]}') for m in FILE_RE.finditer(result_text or ''))
        clean_paths = []
        seen = set()
        for path in paths:
            if not isinstance(path, str) or path in seen:
                continue
            seen.add(path)
            if self._usable_artifact_path(path):
                clean_paths.append(path)
        if clean_paths:
            template['paths'] = clean_paths[:5]
        return template

    def _canonical_tool_name(self, tool_name: str, result_text: str = '') -> str:
        tool = (tool_name or '').strip()
        lowered = f'{tool} {result_text or ""}'.lower()
        for marker, canonical in self._tool_signal_map().items():
            if marker in lowered:
                return canonical
        return tool

    def store_tool_result_event(self, tool_name: str, args: Any, result: Any, *, session_id: str = '', tool_call_id: str = '', scope_key: str = '', user_text: str = '', created_at: float | None = None, duration_ms: int | None = None) -> dict:
        """Canonical runtime ingestion for post_tool_call events."""
        created_at = created_at or time.time()
        payload, result_text = self._json_payload(result)
        tool_name = self._canonical_tool_name(tool_name, result_text)
        success, error = self._tool_success_and_error(payload, result_text)
        args_template = self._args_template_from_event(tool_name, args if isinstance(args, dict) else {}, payload, result_text)
        artifact_verified, artifact_path = self._verify_artifact(tool_name, args_template, result_text)
        error_type = self._classify_error(error or result_text if not success else '')
        try:
            duration_ms = max(0, int(duration_ms or 0))
        except (TypeError, ValueError):
            duration_ms = 0
        identity = tool_call_id or f'{session_id}:{created_at:.6f}'
        result_id = f"tool_result:{stable_hash(tool_name, identity, result_text[:500])[:24]}"
        self.conn.execute(
            """INSERT OR REPLACE INTO tool_results
            (result_id, tool_name, success, error, error_type, artifact_verified, artifact_path, duration_ms, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (result_id, tool_name, 1 if success else 0, error[:200] if error else None, error_type, 1 if artifact_verified else 0, artifact_path[:240], duration_ms, created_at),
        )
        if scope_key and user_text and tool_name:
            self._record_causal_activation(
                scope_key=scope_key,
                user_text=user_text,
                tool_used=tool_name,
                args_template=args_template,
                raw_content=result_text,
                success=success,
                error_type=error_type,
                artifact_verified=artifact_verified,
                artifact_path=artifact_path,
                created_at=created_at,
            )
            if success:
                action_fact = self._tool_action_fact_text(user_text, tool_name, args_template, payload, result_text, artifact_path)
                if action_fact:
                    self._upsert_fact({
                        "fact_id": _fact_id('tool_action_memory', action_fact, scope_key),
                        "subject_entity_id": None,
                        "fact_type": "tool_action_memory",
                        "fact_text": _canonical_fact_text(action_fact),
                        "confidence": 0.86,
                        "source_kind": "post_tool_call_action",
                        "valid_from": created_at,
                        "valid_to": None,
                        "status": "active",
                        "evidence_count": 1,
                        "session_id": session_id,
                        "scope_key": scope_key,
                        "scope_tags_json": tags_to_json(extract_scope_tags(user_text, result_text, scope_key=scope_key)),
                    })
        self.conn.commit()
        return {
            'result_id': result_id,
            'tool_name': tool_name,
            'success': success,
            'error_type': error_type,
            'artifact_verified': artifact_verified,
            'artifact_path': artifact_path,
            'duration_ms': duration_ms,
            'scope_key': scope_key,
        }

    def get_tool_success_rate(self, tool_name: str) -> float:
        """Get success rate for a tool."""
        row = self.conn.execute(
            """SELECT COUNT(*) as total, SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) as successes 
            FROM tool_results WHERE tool_name = ?""",
            (tool_name,),
        ).fetchone()
        if row[0] == 0:
            return 1.0  # Default to success if no data
        return row[1] / row[0]

    def check_recurring_error(self, error_key: str) -> bool:
        """Check if this error happened multiple times before."""
        row = self.conn.execute(
            """SELECT COUNT(*) FROM beliefs 
            WHERE claim_text LIKE ? AND belief_kind = 'failed_attempt'""",
            (f"%{error_key}%",),
        ).fetchone()
        return row[0] > 2 if row else False

    def ingest_turn(
        self,
        session_id: str,
        scope_key: str,
        turn_index: int,
        user_text: str,
        assistant_text: str,
        created_at: float,
    ) -> TurnArtifacts:
        user_text = _strip_system_notes(user_text)
        assistant_text = _strip_system_notes(assistant_text)
        turn_hash = stable_hash(session_id, str(turn_index), user_text, assistant_text)
        self.conn.execute(
            "INSERT OR IGNORE INTO turns (session_id, turn_index, user_text, assistant_text, created_at, ingest_status, hash) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (session_id, turn_index, user_text, assistant_text, created_at, "ingested", turn_hash),
        )
        turn_row = self.conn.execute(
            "SELECT id FROM turns WHERE hash = ?",
            (turn_hash,),
        ).fetchone()
        turn_id = turn_row[0] if turn_row else None

        # Classify user intent
        intent = _classify_intent(user_text)

        entities = self._extract_entities(user_text, assistant_text, created_at)
        for ent in entities:
            self._upsert_entity(ent, turn_id)

        # Extract entity relationships (Feature 2)
        if len(entities) >= 2:
            self._extract_entity_relationships(user_text, assistant_text, entities, scope_key, created_at)

        scope_tags = extract_scope_tags(user_text, assistant_text, scope_key=scope_key)
        self._apply_user_feedback(scope_key, user_text, created_at)
        self._maybe_propose_self_evolution(scope_key, session_id, user_text, assistant_text, created_at)

        facts = self._extract_facts(user_text, assistant_text, created_at, session_id=session_id, scope_key=scope_key, scope_tags=scope_tags)
        for fact in facts:
            if turn_id is not None:
                fact["source_turn_id"] = str(turn_id)
            self._upsert_fact(fact)

        beliefs = self._extract_beliefs(user_text, assistant_text, created_at, session_id=session_id, scope_key=scope_key, scope_tags=scope_tags)
        for belief in beliefs:
            if turn_id is not None:
                belief["source_turn_id"] = str(turn_id)
            self._upsert_belief(belief)

        episode_title = self._episode_title(user_text, assistant_text)
        episode_summary = self._episode_summary(episode_title, user_text, assistant_text, entities)
        episode_id = ''
        if not is_noisy_episode_memory(episode_title, episode_summary, user_text, assistant_text):
            episode_id = self._ensure_episode(session_id, episode_title, created_at, episode_summary, scope_tags)
            if turn_id is not None:
                self.conn.execute(
                    "INSERT OR REPLACE INTO episode_turns (episode_id, turn_id, role_in_episode) VALUES (?, ?, ?)",
                    (episode_id, turn_id, "turn"),
                )
            self._link_episode_entities(episode_id, entities)

        state = self._build_state_packet(session_id, scope_key, user_text, assistant_text, entities, facts, beliefs, intent, created_at)
        self.conn.execute(
            "INSERT OR REPLACE INTO work_state (scope_key, scope_type, state_json, updated_at) VALUES (?, ?, ?, ?)",
            (scope_key, "session", json.dumps(state), created_at),
        )
        self._upsert_work_item(session_id, scope_key, user_text, assistant_text, state, beliefs, created_at, scope_tags)
        self._refresh_working_set(scope_key)
        self._assign_to_cluster(scope_key, user_text, state, created_at)
        self._crystallise_workflow_hint(scope_key, user_text, assistant_text, created_at)
        # Causal learning is event-driven via post_tool_call -> store_tool_result_event().
        # The old session JSONL parser was intentionally removed from the hot path
        # because string-matching tool transcripts created false positive recipes.
        self.conn.commit()

        return TurnArtifacts(
            current_thread=state["current_thread"],
            entities=entities,
            facts=facts,
            beliefs=beliefs,
            next_best_actions=state["next_best_actions"],
            episode_title=episode_title,
            intent=intent,
        )

    def mirror_memory_write(self, target: str, content: str, created_at: float, session_id: str = '', scope_key: str = '') -> None:
        fact = {
            "fact_id": _fact_id(target, content, scope_key),
            "fact_type": target,
            "fact_text": _canonical_fact_text(content),
            "confidence": 0.95,
            "source_kind": "explicit_memory_write",
            "valid_from": created_at,
            "valid_to": None,
            "status": "active",
            "evidence_count": 1,
            "session_id": session_id,
            "scope_key": scope_key,
            "scope_tags_json": tags_to_json(extract_scope_tags(content, scope_key=scope_key)),
        }
        self._upsert_fact(fact)
        self.conn.commit()

    def ingest_delegation(self, task: str, result: str, child_session_id: str, created_at: float, session_id: str = '', scope_key: str = '') -> None:
        belief = {
            "belief_id": f"delegation:{stable_hash(task, result, child_session_id)}",
            "episode_id": None,
            "claim_text": f"Delegation result for task: {task[:160]} -> {result[:160]}",
            "belief_kind": "delegation_result",
            "confidence": 0.6,
            "status": "open",
            "created_at": created_at,
            "updated_at": created_at,
            "validated_by": None,
            "supersedes_belief_id": None,
            "session_id": session_id,
            "scope_key": scope_key,
            "scope_tags_json": tags_to_json(extract_scope_tags(task, result, scope_key=scope_key)),
        }
        self._upsert_belief(belief)
        self.conn.commit()

    def _extract_entities(self, user_text: str, assistant_text: str, created_at: float) -> List[Dict[str, Any]]:
        entities: List[Dict[str, Any]] = []
        combined = f"{user_text}\n{assistant_text}"
        seen = set()
        for match in FILE_RE.finditer(combined):
            path = match.group(1)
            if path in seen:
                continue
            seen.add(path)
            entities.append({
                "entity_id": f"file:{stable_hash(path)}",
                "entity_type": "file",
                "canonical_name": path,
                "display_name": path,
                "attributes_json": json.dumps({"path": path}),
                "last_seen_at": created_at,
                "salience_score": 1.0,
                "mention_text": path,
                "mention_role": "exact",
                "weight": 1.0,
            })
        lower = combined.lower()
        for term in ["ffmpeg", "kokoro", "seedream", "seedance", "gemma", "vision", "tts", "video", "screenshot", "memory", "provider"]:
            if term in lower:
                entities.append({
                    "entity_id": f"concept:{term}",
                    "entity_type": "concept",
                    "canonical_name": term,
                    "display_name": term,
                    "attributes_json": "{}",
                    "last_seen_at": created_at,
                    "salience_score": 0.8,
                    "mention_text": term,
                    "mention_role": "exact",
                    "weight": 0.8,
                })
        return entities

    def _extract_facts(self, user_text: str, assistant_text: str, created_at: float, session_id: str = '', scope_key: str = '', scope_tags: Dict[str, Any] | None = None) -> List[Dict[str, Any]]:
        facts: List[Dict[str, Any]] = []
        atomized_user = _atomize_fact(user_text)
        atomized_assistant = _atomize_fact(assistant_text) if assistant_text else ''
        # Deduplicate against existing beliefs: if the atomized text already exists as a belief, skip storing as fact
        existing_beliefs = set()
        rows = self.conn.execute("SELECT LOWER(claim_text) FROM beliefs").fetchall()
        for row in rows:
            existing_beliefs.add(row[0][:200])
        if atomized_user and atomized_user[:200].lower() in existing_beliefs:
            atomized_user = ''
        if atomized_assistant and atomized_assistant[:200].lower() in existing_beliefs:
            atomized_assistant = ''

        if any(p.search(user_text) for p in USER_PREF_RE) and atomized_user:
            facts.append({
                "fact_id": _fact_id('user_pref', user_text, scope_key),
                "fact_type": "user_preference",
                "fact_text": _canonical_fact_text(user_text),
                "confidence": 0.85,
                "source_kind": "explicit_user",
                "valid_from": created_at,
                "valid_to": None,
                "status": "active",
                "evidence_count": 1,
                "session_id": session_id,
                "scope_key": scope_key,
                "scope_tags_json": tags_to_json(scope_tags),
            })
        # Explicit persistent preferences (user explicitly says to remember something)
        if PERSISTENT_PREF_RE.search(user_text):
            facts.append({
                "fact_id": _fact_id('persistent_pref', user_text, scope_key),
                "fact_type": "persistent_constraint",
                "fact_text": _canonical_fact_text(user_text),
                "confidence": 0.95,
                "source_kind": "explicit_persistent_pref",
                "valid_from": created_at,
                "valid_to": None,
                "status": "active",
                "evidence_count": 1,
                "session_id": session_id,
                "scope_key": scope_key,
                "scope_tags_json": tags_to_json(scope_tags),
            })
            for explicit_fact in _extract_explicit_memory_facts(user_text):
                facts.append({
                    "fact_id": _fact_id('explicit_memory_fact', explicit_fact, scope_key),
                    "fact_type": "explicit_memory_fact",
                    "fact_text": explicit_fact,
                    "confidence": 0.95,
                    "source_kind": "explicit_user_memory",
                    "valid_from": created_at,
                    "valid_to": None,
                    "status": "active",
                    "evidence_count": 1,
                    "session_id": session_id,
                    "scope_key": scope_key,
                    "scope_tags_json": tags_to_json(scope_tags),
                })
        implicit_workflow = _implicit_workflow_fact(user_text)
        if implicit_workflow:
            facts.append({
                "fact_id": _fact_id('implicit_workflow', implicit_workflow, scope_key),
                "fact_type": "workflow_instruction",
                "fact_text": _canonical_fact_text(implicit_workflow),
                "confidence": 0.84,
                "source_kind": "implicit_user_workflow",
                "valid_from": created_at,
                "valid_to": None,
                "status": "active",
                "evidence_count": 1,
                "session_id": session_id,
                "scope_key": scope_key,
                "scope_tags_json": tags_to_json(scope_tags),
            })
        project_memory = _implicit_project_memory_fact(user_text)
        if project_memory and project_memory != implicit_workflow:
            facts.append({
                "fact_id": _fact_id('work_continuity_memory', project_memory, scope_key),
                "fact_type": "work_continuity_memory",
                "fact_text": _canonical_fact_text(project_memory),
                "confidence": 0.80,
                "source_kind": "implicit_user_work",
                "valid_from": created_at,
                "valid_to": None,
                "status": "active",
                "evidence_count": 1,
                "session_id": session_id,
                "scope_key": scope_key,
                "scope_tags_json": tags_to_json(scope_tags),
            })
        assistant_action = _assistant_action_fact(assistant_text)
        if assistant_action:
            facts.append({
                "fact_id": _fact_id('assistant_action_report', assistant_action, scope_key),
                "fact_type": "agent_action_report",
                "fact_text": _canonical_fact_text(f"Agent reported: {assistant_action}"),
                "confidence": 0.80,
                "source_kind": "assistant_action_report",
                "valid_from": created_at,
                "valid_to": None,
                "status": "active",
                "evidence_count": 1,
                "session_id": session_id,
                "scope_key": scope_key,
                "scope_tags_json": tags_to_json(scope_tags),
            })
        if assistant_text and (VALIDATION_RE.search(assistant_text) or re.search(r'\b(user prefers|prefer[s]? .* for|use .* for|should use)\b', assistant_text, re.IGNORECASE)):
            facts.append({
                "fact_id": _fact_id('assistant_validated', assistant_text, scope_key),
                "fact_type": "validated_fact",
                "fact_text": _canonical_fact_text(assistant_text),
                "confidence": 0.75,
                "source_kind": "assistant_report",
                "valid_from": created_at,
                "valid_to": None,
                "status": "active",
                "evidence_count": 1,
                "session_id": session_id,
                "scope_key": scope_key,
                "scope_tags_json": tags_to_json(scope_tags),
            })

        # Automatic fact extraction from assistant responses (Feature 1)
        if assistant_text:
            auto_facts = self._auto_extract_facts_from_assistant(
                assistant_text, created_at, session_id, scope_key, scope_tags
            )
            facts.extend(auto_facts)

        return facts

    def _auto_extract_facts_from_assistant(
        self,
        assistant_text: str,
        created_at: float,
        session_id: str,
        scope_key: str,
        scope_tags: Dict[str, Any] | None
    ) -> List[Dict[str, Any]]:
        """
        Automatically extract facts from assistant responses (Feature 1).

        Patterns:
        - "The [subject] is [predicate]"
        - "[Entity] uses/requires/has [property]"
        - Factual statements
        """
        from .context_fence import should_fence

        facts: List[Dict[str, Any]] = []

        # Pattern 1: "X is Y" - flexible matching for conversational text
        is_pattern = re.compile(r'\b([A-Z][A-Za-z0-9_-]+(?:\s+[A-Za-z0-9_-]+)*)\s+(?:is|are)\s+((?:a|an|the)?\s*[^.!?\n]+)', re.MULTILINE)
        for match in is_pattern.finditer(assistant_text):
            subject, predicate = match.groups()
            fact_text = f"{subject} is {predicate.strip()}"

            if should_fence(fact_text, 'assistant', 'auto'):
                continue

            if len(fact_text) > 20 and len(fact_text) < 200:
                facts.append({
                    "fact_id": _fact_id('auto_extracted_is', fact_text, scope_key),
                    "fact_type": "auto_extracted_fact",
                    "fact_text": _canonical_fact_text(fact_text),
                    "confidence": 0.7,
                    "source_kind": "assistant_auto",
                    "valid_from": created_at,
                    "valid_to": None,
                    "status": "active",
                    "evidence_count": 1,
                    "session_id": session_id,
                    "scope_key": scope_key,
                    "scope_tags_json": tags_to_json(scope_tags),
                    "extraction_method": "auto",
                })

        # Pattern 2: "X uses/requires/has Y" - flexible matching
        relation_pattern = re.compile(r'\b([A-Z][A-Za-z0-9_-]+(?:\s+[A-Za-z0-9_-]+)*)\s+(uses|requires|has|needs|supports|processes|handles)\s+([^.!?,\n]+)', re.IGNORECASE)
        for match in relation_pattern.finditer(assistant_text):
            entity, relation, target = match.groups()
            fact_text = f"{entity} {relation} {target.strip()}"

            if should_fence(fact_text, 'assistant', 'auto'):
                continue

            if len(fact_text) > 15 and len(fact_text) < 150:
                facts.append({
                    "fact_id": _fact_id('auto_extracted_relation', fact_text, scope_key),
                    "fact_type": "auto_extracted_fact",
                    "fact_text": _canonical_fact_text(fact_text),
                    "confidence": 0.7,
                    "source_kind": "assistant_auto",
                    "valid_from": created_at,
                    "valid_to": None,
                    "status": "active",
                    "evidence_count": 1,
                    "session_id": session_id,
                    "scope_key": scope_key,
                    "scope_tags_json": tags_to_json(scope_tags),
                    "extraction_method": "auto",
                })

        return facts[:5]  # Limit to 5 auto-extracted facts per turn

    def _extract_entity_relationships(self, user_text: str, assistant_text: str, entities: List[Dict], scope_key: str, created_at: float):
        """Extract relationships between entities (Feature 2)."""
        from .entity_graph import EntityGraph
        graph = EntityGraph(self.conn)

        combined_text = f"{user_text} {assistant_text}".lower()
        entity_names = [e.get('canonical_name', '').lower() for e in entities]

        # Detect "X uses Y" relationships
        for i, ent_a in enumerate(entities):
            for j, ent_b in enumerate(entities):
                if i >= j:
                    continue
                name_a = ent_a.get('canonical_name', '').lower()
                name_b = ent_b.get('canonical_name', '').lower()

                # Check for relationship patterns
                if f"{name_a} uses {name_b}" in combined_text or f"{name_a} use {name_b}" in combined_text:
                    graph.add_relationship(ent_a['entity_id'], ent_b['entity_id'], 'uses', strength=0.8, scope_key=scope_key)
                elif f"{name_a} processes {name_b}" in combined_text or f"{name_a} process {name_b}" in combined_text:
                    graph.add_relationship(ent_a['entity_id'], ent_b['entity_id'], 'processes', strength=0.8, scope_key=scope_key)

    def _extract_beliefs(self, user_text: str, assistant_text: str, created_at: float, session_id: str = '', scope_key: str = '', scope_tags: Dict[str, Any] | None = None) -> List[Dict[str, Any]]:
        beliefs: List[Dict[str, Any]] = []
        lowered_assistant = (assistant_text or '').lower()
        # Do not treat recap/meta-summary outputs as causal beliefs.
        if any(x in lowered_assistant for x in ['evo šta mogu da rekonstruišem', 'na osnovu onoga što vidim iz memorije', 'sumarizuj', 'recap answer draft', 'recent work recap']):
            return beliefs
        if assistant_text and CAUSE_CUE_RE.search(assistant_text):
            kind = "validated_cause" if VALIDATION_RE.search(assistant_text) else "hypothesis"
            status = "validated" if kind == "validated_cause" else "open"
            beliefs.append({
                "belief_id": f"belief:{kind}:{stable_hash(assistant_text)}",
                "episode_id": None,
                "claim_text": assistant_text[:500],
                "belief_kind": kind,
                "confidence": 0.8 if kind == "validated_cause" else 0.55,
                "status": status,
                "created_at": created_at,
                "updated_at": created_at,
                "validated_by": None,
                "supersedes_belief_id": None,
                "caused_by_work_item_id": None,
                "tool_name": None,
                "session_id": session_id,
                "scope_key": scope_key,
                "scope_tags_json": tags_to_json(scope_tags),
            })
        if assistant_text and RULED_OUT_RE.search(assistant_text):
            claim = RULED_OUT_RE.sub('', assistant_text).strip()
            if len(claim) < 20:
                claim = assistant_text[:500]
            beliefs.append({
                "belief_id": f"belief:ruled_out:{stable_hash(assistant_text)}",
                "episode_id": None,
                "claim_text": claim[:500],
                "belief_kind": "ruled_out_cause",
                "confidence": 0.7,
                "status": "validated",
                "created_at": created_at,
                "updated_at": created_at,
                "validated_by": None,
                "supersedes_belief_id": None,
                "caused_by_work_item_id": None,
                "tool_name": None,
                "session_id": session_id,
                "scope_key": scope_key,
                "scope_tags_json": tags_to_json(scope_tags),
            })
        # Track failed attempts
        if assistant_text and any(term in assistant_text.lower() for term in ['failed', 'error', 'cannot', 'ne mogu', 'nije uspjelo']):
            beliefs.append({
                "belief_id": f"belief:failed_attempt:{stable_hash(assistant_text)}",
                "episode_id": None,
                "claim_text": assistant_text[:500],
                "belief_kind": "failed_attempt",
                "confidence": 0.5,
                "status": "open",
                "created_at": created_at,
                "updated_at": created_at,
                "validated_by": None,
                "supersedes_belief_id": None,
                "caused_by_work_item_id": None,
                "tool_name": None,
                "session_id": session_id,
                "scope_key": scope_key,
                "scope_tags_json": tags_to_json(scope_tags),
            })

        # Automatic belief extraction from assistant responses (Feature 1)
        if assistant_text:
            auto_beliefs = self._auto_extract_beliefs_from_assistant(
                assistant_text, created_at, session_id, scope_key, scope_tags
            )
            beliefs.extend(auto_beliefs)

        return beliefs

    def _auto_extract_beliefs_from_assistant(
        self,
        assistant_text: str,
        created_at: float,
        session_id: str,
        scope_key: str,
        scope_tags: Dict[str, Any] | None
    ) -> List[Dict[str, Any]]:
        """
        Automatically extract beliefs/hypotheses from assistant responses (Feature 1).

        Patterns:
        - "This might be caused by X"
        - "The issue could be X"
        - "I suspect X"
        """
        from .context_fence import should_fence

        beliefs: List[Dict[str, Any]] = []

        # Pattern 1: "might be caused by" / "could be"
        hypothesis_patterns = [
            r'(?:might|could|may)\s+be\s+(?:caused\s+by|due\s+to)\s+([^.!?]+)',
            r'(?:the\s+)?(?:issue|problem|error)\s+(?:might|could|may)\s+be\s+([^.!?]+)',
            r'I\s+suspect\s+(?:that\s+)?([^.!?]+)',
            r'(?:possibly|perhaps|maybe)\s+(?:caused\s+by|due\s+to)\s+([^.!?]+)',
        ]

        for pattern in hypothesis_patterns:
            for match in re.finditer(pattern, assistant_text, re.IGNORECASE):
                hypothesis = match.group(1).strip()
                claim_text = f"Hypothesis: {hypothesis}"

                if should_fence(claim_text, 'assistant', 'auto'):
                    continue

                if len(hypothesis) > 10 and len(hypothesis) < 200:
                    beliefs.append({
                        "belief_id": f"belief:auto_hypothesis:{stable_hash(claim_text)}",
                        "episode_id": None,
                        "claim_text": claim_text[:500],
                        "belief_kind": "hypothesis",
                        "confidence": 0.5,
                        "status": "open",
                        "created_at": created_at,
                        "updated_at": created_at,
                        "validated_by": None,
                        "supersedes_belief_id": None,
                        "caused_by_work_item_id": None,
                        "tool_name": None,
                        "session_id": session_id,
                        "scope_key": scope_key,
                        "scope_tags_json": tags_to_json(scope_tags),
                        "extraction_method": "auto",
                    })

        return beliefs[:3]  # Limit to 3 auto-extracted beliefs per turn

    def _episode_title(self, user_text: str, assistant_text: str) -> str:
        raw = user_text.strip() or assistant_text.strip() or "general thread"
        # Clean low-value titles
        lowered = raw.lower()
        if is_low_signal_thread_title(lowered) or any(lowered.startswith(x) for x in ['da.', 'ne.', 'ok', 'okej', 'da,', 'sta', 'kako', 'jel', 'imam', 'ima', 'ne znam', 'hocu', 'zelim', 'mozes', 'mozes li', 'a sta', 'a kako']):
            # Try to extract meaningful part
            cleaned = re.sub(r'^(da|ne|ok|sta|kako|jel|ima|hocu|zelim|mozes|a sta|a kako)[,.\s]*', '', raw, flags=re.IGNORECASE)
            if len(cleaned) > 15:
                raw = cleaned
        return raw[:120]

    def _episode_summary(self, title: str, user_text: str, assistant_text: str, entities: List[Dict[str, Any]]) -> str:
        lowered = f"{user_text}\n{assistant_text}".lower()
        files = [e['display_name'] for e in entities if e.get('entity_type') == 'file'][:2]
        concepts = [e['display_name'] for e in entities if e.get('entity_type') == 'concept'][:3]
        parts = []
        if files:
            parts.append(f"FILE: {', '.join(files)}")
        if concepts:
            parts.append(f"SCOPE: {', '.join(concepts)}")
        if any(w in lowered for w in ['error', 'failed', 'bug', 'problem', 'ne radi', 'greska']):
            parts.append(f"PROBLEM: {_canonical_fact_text(user_text)[:120]}")
        if any(w in lowered for w in ['fixed', 'resolved', 'works now', 'radi', 'done', 'success']):
            parts.append(f"FIX: {_canonical_fact_text(assistant_text)[:120]}")
        if not parts:
            parts.append(f"TASK: {_canonical_fact_text(title)[:160]}")
        return ' | '.join(parts)[:300]

    def _ensure_episode(self, session_id: str, title: str, created_at: float, summary: str = '', scope_tags: Dict[str, Any] | None = None) -> str:
        if is_noisy_episode_memory(title, summary):
            return ''
        existing = self.conn.execute(
            "SELECT episode_id FROM episodes WHERE title = ? AND status IN ('active', 'dormant') ORDER BY updated_at DESC LIMIT 1",
            (title,),
        ).fetchone()
        if existing:
            episode_id = existing[0]
            self.conn.execute(
                "UPDATE episodes SET updated_at = ?, current_summary = ?, scope_tags_json = ? WHERE episode_id = ?",
                (created_at, summary or title, tags_to_json(scope_tags), episode_id),
            )
            return episode_id
        episode_id = f"episode:{stable_hash(session_id, title)}"
        self.conn.execute(
            "INSERT OR REPLACE INTO episodes (episode_id, kind, title, status, opened_at, updated_at, current_summary, priority_score, recency_score, scope_tags_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (episode_id, "general", title, "active", created_at, created_at, summary or title, 0.5, 1.0, tags_to_json(scope_tags)),
        )
        return episode_id

    def _upsert_entity(self, entity: Dict[str, Any], turn_id: int | None) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO entities (entity_id, entity_type, canonical_name, display_name, attributes_json, last_seen_at, salience_score) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                entity["entity_id"], entity["entity_type"], entity["canonical_name"], entity["display_name"], entity["attributes_json"], entity["last_seen_at"], entity["salience_score"],
            ),
        )
        if turn_id is not None:
            self.conn.execute(
                "INSERT INTO entity_mentions (entity_id, turn_id, episode_id, mention_text, mention_role, weight, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (entity["entity_id"], turn_id, None, entity["mention_text"], entity["mention_role"], entity["weight"], entity["last_seen_at"]),
            )

    def _upsert_fact(self, fact: Dict[str, Any]) -> None:
        fact_id = fact["fact_id"]
        before = row_to_dict(self.conn.execute("SELECT * FROM facts WHERE fact_id=?", (fact_id,)).fetchone())
        self.conn.execute(
            "INSERT OR REPLACE INTO facts (fact_id, subject_entity_id, fact_type, fact_text, confidence, source_kind, valid_from, valid_to, status, evidence_count, session_id, scope_key, scope_tags_json, evidence_packet_id, source_turn_id, source_event_id, extraction_method) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                fact_id, fact.get("subject_entity_id"), fact["fact_type"], fact["fact_text"], fact["confidence"], fact["source_kind"], fact["valid_from"], fact["valid_to"], fact["status"], fact["evidence_count"], fact.get("session_id", ""), fact.get("scope_key", ""), fact.get("scope_tags_json", "{}"), fact.get("evidence_packet_id", ""), fact.get("source_turn_id", ""), fact.get("source_event_id", ""), fact.get("extraction_method", "manual"),
            ),
        )
        after = row_to_dict(self.conn.execute("SELECT * FROM facts WHERE fact_id=?", (fact_id,)).fetchone())
        record_revision(self.conn, object_type='fact', object_id=fact_id, action='upsert', reason=fact.get('source_kind', 'ingest'), before=before, after=after, source_turn_id=str(fact.get('source_turn_id', '') or ''), source_event_id=str(fact.get('source_event_id', '') or ''), created_at=fact.get('valid_from'))

    def _upsert_belief(self, belief: Dict[str, Any]) -> None:
        belief_id = belief["belief_id"]
        before = row_to_dict(self.conn.execute("SELECT * FROM beliefs WHERE belief_id=?", (belief_id,)).fetchone())
        self.conn.execute(
            "INSERT OR REPLACE INTO beliefs (belief_id, episode_id, claim_text, belief_kind, confidence, status, created_at, updated_at, validated_by, supersedes_belief_id, caused_by_work_item_id, tool_name, session_id, scope_key, scope_tags_json, evidence_packet_id, source_turn_id, source_event_id, extraction_method) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                belief_id, belief.get("episode_id"), belief["claim_text"], belief["belief_kind"], belief["confidence"], belief["status"], belief["created_at"], belief["updated_at"], belief.get("validated_by"), belief.get("supersedes_belief_id"), belief.get("caused_by_work_item_id"), belief.get("tool_name"), belief.get("session_id", ""), belief.get("scope_key", ""), belief.get("scope_tags_json", "{}"), belief.get("evidence_packet_id", ""), belief.get("source_turn_id", ""), belief.get("source_event_id", ""), belief.get("extraction_method", "manual"),
            ),
        )
        after = row_to_dict(self.conn.execute("SELECT * FROM beliefs WHERE belief_id=?", (belief_id,)).fetchone())
        record_revision(self.conn, object_type='belief', object_id=belief_id, action='upsert', reason=belief.get('belief_kind', 'ingest'), before=before, after=after, source_turn_id=str(belief.get('source_turn_id', '') or ''), source_event_id=str(belief.get('source_event_id', '') or ''), created_at=belief.get('updated_at'))

    def _link_episode_entities(self, episode_id: str, entities: List[Dict[str, Any]]) -> None:
        for entity in entities:
            self.conn.execute(
                "INSERT OR REPLACE INTO episode_files (episode_id, entity_id, relationship, weight) VALUES (?, ?, ?, ?)",
                (episode_id, entity["entity_id"], "mentioned", entity.get("weight", 1.0)),
            )

    def _upsert_work_item(self, session_id: str, scope_key: str, user_text: str, assistant_text: str, state: Dict[str, Any], beliefs: List[Dict[str, Any]], created_at: float, scope_tags: Dict[str, Any] | None = None) -> None:
        if RECAP_QUERY_RE.search(user_text or ''):
            return
        title = (user_text or state.get("current_thread") or "general thread").strip()[:160]
        if not title or LOW_VALUE_THREAD_RE.match(title) or is_noisy_episode_memory(title, assistant_text=assistant_text):
            return
        work_item_id = f"work_item:{stable_hash(scope_key, title)}"
        lowered_user = (user_text or '').lower()
        lowered_assistant = (assistant_text or '').lower()
        status = 'active'
        resolved_at = None
        negative_resolution = any(term in lowered_assistant for term in ['not fixed', 'nije fixed', 'nije resen', 'nije riješen', 'not resolved', 'still not', 'not working yet'])
        if not negative_resolution and any(term in lowered_assistant for term in ['resolved', 'works now', 'radi ispravno', 'fixed', 'riješen', 'resen']):
            status = 'resolved'
            resolved_at = created_at
        elif any(term in lowered_assistant for term in ['blocked', 'cannot', 'ne mogu', 'still fails']):
            status = 'blocked'
        root_cause = ''
        for belief in beliefs:
            if belief.get('belief_kind') == 'validated_cause' and belief.get('status') == 'validated':
                candidate_root = belief.get('claim_text', '')
                if not _root_cause_relevant(user_text, candidate_root):
                    continue
                root_cause = candidate_root[:500]
                break
        next_actions = list(state.get('next_best_actions') or [])
        next_step = next_actions[0][:300] if next_actions else ''
        evidence = {
            'current_thread': state.get('current_thread', ''),
            'validated_facts': list(state.get('validated_facts') or [])[:3],
            'open_hypotheses': list(state.get('open_hypotheses') or [])[:2],
        }
        priority = 1.0 if status == 'active' else (0.7 if status == 'blocked' else 0.2)

        # Dedup: find existing work item with same or very similar title in this scope
        before = row_to_dict(self.conn.execute("SELECT * FROM work_items WHERE work_item_id=?", (work_item_id,)).fetchone())
        existing = self.conn.execute(
            "SELECT work_item_id, status FROM work_items WHERE scope_key = ? AND work_item_id = ?",
            (scope_key, work_item_id),
        ).fetchone()
        if existing and existing['status'] == 'resolved' and status == 'active':
            # Don't reopen a resolved item from a new turn with same title
            return

        self.conn.execute(
            "INSERT OR REPLACE INTO work_items (work_item_id, scope_key, session_id, title, status, priority, evidence_json, next_step, root_cause, supersedes_work_item_id, created_at, updated_at, resolved_at, scope_tags_json, source_turn_id, source_event_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE((SELECT supersedes_work_item_id FROM work_items WHERE work_item_id = ?), NULL), COALESCE((SELECT created_at FROM work_items WHERE work_item_id = ?), ?), ?, ?, ?, ?, '')",
            (work_item_id, scope_key, session_id, title, status, priority, json.dumps(evidence), next_step, root_cause, work_item_id, work_item_id, created_at, created_at, resolved_at, tags_to_json(scope_tags), ''),
        )
        after = row_to_dict(self.conn.execute("SELECT * FROM work_items WHERE work_item_id=?", (work_item_id,)).fetchone())
        record_revision(self.conn, object_type='work_item', object_id=work_item_id, action='upsert', reason='turn_ingest', before=before, after=after, created_at=created_at)
        if status == 'resolved':
            title_prefix = title[:40].lower()
            self.conn.execute(
                "UPDATE work_items SET status = 'superseded', resolved_at = COALESCE(resolved_at, ?), priority = 0.05 WHERE scope_key = ? AND work_item_id != ? AND lower(title) LIKE ? AND status = 'active'",
                (created_at, scope_key, work_item_id, f"{title_prefix}%"),
            )
        # Cross-session priority boost: if same work item title appeared before
        recurring_count = self.conn.execute(
            "SELECT COUNT(DISTINCT session_id) FROM work_items WHERE lower(title) = lower(?)",
            (title,),
        ).fetchone()
        if recurring_count and recurring_count[0] > 1:
            priority = min(priority + 0.3, 1.0)  # Boost recurring items

    def _assign_to_cluster(self, scope_key: str, user_text: str, state: Dict[str, Any], created_at: float) -> None:
        title = (user_text or state.get('current_thread') or '').strip()[:160]
        if not title or LOW_VALUE_THREAD_RE.match(title) or RECAP_QUERY_RE.search(title) or is_noisy_episode_memory(title):
            return
        keywords = [w for w in title.lower().split() if len(w) > 3][:5]
        if not keywords:
            return
        rows = self.conn.execute(
            "SELECT cluster_id, project_name, member_work_item_ids_json FROM episode_clusters WHERE scope_key = ? ORDER BY last_active_at DESC LIMIT 20",
            (scope_key,),
        ).fetchall()
        best_cluster_id = None
        best_overlap = 0
        for row in rows:
            name_words = row['project_name'].lower().split()
            overlap = sum(1 for k in keywords if any(k in w for w in name_words))
            if overlap > best_overlap:
                best_overlap = overlap
                best_cluster_id = row['cluster_id']
        work_item_id = f"work_item:{stable_hash(scope_key, title)}"
        if best_cluster_id and best_overlap >= 1:
            row = self.conn.execute("SELECT member_work_item_ids_json FROM episode_clusters WHERE cluster_id = ?", (best_cluster_id,)).fetchone()
            members = json.loads(row[0]) if row and row[0] else []
            if work_item_id not in members:
                members.append(work_item_id)
            self.conn.execute(
                "UPDATE episode_clusters SET member_work_item_ids_json = ?, last_active_at = ? WHERE cluster_id = ?",
                (json.dumps(members), created_at, best_cluster_id),
            )
        else:
            cluster_id = f"cluster:{stable_hash(scope_key, title)}"
            self.conn.execute(
                "INSERT OR IGNORE INTO episode_clusters (cluster_id, scope_key, project_name, member_work_item_ids_json, last_active_at, summary) VALUES (?, ?, ?, ?, ?, ?)",
                (cluster_id, scope_key, title[:80], json.dumps([work_item_id]), created_at, ''),
            )

    def _refresh_working_set(self, scope_key: str) -> None:
        rows = self.conn.execute(
            "SELECT work_item_id FROM work_items WHERE scope_key = ? AND status IN ('active','blocked') AND lower(title) NOT IN ('da','ne','ok','okej','sve','yes','no','continue','nastavi') ORDER BY CASE WHEN status='active' THEN 0 ELSE 1 END, priority DESC, updated_at DESC LIMIT 3",
            (scope_key,),
        ).fetchall()
        self.conn.execute("DELETE FROM working_set WHERE scope_key = ?", (scope_key,))
        now = time.time()
        for slot, row in enumerate(rows):
            self.conn.execute(
                "INSERT OR REPLACE INTO working_set (scope_key, work_item_id, added_at, slot) VALUES (?, ?, ?, ?)",
                (scope_key, row['work_item_id'], now, slot),
            )

    def _build_state_packet(self, session_id: str, scope_key: str, user_text: str, assistant_text: str, entities: List[Dict[str, Any]], facts: List[Dict[str, Any]], beliefs: List[Dict[str, Any]], intent: str, created_at: float) -> Dict[str, Any]:
        next_best_actions: List[str] = []

        # Intent-aware action selection
        if intent == 'correction':
            next_best_actions.append("Acknowledge the correction and apply the correction immediately. Do not repeat the mistake.")
        elif intent == 'binding':
            next_best_actions.append("Honor the binding constraint in all future actions. Treat it as absolute.")
        elif intent == 'question':
            next_best_actions.append("Answer the latest question directly using the freshest relevant state.")
        elif intent == 'diagnostic':
            next_best_actions.append("Diagnose the problem using exact entities, recent attempts, and causal evidence before guessing.")
        elif intent == 'command':
            next_best_actions.append("Execute the command with precision. Report result clearly.")
        elif intent == 'preference':
            next_best_actions.append("Apply the stated preference in future interactions.")
        else:
            if QUESTION_RE.search(user_text.strip()):
                next_best_actions.append("Answer the latest question directly using the freshest relevant state.")
            elif any(PROBLEM_RE_ITEM.search(user_text) for PROBLEM_RE_ITEM in PROBLEM_RE):
                next_best_actions.append("Diagnose the problem using exact entities, recent attempts, and causal evidence before guessing.")
            else:
                next_best_actions.append("Continue the active thread without losing continuity.")

        validated = [f["fact_text"] for f in facts if f["confidence"] >= 0.75][:3]
        open_hypotheses = [b["claim_text"] for b in beliefs if b["status"] == "open"][:2]

        current_thread = user_text[:160]
        if LOW_VALUE_THREAD_RE.match(current_thread):
            row = self.conn.execute(
                "SELECT title FROM work_items WHERE scope_key = ? AND status IN ('active','blocked') AND lower(title) NOT IN ('da','ne','ok','okej','sve','yes','no','continue','nastavi') ORDER BY updated_at DESC LIMIT 1",
                (scope_key,),
            ).fetchone()
            if row and row[0]:
                current_thread = row[0][:160]
        if RECAP_QUERY_RE.search(user_text or ''):
            row = self.conn.execute(
                "SELECT title FROM work_items WHERE scope_key = ? AND status IN ('active','blocked') AND lower(title) NOT IN ('da','ne','ok','okej','sve','yes','no','continue','nastavi') ORDER BY updated_at DESC LIMIT 1",
                (scope_key,),
            ).fetchone()
            if row and row[0]:
                current_thread = row[0][:160]

        return {
            "current_thread": current_thread,
            "intent": intent,
            "relevant_entities": [e["display_name"] for e in entities[:6]],
            "validated_facts": validated,
            "open_hypotheses": open_hypotheses,
            "next_best_actions": next_best_actions,
            "updated_at": created_at,
        }

    def _crystallise_workflow_hint(self, scope_key: str, user_text: str, assistant_text: str, created_at: float) -> None:
        trigger_pattern = self._trigger_pattern(user_text)
        if not self._real_task_for_recipe(user_text, trigger_pattern):
            return
        if self._meta_problem_pattern(f'{user_text} {assistant_text}'):
            return
        if not VALIDATION_RE.search(assistant_text or '') and not re.search(r'\b(verified|tested|works|success|generated|saved)\b', assistant_text or '', re.IGNORECASE):
            return
        hint = f"When task matches '{trigger_pattern}', reuse the verified workflow from this task and verify the artifact/result."
        hint_id = f"workflow_hint:{stable_hash(scope_key, trigger_pattern)[:24]}"
        self.conn.execute(
            "INSERT OR IGNORE INTO crystallised_knowledge (id, scope_key, principle_text, source_work_item_id, confidence, created_at) VALUES (?, ?, ?, '', 0.8, ?)",
            (hint_id, scope_key, hint[:240], created_at),
        )

    def _classify_error(self, content: str) -> str:
        lowered = (content or '').lower()
        if not lowered:
            return ''
        if any(token in lowered for token in ['401', 'unauthorized', 'forbidden', 'permission']):
            return 'auth'
        if any(token in lowered for token in ['404', 'not found', 'missing file', 'no such file']):
            return 'not_found'
        if any(token in lowered for token in ['timeout', 'timed out', 'rate limit', '429']):
            return 'transient'
        if any(token in lowered for token in ['invalid', 'bad request', 'schema', 'argument']):
            return 'bad_args'
        if any(token in lowered for token in ['empty', 'zero bytes', '0 bytes']):
            return 'empty_output'
        if any(token in lowered for token in ['error', 'failed', 'cannot']):
            return 'tool_error'
        return ''

    def _artifact_required(self, tool_used: str) -> bool:
        tool = (tool_used or '').lower()
        return any(token in tool for token in ARTIFACT_REQUIRED_TOOL_TOKENS)

    def _usable_artifact_path(self, path: str) -> bool:
        if not path or not path.startswith('/'):
            return False
        lowered = path.lower()
        if lowered in {'/models', '/chat/completions', '/v1/models', '/v1/chat/completions'}:
            return False
        if any(part in lowered for part in ['/chat/', '/completions', '/models']):
            return False
        return bool(re.search(r'\.(png|jpe?g|webp|gif|mp4|mov|mkv|wav|mp3|m4a|ogg|txt|json)$', lowered))

    def _verify_artifact(self, tool_used: str, args_template: Dict[str, Any], raw_content: str) -> tuple[bool, str]:
        paths = []
        for value in args_template.get('paths') or []:
            if isinstance(value, str):
                paths.append(value)
        paths.extend(m.group(1).strip('.,;:) ]}') for m in FILE_RE.finditer(raw_content or ''))
        for candidate in paths:
            if not self._usable_artifact_path(candidate):
                continue
            try:
                artifact = Path(candidate)
                if artifact.exists() and artifact.is_file() and artifact.stat().st_size > 0:
                    return True, str(artifact)
            except Exception:
                continue
        tool = (tool_used or '').lower()
        if 'whisper' in tool:
            return bool(re.search(r'\b(transcript|text)\b.{0,80}\S+', raw_content or '', re.IGNORECASE)), ''
        return False, ''

    def _meta_problem_pattern(self, text: str) -> bool:
        lowered = (text or '').lower().strip()
        if not lowered or len(lowered) < 8:
            return True
        meta_markers = [
            'review conversation above', 'live brain sistem', 'live brain plugin', '10/10 gate',
            'arhitekturu trenutne live baze', 'kako ti se svidja', 'analiziraj live brain',
            'what do you think', 'how do you like', 'done ', 'ukupan utisak',
            'implemented measurement layer', 'precision ratio', 'attribution modes', 'promotion helper',
            'feedback loop', 'hermes restart', 'package rebuilt', 'smoke ok', 'eval ok',
            'metrics healthy', 'manual recipe compiler', 'gotovo implementirao', 'loop mnogo stroži',
            'loop mnogo strozi', 'compiler pamti', 'were right metrics',
        ]
        return any(marker in lowered for marker in meta_markers)

    def _tool_domain(self, tool_used: str) -> str:
        return tool_domain(tool_used)

    def _task_domain(self, user_text: str, scope_tags: Dict[str, Any]) -> str:
        domains = set(scope_tags.get('domain') or [])
        lowered = (user_text or '').lower()
        if 'video' in domains or re.search(r'\b(video|mp4|short|reel)\b', lowered):
            return 'video'
        if 'image' in domains or re.search(r'\b(image|slika|picture|photo|png|jpg)\b', lowered) or any(alias in lowered for alias in IMAGE_GENERATION_ALIASES):
            return 'image'
        if 'audio' in domains or re.search(r'\b(audio|voice|glas|tts|mp3|wav|transcript)\b', lowered):
            return 'audio'
        return ''

    def _tool_matches_task_domain(self, tool_used: str, user_text: str, scope_tags: Dict[str, Any]) -> bool:
        tool_domain = self._tool_domain(tool_used)
        task_domain = self._task_domain(user_text, scope_tags)
        if not tool_domain or not task_domain:
            return True
        if task_domain == tool_domain:
            return True
        if task_domain == 'video' and tool_domain == 'image':
            lowered = (user_text or '').lower()
            return any(token in lowered for token in ['frame', 'thumbnail', 'cover', 'slik', 'image'])
        return False

    def _real_task_for_recipe(self, user_text: str, trigger_pattern: str) -> bool:
        text = f'{user_text} {trigger_pattern}'.lower()
        if self._meta_problem_pattern(text):
            return False
        action_terms = ['make', 'create', 'generate', 'build', 'fix', 'run', 'render', 'napravi', 'uradi', 'generisi', 'generiši', 'popravi', 'izrender']
        artifact_terms = ['image', 'slika', 'video', 'audio', 'voice', 'file', 'fajl', 'mp4', 'png', 'jpg', 'mp3', 'wav', 'ffmpeg', 'seedream', 'tts']
        return any(term in text for term in action_terms) and any(term in text for term in artifact_terms)

    def _specific_reusable_pattern(self, trigger_pattern: str) -> bool:
        words = [w for w in re.findall(r'[\w./-]+', trigger_pattern or '') if len(w) > 3]
        if len(words) < 3:
            return False
        low_value = {'implemented', 'measurement', 'layer', 'place', 'added', 'ratio', 'threshold', 'question', 'answer'}
        return len([w for w in words if w.lower() not in low_value]) >= 3

    def _learnable_recipe_tool(self, tool_used: str) -> bool:
        tool = (tool_used or '').lower()
        return any(token in tool for token in RECIPE_TOOL_TOKENS)

    def _recipe_rejection_reason(self, user_text: str, trigger_pattern: str, tool_used: str, scope_tags: Dict[str, Any], artifact_verified: bool) -> str:
        if not self._learnable_recipe_tool(tool_used):
            return 'non_recipe_tool'
        if not self._real_task_for_recipe(user_text, trigger_pattern):
            return 'meta_or_not_real_task'
        if not self._specific_reusable_pattern(trigger_pattern):
            return 'not_reusable'
        if not self._tool_matches_task_domain(tool_used, user_text, scope_tags):
            return 'domain_mismatch'
        if self._artifact_required(tool_used) and not artifact_verified:
            return 'artifact_unverified'
        return ''

    def _recipe_worth_keeping(self, user_text: str, trigger_pattern: str, tool_used: str, scope_tags: Dict[str, Any], artifact_verified: bool) -> bool:
        return not self._recipe_rejection_reason(user_text, trigger_pattern, tool_used, scope_tags, artifact_verified)

    def _promotion_status(self, tool_used: str, trigger_pattern: str, success: bool, artifact_verified: bool) -> str:
        if not success or self._meta_problem_pattern(trigger_pattern):
            return 'needs_review'
        if self._artifact_required(tool_used):
            return 'candidate'
        return 'active'

    def _feedback_signal(self, user_text: str) -> str:
        text = user_text or ''
        lowered = text.lower()
        negative = any(token in lowered for token in ['ne radi', 'nije radilo', 'failed', 'fail', 'still broken', 'not working', 'wrong', 'nope', 'pogresno', 'pogrešno', 'nije dobro'])
        positive = any(token in lowered for token in ['radi sada', 'works now', 'fixed', 'resolved', 'dobro je', 'odlicno', 'odlično', 'perfect', 'uspesno', 'uspješno'])
        gratitude_only = lowered.strip() in {'thanks', 'hvala', 'thank you', 'fala'}
        if gratitude_only or not FEEDBACK_CONTEXT_RE.search(text):
            return ''
        if FEEDBACK_META_NOISE_RE.search(text) and (len(text) > 80 or '\n' in text or len(re.findall(r'\w+', text)) > 12):
            return ''
        if not FEEDBACK_DIRECT_RE.search(text):
            return ''
        if negative and not positive:
            return 'failure'
        if positive and not negative:
            return 'success'
        return ''

    def _attribution_mode(self, recipe_ids: List[str]) -> str:
        if len(recipe_ids) == 1:
            return 'precise'
        if len(recipe_ids) > 1:
            return 'broad'
        return 'fallback'

    def _recent_impression_recipe_ids(self, scope_key: str, created_at: float, outcome: str, feedback_text: str) -> List[str]:
        rows = self.conn.execute(
            "SELECT impression_id, recipe_ids_json FROM context_impressions WHERE scope_key=? AND outcome='pending' AND created_at >= ? ORDER BY created_at DESC LIMIT 8",
            (scope_key, created_at - 1800),
        ).fetchall()
        for row in rows:
            try:
                ids = [recipe_id for recipe_id in json.loads(row['recipe_ids_json'] or '[]') if isinstance(recipe_id, str)]
            except Exception:
                ids = []
            if not ids:
                continue
            mode = self._attribution_mode(ids)
            self.conn.execute(
                "UPDATE context_impressions SET outcome=?, attribution_mode=?, feedback_text=?, updated_at=? WHERE impression_id=?",
                (outcome, mode, feedback_text[:500], created_at, row['impression_id']),
            )
            return ids
        return []

    def _apply_user_feedback(self, scope_key: str, user_text: str, created_at: float) -> None:
        outcome = self._feedback_signal(user_text)
        if not outcome:
            return
        recipe_ids = self._recent_impression_recipe_ids(scope_key, created_at, outcome, user_text)
        if recipe_ids:
            placeholders = ','.join('?' for _ in recipe_ids)
            if outcome == 'failure':
                from .evolution import SelfEvolutionManager
                SelfEvolutionManager(self.conn).propose(
                    scope_key=scope_key,
                    trigger_text=user_text,
                    proposal_type='demote_fix_recipe',
                    target_area='recipe',
                    rationale='User reported that recently injected recipe/context failed; demote bounded recipe IDs to needs_review.',
                    proposed_action='Move the implicated fix_recipes to needs_review and lower confidence; do not edit code or files.',
                    evidence={'recipe_ids': recipe_ids, 'feedback_text': user_text[:500]},
                    suggested_tests=['live_brain context eval', 'targeted tool/artifact smoke'],
                    auto_apply=True,
                )
            elif outcome == 'success':
                self.conn.execute(
                    f"UPDATE fix_recipes SET confidence=MIN(confidence + 0.05, 0.99), times_confirmed=times_confirmed + 1, updated_at=? WHERE recipe_id IN ({placeholders}) AND status='active'",
                    [created_at] + recipe_ids,
                )
                self.conn.execute(
                    f"UPDATE fix_recipes SET status='candidate', promotion_status='candidate', candidate_since=COALESCE(candidate_since, ?), last_reviewed_at=?, confidence=MIN(confidence + 0.05, 0.8), updated_at=? WHERE recipe_id IN ({placeholders}) AND status='needs_review' AND artifact_verified=1 AND confidence >= 0.5",
                    [created_at, created_at, created_at] + recipe_ids,
                )
            return
        return

    def _maybe_propose_self_evolution(self, scope_key: str, session_id: str, user_text: str, assistant_text: str, created_at: float) -> None:
        user_lowered = (user_text or '').lower()
        if 'live_brain_capability_e2e' in user_lowered:
            return
        review_only_terms = ('review', 'pregled', 'recenz', 'verdikt')
        missing_inquiry_terms = ('šta fali', 'sta fali', 'what is missing', 'hard blocker', 'nema hard blocker', 'nice-to-have')
        change_terms = ('implement', 'patch', 'fix', 'sredi', 'poprav', 'change', 'promeni', 'promijeni', 'dodaj')
        user_for_change_terms = user_lowered.replace('must_fix_next', '')
        if any(term in user_lowered for term in review_only_terms + missing_inquiry_terms) and not any(term in user_for_change_terms for term in change_terms):
            return
        approval_admin_terms = ['approval', 'approve', 'reject', 'pending', 'odobri', 'odbij', 'odobrenj']
        if any(token in user_lowered for token in approval_admin_terms):
            return
        user_request_terms = [
            'live brain', 'self-evol', 'self evolving', 'autonomous', 'memory/context', 'context engine',
            'uradi', 'napravi', 'implement', 'patch', 'fix', 'resolve', 'change', 'promeni', 'promijeni', 'dodaj',
        ]
        if not any(token in user_lowered for token in user_request_terms):
            return
        text = f'{user_text or ""}\n{assistant_text or ""}'
        lowered = text.lower()
        if not any(token in lowered for token in ['live brain', 'self-evol', 'self evolving', 'autonomous', 'memory/context', 'context engine']):
            return
        if not any(token in lowered for token in ['code', 'kod', 'patch', 'schema', 'migration', 'config', 'plugin', 'hook', 'tool']):
            return
        from .evolution import SelfEvolutionManager
        SelfEvolutionManager(self.conn).propose(
            scope_key=scope_key,
            session_id=session_id,
            trigger_text=user_text[:500],
            proposal_type='code_patch',
            target_area='code',
            rationale='Conversation requested or discussed changing Live Brain behavior/code. Code evolution must be gated.',
            proposed_action='Draft a patch proposal, run targeted tests, and require explicit user approval before applying code/config/schema changes.',
            evidence={'requires_code_change': True, 'user_text': user_text[:500]},
            suggested_tests=['python -m py_compile live_brain modules', 'live_brain smoke/eval tests', 'artifact/context smoke'],
            auto_apply=False,
        )

    def _record_causal_activation(self, scope_key: str, user_text: str, tool_used: str, args_template: Dict[str, Any], raw_content: str, success: bool, error_type: str, artifact_verified: bool, artifact_path: str, created_at: float) -> None:
        trigger = (user_text or '')[:120]
        trigger_pattern = self._trigger_pattern(user_text)
        test_result = self._test_result(success, bool(error_type), raw_content)
        scope_tags = extract_scope_tags(user_text, raw_content, scope_key=scope_key)
        scope_tags_json = tags_to_json(scope_tags)
        activation_id = f"activation:{stable_hash(scope_key, trigger_pattern, tool_used, json.dumps(args_template, sort_keys=True))}"
        before = row_to_dict(self.conn.execute("SELECT * FROM causal_activations WHERE activation_id = ?", (activation_id,)).fetchone())
        existing = self.conn.execute("SELECT times_confirmed, success, artifact_verified FROM causal_activations WHERE activation_id = ?", (activation_id,)).fetchone()
        if existing:
            artifact_verified = bool(artifact_verified or existing['artifact_verified'])
            self.conn.execute(
                "UPDATE causal_activations SET times_confirmed = ?, success = ?, outcome = ?, test_result = ?, artifact_verified = ?, artifact_path = ?, error_type = ?, updated_at = ? WHERE activation_id = ?",
                (existing[0] + 1, 1 if success else existing['success'], raw_content[:200], test_result, 1 if artifact_verified else 0, artifact_path[:240], error_type, created_at, activation_id),
            )
        else:
            self.conn.execute(
                "INSERT INTO causal_activations (activation_id, scope_key, trigger_text, trigger_pattern, action_taken, tool_used, args_template_json, outcome, test_result, artifact_verified, artifact_path, error_type, success, confidence, times_confirmed, scope_tags_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (activation_id, scope_key, trigger, trigger_pattern, raw_content[:200], tool_used, json.dumps(args_template, sort_keys=True), raw_content[:200], test_result, 1 if artifact_verified else 0, artifact_path[:240], error_type, 1 if success else 0, 0.8 if success else 0.3, 1, scope_tags_json, created_at, created_at),
            )
        after = row_to_dict(self.conn.execute("SELECT * FROM causal_activations WHERE activation_id = ?", (activation_id,)).fetchone())
        record_revision(self.conn, object_type='causal_activation', object_id=activation_id, action='upsert', reason='post_tool_call', before=before, after=after, created_at=created_at)
        if success:
            rejection_reason = self._recipe_rejection_reason(user_text, trigger_pattern, tool_used, scope_tags, artifact_verified)
            if rejection_reason:
                rejection_id = f"recipe_rejection:{stable_hash(scope_key, trigger_pattern, tool_used, rejection_reason, str(int(created_at)))[:24]}"
                self.conn.execute(
                    "INSERT OR REPLACE INTO recipe_rejections (rejection_id, scope_key, trigger_pattern, tool_name, reason, artifact_verified, source, created_at) VALUES (?, ?, ?, ?, ?, ?, 'candidate_gate', ?)",
                    (rejection_id, scope_key, trigger_pattern[:240], tool_used, rejection_reason, 1 if artifact_verified else 0, created_at),
                )
            else:
                status = self._promotion_status(tool_used, trigger_pattern, success, artifact_verified)
                self._upsert_fix_recipe(scope_key, trigger_pattern, tool_used, args_template, scope_tags_json, created_at, artifact_verified=artifact_verified, artifact_path=artifact_path, error_type=error_type, promotion_status=status)

    def _ingest_causal_activation(self, scope_key: str, user_text: str, assistant_text: str, created_at: float) -> None:
        """Deprecated compatibility hook.

        Causal activation used to be inferred by reparsing session JSONL tool
        messages. That path was too noisy, so runtime learning now enters through
        store_tool_result_event(), which receives structured post_tool_call data.
        This method remains as a no-op for callers pinned to the old API.
        """
        return


    def _upsert_fix_recipe(self, scope_key: str, trigger_pattern: str, tool_used: str, args_template: Dict[str, Any], scope_tags_json: str, created_at: float, *, artifact_verified: bool = False, artifact_path: str = '', error_type: str = '', promotion_status: str = 'candidate') -> None:
        steps = self._recipe_steps(tool_used, args_template)
        success_criteria = self._success_criteria(tool_used, args_template)
        recipe_id = f"recipe:{stable_hash(scope_key, trigger_pattern, tool_used, json.dumps(args_template, sort_keys=True))[:24]}"
        before = row_to_dict(self.conn.execute("SELECT * FROM fix_recipes WHERE recipe_id = ?", (recipe_id,)).fetchone())
        existing = self.conn.execute(
            "SELECT times_confirmed, confidence FROM fix_recipes WHERE recipe_id = ?",
            (recipe_id,),
        ).fetchone()
        times_confirmed = 1
        confidence = 0.85
        if existing:
            times_confirmed = int(existing['times_confirmed'] or 0) + 1
            confidence = max(float(existing['confidence'] or 0), confidence)
        if promotion_status == 'candidate' and artifact_verified and times_confirmed >= MIN_ACTIVE_RECIPE_CONFIRMATIONS:
            promotion_status = 'active'
        status = 'active' if promotion_status == 'active' and (artifact_verified or not self._artifact_required(tool_used)) else promotion_status
        self.conn.execute(
            "INSERT OR REPLACE INTO fix_recipes (recipe_id, scope_key, problem_pattern, tool_name, steps_json, args_template_json, success_criteria, artifact_verified, artifact_path, error_type, promotion_status, candidate_since, promoted_at, last_reviewed_at, confidence, times_confirmed, status, source, scope_tags_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE((SELECT candidate_since FROM fix_recipes WHERE recipe_id = ?), ?), CASE WHEN ?='active' THEN COALESCE((SELECT promoted_at FROM fix_recipes WHERE recipe_id = ?), ?) ELSE (SELECT promoted_at FROM fix_recipes WHERE recipe_id = ?) END, CASE WHEN ?='needs_review' THEN ? ELSE (SELECT last_reviewed_at FROM fix_recipes WHERE recipe_id = ?) END, ?, ?, ?, 'causal_activation', ?, COALESCE((SELECT created_at FROM fix_recipes WHERE recipe_id = ?), ?), ?)",
            (recipe_id, scope_key, trigger_pattern[:240], tool_used, json.dumps(steps, ensure_ascii=False), json.dumps(args_template, sort_keys=True), success_criteria, 1 if artifact_verified else 0, artifact_path[:240], error_type[:80], promotion_status, recipe_id, created_at, status, recipe_id, created_at, recipe_id, status, created_at, recipe_id, confidence, times_confirmed, status, scope_tags_json or '{}', recipe_id, created_at, created_at),
        )
        after = row_to_dict(self.conn.execute("SELECT * FROM fix_recipes WHERE recipe_id = ?", (recipe_id,)).fetchone())
        record_revision(self.conn, object_type='fix_recipe', object_id=recipe_id, action='upsert', reason='causal_activation', before=before, after=after, created_at=created_at)

    def _recipe_steps(self, tool_used: str, args_template: Dict[str, Any]) -> List[str]:
        tool = (tool_used or '').lower()
        if 'image_generate' in tool:
            return ['use image_generate', 'use local input files, not remote URLs', 'set output_path to an absolute path', 'verify the output file exists']
        if 'ffmpeg' in tool:
            return ['run ffmpeg with explicit input/output paths', 'check exit code', 'verify output file exists and has non-zero size']
        if 'tts' in tool:
            return ['use the configured TTS tool', 'write output to an absolute path', 'verify audio file exists']
        if 'whisper' in tool:
            return ['use whisper transcription tool', 'verify transcript text is non-empty']
        steps = [f'use {tool_used}'] if tool_used else ['use the proven tool']
        if args_template.get('paths'):
            steps.append('reuse the known path pattern')
        return steps

    def _success_criteria(self, tool_used: str, args_template: Dict[str, Any]) -> str:
        tool = (tool_used or '').lower()
        if 'image_generate' in tool:
            return 'image file exists at absolute output path and is deliverable'
        if 'ffmpeg' in tool:
            return 'video file exists, non-zero size, playable'
        if 'tts' in tool:
            return 'audio file exists, non-zero size, playable'
        return 'tool returns success and expected artifact exists'

    def _tool_signal_map(self) -> Dict[str, str]:
        return dict(TOOL_SIGNAL_TERMS)

    def _trigger_pattern(self, text: str) -> str:
        words = [w.lower() for w in re.findall(r'[\w./-]+', text or '') if len(w) > 3]
        signal = [w for w in words if w not in TRIGGER_PATTERN_STOP_WORDS][:8]
        return ' '.join(signal)[:160]

    def _args_template(self, tool_used: str, content: str) -> Dict[str, Any]:
        template: Dict[str, Any] = {'tool': tool_used}
        paths = [m.group(1).strip('.,;:) ]}') for m in FILE_RE.finditer(content or '')][:5]
        if paths:
            template['paths'] = paths
        model_match = re.search(r'(?:model|provider)[=: ]+[\"\']?([\w./:-]+)', content or '', re.IGNORECASE)
        if model_match:
            template['model'] = model_match.group(1)[:120]
        return template

    def _test_result(self, success: bool, failure: bool, content: str) -> str:
        if success:
            return 'success'
        if failure:
            return 'failure'
        return 'unknown'
