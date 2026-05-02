from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List

try:
    from .scopes_config import DOMAIN_TERMS, TOOL_TERMS
except Exception:
    _config_path = Path(__file__).resolve().with_name('scopes_config.py')
    _config_spec = importlib.util.spec_from_file_location('_live_brain_scopes_config', _config_path)
    _config_mod = importlib.util.module_from_spec(_config_spec)
    sys.modules['_live_brain_scopes_config'] = _config_mod
    _config_spec.loader.exec_module(_config_mod)
    DOMAIN_TERMS = _config_mod.DOMAIN_TERMS
    TOOL_TERMS = _config_mod.TOOL_TERMS

FILE_RE = re.compile(r'(?:^|\s)(/[\w./-]+|[\w.-]+/[\w./-]+)')


def normalize_tag(value: str) -> str:
    return re.sub(r'\s+', ' ', (value or '').strip().lower())[:160]


def extract_scope_tags(*texts: str, scope_key: str = '') -> Dict[str, List[str]]:
    combined = '\n'.join(t or '' for t in texts)
    lowered = combined.lower()
    tags: Dict[str, set[str]] = {
        'scope_key': {scope_key} if scope_key else set(),
        'file': set(),
        'repo': set(),
        'tool': set(),
        'domain': set(),
        'task': set(),
    }
    for match in FILE_RE.finditer(combined):
        raw = match.group(1).strip('.,;:) ]}')
        if not raw or raw.startswith('http'):
            continue
        tags['file'].add(normalize_tag(raw))
        parts = Path(raw).parts
        if len(parts) >= 3 and raw.startswith('/'):
            tags['repo'].add(normalize_tag('/'.join(parts[:4])))
        elif len(parts) >= 2:
            tags['repo'].add(normalize_tag(parts[0]))
    for needle, tag in TOOL_TERMS.items():
        if needle in lowered:
            tags['tool'].add(tag)
    for needle, tag in DOMAIN_TERMS.items():
        if needle in lowered:
            tags['domain'].add(tag)
    task = first_meaningful_task(*texts)
    if task:
        tags['task'].add(normalize_tag(task))
    return {k: sorted(v) for k, v in tags.items() if v}


def first_meaningful_task(*texts: str) -> str:
    for text in texts:
        for line in (text or '').splitlines():
            cleaned = line.strip().strip('-*# ')
            if len(cleaned) < 12:
                continue
            if cleaned.lower().startswith(('[note:', '[system note:', 'review the conversation above')):
                continue
            return cleaned[:160]
    return ''


def tags_to_json(tags: Dict[str, Iterable[str]] | None) -> str:
    if not tags:
        return '{}'
    return json.dumps({k: sorted({normalize_tag(str(v)) for v in values if str(v).strip()}) for k, values in tags.items() if values}, sort_keys=True)


def tags_from_json(raw: str | None) -> Dict[str, List[str]]:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except Exception:
        return {}
    return {str(k): [normalize_tag(str(v)) for v in values] for k, values in data.items() if isinstance(values, list)}


def specificity(tags: Dict[str, Iterable[str]] | None) -> int:
    if not tags:
        return 0
    weights = {'file': 5, 'repo': 4, 'tool': 3, 'task': 3, 'domain': 2, 'scope_key': 1, 'global': 0}
    score = 0
    for kind, values in tags.items():
        score += weights.get(kind, 1) * len(list(values or []))
    return score


def scope_matches(rule_tags: Dict[str, List[str]], active_tags: Dict[str, List[str]]) -> bool:
    if not rule_tags or rule_tags.get('global'):
        return True
    matched_specific = False
    for kind, wanted in rule_tags.items():
        if kind == 'global':
            continue
        active = set(active_tags.get(kind, []))
        wanted_set = set(wanted or [])
        if not wanted_set:
            continue
        if kind == 'scope_key':
            if active and wanted_set.isdisjoint(active):
                return False
            continue
        if wanted_set.isdisjoint(active):
            return False
        matched_specific = True
    return matched_specific or bool(rule_tags.get('scope_key'))
