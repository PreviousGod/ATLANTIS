from __future__ import annotations

import json
import re
import time
from typing import Any, Dict, List

# Token budget per briefing section (approximate word count)
SECTION_BUDGETS = {
    'RECENT WORK': 4,       # items
    'ACTIVE WORK ITEM': 1,  # single item, full detail
    'VALIDATED FACTS': 4,
    'OPEN HYPOTHESES': 3,
    'RULED OUT': 3,
    'RELEVANT ENTITIES': 4,
    'SESSION RULES': 3,
    'NEXT ACTIONS': 3,
    'RECAP ANSWER DRAFT': 5,
}

WORD_RE = re.compile(r"[\w./-]+")
RUN_MARKER_RE = re.compile(r"\b(?:run|lbcap|codename)[-_][a-z0-9]+\b", re.IGNORECASE)
LOW_SIGNAL_TERMS = {
    'problem', 'error', 'issue', 'tool', 'tools', 'that', 'this', 'what', 'how', 'which',
    'tell', 'reci', 'sta', 'kako', 'koji', 'koja', 'bez', 'mene', 'pitas', 'ista',
    'imali', 'smo', 'with', 'from', 'into', 'root', 'cause', 'sledeci', 'konkretan', 'korak'
}
# Low-value work item titles to skip during retrieval

def _marker_tokens(text: str) -> set[str]:
    return {m.group(0).lower() for m in RUN_MARKER_RE.finditer(text or '')}


def _marker_conflicts(query_text: str, row_text: str) -> bool:
    query_tokens = _marker_tokens(query_text)
    row_tokens = _marker_tokens(row_text)
    return bool(query_tokens and row_tokens and query_tokens.isdisjoint(row_tokens))

SKIP_ITEM_TITLES = frozenset([
    'zdravo', 'hello', 'hi', 'test ping', 'pong',
    'sumarizuj sta si sve radio do sad', 'e hermes', 'jel imas nekih problema',
])


from pathlib import Path


