#!/usr/bin/env python3
from __future__ import annotations

import tempfile
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from live_brain.store import LiveBrainStore


def test_epistemic_research_required_then_learned_fact_reused() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = LiveBrainStore(str(Path(tmp) / 'brain.db'))
        store.initialize_schema()
        scope = 'agent:main:telegram:dm:test'
        question = 'Možeš li sam da trejduješ funded account?'

        brief = store.compile_epistemic_brief(scope, question)
        assert 'EPISTEMIC STATUS' in brief, brief
        assert 'Research required before final answer' in brief, brief
        assert 'web_search' in brief, brief
        debug = store.debug_epistemic(scope, question)
        assert debug['plan']['needs_research'] is True, debug
        job_id = debug['plan']['job_id']

        source = store.record_epistemic_source(
            scope_key=scope,
            job_id=job_id,
            url='https://ftmo.com/en/trading-objectives/',
            title='FTMO Trading Objectives',
            summary='Maximum Daily Loss and Maximum Loss rules',
            confidence=0.7,
        )
        assert source['authority'] == 'official', source

        fact = store.record_epistemic_fact(
            scope_key=scope,
            job_id=job_id,
            question=question,
            fact_text='FTMO-style funded accounts require strict daily and total loss limits; any autonomous trader must enforce those limits before placing trades.',
            source_urls=['https://ftmo.com/en/trading-objectives/'],
            confidence=0.86,
            ttl_seconds=86400,
        )
        assert fact['status'] == 'recorded', fact
        assert fact['authority'] == 'official', fact
        assert fact['mirrored_global'] is True, fact

        learned = store.compile_epistemic_brief(scope, question)
        assert 'Learned fact' in learned, learned
        assert 'Research required before final answer' not in learned, learned
        global_learned = store.compile_epistemic_brief('agent:main:telegram:dm:other', question)
        assert 'Learned fact' in global_learned, global_learned
        store.close()


def test_web_tool_results_are_recorded_as_sources() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = LiveBrainStore(str(Path(tmp) / 'brain.db'))
        store.initialize_schema()
        scope = 'agent:main:telegram:dm:test'
        question = 'Koja su najnovija YouTube Shorts monetization pravila?'
        job_id = store.debug_epistemic(scope, question)['plan']['job_id']
        result = {
            'success': True,
            'data': {
                'web': [
                    {
                        'title': 'YouTube Partner Program overview',
                        'url': 'https://support.google.com/youtube/answer/72851',
                        'description': 'Official YouTube monetization help page.',
                    }
                ]
            },
        }
        recorded = store.record_epistemic_tool_result(
            scope_key=scope,
            tool_name='web_search',
            args={'query': question},
            result=result,
            session_id='s',
        )
        assert recorded['job_id'] == job_id, recorded
        assert recorded['recorded_sources'], recorded
        row = store.conn.execute('SELECT authority FROM epistemic_web_sources WHERE scope_key=?', (scope,)).fetchone()
        assert row and row['authority'] == 'official', row['authority'] if row else None
        store.close()



def test_provider_specific_query_requires_matching_provider_source() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = LiveBrainStore(str(Path(tmp) / 'brain.db'))
        store.initialize_schema()
        scope = 'agent:main:telegram:dm:test'
        generic_question = 'Možeš li sam da trejduješ funded account?'
        job_id = store.debug_epistemic(scope, generic_question)['plan']['job_id']
        store.record_epistemic_fact(
            scope_key=scope,
            job_id=job_id,
            question=generic_question,
            fact_text='Funded-account trading must enforce FTMO and Topstep loss rules before autonomous execution.',
            source_urls=['https://ftmo.com/en/trading-objectives/', 'https://help.topstep.com/en/articles/10490293-topstepx-trailing-personal-daily-loss-limit'],
            confidence=0.88,
            ttl_seconds=86400,
        )
        apex_brief = store.compile_epistemic_brief(scope, 'Koja su najnovija pravila za Apex funded account?')
        assert 'Research required before final answer' in apex_brief, apex_brief
        assert 'Learned fact' not in apex_brief, apex_brief
        store.close()



