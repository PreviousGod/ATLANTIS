from __future__ import annotations

import json
import time
from typing import Dict, List
from .utils import stable_id



class ResearchManager:
    def __init__(self, conn, ingestor=None, causal=None, session_id: str = '', scope_key: str = ''):
        self.conn = conn
        self.ingestor = ingestor
        self.causal = causal
        self.session_id = session_id
        self.scope_key = scope_key

    def plan_research(self, question: str, scope: str = 'auto') -> dict:
        q = (question or '').strip()
        if not q:
            return {'error': 'question is required'}
        if scope == 'auto':
            if any(w in q.lower() for w in ['repo', 'file', 'ffmpeg', 'config', 'plugin', 'tool']):
                steps = ['search local code/state first', 'check local docs/config second', 'use web only if local evidence is insufficient']
                resolved_scope = 'local'
            else:
                steps = ['check local state first', 'check official docs second', 'use web last if needed']
                resolved_scope = 'docs'
        elif scope == 'local':
            steps = ['search local code/state', 'compare exact files/tools/entities', 'inspect recent active episode']
            resolved_scope = 'local'
        elif scope == 'docs':
            steps = ['check official docs', 'compare with local config/state', 'only then broaden if unresolved']
            resolved_scope = 'docs'
        else:
            steps = ['use web search carefully', 'prefer authoritative docs', 'store only evidence-backed findings']
            resolved_scope = 'web'
        research_id = stable_id('research', self.scope_key, q, resolved_scope)
        return {
            'research_id': research_id,
            'scope': resolved_scope,
            'question': q,
            'steps': steps,
            'principle': 'Do not guess; gather evidence in bounded order.',
            'record_with': {'tool': 'brain_research', 'research_id': research_id, 'summary': '<evidence-backed finding>'},
        }

    def record_result(self, research_id: str, source_kind: str, source_ref: str, summary: str, confidence: float = 0.6, actionability: float = 0.6, raw_excerpt: str = '') -> dict:
        summary = (summary or '').strip()
        if not summary:
            return {'error': 'summary is required'}
        confidence = max(0.0, min(float(confidence), 1.0))
        actionability = max(0.0, min(float(actionability), 1.0))
        research_id = research_id or stable_id('research', self.scope_key, source_kind, source_ref, summary)
        now = time.time()
        claim = summary if not source_ref else f'{summary} (source: {source_kind}:{source_ref})'
        belief = None
        if self.causal:
            action = 'validated' if confidence >= 0.75 else 'hypothesis'
            belief = self.causal.mark_belief(
                belief_id=research_id.replace('research:', 'belief:research:'),
                claim_text=claim[:500],
                action=action,
                evidence_text=raw_excerpt or source_ref,
                session_id=self.session_id,
                scope_key=self.scope_key,
            )
        fact = None
        if self.ingestor and confidence >= 0.75 and actionability >= 0.5:
            fact = self.ingestor.store_fact(
                'research_result',
                claim[:500],
                confidence,
                f'research:{source_kind}',
                now,
                evidence_count=1,
                session_id=self.session_id,
                scope_key=self.scope_key,
            )
        self.conn.commit()
        return {
            'status': 'recorded',
            'research_id': research_id,
            'belief_id': belief.get('belief_id') if isinstance(belief, dict) else '',
            'fact_id': fact.get('fact_id') if isinstance(fact, dict) else '',
            'confidence': confidence,
            'actionability': actionability,
        }
