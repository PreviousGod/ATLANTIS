"""Text filtering and redaction for live_brain_ctx."""

import json
import re
from typing import Any, List


# Constants
_MAX_FACT_LEN = 200
_LOW_SIGNAL_WORDS = {'problem', 'plugin', 'memory', 'brain', 'generation', 'generate', 'napravi', 'uradi', 'kako', 'sta', 'šta', 'what', 'which', 'with', 'video', 'image', 'radi', 'recap', 'poslednje', 'uradjeno', 'urađeno', 'gde', 'gdje', 'dje', 'stali', 'stao', 'stala', 'rekao', 'rekla', 'rekli', 'sam', 'smo', 'sta', 'šta', 'odgovori', 'odgovor', 'sećanja', 'sećanja', 'secanja', 'secanca', 'traži', 'trazi', 'ponavljam', 'ponavljati', 'ponovi'}
_RECALL_QUERY_WORDS = {'gde', 'gdje', 'dje', 'dokle', 'stali', 'stao', 'stala', 'ostali', 'dosli', 'došli', 'rekao', 'rekla', 'rekli', 'told', 'where', 'were', 'leave', 'left', 'off', 'odgovori', 'odgovor', 'sećanja', 'secanja', 'traži', 'trazi', 'ponavljam', 'ponovi'}
_MUSIC_MEMORY_ALIASES = (
    'pesm', 'pjesm', 'song', 'songs', 'music', 'muzik', 'cover', 'flamenco',
    'triler', 'trileri', 'trilerima', 'serbezovski', 'esmeralda', 'lyrics',
    'romska', 'romski', 'spanski', 'španski', 'spanish', 'gitar', 'gitara', 'reference', 'referenca',
)

# Regexes
_SECRET_RE = re.compile(r'\b(?:sk-[A-Za-z0-9_-]{12,}|sk-or-v1-[A-Za-z0-9_-]{12,}|[A-Za-z0-9_]*(?:api[_-]?key|token|secret|password|passwd|bearer)[A-Za-z0-9_]*\s*[:=]\s*\S+)', re.IGNORECASE)
_NOISY_MEMORY_RE = re.compile(
    r'(##\s*summary|###\s*situacija|the user sent an image|the user sent a voice message|selfie photo|personal trust|'
    r'gave me his selfie|openrouter api key|api key \(active|client_secret|review the conversation above)',
    re.IGNORECASE,
)
_LOW_VALUE_FACT_RE = re.compile(r'(dobra pitanje|refaktorisao live brain|evo kako bih|na osnovu memory context)', re.IGNORECASE)
_SYNTHETIC_MEMORY_RE = re.compile(r'\b(?:ack-seed|ack-infer|live_brain_human_memory_seed|memory_sync_fix_test|lbmemsync-|hmem-|kestrel\s+harbor|live_brain_capability_e2e|upamti\s+ovo\s+kao\s+stvarno\s+pravilo)\b', re.IGNORECASE)


def is_low_signal_thread_title(title: str) -> bool:
    """Check if thread title is low signal."""
    return re.sub(r'\s+', ' ', (title or '').strip().lower()).strip(' .,!?:;') in {
        'da', 'ne', 'ok', 'okej', 'hmm', 'hm', 'yes', 'no', 'continue', 'nastavi',
        'cekaj', 'čekaj', 'naravno', 'moze', 'može', 'vazi', 'važi', 'ajde', 'dobro',
    }


def is_noisy_episode_memory(title: str, summary: str = '', user_text: str = '', assistant_text: str = '') -> bool:
    """Check if episode memory is noisy."""
    combined = '\n'.join(part for part in (title or '', summary or '', user_text or '', assistant_text or '') if part)
    return bool(re.search(r'(review\s+the\s+conversation\s+above|consider\s+saving\s+or\s+updating\s+a\s+skill|skill\s+(updated|a[zž]uriran)|pending\s+self[- ]?evolution|current_summary|scope_tags_json|reality_state|open_loops)', combined, re.I)) or is_low_signal_thread_title(title)


