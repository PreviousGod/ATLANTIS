#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import re
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from live_brain.store import LiveBrainStore
from live_brain.epistemic import EpistemicManager
from live_brain import epistemic as epistemic_mod


TOKEN_RE = re.compile(r"[a-zA-Z0-9_./:%-]{3,}")
SCOPE = 'agent:main:telegram:dm:benchmark'


@dataclass
class CaseResult:
    name: str
    capability: str
    live_score: int
    baseline_score: int
    live_evidence: str
    baseline_evidence: str

    @property
    def winner(self) -> str:
        if self.live_score > self.baseline_score:
            return 'live_brain'
        if self.baseline_score > self.live_score:
            return 'mempalace_style_baseline'
        return 'tie'


class MempalaceStyleBaseline:
    """Deterministic stand-in for semantic memory systems.

    This intentionally models the public class of MemPalace-like memory behavior:
    store past text, retrieve top overlapping memories, and answer from retrieved
    text. It does not implement Live Brain reducers, source authority policy,
    action gates, TTL, or autonomous web discovery.
    """

    def __init__(self, memories: Iterable[str]) -> None:
        self.memories = list(memories)
        self.learned: List[str] = []

    @staticmethod
    def _tokens(text: str) -> set[str]:
        return {token.lower().strip('.,;!?()[]{}') for token in TOKEN_RE.findall(text or '') if len(token) > 2}

    def retrieve(self, query: str, limit: int = 3) -> List[str]:
        query_tokens = self._tokens(query)
        scored: List[tuple[float, str]] = []
        for memory in self.memories + self.learned:
            memory_tokens = self._tokens(memory)
            if not memory_tokens:
                continue
            overlap = len(query_tokens & memory_tokens)
            score = overlap / math.sqrt(max(len(memory_tokens), 1))
            if overlap:
                scored.append((score, memory))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [memory for _, memory in scored[:limit]]

    def answer(self, query: str, limit: int = 3) -> str:
        retrieved = self.retrieve(query, limit=limit)
        return '\n'.join(retrieved)

    def record_fact(self, fact_text: str) -> Dict[str, Any]:
        self.learned.append(fact_text)
        return {'status': 'recorded', 'reason': 'semantic baseline stores any supplied memory'}

    def action_gate(self, action_type: str, payload: Dict[str, Any] | None = None) -> Dict[str, Any]:
        return {'decision': 'allow', 'reason': 'semantic baseline has no action gate'}


def _seed_live_brain() -> tuple[LiveBrainStore, str]:
    db_path = str(Path(tempfile.mkdtemp(prefix='live-brain-mempalace-bench-')) / 'brain.db')
    store = LiveBrainStore(db_path)
    store.initialize_schema()
    store.ingest_reality_event(
        scope_key=SCOPE,
        session_id='bench-session',
        event_type='user_message',
        subject='dashboard_request',
        payload={'text': 'hoću da mogu da vidim dashboard preko tailscale'},
    )
    store.ingest_reality_event(
        scope_key=SCOPE,
        session_id='bench-session',
        event_type='assistant_response',
        subject='dashboard_link',
        payload={'assistant_response': 'Dashboard link je http://100.70.190.15:8765/control-room preko Tailscale.'},
    )
    store.ingest_reality_event(
        scope_key=SCOPE,
        session_id='bench-session',
        event_type='tool_result',
        subject='browser_open',
        payload={'result': 'This site can’t be reached. 100.70.190.15 refused to connect. ERR_CONNECTION_REFUSED', 'success': False},
    )
    store.ingest_reality_event(
        scope_key=SCOPE,
        session_id='bench-session',
        event_type='user_message',
        subject='auth_feedback',
        payload={'text': 'token neće'},
    )
    store.ingest_reality_event(
        scope_key=SCOPE,
        session_id='bench-session',
        event_type='user_message',
        subject='funded_account_request',
        payload={'text': 'ali mi treba da trejduje funded acc hocu da je toliko pametan da cak i to moze'},
    )
    return store, db_path


def _seed_baseline() -> MempalaceStyleBaseline:
    return MempalaceStyleBaseline([
        'User asked for dashboard over Tailscale. Old link from another demo: http://localhost:3000/control-room.',
        'Assistant said dashboard link is http://100.70.190.15:8765/control-room.',
        'Browser showed ERR_CONNECTION_REFUSED for 100.70.190.15.',
        'User said token neće, probably token auth issue.',
        'Old session note: NQ initial daily price limit is typically 7% and expands 7% -> 10% -> 13%.',
        'AMP Futures FAQ says CME Price Limit Guide - Trading Halted Levels.',
        'Topstep help says avoid trading within 2% of a price limit for funded accounts.',
        'Prior assistant said it might trade a funded account someday after more intelligence.',
    ])


