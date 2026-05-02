from __future__ import annotations

import hashlib
import re


LOW_SIGNAL_THREAD_TITLES = {
    'da', 'ne', 'ok', 'okej', 'hmm', 'hm', 'yes', 'no', 'continue', 'nastavi',
    'cekaj', 'čekaj', 'naravno', 'moze', 'može', 'vazi', 'važi', 'ajde', 'dobro',
}

_META_MEMORY_RE = re.compile(
    r'(review\s+the\s+conversation\s+above|consider\s+saving\s+or\s+updating\s+a\s+skill|'
    r'skill\s+(?:updated|a[zž]uriran)|pending\s+self[- ]?evolution|approve\s+latest\s+pending\s+self[- ]?evolution|'
    r'model\s+was\s+just\s+switched|runtime\s+test|context\s+impressions|'
    r'current_summary|scope_tags_json|reality_state|open_loops|brain_state_debug|'
    r'kad\s+te\s+pitam\s+ne[sš]to\s+[sš]to\s+ne\s+zna[sš]|'
    r'[sš]ta\s+radi[sš]\s+kad\s+te\s+pitam\s+ne[sš]to\s+[sš]to\s+ne\s+zna[sš])',
    re.IGNORECASE,
)

_TEST_MEMORY_RE = re.compile(
    r'^(test\s+(?:autonomous|epistemic|production)|production\s+smoke\s+test|smoke\s+test:|e2e\s+test:)',
    re.IGNORECASE,
)

_META_FIX_RE = re.compile(
    r'\b(memory|plugin|live\s*brain|episode|context|skill|approval|debug|database|db|sql|terminal|research_jobs)\b',
    re.IGNORECASE,
)


def stable_id(prefix: str, *parts: str) -> str:
    h = hashlib.sha256()
    for part in parts:
        h.update((part or '').encode('utf-8', 'ignore'))
        h.update(b'\x1f')
    return f"{prefix}:{h.hexdigest()[:24]}"


def normalized_text(value: str) -> str:
    return re.sub(r'\s+', ' ', (value or '').strip().lower())


def is_low_signal_thread_title(title: str) -> bool:
    return normalized_text(title).strip(' .,!?:;') in LOW_SIGNAL_THREAD_TITLES


def is_meta_memory_text(text: str) -> bool:
    return bool(_META_MEMORY_RE.search(text or ''))


def is_test_or_smoke_memory(title: str) -> bool:
    return bool(_TEST_MEMORY_RE.search((title or '').strip()))


def is_noisy_episode_memory(title: str, summary: str = '', user_text: str = '', assistant_text: str = '') -> bool:
    title_text = (title or '').strip()
    summary_text = (summary or '').strip()
    combined = '\n'.join(part for part in (title_text, summary_text, user_text or '', assistant_text or '') if part)
    if not title_text and not summary_text:
        return True
    if is_low_signal_thread_title(title_text):
        return True
    if is_test_or_smoke_memory(title_text):
        return True
    if is_meta_memory_text(combined):
        return True
    upper_summary = summary_text.upper()
    if 'FIX:' in upper_summary and _META_FIX_RE.search(combined):
        useful_fix_tokens = (' TOOL', ' FILE', ' PATH', ' COMMAND', ' RUN ', ' USE ', ' ADD ', ' SET ', ' VERIFY', ' IMAGE_GENERATE', ' FFMPEG')
        fix_text = upper_summary.split('FIX:', 1)[1]
        if not any(token in fix_text for token in useful_fix_tokens):
            return True
    if upper_summary.startswith('TASK:') and normalized_text(summary_text[5:]) == normalized_text(title_text):
        return is_low_signal_thread_title(title_text) or len(title_text.split()) <= 2
    return False
