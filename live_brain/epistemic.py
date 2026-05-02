from __future__ import annotations

import hashlib
import html
import json
import re
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlparse

from .utils import stable_id
from .audit import ensure_schema as ensure_audit_schema, record_evidence_packet, record_revision, row_to_dict


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS epistemic_gaps (
    gap_id TEXT PRIMARY KEY,
    scope_key TEXT NOT NULL,
    question TEXT NOT NULL,
    normalized_question TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    reason TEXT NOT NULL DEFAULT '',
    priority REAL NOT NULL DEFAULT 0.6,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    resolved_at REAL
);
CREATE INDEX IF NOT EXISTS idx_epistemic_gaps_scope ON epistemic_gaps(scope_key, status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_epistemic_gaps_norm ON epistemic_gaps(normalized_question, status);

CREATE TABLE IF NOT EXISTS epistemic_research_jobs (
    job_id TEXT PRIMARY KEY,
    gap_id TEXT NOT NULL,
    scope_key TEXT NOT NULL,
    session_id TEXT NOT NULL DEFAULT '',
    question TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'needs_research',
    policy_json TEXT NOT NULL DEFAULT '{}',
    recommended_queries_json TEXT NOT NULL DEFAULT '[]',
    result_summary TEXT NOT NULL DEFAULT '',
    confidence REAL NOT NULL DEFAULT 0.0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    completed_at REAL
);
CREATE INDEX IF NOT EXISTS idx_epistemic_research_jobs_scope ON epistemic_research_jobs(scope_key, status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_epistemic_research_jobs_gap ON epistemic_research_jobs(gap_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS epistemic_web_sources (
    source_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL DEFAULT '',
    scope_key TEXT NOT NULL,
    url TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    source_kind TEXT NOT NULL DEFAULT 'web',
    authority TEXT NOT NULL DEFAULT 'unknown',
    summary TEXT NOT NULL DEFAULT '',
    raw_excerpt TEXT NOT NULL DEFAULT '',
    content_hash TEXT NOT NULL DEFAULT '',
    confidence REAL NOT NULL DEFAULT 0.5,
    extracted_at REAL NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_epistemic_web_sources_scope ON epistemic_web_sources(scope_key, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_epistemic_web_sources_job ON epistemic_web_sources(job_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_epistemic_web_sources_url ON epistemic_web_sources(url);

CREATE TABLE IF NOT EXISTS epistemic_learned_facts (
    fact_id TEXT PRIMARY KEY,
    scope_key TEXT NOT NULL,
    question TEXT NOT NULL DEFAULT '',
    fact_text TEXT NOT NULL,
    source_ids_json TEXT NOT NULL DEFAULT '[]',
    source_urls_json TEXT NOT NULL DEFAULT '[]',
    confidence REAL NOT NULL DEFAULT 0.7,
    source_kind TEXT NOT NULL DEFAULT 'web',
    authority TEXT NOT NULL DEFAULT 'unknown',
    status TEXT NOT NULL DEFAULT 'active',
    valid_from REAL NOT NULL,
    expires_at REAL,
    evidence_packet_id TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_epistemic_learned_facts_scope ON epistemic_learned_facts(scope_key, status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_epistemic_learned_facts_expiry ON epistemic_learned_facts(expires_at);
"""

_TEMPORAL_RE = re.compile(
    r"\b(latest|current|today|now|202\d|najnovij\w*|aktueln\w*|trenutn\w*|danas|sada|sad|"
    r"pravila|rules?|regulation|law|zakon|cena|price|pricing|version|api|docs?|documentation|"
    r"CEO|president|calendar|schedule|news|monetiz\w*|algorithm)\b",
    re.IGNORECASE,
)
_EXPLICIT_RESEARCH_RE = re.compile(
    r"\b(search|research|look\s*up|verify|check\s+online|internet|web|source|official|"
    r"proveri|provjeri|istrazi|istraži|nauci|nauči|ako\s+ne\s+zna|ne\s+znaš|ne\s+znas)\b",
    re.IGNORECASE,
)
_HIGH_STAKES_RE = re.compile(
    r"\b(funded\s+account|prop\s*firm|trejd\w*|trading|trade\w*|broker|forex|futures|stocks?|"
    r"financial|finance|invest\w*|tax|legal|medical|health|contract|compliance|risk|"
    r"ftmo|topstep|apex|alpaca|interactive\s+brokers|tradovate|cme|nq|es|mnq|mes)\b",
    re.IGNORECASE,
)
_NUMERIC_CLAIM_RE = re.compile(
    r"(?:\d+(?:\.\d+)?\s*(?:%|percent|procenata|poena|points?|ticks?|bps)|"
    r"\b\d+\s*(?:→|->)\s*\d+\b)",
    re.IGNORECASE,
)

_STOP_WORDS = {
    'what', 'which', 'when', 'where', 'kako', 'koji', 'koja', 'koje', 'sta', 'šta', 'zasto', 'zašto',
    'mozes', 'možeš', 'treba', 'hocu', 'hoću', 'meni', 'nama', 'sada', 'danas', 'this', 'that', 'with',
    'internet', 'official', 'source', 'sources',
}
_OFFICIAL_DOMAINS = {
    'ftmo.com', 'topstep.com', 'apextraderfunding.com', 'alpaca.markets', 'interactivebrokers.com',
    'tradovate.com', 'ninjatrader.com', 'cmegroup.com', 'nasdaq.com', 'nyse.com', 'sec.gov', 'cftc.gov',
    'nfa.futures.org', 'irs.gov', 'ftc.gov', 'fda.gov', 'nih.gov', 'openai.com', 'docs.python.org',
    'youtube.com', 'support.google.com', 'developers.google.com', 'google.com',
}

_OFFICIAL_SOURCE_CANDIDATES = {
    'apex': [
        ('https://support.apextraderfunding.com/hc/en-us/articles/47257193113371-Daily-Loss-Limit-Explained', 'Apex Trader Funding Daily Loss Limit Explained', 'Official Apex support page about Daily Loss Limit behavior.'),
        ('https://support.apextraderfunding.com/hc/en-us/articles/4404875002139-What-are-the-Consistency-Rules-For-PA-and-Funded-Accounts', 'Apex PA/Funded Account Consistency Rules', 'Official Apex support page about PA and funded account consistency rules.'),
        ('https://support.apextraderfunding.com/hc/en-us/articles/40507212951451-PA-Payout-Parameters', 'Apex PA Payout Parameters', 'Official Apex support page about payout parameters.'),
        ('https://apextraderfunding.com/member', 'Apex Trader Funding Members Area', 'Official Apex site; some current rules may require member/login access.'),
    ],
    'ftmo': [
        ('https://ftmo.com/en/trading-objectives/', 'FTMO Trading Objectives', 'Official FTMO page covering Maximum Daily Loss and Maximum Loss objectives.'),
    ],
    'topstep': [
        ('https://help.topstep.com/en/articles/10490293-topstepx-trailing-personal-daily-loss-limit', 'TopstepX Daily Loss Limit', 'Official Topstep help page covering daily loss limit behavior.'),
    ],
    'youtube': [
        ('https://support.google.com/youtube/answer/72851', 'YouTube Partner Program overview', 'Official YouTube Help page for monetization eligibility.'),
        ('https://support.google.com/youtube/answer/12504220', 'YouTube Shorts monetization policies', 'Official YouTube Help page for Shorts monetization/revenue sharing.'),
    ],
}

_AUTHORITATIVE_AUTHORITIES = {'official', 'primary_or_institutional', 'primary_or_support'}


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def loads(raw: Any, default: Any) -> Any:
    if raw in (None, ''):
        return default
    if not isinstance(raw, str):
        return raw
    try:
        return json.loads(raw)
    except Exception:
        return default


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def normalize_question(question: str) -> str:
    words = [w.lower() for w in re.findall(r"[\w.-]+", question or '') if len(w) > 2]
    words = [w for w in words if w not in _STOP_WORDS]
    return ' '.join(words[:18]) or (question or '').strip().lower()[:160]


def query_words(text: str) -> List[str]:
    return [w.lower() for w in re.findall(r"[\w.-]+", text or '') if len(w) > 3 and w.lower() not in _STOP_WORDS]


def domain_for_url(url: str) -> str:
    try:
        host = urlparse(url).netloc.lower()
    except Exception:
        host = ''
    if host.startswith('www.'):
        host = host[4:]
    return host


def entity_terms(text: str) -> List[str]:
    lowered = (text or '').lower()
    stop = _STOP_WORDS | {'funded', 'account', 'accounts', 'pravila', 'rules', 'rule', 'trading', 'trade', 'trejdujes', 'trejduješ', 'najnovija', 'latest', 'current', 'futures', 'future', 'stocks', 'stock', 'forex', 'broker', 'price', 'limit', 'limits', 'halt', 'halts', 'official', 'documentation', 'docs', 'risk', 'disclosure'}
    raw = [w for w in re.findall(r"[a-zA-Z][a-zA-Z0-9-]{2,}", lowered) if w not in stop]
    phrases = []
    if 'interactive brokers' in lowered:
        phrases.append('interactivebrokers')
    if 'apex trader funding' in lowered:
        phrases.append('apextraderfunding')
    if 'topstep' in lowered:
        phrases.append('topstep')
    if 'ftmo' in lowered:
        phrases.append('ftmo')
    terms: List[str] = []
    for item in phrases + raw:
        normalized = re.sub(r'[^a-z0-9]+', '', item.lower())
        if len(normalized) >= 3 and normalized not in terms:
            terms.append(normalized)
    return terms[:8]


def authority_for_url(url: str, context: str = '') -> str:
    domain = domain_for_url(url)
    if not domain:
        return 'unknown'
    if domain.endswith('.gov') or domain in _OFFICIAL_DOMAINS or any(domain == d or domain.endswith('.' + d) for d in _OFFICIAL_DOMAINS):
        return 'official'
    compact_domain = re.sub(r'[^a-z0-9]+', '', domain)
    if context:
        for term in entity_terms(context):
            hosted_or_social = (
                'reddit.', 'wikipedia.', 'youtube.', 'x.com', 'twitter.', 'facebook.', 'linkedin.',
                'blog.', 'medium.com', 'substack.com',
                'atlassian.net', 'notion.site', 'github.io', 'gitbook.io', 'readme.io', 'zendesk.com',
            )
            if len(term) >= 4 and term in compact_domain and not any(bad in domain for bad in hosted_or_social):
                return 'official'
    if domain.endswith('.edu') or domain.endswith('.org'):
        return 'primary_or_institutional'
    if any(token in domain for token in ('docs.', 'support.', 'help.', 'developer', 'api.')):
        return 'primary_or_support'
    return 'secondary'


def source_confidence(authority: str, base: float = 0.6) -> float:
    if authority == 'official':
        return max(base, 0.86)
    if authority in {'primary_or_institutional', 'primary_or_support'}:
        return max(base, 0.76)
    if authority == 'secondary':
        return max(base, 0.58)
    return base


_PROVIDER_TERMS = {'ftmo', 'topstep', 'apex', 'alpaca', 'interactivebrokers', 'interactive', 'tradovate', 'ninjatrader', 'cme'}


def provider_terms(text: str) -> List[str]:
    lowered = (text or '').lower()
    terms = set(entity_terms(text)) & _PROVIDER_TERMS
    if 'interactive brokers' in lowered or 'interactivebrokers' in lowered:
        terms.add('interactive')
    return sorted(terms)


def _source_tokens_for_provider(term: str) -> List[str]:
    aliases = {
        'apex': ['apex', 'apextraderfunding'],
        'interactive': ['interactivebrokers', 'interactive-brokers'],
        'interactivebrokers': ['interactivebrokers', 'interactive-brokers'],
        'cme': ['cme', 'cmegroup'],
    }
    return aliases.get(term, [term])


def sources_match_required_providers(question: str, source_urls: Iterable[str]) -> bool:
    required = provider_terms(question)
    if not required:
        return True
    domains = [domain_for_url(str(url or '')) for url in source_urls]
    haystack = ' '.join(domain.lower() for domain in domains if domain)
    compact = re.sub(r'[^a-z0-9]+', '', haystack)
    for term in required:
        if any(alias.replace('-', '') in compact or alias in haystack for alias in _source_tokens_for_provider(term)):
            return True
    return False


def overlap_score(text: str, words: Iterable[str]) -> float:
    words = list(words)
    if not words:
        return 0.0
    lowered = (text or '').lower()
    hits = sum(1 for w in words if w in lowered)
    return hits / max(len(words), 1)



def candidate_sources_for_question(question: str) -> List[Tuple[str, str, str]]:
    # Legacy bootstrap fallback only. Generic web discovery is the primary path.
    lowered = (question or '').lower()
    candidates: List[Tuple[str, str, str]] = []
    for key, rows in _OFFICIAL_SOURCE_CANDIDATES.items():
        if key in lowered:
            candidates.extend(rows)
    return candidates


def _decode_ddg_url(url: str) -> str:
    try:
        parsed = urllib.parse.urlparse(url)
        qs = urllib.parse.parse_qs(parsed.query)
        if 'uddg' in qs and qs['uddg']:
            return qs['uddg'][0]
    except Exception:
        pass
    return url


def ddg_instant_sources(question: str, *, timeout: float = 8.0) -> List[Tuple[str, str, str]]:
    query = urllib.parse.urlencode({'q': question, 'format': 'json', 'no_redirect': '1', 'no_html': '1'})
    url = f'https://api.duckduckgo.com/?{query}'
    request = urllib.request.Request(url, headers={'User-Agent': 'LiveBrainEpistemic/1.0'})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode('utf-8', 'replace'))
    except Exception:
        return []
    sources: List[Tuple[str, str, str]] = []
    if data.get('AbstractURL'):
        sources.append((str(data.get('AbstractURL')), str(data.get('Heading') or ''), str(data.get('AbstractText') or '')))
    def walk(items: Any) -> None:
        if not isinstance(items, list):
            return
        for item in items:
            if not isinstance(item, dict):
                continue
            if item.get('FirstURL'):
                sources.append((str(item.get('FirstURL')), str(item.get('Text') or '')[:120], str(item.get('Text') or '')))
            if isinstance(item.get('Topics'), list):
                walk(item.get('Topics'))
    walk(data.get('RelatedTopics'))
    return dedupe_sources(sources)


def ddg_html_sources(query_text: str, *, timeout: float = 10.0, limit: int = 10) -> List[Tuple[str, str, str]]:
    query = urllib.parse.urlencode({'q': query_text})
    request = urllib.request.Request(
        f'https://html.duckduckgo.com/html/?{query}',
        headers={'User-Agent': 'Mozilla/5.0 LiveBrainEpistemic/1.0'},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode('utf-8', 'replace')
    except Exception:
        return []
    sources: List[Tuple[str, str, str]] = []
    pattern = re.compile(r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', re.I | re.S)
    for match in pattern.finditer(body):
        url = html.unescape(_decode_ddg_url(match.group(1)))
        title = re.sub(r'<[^>]+>', ' ', match.group(2))
        title = re.sub(r'\s+', ' ', html.unescape(title)).strip()
        if url.startswith('http'):
            sources.append((url, title, title))
        if len(sources) >= limit:
            break
    return dedupe_sources(sources)


def dedupe_sources(sources: List[Tuple[str, str, str]]) -> List[Tuple[str, str, str]]:
    deduped: List[Tuple[str, str, str]] = []
    seen = set()
    for url, title, summary in sources:
        if not url or url in seen:
            continue
        seen.add(url)
        deduped.append((url, title, summary))
    return deduped


def discover_sources(
    question: str,
    queries: Optional[List[str]] = None,
    *,
    limit: int = 8,
    max_queries: int = 4,
    timeout: float = 6.0,
) -> List[Tuple[str, str, str, str, float]]:
    raw_sources: List[Tuple[str, str, str]] = []
    query_limit = max(1, int(max_queries or 1))
    request_timeout = max(0.5, float(timeout or 1.0))
    for query_text in dedupe_query_list([question] + list(queries or []))[:query_limit]:
        raw_sources.extend(ddg_html_sources(query_text, timeout=request_timeout, limit=limit))
        raw_sources.extend(ddg_instant_sources(query_text, timeout=request_timeout))
    scored: List[Tuple[float, Tuple[str, str, str, str, float]]] = []
    words = query_words(question)
    for url, title, summary in dedupe_sources(raw_sources):
        authority = authority_for_url(url, question)
        provider_match = sources_match_required_providers(question, [url])
        authority_bonus = {'official': 1.0, 'primary_or_support': 0.78, 'primary_or_institutional': 0.7, 'secondary': 0.35}.get(authority, 0.15)
        if provider_terms(question) and not provider_match:
            authority_bonus = min(authority_bonus, 0.42)
        relevance = overlap_score(f'{url} {title} {summary}', words)
        score = authority_bonus + relevance
        confidence = source_confidence(authority, 0.62 if authority == 'official' else 0.5)
        if provider_terms(question) and not provider_match:
            confidence = min(confidence, 0.62)
        scored.append((score, (url, title, summary, authority, confidence)))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [item for _, item in scored[:limit]]


def dedupe_query_list(items: List[str]) -> List[str]:
    out: List[str] = []
    seen = set()
    for item in items:
        clean = re.sub(r'\s+', ' ', str(item or '')).strip()
        key = clean.lower()
        if clean and key not in seen:
            seen.add(key)
            out.append(clean)
    return out


@dataclass
class EpistemicClassification:
    should_research: bool
    reason: str
    priority: float
    ttl_seconds: int
    source_policy: Dict[str, Any]
    recommended_queries: List[str]


class EpistemicManager:
    def __init__(self, conn, ingestor=None, session_id: str = '', scope_key: str = ''):
        self.conn = conn
        self.ingestor = ingestor
        self.session_id = session_id
        self.scope_key = scope_key

    def ensure_schema(self) -> None:
        self.conn.executescript(SCHEMA_SQL)
        ensure_audit_schema(self.conn)

    def cleanup_expired(self, now: Optional[float] = None) -> int:
        self.ensure_schema()
        now = float(now or time.time())
        rows = self.conn.execute(
            "SELECT * FROM epistemic_learned_facts WHERE status='active' AND expires_at IS NOT NULL AND expires_at <= ?",
            (now,),
        ).fetchall()
        for row in rows:
            before = row_to_dict(row)
            self.conn.execute(
                "UPDATE epistemic_learned_facts SET status='expired', updated_at=? WHERE fact_id=?",
                (now, row['fact_id']),
            )
            after = row_to_dict(self.conn.execute("SELECT * FROM epistemic_learned_facts WHERE fact_id=?", (row['fact_id'],)).fetchone())
            record_revision(self.conn, object_type='epistemic_learned_fact', object_id=row['fact_id'], action='expire', reason='validity_window_elapsed', before=before, after=after, created_at=now)
        self.conn.commit()
        return len(rows)

    def classify(self, question: str) -> EpistemicClassification:
        q = (question or '').strip()
        lowered = q.lower()
        explicit = bool(_EXPLICIT_RESEARCH_RE.search(q))
        temporal = bool(_TEMPORAL_RE.search(q))
        high_stakes = bool(_HIGH_STAKES_RE.search(q))
        should = bool(q and (explicit or temporal or high_stakes))
        reasons = []
        if explicit:
            reasons.append('explicit_research_or_unknown')
        if temporal:
            reasons.append('current_or_changeable_fact')
        if high_stakes:
            reasons.append('high_stakes_domain')
        priority = 0.72
        ttl_seconds = 7 * 86400
        required_authority = 'authoritative'
        min_sources = 2
        if high_stakes:
            priority = 0.94
            ttl_seconds = 24 * 3600
            required_authority = 'official_or_primary'
            min_sources = 2
        elif temporal:
            priority = 0.84
            ttl_seconds = 48 * 3600
        if 'funded' in lowered or 'prop' in lowered or any(term in lowered for term in ('ftmo', 'topstep', 'apex')):
            queries = [
                f"{q} official rules",
                f"{q} official help support",
                f"{q} daily loss limit payout rules official",
            ]
        elif any(term in lowered for term in ('trading', 'trade', 'trejd', 'forex', 'futures', 'stocks', 'broker')):
            queries = [
                f"{q} official documentation",
                f"{q} regulator official risk disclosure",
                f"{q} exchange broker official rules",
            ]
        elif 'youtube' in lowered or 'shorts' in lowered or 'monetiz' in lowered:
            queries = [
                f"{q} official",
                "YouTube Shorts monetization requirements official help",
                "YouTube Partner Program Shorts revenue sharing official",
            ]
        else:
            queries = [f"{q} official docs", q]
        return EpistemicClassification(
            should_research=should,
            reason='+'.join(reasons) if reasons else 'no_external_research_trigger',
            priority=priority,
            ttl_seconds=ttl_seconds,
            source_policy={
                'prefer': ['official docs', 'primary sources', 'recent authoritative sources'],
                'avoid': ['unsourced blogs', 'forums unless used only as anecdotal evidence'],
                'required_authority': required_authority,
                'min_sources': min_sources,
                'record_with': 'brain_epistemic(action=record_fact) after web_search/web_extract',
            },
            recommended_queries=queries[:4],
        )

    def recall_facts(self, scope_key: str, question: str, *, limit: int = 5) -> List[Dict[str, Any]]:
        self.ensure_schema()
        self.cleanup_expired()
        scope_key = scope_key or self.scope_key or 'global'
        words = query_words(question)
        required_providers = provider_terms(question)
        rows = self.conn.execute(
            """
            SELECT fact_id, question, fact_text, source_urls_json, confidence, authority, expires_at, updated_at
            FROM epistemic_learned_facts
            WHERE status='active' AND scope_key IN (?, 'global') AND (expires_at IS NULL OR expires_at > ?)
            ORDER BY CASE WHEN scope_key=? THEN 0 ELSE 1 END, confidence DESC, updated_at DESC
            LIMIT 40
            """,
            (scope_key, time.time(), scope_key),
        ).fetchall()
        scored: List[Tuple[float, Dict[str, Any]]] = []
        seen_facts = set()
        for row in rows:
            fact_text = str(row['fact_text'] or '')
            fact_key = re.sub(r'\s+', ' ', fact_text.strip().lower())[:500]
            if fact_key in seen_facts:
                continue
            seen_facts.add(fact_key)
            urls = ' '.join(loads(row['source_urls_json'], []))
            searchable = f"{fact_text} {row['question'] or ''} {urls}"
            if required_providers and not any(term in searchable.lower() for term in required_providers):
                continue
            score = overlap_score(searchable, words)
            if words and score <= 0:
                score = overlap_score(urls, words) * 0.7
            if words and score <= 0:
                continue
            item = dict(row)
            item['source_urls'] = loads(row['source_urls_json'], [])
            scored.append((score + float(row['confidence'] or 0) * 0.2, item))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [item for _, item in scored[:limit]]

    def plan_if_needed(self, scope_key: str, question: str, *, session_id: str = '') -> Dict[str, Any]:
        self.ensure_schema()
        self.cleanup_expired()
        scope_key = scope_key or self.scope_key or 'global'
        session_id = session_id or self.session_id or ''
        classification = self.classify(question)
        facts = self.recall_facts(scope_key, question)
        if not classification.should_research or facts:
            return {
                'needs_research': False,
                'reason': classification.reason,
                'fresh_facts': facts,
                'classification': classification.__dict__,
            }
        now = time.time()
        normalized = normalize_question(question)
        gap_id = stable_id('knowledge_gap', scope_key, normalized)
        job_id = stable_id('research_job', scope_key, normalized, classification.reason)
        existing = self.conn.execute("SELECT status FROM epistemic_research_jobs WHERE job_id=?", (job_id,)).fetchone()
        self.conn.execute(
            """
            INSERT OR REPLACE INTO epistemic_gaps
            (gap_id, scope_key, question, normalized_question, status, reason, priority, created_at, updated_at, resolved_at)
            VALUES (?, ?, ?, ?, COALESCE((SELECT status FROM epistemic_gaps WHERE gap_id=?), 'open'), ?, ?, COALESCE((SELECT created_at FROM epistemic_gaps WHERE gap_id=?), ?), ?, NULL)
            """,
            (gap_id, scope_key, question[:500], normalized, gap_id, classification.reason, classification.priority, gap_id, now, now),
        )
        status = 'needs_research'
        if existing and str(existing['status']) in {'researching', 'resolved'}:
            status = str(existing['status'])
        self.conn.execute(
            """
            INSERT OR REPLACE INTO epistemic_research_jobs
            (job_id, gap_id, scope_key, session_id, question, status, policy_json, recommended_queries_json, result_summary, confidence, created_at, updated_at, completed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, COALESCE((SELECT result_summary FROM epistemic_research_jobs WHERE job_id=?), ''), COALESCE((SELECT confidence FROM epistemic_research_jobs WHERE job_id=?), 0.0), COALESCE((SELECT created_at FROM epistemic_research_jobs WHERE job_id=?), ?), ?, COALESCE((SELECT completed_at FROM epistemic_research_jobs WHERE job_id=?), NULL))
            """,
            (
                job_id,
                gap_id,
                scope_key,
                session_id,
                question[:500],
                status,
                dumps(classification.source_policy),
                dumps(classification.recommended_queries),
                job_id,
                job_id,
                job_id,
                now,
                now,
                job_id,
            ),
        )
        self.conn.commit()
        return {
            'needs_research': True,
            'gap_id': gap_id,
            'job_id': job_id,
            'question': question,
            'reason': classification.reason,
            'priority': classification.priority,
            'ttl_seconds': classification.ttl_seconds,
            'source_policy': classification.source_policy,
            'recommended_queries': classification.recommended_queries,
            'fresh_facts': [],
        }

    def latest_job(self, scope_key: str, question: str = '') -> str:
        self.ensure_schema()
        scope_key = scope_key or self.scope_key or 'global'
        words = query_words(question)
        rows = self.conn.execute(
            "SELECT job_id, question, updated_at FROM epistemic_research_jobs WHERE scope_key=? AND status IN ('needs_research','researching') ORDER BY updated_at DESC LIMIT 12",
            (scope_key,),
        ).fetchall()
        if not rows:
            return ''
        if not words:
            return str(rows[0]['job_id'])
        best = sorted(rows, key=lambda row: overlap_score(str(row['question'] or ''), words), reverse=True)[0]
        return str(best['job_id'])

    def record_source(
        self,
        *,
        scope_key: str,
        job_id: str = '',
        url: str = '',
        title: str = '',
        source_kind: str = 'web',
        summary: str = '',
        raw_excerpt: str = '',
        confidence: float = 0.6,
        question: str = '',
    ) -> Dict[str, Any]:
        self.ensure_schema()
        scope_key = scope_key or self.scope_key or 'global'
        job_id = job_id or self.latest_job(scope_key, summary or title or url)
        authority_context = question or f'{title} {summary} {raw_excerpt}'
        authority = authority_for_url(url, authority_context)
        confidence = source_confidence(authority, clamp(float(confidence)))
        now = time.time()
        content = f'{url}\n{title}\n{summary}\n{raw_excerpt}'
        content_hash = hashlib.sha256(content.encode('utf-8', 'ignore')).hexdigest()[:24]
        source_id = stable_id('web_source', scope_key, job_id, url or title, content_hash)
        self.conn.execute(
            """
            INSERT OR REPLACE INTO epistemic_web_sources
            (source_id, job_id, scope_key, url, title, source_kind, authority, summary, raw_excerpt, content_hash, confidence, extracted_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source_id,
                job_id,
                scope_key,
                url[:1000],
                title[:300],
                source_kind[:80],
                authority,
                summary[:1200],
                raw_excerpt[:2000],
                content_hash,
                confidence,
                now,
                now,
            ),
        )
        if job_id:
            self.conn.execute("UPDATE epistemic_research_jobs SET status='researching', updated_at=? WHERE job_id=? AND status!='resolved'", (now, job_id))
        self.conn.commit()
        return {
            'source_id': source_id,
            'job_id': job_id,
            'authority': authority,
            'confidence': confidence,
            'url': url,
            'title': title,
            'summary': summary,
            'source_kind': source_kind,
        }

    def record_fact(
        self,
        *,
        scope_key: str,
        fact_text: str,
        question: str = '',
        job_id: str = '',
        source_urls: Optional[List[str]] = None,
        source_ids: Optional[List[str]] = None,
        source_kind: str = 'web',
        confidence: float = 0.75,
        ttl_seconds: Optional[int] = None,
        raw_excerpt: str = '',
    ) -> Dict[str, Any]:
        self.ensure_schema()
        fact_text = (fact_text or '').strip()
        if not fact_text:
            return {'error': 'fact_text is required'}
        scope_key = scope_key or self.scope_key or 'global'
        job_id = job_id or self.latest_job(scope_key, question or fact_text)
        source_urls = source_urls or []
        source_ids = source_ids or []
        if not source_urls and job_id:
            rows = self.conn.execute("SELECT source_id, url FROM epistemic_web_sources WHERE job_id=? ORDER BY confidence DESC, created_at DESC LIMIT 5", (job_id,)).fetchall()
            source_ids = source_ids or [str(row['source_id']) for row in rows]
            source_urls = [str(row['url']) for row in rows if row['url']]
        authorities = [authority_for_url(url, question or fact_text) for url in source_urls]
        authority = 'official' if 'official' in authorities else (authorities[0] if authorities else 'unknown')
        confidence = source_confidence(authority, clamp(float(confidence))) if source_urls else clamp(float(confidence))
        now = time.time()
        classification = self.classify(question or fact_text)
        if source_kind == 'web' and classification.should_research and source_urls:
            valid_pairs = [
                (url, auth) for url, auth in zip(source_urls, authorities)
                if auth in _AUTHORITATIVE_AUTHORITIES and sources_match_required_providers(question or fact_text, [url])
            ]
            if classification.source_policy.get('required_authority') == 'official_or_primary':
                source_urls = [url for url, _ in valid_pairs]
                authorities = [auth for _, auth in valid_pairs]
                source_ids = source_ids[:len(source_urls)] if source_ids else source_ids
                authority = 'official' if 'official' in authorities else (authorities[0] if authorities else 'unknown')
                confidence = source_confidence(authority, clamp(float(confidence))) if source_urls else clamp(float(confidence))
            if not sources_match_required_providers(question or fact_text, source_urls):
                if job_id:
                    self.conn.execute(
                        "UPDATE epistemic_research_jobs SET status='needs_research', result_summary=?, confidence=?, updated_at=? WHERE job_id=?",
                        ('source URLs do not match the requested provider/entity', confidence, now, job_id),
                    )
                    self.conn.commit()
                return {
                    'status': 'not_recorded_provider_mismatch',
                    'job_id': job_id,
                    'confidence': confidence,
                    'authority': authority,
                    'reason': 'source URLs do not match the requested provider/entity',
                }
            if classification.source_policy.get('required_authority') == 'official_or_primary' and not any(item in _AUTHORITATIVE_AUTHORITIES for item in authorities):
                if job_id:
                    self.conn.execute(
                        "UPDATE epistemic_research_jobs SET status='needs_research', result_summary=?, confidence=?, updated_at=? WHERE job_id=?",
                        ('authoritative source required for high-stakes fact', confidence, now, job_id),
                    )
                    self.conn.commit()
                return {
                    'status': 'not_recorded_insufficient_authority',
                    'job_id': job_id,
                    'confidence': confidence,
                    'authority': authority,
                    'reason': 'high-stakes learned facts require official or primary sources',
                }
            if _NUMERIC_CLAIM_RE.search(fact_text) and len((raw_excerpt or '').strip()) < 80:
                if job_id:
                    self.conn.execute(
                        "UPDATE epistemic_research_jobs SET status='needs_research', result_summary=?, confidence=?, updated_at=? WHERE job_id=?",
                        ('numeric high-stakes claim requires extracted evidence/raw_excerpt', confidence, now, job_id),
                    )
                    self.conn.commit()
                return {
                    'status': 'not_recorded_needs_extracted_evidence',
                    'job_id': job_id,
                    'confidence': confidence,
                    'authority': authority,
                    'reason': 'numeric high-stakes claims require extracted source evidence/raw_excerpt, not search result titles only',
                }
        if ttl_seconds is None and classification.should_research:
            ttl_seconds = classification.ttl_seconds
        if confidence < 0.5 or (not source_urls and source_kind == 'web'):
            if job_id:
                self.conn.execute(
                    "UPDATE epistemic_research_jobs SET status='needs_research', result_summary=?, confidence=?, updated_at=? WHERE job_id=?",
                    (fact_text[:1000], confidence, now, job_id),
                )
                self.conn.commit()
            return {
                'status': 'not_recorded_low_confidence',
                'job_id': job_id,
                'confidence': confidence,
                'authority': authority,
                'reason': 'learned facts require confidence >= 0.5 and at least one source URL',
            }
        expires_at = now + int(ttl_seconds) if ttl_seconds else None
        fact_id = stable_id('learned_fact', scope_key, normalize_question(question or fact_text), fact_text, ','.join(source_urls[:3]))
        evidence_packet_id = record_evidence_packet(
            self.conn,
            scope_key=scope_key,
            object_type='epistemic_learned_fact',
            object_id=fact_id,
            claim=fact_text,
            source_urls=source_urls,
            source_ids=source_ids,
            authority=authority,
            raw_excerpt=raw_excerpt,
            confidence=confidence,
            valid_until=expires_at,
            source_kind=source_kind,
            created_at=now,
        )
        before = row_to_dict(self.conn.execute("SELECT * FROM epistemic_learned_facts WHERE fact_id=?", (fact_id,)).fetchone())
        self.conn.execute(
            """
            INSERT OR REPLACE INTO epistemic_learned_facts
            (fact_id, scope_key, question, fact_text, source_ids_json, source_urls_json, confidence, source_kind, authority, status, valid_from, expires_at, evidence_packet_id, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, COALESCE((SELECT created_at FROM epistemic_learned_facts WHERE fact_id=?), ?), ?)
            """,
            (fact_id, scope_key, question[:500], fact_text[:800], dumps(source_ids), dumps(source_urls), confidence, source_kind, authority, now, expires_at, evidence_packet_id, fact_id, now, now),
        )
        after = row_to_dict(self.conn.execute("SELECT * FROM epistemic_learned_facts WHERE fact_id=?", (fact_id,)).fetchone())
        record_revision(self.conn, object_type='epistemic_learned_fact', object_id=fact_id, action='record_fact', reason=authority, before=before, after=after, created_at=now)
        mirrored_global = False
        if scope_key != 'global' and source_urls and authority in {'official', 'primary_or_institutional', 'primary_or_support'} and confidence >= 0.75:
            global_fact_id = stable_id('learned_fact', 'global', normalize_question(question or fact_text), fact_text, ','.join(source_urls[:3]))
            global_evidence_packet_id = record_evidence_packet(
                self.conn,
                scope_key='global',
                object_type='epistemic_learned_fact',
                object_id=global_fact_id,
                claim=fact_text,
                source_urls=source_urls,
                source_ids=source_ids,
                authority=authority,
                raw_excerpt=raw_excerpt,
                confidence=confidence,
                valid_until=expires_at,
                source_kind=source_kind,
                created_at=now,
            )
            before_global = row_to_dict(self.conn.execute("SELECT * FROM epistemic_learned_facts WHERE fact_id=?", (global_fact_id,)).fetchone())
            self.conn.execute(
                """
                INSERT OR REPLACE INTO epistemic_learned_facts
                (fact_id, scope_key, question, fact_text, source_ids_json, source_urls_json, confidence, source_kind, authority, status, valid_from, expires_at, evidence_packet_id, created_at, updated_at)
                VALUES (?, 'global', ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, COALESCE((SELECT created_at FROM epistemic_learned_facts WHERE fact_id=?), ?), ?)
                """,
                (global_fact_id, question[:500], fact_text[:800], dumps(source_ids), dumps(source_urls), confidence, source_kind, authority, now, expires_at, global_evidence_packet_id, global_fact_id, now, now),
            )
            after_global = row_to_dict(self.conn.execute("SELECT * FROM epistemic_learned_facts WHERE fact_id=?", (global_fact_id,)).fetchone())
            record_revision(self.conn, object_type='epistemic_learned_fact', object_id=global_fact_id, action='record_fact', reason=f'global_mirror:{authority}', before=before_global, after=after_global, created_at=now)
            mirrored_global = True
        if job_id:
            self.conn.execute(
                "UPDATE epistemic_research_jobs SET status='resolved', result_summary=?, confidence=?, updated_at=?, completed_at=? WHERE job_id=?",
                (fact_text[:1000], confidence, now, now, job_id),
            )
            row = self.conn.execute("SELECT gap_id FROM epistemic_research_jobs WHERE job_id=?", (job_id,)).fetchone()
            if row:
                self.conn.execute("UPDATE epistemic_gaps SET status='resolved', updated_at=?, resolved_at=? WHERE gap_id=?", (now, now, row['gap_id']))
        if self.ingestor and confidence >= 0.75:
            try:
                self.ingestor.store_fact(
                    'learned_from_web',
                    fact_text[:500],
                    confidence,
                    f'epistemic:{authority}',
                    now,
                    evidence_count=max(1, len(source_urls)),
                    session_id=self.session_id,
                    scope_key=scope_key,
                    evidence_packet_id=evidence_packet_id,
                )
            except Exception:
                pass
        self.conn.commit()
        return {
            'status': 'recorded',
            'fact_id': fact_id,
            'job_id': job_id,
            'confidence': confidence,
            'authority': authority,
            'expires_at': expires_at,
            'source_urls': source_urls,
            'evidence_packet_id': evidence_packet_id,
            'mirrored_global': mirrored_global,
        }


    def search_web(
        self,
        *,
        scope_key: str,
        question: str,
        job_id: str = '',
        limit: int = 6,
        max_queries: int = 4,
        timeout: float = 6.0,
    ) -> Dict[str, Any]:
        self.ensure_schema()
        scope_key = scope_key or self.scope_key or 'global'
        plan = self.plan_if_needed(scope_key, question, session_id=self.session_id)
        job_id = job_id or str(plan.get('job_id') or self.latest_job(scope_key, question))
        queries = list((plan.get('recommended_queries') if isinstance(plan, dict) else []) or [])
        discovered = discover_sources(
            question,
            queries,
            limit=max(1, int(limit or 6)),
            max_queries=max(1, int(max_queries or 1)),
            timeout=max(0.5, float(timeout or 1.0)),
        )
        candidates = [(url, title, summary, authority, confidence) for url, title, summary, authority, confidence in discovered]
        has_authoritative = any(authority in _AUTHORITATIVE_AUTHORITIES for _, _, _, authority, _ in candidates)
        fallback_candidates: List[Tuple[str, str, str, str, float]] = []
        if not candidates or not has_authoritative:
            # Last-resort bootstrap: generic discovery remains primary, static known URLs are only a fallback.
            fallback_candidates = [
                (url, title, summary, authority_for_url(url, question), source_confidence(authority_for_url(url, question), 0.58))
                for url, title, summary in candidate_sources_for_question(question)
            ]
            if fallback_candidates:
                candidates = fallback_candidates + candidates
        records = []
        for url, title, summary, authority, confidence in candidates[: max(1, int(limit or 6))]:
            records.append(self.record_source(
                scope_key=scope_key,
                job_id=job_id,
                url=url,
                title=title,
                source_kind='epistemic_autonomous_search',
                summary=summary,
                confidence=confidence,
                question=question,
            ))
        authoritative_records = [
            record for record in records
            if record.get('authority') in _AUTHORITATIVE_AUTHORITIES and sources_match_required_providers(question, [str(record.get('url') or '')])
        ]
        safe_answer = ''
        if authoritative_records:
            status = 'sources_found'
            visible_sources = authoritative_records
            source_lines = '\n'.join(f"- {record.get('url')}" for record in authoritative_records[:4])
            safe_answer = (
                "Official sources found, but exact current numeric/contract-specific values were not extracted. "
                "Safe answer: cite only these official URLs and say exact current NQ price-limit values/rules must be checked on the CME page/bulletin before any trading decision.\n"
                f"{source_lines}"
            )
            next_action = 'If web_extract/browser is available, extract the official page before giving numeric/current/high-stakes details. If extraction is unavailable, stop and answer with safe_answer; do not use terminal/search_files or secondary snippets, and do not record numeric facts from search-result titles.'
        elif records:
            status = 'no_authoritative_sources'
            visible_sources = records
            safe_answer = 'No official/primary source found. Safe answer: say research is inconclusive and do not provide current/high-stakes facts.'
            next_action = 'Search again or use web_extract; do not record or answer high-stakes/current facts from secondary-only sources.'
        else:
            status = 'no_sources_found'
            visible_sources = []
            safe_answer = 'No source found. Safe answer: say you cannot verify this yet and do not provide unverified facts.'
            next_action = 'No source found; do not answer with unverified facts.'
        discovery = 'generic_web_search' if discovered else ('legacy_bootstrap_fallback' if fallback_candidates else 'none')
        if discovered and fallback_candidates:
            discovery = 'generic_web_search_plus_legacy_fallback'
        return {
            'status': status,
            'job_id': job_id,
            'question': question,
            'sources': visible_sources,
            'authoritative_sources': authoritative_records,
            'candidate_count': len(records),
            'omitted_secondary_count': max(0, len(records) - len(visible_sources)),
            'discovery': discovery,
            'safe_answer': safe_answer,
            'next_action': next_action,
        }

    def record_tool_result(self, *, scope_key: str, tool_name: str, args: Dict[str, Any], result: Any, session_id: str = '') -> Dict[str, Any]:
        tool_name = (tool_name or '').strip()
        if tool_name not in {'web_search', 'web_extract'}:
            return {'ignored': True}
        self.ensure_schema()
        scope_key = scope_key or self.scope_key or 'global'
        question = str(args.get('query') or ' '.join(args.get('urls') or []) if isinstance(args, dict) else '')
        job_id = self.latest_job(scope_key, question)
        result_text = result if isinstance(result, str) else dumps(result)
        records: List[Dict[str, Any]] = []
        parsed = loads(result_text, None) if isinstance(result_text, str) else result
        if tool_name == 'web_search':
            candidates = []
            if isinstance(parsed, dict):
                data = parsed.get('data') if isinstance(parsed.get('data'), dict) else parsed
                web = data.get('web') if isinstance(data, dict) else None
                if isinstance(web, list):
                    candidates.extend(web)
                elif isinstance(parsed.get('results'), list):
                    candidates.extend(parsed.get('results'))
            for item in candidates[:8]:
                if not isinstance(item, dict):
                    continue
                url = str(item.get('url') or item.get('link') or '')
                title = str(item.get('title') or '')
                desc = str(item.get('description') or item.get('snippet') or item.get('content') or '')
                if url or title:
                    records.append(self.record_source(scope_key=scope_key, job_id=job_id, url=url, title=title, source_kind='web_search_result', summary=desc, confidence=0.58, question=question))
        elif tool_name == 'web_extract':
            urls = args.get('urls') if isinstance(args, dict) else []
            if isinstance(urls, str):
                urls = [urls]
            found_urls = [str(u) for u in urls if str(u).strip()]
            if not found_urls:
                found_urls = re.findall(r'https?://[^\s)\]}"]+', result_text or '')[:5]
            summary = result_text[:1200]
            for url in found_urls[:5]:
                records.append(self.record_source(scope_key=scope_key, job_id=job_id, url=url, title='', source_kind='web_extract', summary=summary, raw_excerpt=result_text[:2000], confidence=0.66))
        return {'recorded_sources': records, 'job_id': job_id}

    def compile_brief(self, scope_key: str, question: str, *, max_facts: int = 4) -> str:
        self.ensure_schema()
        scope_key = scope_key or self.scope_key or 'global'
        facts = self.recall_facts(scope_key, question, limit=max_facts)
        plan = self.plan_if_needed(scope_key, question, session_id=self.session_id)
        lines: List[str] = []
        if facts:
            for fact in facts[:max_facts]:
                urls = fact.get('source_urls') or []
                source_hint = domain_for_url(urls[0]) if urls else fact.get('authority', '')
                suffix = f" (source: {source_hint}, confidence={float(fact.get('confidence') or 0):.2f})" if source_hint else ''
                lines.append(f"Learned fact: {fact['fact_text'][:220]}{suffix}")
        if plan.get('needs_research'):
            queries = plan.get('recommended_queries') or []
            policy = plan.get('source_policy') or {}
            lines.append(f"Research required before final answer: {plan.get('reason')}.")
            lines.append(f"Research job: {plan.get('job_id')}.")
            if queries:
                lines.append('Use web_search or brain_epistemic(action=search_web) first query: ' + str(queries[0])[:180])
            lines.append('Then use web_extract when available, or returned official source summaries, and record findings with brain_epistemic(action=record_fact).')
            lines.append('Source policy: prefer ' + ', '.join(policy.get('prefer') or ['official/primary sources']) + '; avoid unsourced claims.')
        if not lines:
            return ''
        return "EPISTEMIC STATUS:\n- " + "\n- ".join(lines[:8])

    def debug(self, scope_key: str, question: str = '') -> Dict[str, Any]:
        self.ensure_schema()
        scope_key = scope_key or self.scope_key or 'global'
        classification = self.classify(question)
        plan = self.plan_if_needed(scope_key, question, session_id=self.session_id)
        rows = lambda sql, params=(): [dict(row) for row in self.conn.execute(sql, tuple(params)).fetchall()]
        return {
            'scope_key': scope_key,
            'question': question,
            'classification': classification.__dict__,
            'plan': plan,
            'facts': self.recall_facts(scope_key, question),
            'open_gaps': rows("SELECT * FROM epistemic_gaps WHERE scope_key=? ORDER BY updated_at DESC LIMIT 10", (scope_key,)),
            'jobs': rows("SELECT * FROM epistemic_research_jobs WHERE scope_key=? ORDER BY updated_at DESC LIMIT 10", (scope_key,)),
            'sources': rows("SELECT * FROM epistemic_web_sources WHERE scope_key=? ORDER BY created_at DESC LIMIT 10", (scope_key,)),
        }