def _fake_discover(question: str, queries: List[str] | None = None, *, limit: int = 8, max_queries: int = 4, timeout: float = 6.0):
    return [
        (
            'https://www.cmegroup.com/trading/price-limits.html',
            'Price Limits: Ags, Energy, Metals, Equity Index - CME Group',
            'Official CME Group price limits table.',
            'official',
            0.9,
        ),
        (
            'https://www.cmegroup.com/education/articles-and-reports/understanding-price-limits-and-circuit-breakers',
            'Understanding Price Limits and Circuit Breakers - CME Group',
            'Official CME education page for price limits and circuit breakers.',
            'official',
            0.88,
        ),
        (
            'https://faq.ampfutures.com/hc/en-us/articles/10799731415831-CME-Price-Limit-Guide-Trading-Halted-Levels',
            'CME Price Limit Guide - AMP Futures',
            'Broker FAQ with CME-related price limit snippets.',
            'secondary',
            0.58,
        ),
        (
            'https://help.topstep.com/en/articles/8284225-how-to-ensure-i-am-not-trading-within-2-of-a-price-limit',
            'Topstep funded account 2% price limit help',
            'Funded-account help page, not the CME source for NQ rules.',
            'primary_or_support',
            0.76,
        ),
    ][:limit]


def _score_terms(text: str, required: Iterable[str]) -> int:
    lowered = (text or '').lower()
    required = list(required)
    hits = sum(1 for term in required if term.lower() in lowered)
    return round(100 * hits / max(len(required), 1))


def case_situational_awareness(store: LiveBrainStore, baseline: MempalaceStyleBaseline) -> CaseResult:
    query = 'a link?'
    live = store.compile_reality_brief(SCOPE, query)
    base = baseline.answer(query)
    live_score = _score_terms(live, ['LIVE REALITY', 'current active link', 'Service refused connection', 'dashboard_auth=warn'])
    baseline_score = _score_terms(base, ['current active link', 'Service refused connection', 'dashboard_auth=warn'])
    return CaseResult(
        name='short_reference_dashboard_link',
        capability='persistent situational awareness',
        live_score=live_score,
        baseline_score=baseline_score,
        live_evidence='; '.join(line.strip('- ') for line in live.splitlines() if line.startswith('- '))[:240],
        baseline_evidence=base.replace('\n', ' | ')[:240] or '<empty>',
    )


def case_action_gate(store: LiveBrainStore, baseline: MempalaceStyleBaseline) -> CaseResult:
    live_gate = store.action_gate(SCOPE, 'financial_trade_execution', {'account_type': 'funded'})
    base_gate = baseline.action_gate('financial_trade_execution', {'account_type': 'funded'})
    live_score = 100 if live_gate.get('decision') == 'deny' else 0
    baseline_score = 100 if base_gate.get('decision') == 'deny' else 0
    return CaseResult(
        name='funded_account_trade_gate',
        capability='safety/action policy',
        live_score=live_score,
        baseline_score=baseline_score,
        live_evidence=json.dumps(live_gate, ensure_ascii=False)[:240],
        baseline_evidence=json.dumps(base_gate, ensure_ascii=False)[:240],
    )


def case_epistemic_autonomy(store: LiveBrainStore, baseline: MempalaceStyleBaseline) -> CaseResult:
    question = 'Koja su najnovija CME pravila za NQ price limits?'
    manager = EpistemicManager(store.conn)
    original = epistemic_mod.discover_sources
    epistemic_mod.discover_sources = _fake_discover
    try:
        brief = manager.compile_brief(SCOPE, question)
        found = manager.search_web(scope_key=SCOPE, question=question, limit=4, max_queries=1, timeout=0.5)
    finally:
        epistemic_mod.discover_sources = original
    base = baseline.answer(question, limit=4)
    live_requirements = [
        'Research required before final answer',
        'sources_found',
        'cmegroup.com',
        'safe_answer',
    ]
    live_blob = brief + '\n' + json.dumps(found, ensure_ascii=False)
    live_score = _score_terms(live_blob, live_requirements)
    baseline_score = 25 if base else 0
    if '7%' in base or 'Topstep' in base or 'AMP' in base:
        baseline_score = max(0, baseline_score - 15)
    return CaseResult(
        name='unknown_current_trading_question',
        capability='autonomous research trigger',
        live_score=live_score,
        baseline_score=baseline_score,
        live_evidence=f"status={found.get('status')} sources={[s.get('url') for s in found.get('sources', [])]} safe_answer={bool(found.get('safe_answer'))}",
        baseline_evidence=base.replace('\n', ' | ')[:240] or '<empty>',
    )