def test_search_web_fallback_records_official_sources() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = LiveBrainStore(str(Path(tmp) / 'brain.db'))
        store.initialize_schema()
        scope = 'agent:main:telegram:dm:test'
        result = store.conn  # keep conn initialized for direct manager path through store API
        search = store.record_epistemic_source  # smoke store method presence
        from live_brain.epistemic import EpistemicManager
        found = EpistemicManager(store.conn).search_web(
            scope_key=scope,
            question='Koja su najnovija pravila za Apex funded account?',
        )
        assert found['status'] == 'sources_found', found
        urls = [source['url'] for source in found['sources']]
        assert any('apextraderfunding.com' in url for url in urls), urls
        store.close()


def test_low_confidence_source_less_fact_is_not_learned() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = LiveBrainStore(str(Path(tmp) / 'brain.db'))
        store.initialize_schema()
        scope = 'agent:main:telegram:dm:test'
        result = store.record_epistemic_fact(
            scope_key=scope,
            question='Apex rules?',
            fact_text='Unable to verify Apex rules because web access failed.',
            source_urls=[],
            confidence=0.1,
        )
        assert result['status'] == 'not_recorded_low_confidence', result
        rows = store.conn.execute('SELECT COUNT(*) c FROM epistemic_learned_facts WHERE scope_key=?', (scope,)).fetchone()['c']
        assert rows == 0, rows
        store.close()


def test_generic_discovery_prefers_official_trading_source_without_legacy_candidate() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = LiveBrainStore(str(Path(tmp) / 'brain.db'))
        store.initialize_schema()
        scope = 'agent:main:telegram:dm:test'
        from live_brain import epistemic as epistemic_mod
        original_discover = epistemic_mod.discover_sources

        def fake_discover(question, queries=None, *, limit=8, max_queries=4, timeout=6.0):
            return [
                ('https://faq.ampfutures.com/hc/en-us/articles/360042020633-Price-Limits', 'AMP FAQ price limits', 'Broker FAQ about price limits.', 'secondary', 0.58),
                ('https://www.cmegroup.com/trading/price-limits.html', 'Price Limits - CME Group', 'Official CME Group futures price limits.', 'official', 0.9),
            ]

        epistemic_mod.discover_sources = fake_discover
        try:
            from live_brain.epistemic import EpistemicManager
            found = EpistemicManager(store.conn).search_web(
                scope_key=scope,
                question='Koja su najnovija CME pravila za NQ price limits?',
                max_queries=1,
                timeout=0.5,
            )
            assert found['status'] == 'sources_found', found
            assert found['discovery'] == 'generic_web_search', found
            assert found['authoritative_sources'], found
            assert found['authoritative_sources'][0]['authority'] == 'official', found
            assert all('cmegroup.com' in source['url'] for source in found['authoritative_sources']), found
            urls = [source['url'] for source in found['sources']]
            assert any('cmegroup.com' in url for url in urls), urls
        finally:
            epistemic_mod.discover_sources = original_discover
            store.close()


def test_official_but_wrong_provider_is_not_authoritative_for_cme_question() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = LiveBrainStore(str(Path(tmp) / 'brain.db'))
        store.initialize_schema()
        scope = 'agent:main:telegram:dm:test'
        from live_brain import epistemic as epistemic_mod
        original_discover = epistemic_mod.discover_sources

        def fake_discover(question, queries=None, *, limit=8, max_queries=4, timeout=6.0):
            return [
                ('https://help.topstep.com/en/articles/8284225-how-to-ensure-i-am-not-trading-within-2-of-a-price-limit', 'Topstep price limit help', 'Topstep help article.', 'official', 0.86),
            ]

        epistemic_mod.discover_sources = fake_discover
        try:
            from live_brain.epistemic import EpistemicManager
            found = EpistemicManager(store.conn).search_web(
                scope_key=scope,
                question='Koja su najnovija CME pravila za NQ price limits?',
                max_queries=1,
                timeout=0.5,
            )
            assert found['status'] == 'no_authoritative_sources', found
            assert not found['authoritative_sources'], found
            assert found['sources'], found
        finally:
            epistemic_mod.discover_sources = original_discover
            store.close()


