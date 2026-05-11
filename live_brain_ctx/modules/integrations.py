"""Integration helpers for Reality Engine and Epistemic Manager.

Extracted from the live_brain_ctx monolith. Provides loading, querying,
and recording functions for the reality and epistemic subsystems.
"""
from __future__ import annotations

import functools
import importlib.util
import json
import logging
import os
import re
import sqlite3
import sys
import time
import types
from pathlib import Path
from typing import Any, Dict, List
from urllib.parse import urlparse

from .query_filters import _is_chit_chat, _is_review_only_query
from .query_classification import _is_approval_query

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Local DB helpers (mirrors monolith)
# ---------------------------------------------------------------------------

def _hermes_home() -> str:
    return os.environ.get('HERMES_HOME', str(Path.home() / '.hermes'))


def _db_path() -> str:
    return str(Path(_hermes_home()) / 'live_brain' / 'live_brain.db')


def _get_connection():
    """Get a database connection."""
    return sqlite3.connect(_db_path(), timeout=5.0)


def _configure_ctx_sqlite(conn: sqlite3.Connection) -> None:
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA synchronous=NORMAL')
    conn.execute('PRAGMA busy_timeout=30000')
    conn.execute('PRAGMA temp_store=MEMORY')


# ---------------------------------------------------------------------------
# Reality Engine
# ---------------------------------------------------------------------------

@functools.lru_cache(maxsize=1)
def _load_reality_engine_class():
    try:
        from live_brain.reality import RealityEngine
        return RealityEngine
    except Exception:
        pass
    try:
        import importlib.util as _importlib_util
        import sys as _sys
        import types as _types
        package_name = '_live_brain_ctx_reality_pkg'
        live_brain_dir = Path(__file__).resolve().parent.parent.parent / 'live_brain'
        if not live_brain_dir.exists():
            # Try sibling in plugins dir
            live_brain_dir = Path(__file__).resolve().parent.parent.parent.parent / 'live_brain'
        if not live_brain_dir.exists():
            return None
        if package_name not in _sys.modules:
            package = _types.ModuleType(package_name)
            package.__path__ = [str(live_brain_dir)]
            _sys.modules[package_name] = package
        for module_name in ['utils', 'reality']:
            full_name = f'{package_name}.{module_name}'
            if full_name in _sys.modules:
                continue
            spec = _importlib_util.spec_from_file_location(full_name, live_brain_dir / f'{module_name}.py')
            if spec is None or spec.loader is None:
                return None
            module = _importlib_util.module_from_spec(spec)
            module.__package__ = package_name
            _sys.modules[full_name] = module
            spec.loader.exec_module(module)
        return _sys.modules[f'{package_name}.reality'].RealityEngine
    except Exception:
        return None


def _record_reality_event(scope_key: str, event_type: str, subject: str, payload: Dict[str, Any], *, session_id: str = '', source: str = 'live_brain_ctx', confidence: float = 0.75, created_at: float | None = None) -> dict:
    db_path = _db_path()
    if not Path(db_path).exists():
        return {}
    conn = None
    try:
        conn = _get_connection()
        conn.row_factory = sqlite3.Row
        _configure_ctx_sqlite(conn)
        RealityEngine = _load_reality_engine_class()
        if RealityEngine is None:
            return {}
        return RealityEngine(conn).ingest_event(
            scope_key=scope_key or session_id or 'global',
            event_type=event_type,
            subject=subject,
            payload=payload,
            session_id=session_id,
            source=source,
            confidence=confidence,
            created_at=created_at,
        )
    except Exception:
        try:
            if conn:
                conn.rollback()
        except Exception:
            pass
        return {}


def _load_reality_brief(scope_key: str, user_message: str) -> str:
    db_path = _db_path()
    if not Path(db_path).exists():
        return ''
    conn = None
    try:
        conn = _get_connection()
        conn.row_factory = sqlite3.Row
        RealityEngine = _load_reality_engine_class()
        return RealityEngine(conn).compile_brief(scope_key or 'global', user_message or '')
    except Exception:
        return ''