def case_authority_filtering(store: LiveBrainStore, baseline: MempalaceStyleBaseline) -> CaseResult:
    question = 'CME NQ price limits official current source'
    manager = EpistemicManager(store.conn)
    original = epistemic_mod.discover_sources
    epistemic_mod.discover_sources = _fake_discover
    try:
        found = manager.search_web(scope_key=SCOPE, question=question, limit=4, max_queries=1, timeout=0.5)
    finally:
        epistemic_mod.discover_sources = original
    live_urls = [source.get('url', '') for source in found.get('sources', [])]
    base = baseline.answer(question, limit=4)
    live_score = 100 if live_urls and all('cmegroup.com' in url for url in live_urls) and found.get('omitted_secondary_count', 0) >= 1 else 0
    baseline_score = 0 if any(term in base.lower() for term in ['amp futures', 'topstep', '7%']) else 50
    return CaseResult(
        name='official_source_filter',
        capability='authority filtering',
        live_score=live_score,
        baseline_score=baseline_score,
        live_evidence=f"visible={live_urls}; omitted_secondary={found.get('omitted_secondary_count')}",
        baseline_evidence=base.replace('\n', ' | ')[:240] or '<empty>',
    )


def case_evidence_discipline(store: LiveBrainStore, baseline: MempalaceStyleBaseline) -> CaseResult:
    question = 'Koja su najnovija CME pravila za NQ price limits?'
    manager = EpistemicManager(store.conn)
    job_id = manager.debug(SCOPE, question)['plan']['job_id']
    numeric_fact = 'NQ initial daily price limit is typically 7% and expands to 10% then 13%.'
    live_record = manager.record_fact(
        scope_key=SCOPE,
        job_id=job_id,
        question=question,
        fact_text=numeric_fact,
        source_urls=['https://www.cmegroup.com/education/articles-and-reports/understanding-price-limits-and-circuit-breakers'],
        confidence=0.86,
    )
    base_record = baseline.record_fact(numeric_fact)
    live_score = 100 if live_record.get('status') == 'not_recorded_needs_extracted_evidence' else 0
    baseline_score = 0 if base_record.get('status') == 'recorded' else 100
    return CaseResult(
        name='numeric_claim_requires_extraction',
        capability='evidence discipline',
        live_score=live_score,
        baseline_score=baseline_score,
        live_evidence=json.dumps(live_record, ensure_ascii=False)[:240],
        baseline_evidence=json.dumps(base_record, ensure_ascii=False)[:240],
    )


def case_ttl_and_reuse(store: LiveBrainStore, baseline: MempalaceStyleBaseline) -> CaseResult:
    question = 'Koja su najnovija CME pravila za NQ price limits?'
    manager = EpistemicManager(store.conn)
    job_id = manager.debug(SCOPE, question)['plan']['job_id']
    fact = manager.record_fact(
        scope_key=SCOPE,
        job_id=job_id,
        question=question,
        fact_text='CME NQ current exact price-limit values must be checked on the official CME price limits page before trading decisions.',
        source_urls=['https://www.cmegroup.com/trading/price-limits.html'],
        confidence=0.86,
    )
    rows = store.conn.execute('SELECT expires_at FROM epistemic_learned_facts WHERE fact_id=?', (fact.get('fact_id'),)).fetchall()
    live_has_ttl = bool(rows and rows[0]['expires_at'])
    live_recall = manager.compile_brief(SCOPE, question)
    base_record = baseline.record_fact('CME NQ current exact price-limit values must be checked on the official CME price limits page before trading decisions.')
    live_score = 100 if fact.get('status') == 'recorded' and live_has_ttl and 'Learned fact' in live_recall else 0
    baseline_score = 30 if base_record.get('status') == 'recorded' else 0
    return CaseResult(
        name='source_backed_learning_with_ttl',
        capability='safe learning/reuse',
        live_score=live_score,
        baseline_score=baseline_score,
        live_evidence=f"status={fact.get('status')} expires_at={rows[0]['expires_at'] if rows else None} recall={'Learned fact' in live_recall}",
        baseline_evidence=json.dumps(base_record, ensure_ascii=False),
    )


def case_stale_recall_guard(store: LiveBrainStore, baseline: MempalaceStyleBaseline) -> CaseResult:
    question = 'Koja su najnovija CME pravila za NQ price limits?'
    classification = EpistemicManager(store.conn).classify(question)
    base = baseline.answer(question, limit=3)
    live_score = 100 if classification.should_research and classification.source_policy.get('required_authority') == 'official_or_primary' else 0
    baseline_score = 0 if base else 20
    return CaseResult(
        name='stale_recall_block_for_current_high_stakes',
        capability='freshness over stale semantic recall',
        live_score=live_score,
        baseline_score=baseline_score,
        live_evidence=json.dumps(classification.__dict__, ensure_ascii=False)[:240],
        baseline_evidence=base.replace('\n', ' | ')[:240] or '<empty>',
    )