def _truncate_fact(text: str) -> str:
    return _redact(text or "")[:_MAX_FACT_LEN]


def _redact(text: str) -> str:
    text = _SECRET_RE.sub('[REDACTED_SECRET]', text or '')
    return re.sub(r'\bAPI\s*key\w*\b', 'credential', text, flags=re.IGNORECASE)


def redact_for_storage(value: Any) -> Any:
    """Redact secrets recursively before persisting user-controlled data."""
    if isinstance(value, str):
        return _redact(value)
    if isinstance(value, list):
        return [redact_for_storage(item) for item in value]
    if isinstance(value, dict):
        return {str(key): redact_for_storage(item) for key, item in value.items()}
    return value


def redact_json_text(raw: str) -> str:
    """Redact a JSON document string while preserving its shape when possible."""
    if not raw:
        return raw
    try:
        parsed = json.loads(raw)
    except Exception:
        return _redact(raw)
    return json.dumps(redact_for_storage(parsed), ensure_ascii=False, sort_keys=True)


def _expand_query_words(words: List[str], low_signal_words: set) -> List[str]:
    expanded: List[str] = []
    for word in words:
        value = (word or '').lower().strip('.,!?;:…')
        if not value or value in low_signal_words or value in _RECALL_QUERY_WORDS:
            continue
        expanded.append(value)
        if value.startswith(('pesm', 'pjesm')) or value in {'song', 'songs', 'music', 'muzika', 'muziku', 'cover'}:
            expanded.extend(_MUSIC_MEMORY_ALIASES)
        elif value == 'suno':
            expanded.extend(('suno', *_MUSIC_MEMORY_ALIASES))
        elif value.startswith(('muzik', 'triler', 'flamenco', 'serbez', 'esmeralda')):
            expanded.extend(_MUSIC_MEMORY_ALIASES)
    deduped: List[str] = []
    seen = set()
    for value in expanded:
        if value and value not in seen:
            seen.add(value)
            deduped.append(value)
    return deduped


def _meaningful_query_words(words: List[str], low_signal_words: set) -> List[str]:
    return _expand_query_words(words, low_signal_words)


def _is_low_signal_episode(title: str, summary: str, low_signal_words: set) -> bool:
    if is_noisy_episode_memory(title, summary):
        return True
    text = (summary or '').strip()
    upper = text.upper()
    if upper.startswith('SCOPE:') and 'PROBLEM:' in upper and 'FIX:' not in upper and 'ROOT' not in upper:
        return True
    if upper.startswith('SCOPE:') and 'PROBLEM:' in upper and 'FIX:' in upper:
        fix_text = text.upper().split('FIX:', 1)[1].strip()
        useful_tokens = ('TOOL', 'FILE', 'PATH', 'COMMAND', 'RUN', 'USE ', 'ADD ', 'SET ', 'VERIFY', 'IMAGE_GENERATE', 'FFMPEG')
        if not any(token in fix_text for token in useful_tokens):
            return True
    title_words = set(re.findall(r'[\w./-]+', (title or '').lower()))
    summary_words = set(re.findall(r'[\w./-]+', text.lower()))
    meaningful = _meaningful_query_words([w for w in title_words if len(w) > 3], low_signal_words)
    if meaningful and len(summary_words - title_words) <= 3 and len(meaningful) >= 3:
        return True
    return False


def _is_noisy_memory(text: str) -> bool:
    if not text:
        return True
    if _SYNTHETIC_MEMORY_RE.search(text):
        return True
    if is_noisy_episode_memory(text, text):
        return True
    lowered = text.lower().strip()
    if _NOISY_MEMORY_RE.search(text):
        return True
    if _LOW_VALUE_FACT_RE.search(text):
        return True
    if lowered.startswith(('[note:', '[system note:', '## summary', '###')):
        return True
    if len(text) > 300 and ('\n' in text or '###' in text or '```' in text or '|' in text):
        return True
    if text.count('\n') >= 2:
        return True
    return False