# ---------------------------------------------------------------------------
# Epistemic Manager
# ---------------------------------------------------------------------------

@functools.lru_cache(maxsize=1)
def _load_epistemic_manager_class():
    try:
        from live_brain.epistemic import EpistemicManager
        return EpistemicManager
    except Exception:
        pass
    try:
        import importlib.util as _importlib_util
        import sys as _sys
        package_name = '_live_brain_ctx_epistemic_pkg'
        live_brain_dir = Path(__file__).resolve().parent.parent.parent / 'live_brain'
        if not live_brain_dir.exists():
            live_brain_dir = Path(__file__).resolve().parent.parent.parent.parent / 'live_brain'
        if not live_brain_dir.exists():
            return None
        if package_name not in _sys.modules:
            import types as _types
            package = _types.ModuleType(package_name)
            package.__path__ = [str(live_brain_dir)]
            _sys.modules[package_name] = package
        for module_name in ['utils', 'epistemic']:
            full_name = f'{package_name}.{module_name}'
            if full_name in _sys.modules:
                continue
            spec = _importlib_util.spec_from_file_location(full_name, live_brain_dir / f'{module_name}.py')
            if spec is None or spec.loader is None:
                return None
            module = _importlib_util.module_from_spec(spec)
            module.__package__ = package_name
            _sys.modules[full_name] = module
            spec.loader.exec_module(module)
        return _sys.modules[f'{package_name}.epistemic'].EpistemicManager
    except Exception:
        return None


def _load_epistemic_brief(scope_key: str, user_message: str, session_id: str = '') -> str:
    db_path = _db_path()
    if not Path(db_path).exists():
        return ''
    if _is_chit_chat(user_message or '') or _is_review_only_query(user_message or '') or _is_approval_query(user_message or ''):
        return ''
    conn = None
    try:
        conn = _get_connection()
        conn.row_factory = sqlite3.Row
        EpistemicManager = _load_epistemic_manager_class()
        return EpistemicManager(conn, session_id=session_id, scope_key=scope_key or 'global').compile_brief(scope_key or 'global', user_message or '')
    except Exception:
        return ''


# ---------------------------------------------------------------------------
# URL / source helpers
# ---------------------------------------------------------------------------

_AUTHORITATIVE_EPISTEMIC_AUTHORITIES = {'official', 'primary_or_institutional', 'primary_or_support'}
_URL_RE = re.compile(r'https?://[^\s)\]}>,"\']+')
_UNVERIFIED_ANSWER_RE = re.compile(
    r"\b(?:ne\s+mogu\s+(?:da\s+)?(?:potvrdim|verifikujem)|nisam\s+prona|bez\s+izvora|"
    r"cannot\s+verify|could\s+not\s+verify|unable\s+to\s+verify|no\s+source|unverified)\b",
    re.IGNORECASE,
)


def _extract_urls_from_text(text: str) -> List[str]:
    urls: List[str] = []
    seen = set()
    for match in _URL_RE.findall(text or ''):
        clean = match.rstrip('.,;:!?')
        if clean and clean not in seen:
            seen.add(clean)
            urls.append(clean)
    return urls[:8]


def _epistemic_job_sources(conn: sqlite3.Connection, scope_key: str, job_id: str, *, limit: int = 6) -> List[Dict[str, Any]]:
    if not job_id:
        return []
    rows = conn.execute(
        """
        SELECT source_id, url, title, summary, authority, confidence, created_at
        FROM epistemic_web_sources
        WHERE scope_key=? AND job_id=? AND url!=''
        ORDER BY CASE WHEN authority='official' THEN 0 WHEN authority IN ('primary_or_institutional','primary_or_support') THEN 1 ELSE 2 END, confidence DESC, created_at DESC
        LIMIT ?
        """,
        (scope_key, job_id, int(limit)),
    ).fetchall()
    return [dict(row) for row in rows]