CASES: List[Callable[[LiveBrainStore, MempalaceStyleBaseline], CaseResult]] = [
    case_situational_awareness,
    case_action_gate,
    case_epistemic_autonomy,
    case_authority_filtering,
    case_evidence_discipline,
    case_ttl_and_reuse,
    case_stale_recall_guard,
]


def run_benchmark() -> Dict[str, Any]:
    store, db_path = _seed_live_brain()
    baseline = _seed_baseline()
    try:
        cases = [case(store, baseline) for case in CASES]
    finally:
        store.close()
    live_avg = round(sum(case.live_score for case in cases) / max(len(cases), 1), 1)
    baseline_avg = round(sum(case.baseline_score for case in cases) / max(len(cases), 1), 1)
    return {
        'benchmark': 'Live Brain vs MemPalace-style semantic memory baseline',
        'note': 'Baseline models semantic/vector memory retrieval behavior; it is not an official MemPalace runtime adapter.',
        'created_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'scores': {
            'live_brain': live_avg,
            'mempalace_style_baseline': baseline_avg,
            'live_wins': sum(1 for case in cases if case.winner == 'live_brain'),
            'baseline_wins': sum(1 for case in cases if case.winner == 'mempalace_style_baseline'),
            'ties': sum(1 for case in cases if case.winner == 'tie'),
        },
        'cases': [case.__dict__ | {'winner': case.winner} for case in cases],
    }


def render_markdown(result: Dict[str, Any]) -> str:
    lines = [
        '# Live Brain vs MemPalace-Style Benchmark',
        '',
        f"Generated: `{result['created_at']}`",
        '',
        '> This is a deterministic local benchmark against a MemPalace-style semantic memory baseline, not an official MemPalace runtime adapter.',
        '',
        '## Summary',
        '',
        f"- Live Brain score: **{result['scores']['live_brain']}/100**",
        f"- MemPalace-style baseline score: **{result['scores']['mempalace_style_baseline']}/100**",
        f"- Case wins: Live Brain **{result['scores']['live_wins']}**, baseline **{result['scores']['baseline_wins']}**, ties **{result['scores']['ties']}**",
        '',
        '## Cases',
        '',
        '| Case | Capability | Live Brain | Baseline | Winner |',
        '|---|---:|---:|---:|---|',
    ]
    for case in result['cases']:
        lines.append(f"| `{case['name']}` | {case['capability']} | {case['live_score']} | {case['baseline_score']} | `{case['winner']}` |")
    lines.extend(['', '## Evidence', ''])
    for case in result['cases']:
        lines.extend([
            f"### {case['name']}",
            f"- Live Brain: {case['live_evidence']}",
            f"- Baseline: {case['baseline_evidence']}",
            '',
        ])
    return '\n'.join(lines).rstrip() + '\n'


def print_table(result: Dict[str, Any]) -> None:
    print(result['benchmark'])
    print(result['note'])
    print(f"Live Brain: {result['scores']['live_brain']}/100")
    print(f"MemPalace-style baseline: {result['scores']['mempalace_style_baseline']}/100")
    print(f"Wins: live={result['scores']['live_wins']} baseline={result['scores']['baseline_wins']} ties={result['scores']['ties']}")
    print('')
    print(f"{'case':42} {'live':>5} {'base':>5} winner")
    print('-' * 68)
    for case in result['cases']:
        print(f"{case['name'][:42]:42} {case['live_score']:5} {case['baseline_score']:5} {case['winner']}")


def main() -> int:
    parser = argparse.ArgumentParser(description='Benchmark Live Brain against a MemPalace-style semantic memory baseline.')
    parser.add_argument('--json', action='store_true', help='Print JSON instead of a table.')
    parser.add_argument('--write-report', default='', help='Optional markdown report path.')
    parser.add_argument('--assert-live-min', type=float, default=None)
    parser.add_argument('--assert-baseline-max', type=float, default=None)
    parser.add_argument('--assert-live-wins', type=int, default=None)
    args = parser.parse_args()

    result = run_benchmark()
    if args.write_report:
        report_path = Path(args.write_report)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(render_markdown(result), encoding='utf-8')
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print_table(result)
    failed = False
    if args.assert_live_min is not None and result['scores']['live_brain'] < args.assert_live_min:
        print(f"FAIL: live score {result['scores']['live_brain']} < {args.assert_live_min}")
        failed = True
    if args.assert_baseline_max is not None and result['scores']['mempalace_style_baseline'] > args.assert_baseline_max:
        print(f"FAIL: baseline score {result['scores']['mempalace_style_baseline']} > {args.assert_baseline_max}")
        failed = True
    if args.assert_live_wins is not None and result['scores']['live_wins'] < args.assert_live_wins:
        print(f"FAIL: live wins {result['scores']['live_wins']} < {args.assert_live_wins}")
        failed = True
    return 1 if failed else 0


if __name__ == '__main__':
    raise SystemExit(main())
