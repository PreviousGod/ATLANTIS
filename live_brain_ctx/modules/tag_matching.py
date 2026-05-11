"""Tag matching utilities for scope-based memory filtering."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from live_brain.scopes import extract_scope_tags, scope_matches, tags_from_json
except Exception:
    try:
        import importlib.util as _importlib_util
        _base_path = Path(__file__).resolve().parent.parent.parent / 'live_brain'
        _scopes_spec = _importlib_util.spec_from_file_location('_live_brain_ctx_scopes', _base_path / 'scopes.py')
        _scopes_mod = _importlib_util.module_from_spec(_scopes_spec)
        _scopes_spec.loader.exec_module(_scopes_mod)
        extract_scope_tags = _scopes_mod.extract_scope_tags
        scope_matches = _scopes_mod.scope_matches
        tags_from_json = _scopes_mod.tags_from_json
    except Exception:
        extract_scope_tags = None
        scope_matches = None
        tags_from_json = None


def _active_tags(user_message: str, scope_key: str) -> Dict[str, List[str]]:
    if extract_scope_tags:
        return extract_scope_tags(user_message, scope_key=scope_key)
    return {'scope_key': [scope_key]} if scope_key else {}


def _row_tags(row: sqlite3.Row) -> Dict[str, List[str]]:
    if not tags_from_json:
        return {}
    try:
        return tags_from_json(row['scope_tags_json'])
    except Exception:
        return {}


def _matches(row: sqlite3.Row, active_tags: Dict[str, List[str]], fallback_scope_key: str = '') -> bool:
    tags = _row_tags(row)
    try:
        row_scope = row['scope_key']
    except Exception:
        row_scope = ''

    # Require exact scope match - no fallback
    if row_scope and row_scope != fallback_scope_key:
        return False

    if tags and scope_matches:
        if scope_matches(tags, active_tags):
            return True
        # Check hard keys for conflict
        hard_keys = ('tool', 'repo', 'file', 'project', 'domain')
        for key in hard_keys:
            left = set(tags.get(key) or [])
            right = set(active_tags.get(key) or [])
            if left and right and left.isdisjoint(right):
                return False
        return True

    return row_scope == fallback_scope_key


def _causal_matches(row: sqlite3.Row, active_tags: Dict[str, List[str]], fallback_scope_key: str = '') -> bool:
    tags = _row_tags(row)
    if not tags:
        return _matches(row, active_tags, fallback_scope_key)
    relaxed = {k: v for k, v in tags.items() if k in ('scope_key', 'tool', 'domain', 'repo', 'file')}
    if relaxed and scope_matches:
        return scope_matches(relaxed, active_tags)
    return _matches(row, active_tags, fallback_scope_key)