def _format_autonomous_research_context(search_result: Dict[str, Any], sources: List[Dict[str, Any]]) -> str:
    lines = [
        'AUTONOMOUS WEB RESEARCH:',
        '- Live Brain detected an unknown/current/high-stakes question and searched before the LLM call.',
    ]
    authoritative = [source for source in sources if source.get('authority') in _AUTHORITATIVE_EPISTEMIC_AUTHORITIES]
    chosen = authoritative or sources
    if chosen:
        for source in chosen[:4]:
            title = str(source.get('title') or '').strip()
            url = str(source.get('url') or '').strip()
            authority = str(source.get('authority') or 'unknown')
            confidence = float(source.get('confidence') or 0.0)
            label = f'{title} — {url}' if title else url
            lines.append(f'- Source: {label} ({authority}, confidence={confidence:.2f})')
    else:
        lines.append('- Search attempted, but no source was found.')
    status = str(search_result.get('status') or '')
    if status and status != 'sources_found':
        lines.append(f'- Research status: {status}; do not answer from stale memory or secondary-only evidence.')
    lines.append('- Safe rule: answer only from listed official/primary sources; if evidence is insufficient, call web_extract/web_search; do not answer from stale memory.')
    lines.append('- Evidence rule: if pages are discovered but not extracted, cite the URLs and say exact current values require the CME page/bulletin; do not invent numeric or contract-specific limits.')
    lines.append('- Persistence rule: after the final answer, Live Brain records source-backed facts automatically.')
    return '\n'.join(lines)


def _load_epistemic_autonomous_context(scope_key: str, user_message: str, session_id: str = '') -> str:
    if os.environ.get('LIVE_BRAIN_AUTONOMOUS_RESEARCH', '1') == '0':
        return ''
    db_path = _db_path()
    if not Path(db_path).exists() or _is_chit_chat(user_message or ''):
        return ''
    conn = None
    try:
        conn = _get_connection()
        conn.row_factory = sqlite3.Row
        EpistemicManager = _load_epistemic_manager_class()
        manager = EpistemicManager(conn, session_id=session_id, scope_key=scope_key or 'global')
        plan = manager.plan_if_needed(scope_key or 'global', user_message or '', session_id=session_id)
        if not plan.get('needs_research'):
            return ''
        job_id = str(plan.get('job_id') or manager.latest_job(scope_key or 'global', user_message or ''))
        existing_sources = _epistemic_job_sources(conn, scope_key or 'global', job_id, limit=4)
        if any(source.get('authority') in _AUTHORITATIVE_EPISTEMIC_AUTHORITIES for source in existing_sources):
            return _format_autonomous_research_context({'status': 'sources_found', 'job_id': job_id, 'discovery': 'cached'}, existing_sources)
        timeout = float(os.environ.get('LIVE_BRAIN_AUTONOMOUS_RESEARCH_TIMEOUT', '1.5'))
        max_queries = int(os.environ.get('LIVE_BRAIN_AUTONOMOUS_RESEARCH_MAX_QUERIES', '2'))
        result = manager.search_web(
            scope_key=scope_key or 'global',
            question=user_message or '',
            job_id=job_id,
            limit=4,
            max_queries=max_queries,
            timeout=timeout,
        )
        sources = list(result.get('authoritative_sources') or result.get('sources') or [])
        if not sources:
            sources = _epistemic_job_sources(conn, scope_key or 'global', job_id, limit=4)
        return _format_autonomous_research_context(result, sources)
    except Exception:
        return ''