def test_generic_trading_words_do_not_make_secondary_source_official() -> None:
    from live_brain.epistemic import authority_for_url
    question = 'Koja su najnovija CME futures price limit pravila?'
    assert authority_for_url('https://faq.ampfutures.com/hc/en-us/articles/360042020633-Price-Limits', question) == 'secondary'
    assert authority_for_url('https://blog.ampglobal.com/cme-price-limit-guide-trading-halted-levels', question) == 'secondary'
    assert authority_for_url('https://cmegroupclientsite.atlassian.net/wiki/display/EPICSANDBOX/Limits+and+Banding', question) == 'secondary'
    assert authority_for_url('https://www.cmegroup.com/trading/price-limits.html', question) == 'official'


def test_high_stakes_fact_rejects_secondary_only_sources() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = LiveBrainStore(str(Path(tmp) / 'brain.db'))
        store.initialize_schema()
        scope = 'agent:main:telegram:dm:test'
        question = 'Koja su najnovija CME pravila za NQ price limits?'
        job_id = store.debug_epistemic(scope, question)['plan']['job_id']
        result = store.record_epistemic_fact(
            scope_key=scope,
            job_id=job_id,
            question=question,
            fact_text='CME NQ price limits should be checked before trading.',
            source_urls=['https://faq.ampfutures.com/hc/en-us/articles/360042020633-Price-Limits'],
            confidence=0.86,
            ttl_seconds=86400,
        )
        assert result['status'] == 'not_recorded_provider_mismatch' or result['status'] == 'not_recorded_insufficient_authority', result
        rows = store.conn.execute('SELECT COUNT(*) c FROM epistemic_learned_facts WHERE scope_key=?', (scope,)).fetchone()['c']
        assert rows == 0, rows
        store.close()


def test_numeric_high_stakes_fact_requires_extracted_evidence() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = LiveBrainStore(str(Path(tmp) / 'brain.db'))
        store.initialize_schema()
        scope = 'agent:main:telegram:dm:test'
        question = 'Koja su najnovija CME pravila za NQ price limits?'
        job_id = store.debug_epistemic(scope, question)['plan']['job_id']
        result = store.record_epistemic_fact(
            scope_key=scope,
            job_id=job_id,
            question=question,
            fact_text='NQ initial daily price limit is typically 7% and expands to 10% then 13%.',
            source_urls=['https://www.cmegroup.com/education/articles-and-reports/understanding-price-limits-and-circuit-breakers'],
            confidence=0.86,
            ttl_seconds=86400,
        )
        assert result['status'] == 'not_recorded_needs_extracted_evidence', result
        rows = store.conn.execute('SELECT COUNT(*) c FROM epistemic_learned_facts WHERE scope_key=?', (scope,)).fetchone()['c']
        assert rows == 0, rows
        with_excerpt = store.record_epistemic_fact(
            scope_key=scope,
            job_id=job_id,
            question=question,
            fact_text='NQ initial daily price limit is typically 7% according to the extracted CME article text.',
            source_urls=['https://www.cmegroup.com/education/articles-and-reports/understanding-price-limits-and-circuit-breakers'],
            confidence=0.86,
            ttl_seconds=86400,
            raw_excerpt='Official CME extracted article excerpt describing equity index futures price limits and a 7% initial daily price limit for the relevant session.'
        )
        assert with_excerpt['status'] == 'recorded', with_excerpt
        store.close()


if __name__ == '__main__':
    test_epistemic_research_required_then_learned_fact_reused()
    test_web_tool_results_are_recorded_as_sources()
    test_provider_specific_query_requires_matching_provider_source()
    test_search_web_fallback_records_official_sources()
    test_low_confidence_source_less_fact_is_not_learned()
    test_generic_discovery_prefers_official_trading_source_without_legacy_candidate()
    test_official_but_wrong_provider_is_not_authoritative_for_cme_question()
    test_generic_trading_words_do_not_make_secondary_source_official()
    test_high_stakes_fact_rejects_secondary_only_sources()
    test_numeric_high_stakes_fact_requires_extracted_evidence()
    print('live_brain_epistemic_test: PASS')
