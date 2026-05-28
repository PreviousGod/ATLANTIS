"""Structured research layer for Nucleus.

Research does not answer normal chat directly. It gathers local/web evidence,
writes durable facts/recipes into Live Brain, and adds knowledge paths to Pargod.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from pathlib import Path

from .domain_profiles import DomainProfile, get_domain_profile
from .web_search import search

log = logging.getLogger("nucleus")

_TOKEN_RE = re.compile(r"[a-zA-Z0-9_]{3,}")
_MAX_SNIPPET = 700
_MAX_LOCAL_MATCHES = 5
_MAX_WEB_MATCHES = 5


def stable_id(prefix: str, text: str, length: int = 12) -> str:
    digest = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:length]
    return f"{prefix}_{digest}"


def _tokens(text: str) -> set[str]:
    return {tok.lower() for tok in _TOKEN_RE.findall(text or "")}


def _read_limited(path: Path, limit: int) -> str:
    try:
        with path.open("rb") as handle:
            data = handle.read(limit)
        return data.decode("utf-8", "replace")
    except OSError:
        return ""


def _compact(text: str, limit: int = _MAX_SNIPPET) -> str:
    compacted = re.sub(r"\s+", " ", text or "").strip()
    return compacted[:limit]


def _best_excerpt(text: str, terms: set[str], limit: int = _MAX_SNIPPET) -> str:
    if not text:
        return ""
    lowered = text.lower()
    positions = [lowered.find(term) for term in terms if term in lowered]
    positions = [pos for pos in positions if pos >= 0]
    start = max(0, min(positions) - 180) if positions else 0
    return _compact(text[start:start + limit], limit)


def _score_text(text: str, terms: set[str]) -> int:
    if not text or not terms:
        return 0
    lowered = text.lower()
    return sum(lowered.count(term) for term in terms)


def collect_local_sources(problem: str, profile: DomainProfile, max_matches: int = _MAX_LOCAL_MATCHES) -> list[dict]:
    terms = _tokens(problem)
    matches = []
    for path in profile.iter_local_sources():
        text = _read_limited(path, profile.max_file_bytes)
        score = _score_text(text, terms) + _score_text(str(path), terms) * 2
        if score <= 0:
            continue
        matches.append({
            "kind": "local",
            "path": str(path),
            "title": path.name,
            "snippet": _best_excerpt(text, terms),
            "score": score,
        })
    matches.sort(key=lambda item: item["score"], reverse=True)
    return matches[:max_matches]


def collect_web_sources(problem: str, profile: DomainProfile, max_matches: int = _MAX_WEB_MATCHES) -> list[dict]:
    query = f"{problem} {profile.search_suffix}".strip()
    try:
        results = search(query, limit=max_matches)
    except Exception as exc:
        log.info("Research web search failed for %r: %s", problem, exc)
        return []
    sources = []
    for result in results or []:
        url = result.get("url", "")
        if not url or not profile.allows_url(url):
            continue
        sources.append({
            "kind": "web",
            "url": url,
            "title": result.get("title", ""),
            "snippet": _compact(result.get("snippet", result.get("title", ""))),
            "score": 1,
        })
    return sources[:max_matches]


def build_fix_recipe(problem: str, scope: str, local_sources: list[dict], web_sources: list[dict]) -> dict | None:
    sources = local_sources + web_sources
    if not sources:
        return None
    steps = [
        f"Identify the failing {scope} component from logs or the exact error message.",
        "Review the highest-confidence Nucleus research sources listed with this recipe.",
        "Apply the smallest local code/config change that matches the documented behavior.",
        "Validate with the narrowest relevant test or service status check before broader rollout.",
    ]
    success = "Problem is reproducible before the fix, resolved after the fix, and no related regression appears in logs/tests."
    return {
        "problem_pattern": problem,
        "steps": steps,
        "success_criteria": success,
        "source_refs": [source.get("path") or source.get("url") for source in sources],
    }


def _facts_from_sources(problem: str, sources: list[dict]) -> list[str]:
    facts = []
    for source in sources[:6]:
        ref = source.get("path") or source.get("url") or source.get("title") or "unknown source"
        snippet = source.get("snippet", "")
        if snippet:
            facts.append(f"Research for '{problem}' found relevant evidence in {ref}: {snippet[:260]}")
    return facts


def _write_live_brain(result: dict, brain_sync) -> dict:
    if not brain_sync:
        return {}
    writes = {"facts": [], "fix_recipe": None, "research": None}
    citations = result.get("citations", [])
    for fact in result.get("facts", []):
        fact_id = brain_sync.write_fact(
            fact,
            scope_key=result["scope"],
            question=result["problem"],
            confidence=result["confidence"],
            source_urls=citations,
        )
        if fact_id:
            writes["facts"].append(fact_id)
    if result.get("fix_recipe"):
        writes["fix_recipe"] = brain_sync.write_fix_recipe(
            result["problem"],
            result["fix_recipe"]["steps"],
            scope_key=result["scope"],
            sources=citations,
            success_criteria=result["fix_recipe"].get("success_criteria", ""),
            confidence=result["confidence"],
        )
    writes["research"] = brain_sync.write_research_trace(result)
    return writes


def _write_pargod(result: dict, pargod) -> list[str]:
    if not pargod:
        return []
    if hasattr(pargod, "add_research_result"):
        return pargod.add_research_result(result)
    created = []
    problem_label = stable_id("problem", f"{result['scope']}:{result['problem']}")
    if not pargod.get_node(problem_label):
        pargod.add_node("problem", problem_label, result["problem"])
        created.append(problem_label)
    for source in result.get("local_sources", []) + result.get("web_sources", []):
        ref = source.get("path") or source.get("url") or source.get("title", "")
        label = stable_id("kb", ref + source.get("snippet", ""))
        if not pargod.get_node(label):
            pargod.add_node("knowledge", label, source.get("snippet", ""))
            created.append(label)
        pargod.add_edge(problem_label, label, "INFORMED_BY", 0.5)
    if result.get("fix_recipe"):
        recipe_label = stable_id("recipe", result["problem"] + json.dumps(result["fix_recipe"], sort_keys=True))
        if not pargod.get_node(recipe_label):
            pargod.add_node("knowledge", recipe_label, json.dumps(result["fix_recipe"], ensure_ascii=False))
            created.append(recipe_label)
        pargod.add_edge(problem_label, recipe_label, "SUPPORTS", 0.7)
    return created


def research_problem(
    problem: str,
    *,
    profile: DomainProfile | None = None,
    brain_sync=None,
    pargod=None,
    include_web: bool = True,
) -> dict | None:
    problem = (problem or "").strip()
    if not problem:
        return None
    profile = profile or get_domain_profile(problem)
    local_sources = collect_local_sources(problem, profile)
    web_sources = collect_web_sources(problem, profile) if include_web else []
    sources = local_sources + web_sources
    if not sources:
        return None
    confidence = min(0.95, 0.45 + len(local_sources) * 0.1 + len(web_sources) * 0.08)
    citations = [source.get("path") or source.get("url") for source in sources if source.get("path") or source.get("url")]
    result = {
        "problem": problem,
        "scope": profile.scope,
        "query": f"{problem} {profile.search_suffix}".strip(),
        "local_sources": local_sources,
        "web_sources": web_sources,
        "facts": _facts_from_sources(problem, sources),
        "fix_recipe": build_fix_recipe(problem, profile.scope, local_sources, web_sources),
        "confidence": round(confidence, 2),
        "citations": citations,
    }
    result["live_brain_writes"] = _write_live_brain(result, brain_sync)
    result["pargod_nodes"] = _write_pargod(result, pargod)
    log.info(
        "RESEARCH: %r scope=%s local=%d web=%d confidence=%.2f",
        problem, profile.scope, len(local_sources), len(web_sources), result["confidence"],
    )
    return result


def format_research_summary(result: dict) -> str:
    if not result:
        return "[NUCLEUS/RESEARCH] No research result."
    lines = [
        f"[NUCLEUS/RESEARCH] scope={result.get('scope')} confidence={result.get('confidence')}",
        f"Problem: {result.get('problem')}",
    ]
    if result.get("facts"):
        lines.append("\nLearned facts:")
        for fact in result["facts"][:4]:
            lines.append(f"- {fact}")
    if result.get("fix_recipe"):
        lines.append("\nSuggested recipe:")
        for step in result["fix_recipe"].get("steps", [])[:5]:
            lines.append(f"- {step}")
    if result.get("citations"):
        lines.append("\nSources:")
        for citation in result["citations"][:6]:
            lines.append(f"- {citation}")
    return "\n".join(lines)