def _record_epistemic_answer_if_source_backed(scope_key: str, user_message: str, assistant_response: str, session_id: str = '') -> None:
    if os.environ.get('LIVE_BRAIN_AUTONOMOUS_LEARNING', '1') == '0':
        return
    if not user_message or not assistant_response or _UNVERIFIED_ANSWER_RE.search(assistant_response):
        return
    db_path = _db_path()
    if not Path(db_path).exists() or _is_chit_chat(user_message or ''):
        return
    conn = None
    try:
        conn = _get_connection()
        conn.row_factory = sqlite3.Row
        EpistemicManager = _load_epistemic_manager_class()
        manager = EpistemicManager(conn, session_id=session_id, scope_key=scope_key or 'global')
        plan = manager.plan_if_needed(scope_key or 'global', user_message, session_id=session_id)
        job_id = str(plan.get('job_id') or manager.latest_job(scope_key or 'global', user_message))
        if not job_id:
            return
        job_sources = _epistemic_job_sources(conn, scope_key or 'global', job_id, limit=8)
        authoritative_sources = [source for source in job_sources if source.get('authority') in _AUTHORITATIVE_EPISTEMIC_AUTHORITIES]
        known_urls = [str(source.get('url') or '') for source in authoritative_sources if source.get('url')]
        answer_urls = _extract_urls_from_text(assistant_response)
        if answer_urls:
            known_domains = {url.split('/')[2].lower().removeprefix('www.') for url in known_urls if url.startswith('http') and len(url.split('/')) > 2}
            source_urls = []
            for url in answer_urls:
                parts = url.split('/')
                domain = parts[2].lower().removeprefix('www.') if len(parts) > 2 else ''
                if not known_domains or domain in known_domains or any(domain.endswith('.' + item) or item.endswith('.' + domain) for item in known_domains):
                    source_urls.append(url)
        else:
            answer_lower = assistant_response.lower()
            source_urls = [url for url in known_urls if url.split('/')[2].lower().removeprefix('www.') in answer_lower] if known_urls else []
        if not source_urls and authoritative_sources and re.search(r'\b(source|sources|izvor|izvori)\b', assistant_response, re.IGNORECASE):
            source_urls = known_urls[:3]
        if not source_urls:
            return
        ttl_seconds = int(plan.get('ttl_seconds') or 24 * 3600)
        confidence = 0.84 if answer_urls else 0.78
        fact_text = re.sub(r'\s+', ' ', assistant_response).strip()[:800]
        manager.record_fact(
            scope_key=scope_key or 'global',
            question=user_message,
            job_id=job_id,
            fact_text=fact_text,
            source_urls=source_urls[:5],
            confidence=confidence,
            ttl_seconds=ttl_seconds,
        )
    except Exception:
        try:
            if conn:
                conn.rollback()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Query classification helpers for integration context
# ---------------------------------------------------------------------------

def _should_load_reality_brief(user_message: str) -> bool:
    lowered = (user_message or '').strip().lower()
    if not lowered:
        return False
    if lowered in {'ok', 'da', 'ne', 'hmm', 'hm'}:
        return False
    if lowered in {'to', 'ovo', 'taj', 'ta', 'uradi to', 'a link', 'a link?'}:
        return True
    return not _is_chit_chat(user_message or '')


def _should_isolate_epistemic_context(user_message: str) -> bool:
    lowered = (user_message or '').strip().lower()
    if not lowered or _is_chit_chat(lowered):
        return False
    if 'live_brain_capability_e2e research' in lowered:
        return True
    current_terms = (
        'latest', 'current', 'today', 'now', 'najnovij', 'aktueln', 'trenutn',
        'danas', 'sada', 'sad', 'source url', 'authoritative', 'official source',
        'zvanič', 'zvanic', 'izvor', 'izvore',
    )
    # Split into trading terms and financial context to prevent false positives
    trading_terms = ('trading', 'futures', 'funded account')
    financial_context = ('price', 'limit', 'cme', 'nq', 'nasdaq', 'broker', 'margin', 'account', 'balance', 'rulebook', 'pravila')

    has_trading = any(term in lowered for term in trading_terms)
    has_financial_context = any(term in lowered for term in financial_context)
    has_current = any(term in lowered for term in current_terms)

    # Require both trading AND financial context to avoid SQL false positives
    return has_current and has_trading and has_financial_context


def _epistemic_query_text(user_message: str) -> str:
    text = user_message or ''
    text = re.sub(r'\bLIVE_BRAIN_CAPABILITY_E2E\s+research\s+run[-_][a-z0-9]+\s*:\s*', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\brun[-_][a-z0-9]+\b', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\bcodename[-_][a-z0-9]+\b', '', text, flags=re.IGNORECASE)
    return re.sub(r'\s+', ' ', text).strip() or (user_message or '')