class RetrievalRouter:
    def __init__(self, conn, hermes_home: str = ''):
        self.conn = conn
        self.hermes_home = hermes_home or str(Path.home() / '.hermes')

    def recap_recent_work(self, limit: int = 3) -> str:
        recaps = self._recent_canonical_recaps(limit)
        if recaps:
            lines = ['RECENT WORK RECAP']
            for r in recaps:
                lines.append(f"- Task: {r['task']}")
                if r['main_problem']:
                    lines.append(f"  Problem: {r['main_problem']}")
                if r['root_cause']:
                    lines.append(f"  Root cause: {r['root_cause']}")
                if r['what_changed']:
                    lines.append(f"  Changed: {r['what_changed']}")
                if r['current_status']:
                    lines.append(f"  Status: {r['current_status']}")
                if r['next_step']:
                    lines.append(f"  Next: {r['next_step']}")
            return '\n'.join(lines)
        episodes = self._recent_episode_recap(max_items=limit)
        if not episodes:
            return 'No recent work found in live brain.'
        lines = ['RECENT WORK RECAP']
        for ep in episodes:
            title = ep.get('title', '')
            summary = ep.get('current_summary', '')
            lines.append(f'- {title}: {summary[:220]}')
        return '\n'.join(lines)

    def build_briefing(self, scope_key: str, query: str, max_items: int = 3) -> str:
        state = self._load_state(scope_key)
        entities = self._extract_query_terms(query)
        active_work_item = self._find_active_work_item(scope_key, entities)
        active_episode = self._find_active_episode(entities)
        exact_entities = self._find_exact_entities(entities, max_items=max_items)
        facts = self._find_recent_facts(scope_key, entities, max_items=max_items)
        beliefs = self._find_recent_beliefs(scope_key, entities, max_items=max_items)
        rules = self._find_applicable_rules(query, max_items=max_items)
        is_recap = self._is_recap_query(query)
        recent_recaps = self._recent_canonical_recaps(max_items) if is_recap else []
        recent_episodes = self._recent_episode_recap(max_items=max_items) if is_recap and not recent_recaps else []

        lines = ["LIVE BRAIN BRIEFING"]

        if is_recap:
            active_recap_item = self._find_active_work_item(scope_key, [])
            if active_recap_item:
                lines.append("RECENT WORK:")
                lines.append(f"- Task: {active_recap_item['title']}")
                if active_recap_item.get('root_cause'):
                    lines.append(f"  Root cause: {active_recap_item['root_cause']}")
                if active_recap_item.get('status'):
                    lines.append(f"  Status: {active_recap_item['status']}")
                if active_recap_item.get('next_step'):
                    lines.append(f"  Next: {active_recap_item['next_step']}")
                lines.append("RECAP ANSWER DRAFT:")
                lines.append("Evo kratkog pregleda najskorijeg rada:")
                lines.append(f"- {active_recap_item['title']}: status={active_recap_item['status']}; root_cause={(active_recap_item.get('root_cause') or 'n/a')[:120]}")
            elif recent_recaps:
                lines.append("RECENT WORK:")
                lines.append(self.recap_recent_work(limit=max_items))
                lines.append("RECAP ANSWER DRAFT:")
                lines.append("Evo kratkog pregleda najskorijeg rada:")
                for r in recent_recaps:
                    lines.append(f"- {r['task']}: status={r['current_status']}; root_cause={r['root_cause'][:120] if r['root_cause'] else 'n/a'}")
            elif recent_episodes:
                lines.append("RECENT WORK:")
                for ep in recent_episodes:
                    lines.append(f"- {ep['title']} :: {ep['current_summary'][:140]}")
                lines.append("RECAP ANSWER DRAFT:")
                lines.append("Evo kratkog pregleda najskorijeg rada:")
                for ep in recent_episodes:
                    lines.append(f"- {ep['title']}: {ep['current_summary'][:180]}")
        else:
            if active_work_item:
                lines.append(f"ACTIVE WORK ITEM: {active_work_item['title']}")
                lines.append(f"WORK ITEM STATUS: {active_work_item['status']}")
                if active_work_item.get('root_cause'):
                    lines.append(f"WORK ITEM ROOT CAUSE: {active_work_item['root_cause']}")
                if active_work_item.get('next_step'):
                    lines.append(f"WORK ITEM NEXT STEP: {active_work_item['next_step']}")
                hours_stale = (time.time() - active_work_item['updated_at']) / 3600.0
                if active_work_item['status'] == 'active' and hours_stale > 72:
                    lines.append(f"⚠️ WORK ITEM STALE: {hours_stale:.0f}h old — consider resolving or refreshing")
            elif state.get("current_thread"):
                lines.append(f"CURRENT THREAD: {state['current_thread']}")
            if active_episode:
                lines.append(f"ACTIVE EPISODE: {active_episode['title']}")
                if active_episode.get("current_summary"):
                    lines.append(f"EPISODE SUMMARY: {active_episode['current_summary']}")

        if exact_entities:
            lines.append("RELEVANT ENTITIES:")
            for ent in exact_entities[:max_items]:
                lines.append(f"- {ent['display_name']} ({ent['entity_type']})")

        if facts:
            lines.append("VALIDATED FACTS:")
            for fact in facts[:max_items]:
                lines.append(f"- {fact['fact_text']}")

        if rules:
            lines.append("ACTIVE RULES:")
            for rule in rules[:max_items]:
                cond = rule.get('condition', {})
                action = rule.get('action', {})
                lines.append(f"- [{rule['category']}] if {cond} -> {action}")

        # Only surface validated causes that are relevant to the current query/entities,
        # not arbitrary global solved causes from unrelated tasks.
        validated_causes = [b for b in beliefs if b['status'] == 'validated' and b['belief_kind'] == 'validated_cause' and self._belief_matches_query(b['claim_text'], entities, exact_entities)]
        if validated_causes:
            lines.append("VALIDATED CAUSES:")
            for belief in validated_causes[:max_items]:
                lines.append(f"- {belief['claim_text']}")

        open_beliefs = [b for b in beliefs if b['status'] == 'open' and not self._is_meta_belief(b['claim_text'])]
        if open_beliefs:
            lines.append("OPEN HYPOTHESES:")
            for belief in open_beliefs[:max_items]:
                lines.append(f"- {belief['claim_text']}")

        ruled_out = [b for b in beliefs if b['belief_kind'] == 'ruled_out_cause']
        if ruled_out:
            lines.append("RULED OUT:")
            for belief in ruled_out[:max_items]:
                lines.append(f"- {belief['claim_text']}")

        superseded = [b for b in beliefs if b['status'] == 'superseded' and b.get('_superseded_by')]
        if superseded:
            lines.append("⚠️ SUPERSEDED:")
            for belief in superseded[:max_items]:
                lines.append(f"- {belief['claim_text']}")
                lines.append(f"  superseded by: {belief['_superseded_by']}")

        actions = list(state.get("next_best_actions") or [])
        actions.extend(self._actions_for_query(query, active_episode, facts, beliefs))
        deduped = []
        for action in actions:
            if action and action not in deduped:
                deduped.append(action)
        if deduped:
            lines.append("NEXT BEST ACTIONS:")
            for action in deduped[:max_items]:
                lines.append(f"- {action}")

        if len(lines) == 1:
            return ""
        return self._enforce_budget(lines)

    def _enforce_budget(self, lines: List[str]) -> str:
        """Apply token budgets per section, truncating long sections."""
        result: List[str] = []
        current_section = None
        section_lines: List[str] = []
        budget_for = SECTION_BUDGETS.get

        def flush_section(section: str | None, items: List[str]) -> None:
            if section and items:
                budget = budget_for(section, 4)
                result.extend(items[:budget])
            elif items:
                result.extend(items[:4])

        for line in lines:
            stripped = line.rstrip()
            is_header = stripped and not stripped.startswith('  ') and not stripped.startswith('- ') and ':' in stripped
            if is_header and current_section:
                flush_section(current_section, section_lines)
                section_lines = []
            if is_header:
                current_section = stripped.split(':')[0].strip()
            section_lines.append(stripped)

        flush_section(current_section, section_lines)
        return '\n'.join(result)

    def _load_state(self, scope_key: str) -> Dict[str, Any]:
        row = self.conn.execute(
            "SELECT state_json FROM work_state WHERE scope_key = ?",
            (scope_key,),
        ).fetchone()
        if not row:
            return {}
        try:
            return json.loads(row[0])
        except Exception:
            return {}

    def _extract_query_terms(self, query: str) -> List[str]:
        terms = []
        for m in WORD_RE.finditer(query):
            t = m.group(0).lower()
            if len(t) < 3:
                continue
            if t in LOW_SIGNAL_TERMS:
                continue
            terms.append(t)
        return terms

    def _find_active_episode(self, terms: List[str]):
        rows = self.conn.execute(
            "SELECT episode_id, title, current_summary, updated_at FROM episodes WHERE status IN ('active','dormant') ORDER BY updated_at DESC LIMIT 30"
        ).fetchall()
        if not rows:
            return None
        if not terms:
            return dict(rows[0])
        scored = []
        now = time.time()
        for row in rows:
            title = (row['title'] or '').lower()
            summary = (row['current_summary'] or '').lower()
            if not title:
                continue
            if title.startswith('[system note:') or title.startswith('[context compaction'):
                continue
            if any(term == title for term in SKIP_ITEM_TITLES):
                continue
            if title.startswith('phase') or title.startswith('v2-') or title.startswith('final-') or title.startswith('canonical-'):
                continue
            if _marker_conflicts(' '.join(terms), f'{title} {summary}'):
                continue
            overlap = sum(1 for t in terms if t in title or t in summary)
            hours = max(0.0, (now - row['updated_at']) / 3600.0)
            recency = 1.0 / (1.0 + hours / 24.0)
            score = overlap * 0.8 + recency * 0.2
            scored.append((score, dict(row)))
        scored.sort(key=lambda x: x[0], reverse=True)
        if terms and scored and scored[0][0] <= 0.25:
            return None
        return scored[0][1]

    def _find_active_work_item(self, scope_key: str, terms: List[str]):
        working_rows = self.conn.execute(
            """SELECT w.work_item_id, w.title, w.status, w.priority, w.root_cause, w.next_step, w.updated_at, w.resolved_at
               FROM working_set ws JOIN work_items w ON w.work_item_id = ws.work_item_id
               WHERE ws.scope_key = ?
               ORDER BY ws.slot ASC LIMIT 3""",
            (scope_key,),
        ).fetchall()
        if working_rows and not terms:
            return dict(working_rows[0])
        rows = self.conn.execute(
            "SELECT work_item_id, title, status, priority, root_cause, next_step, updated_at, resolved_at FROM work_items WHERE scope_key = ? AND lower(title) NOT LIKE 'sumarizuj%' AND lower(title) NOT LIKE 'what did you do%' AND lower(title) NOT LIKE 'recap%' AND lower(title) NOT LIKE 'pregled%' AND lower(title) NOT IN ('da','ne','ok','okej','sve','yes','no','continue','nastavi') ORDER BY updated_at DESC LIMIT 30",
            (scope_key,),
        ).fetchall()
        if not rows:
            return None
        now = time.time()
        scored = []
        for row in rows:
            title = (row['title'] or '').lower()
            row_text = f"{title} {row['root_cause'] or ''} {row['next_step'] or ''}".lower()
            if _marker_conflicts(' '.join(terms), row_text):
                continue
            overlap = sum(1 for t in terms if t in title) if terms else 0
            hours = max(0.0, (now - row['updated_at']) / 3600.0)
            recency = 1.0 / (1.0 + hours / 24.0)
            status = row['status'] or 'active'
            status_bonus = 1.0 if status == 'active' else (0.5 if status == 'blocked' else -1.0)
            resolved_penalty = 0.6 if row['resolved_at'] else 0.0
            # Explicit staleness penalty: active items not touched in 72h lose priority
            stale_penalty = 0.2 if status == 'active' and hours > 72 else 0.0
            raw_score = float(row['priority'] or 0.5) * 0.4 + overlap * 0.4 + recency * 0.3 + status_bonus - resolved_penalty - stale_penalty
            # Clamp to [0, 1]
            score = max(0.0, min(1.0, raw_score))
            scored.append((score, dict(row)))
        scored.sort(key=lambda x: x[0], reverse=True)
        best = scored[0][1]
        if best.get('status') == 'resolved' and not terms:
            return None
        return best

    def _predict_next_step(self, scope_key: str, active_work_item) -> str:
        if not active_work_item:
            return ''
        if active_work_item.get('next_step'):
            return active_work_item['next_step']
        rows = self.conn.execute(
            "SELECT next_step FROM work_items WHERE scope_key = ? AND status = 'resolved' AND next_step != '' ORDER BY updated_at DESC LIMIT 5",
            (scope_key,),
        ).fetchall()
        if rows:
            return rows[0][0]
        return ''

    def _find_exact_entities(self, terms: List[str], max_items: int = 3) -> List[Dict[str, Any]]:
        if not terms:
            return []
        rows = self.conn.execute(
            "SELECT entity_id, entity_type, canonical_name, display_name, last_seen_at, salience_score FROM entities ORDER BY last_seen_at DESC LIMIT 100"
        ).fetchall()
        now = time.time()
        matches = []
        for row in rows:
            name = (row['canonical_name'] or '').lower()
            overlap = sum(1 for t in terms if t in name)
            if overlap == 0:
                continue
            hours = max(0.0, (now - row['last_seen_at']) / 3600.0)
            recency = 1.0 / (1.0 + hours / 24.0)
            score = overlap * 0.7 + recency * 0.2 + float(row['salience_score'] or 0) * 0.1
            matches.append((score, dict(row)))
        matches.sort(key=lambda x: x[0], reverse=True)
        return [m[1] for m in matches[:max_items]]

    def _find_recent_facts(self, scope_key: str, terms: List[str], max_items: int = 3) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT fact_id, fact_type, fact_text, confidence, valid_from, status, session_id, scope_key FROM facts WHERE status = 'active' ORDER BY CASE WHEN scope_key = ? THEN 0 ELSE 1 END, valid_from DESC LIMIT 100",
            (scope_key,)
        ).fetchall()
        now = time.time()
        matches = []
        for row in rows:
            text = (row['fact_text'] or '').lower()
            if _marker_conflicts(' '.join(terms), text):
                continue
            overlap = sum(1 for t in terms if t in text) if terms else 1
            if overlap == 0:
                continue
            # Ignore meta/recap artifacts masquerading as validated facts.
            if self._is_meta_belief(text):
                continue
            hours = max(0.0, (now - row['valid_from']) / 3600.0)
            recency = 1.0 / (1.0 + hours / 24.0)
            score = overlap * 0.5 + recency * 0.2 + float(row['confidence'] or 0) * 0.3
            matches.append((score, dict(row)))
        matches.sort(key=lambda x: x[0], reverse=True)
        return [m[1] for m in matches[:max_items]]

    def _find_recent_beliefs(self, scope_key: str, terms: List[str], max_items: int = 3) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT belief_id, claim_text, belief_kind, confidence, status, updated_at, session_id, scope_key, supersedes_belief_id FROM beliefs WHERE status != 'invalidated' ORDER BY CASE WHEN scope_key = ? THEN 0 ELSE 1 END, updated_at DESC LIMIT 100",
            (scope_key,)
        ).fetchall()
        now = time.time()
        # Pre-load superseder claims for superseded beliefs in one pass
        superseder_map: Dict[str, str] = {}
        for row in rows:
            if row['status'] == 'superseded' and row['supersedes_belief_id']:
                sid = row['supersedes_belief_id']
                if sid not in superseder_map:
                    r = self.conn.execute(
                        "SELECT claim_text FROM beliefs WHERE belief_id = ?", (sid,)
                    ).fetchone()
                    superseder_map[sid] = r['claim_text'] if r else ''
        matches = []
        best_by_claim = {}
        for row in rows:
            text = (row['claim_text'] or '').lower()
            if _marker_conflicts(' '.join(terms), text):
                continue
            overlap = sum(1 for t in terms if t in text) if terms else 1
            if overlap == 0:
                continue
            hours = max(0.0, (now - row['updated_at']) / 3600.0)
            recency = 1.0 / (1.0 + hours / 24.0)
            status_bonus = 0.25 if row['status'] == 'validated' else (-0.15 if row['status'] == 'falsified' else 0.0)
            kind_bonus = 0.15 if row['belief_kind'] == 'validated_cause' else (0.1 if row['belief_kind'] == 'ruled_out_cause' else 0.0)
            score = overlap * 0.4 + recency * 0.15 + float(row['confidence'] or 0) * 0.3 + status_bonus + kind_bonus
            claim_key = text.strip()
            current = best_by_claim.get(claim_key)
            if current is None or score > current[0]:
                belief_dict = dict(row)
                # Annotate superseded beliefs with the superseder's claim
                if belief_dict['status'] == 'superseded' and belief_dict['supersedes_belief_id']:
                    belief_dict['_superseded_by'] = superseder_map.get(belief_dict['supersedes_belief_id'], '')
                best_by_claim[claim_key] = (score, belief_dict)
        matches = list(best_by_claim.values())
        matches.sort(key=lambda x: x[0], reverse=True)
        return [m[1] for m in matches[:max_items]]

    def _actions_for_query(self, query: str, active_episode, facts, beliefs) -> List[str]:
        q = query.lower()
        actions: List[str] = []
        is_diag = any(w in q for w in ["error", "bug", "ne radi", "problem", "fails", "ffmpeg", "invalid", "root cause"])
        is_image = any(w in q for w in ["image", "analyzer", "vision", "gemma", "gemini", "screenshot", "telegram"])
        if is_diag:
            actions.append("Use exact files/tools and recent evidence before proposing a cause.")
        if is_image:
            actions.append("State the most likely root cause from the available evidence, then give one concrete next debugging step. Do not ask the user for clarification if artifacts already exist.")
            actions.append("If local evidence is already available, perform the next local reproduction/logging step yourself before asking the user anything.")
            actions.append("Prefer: inspect gateway path -> add targeted debug log -> reproduce with one image -> read log -> conclude.")
            actions.append("If you can modify the local codebase safely, patch in diagnostic logging yourself instead of telling the user to do it.")
        if any(w in q for w in ["nastavi", "continue", "isti", "again", "opet"]):
            actions.append("Continue from the active episode instead of asking for context again.")
        if self._is_recap_query(query):
            actions.append("Summarize recent work directly from live brain episodes shown above. Do not call session_search first if RECENT WORK is present.")
        if not facts and not beliefs:
            actions.append("Search local state first; if uncertainty remains, prepare focused research rather than guessing.")
        if active_episode:
            actions.append("Keep continuity with the current active episode and avoid stale older context.")
        return actions

    def _is_recap_query(self, query: str) -> bool:
        q = query.lower()
        return any(w in q for w in ['sumarizuj', 'summarize', 'sta si radio', 'what did you do', 'do sad', 'do sada', 'pregled', 'recap'])

    def _recent_episode_recap(self, max_items: int = 3) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT title, current_summary, updated_at FROM episodes ORDER BY updated_at DESC LIMIT 50"
        ).fetchall()
        out = []
        seen = set()
        for row in rows:
            title = (row['title'] or '').strip()
            summary = (row['current_summary'] or '').strip()
            lowered = title.lower()
            if not title or title in seen:
                continue
            if lowered.startswith('[system note:') or lowered.startswith('[context compaction'):
                continue
            if any(term == lowered for term in SKIP_ITEM_TITLES):
                continue
            if len(title) < 12 and len(summary) < 20:
                continue
            seen.add(title)
            out.append(dict(row))
            if len(out) >= max_items:
                break
        return out

    def _recent_canonical_recaps(self, max_items: int = 3) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT task, objective, main_problem, root_cause, ruled_out_causes, what_changed, current_status, next_step, confidence, updated_at FROM canonical_recaps WHERE scope_key != '' ORDER BY updated_at DESC LIMIT 80"
        ).fetchall()
        scored = []
        seen = set()
        now = time.time()
        for row in rows:
            task = (row['task'] or '').strip().lower()
            if not task:
                continue
            # Filter internal/dev/test sessions from user-facing recap
            if task.startswith('phase') or task.startswith('v2-') or task.startswith('final-') or task.startswith('canonical-'):
                continue
            if 'telegram-recap' in task or 'runtime-test' in task or 'debug ffmpeg for /tmp/demo.mp4' in task:
                continue
            if task.startswith('what was the original') or task.startswith('continue from the same task') or task.startswith('you are in /home/deyaan666/live_brain_'):
                continue
            if task.startswith('review the conversation above and consider saving or updating a skill if appropriate'):
                continue
            # Deprioritize vague user replies / generic chatter
            if task in {'da', 'ne', 'ne znam vidi', 'restartovao sam', 'ok', 'okej', 'jel imas nekih problema'}:
                continue
            dedupe_key = (task, (row['root_cause'] or '').strip().lower(), (row['current_status'] or '').strip().lower())
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            hours = max(0.0, (now - row['updated_at']) / 3600.0)
            recency = 1.0 / (1.0 + hours / 24.0)
            # Simpler substance scoring: information density, not keyword hacks
            has_problem = 1.0 if (row['main_problem'] or row['root_cause']) else 0.0
            has_status = 1.0 if row['current_status'] in ('blocked', 'partially resolved', 'resolved') else 0.0
            substance = has_problem * 0.6 + has_status * 0.4
            score = substance * 0.7 + recency * 0.3
            scored.append((score, dict(row)))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [r for _, r in scored[:max_items]]

    def _belief_matches_query(self, text: str, terms: List[str], exact_entities: List[Dict[str, Any]]) -> bool:
        lowered = (text or '').lower()
        entity_names = [(e.get('display_name') or '').lower() for e in (exact_entities or [])]
        entity_terms = [n for n in entity_names if n]
        overlap = sum(1 for t in terms if t in lowered)
        entity_overlap = sum(1 for n in entity_terms if n in lowered)
        return overlap > 0 or entity_overlap > 0

    def _find_applicable_rules(self, query: str, max_items: int = 3) -> List[Dict[str, Any]]:
        q = (query or '').lower()
        rows = self.conn.execute("SELECT rule_id, scope, category, condition_json, action_json, confidence, times_confirmed FROM rules WHERE status='active' ORDER BY confidence DESC, times_confirmed DESC, updated_at DESC LIMIT 50").fetchall()
        out = []
        for row in rows:
            cond = json.loads(row['condition_json'])
            action = json.loads(row['action_json'])
            score = 0
            if cond.get('voice', '').lower() in q:
                score += 2
            if cond.get('text_language', '').lower() in q:
                score += 2
            if cond.get('provider', '').lower() in q:
                score += 2
            if 'tts' in q and row['category'] == 'voice_selection':
                score += 2
            if score > 0:
                out.append({
                    'rule_id': row['rule_id'],
                    'scope': row['scope'],
                    'category': row['category'],
                    'condition': cond,
                    'action': action,
                    'confidence': row['confidence'],
                    'times_confirmed': row['times_confirmed'],
                    '_score': score,
                })
        out.sort(key=lambda r: (r['_score'], r['confidence'], r['times_confirmed']), reverse=True)
        return out[:max_items]

    def _is_meta_belief(self, text: str) -> bool:
        lowered = (text or '').lower()
        return any(x in lowered for x in [
            'na osnovu onoga što vidim iz memorije',
            'based na onome što vidim iz memorije',
            'evo šta mogu da rekonstruišem',
            'sumarizuj',
            'recent work recap',
            'glavno otkriće koje sam dodao',
            'skill ažuriran',
        ])
