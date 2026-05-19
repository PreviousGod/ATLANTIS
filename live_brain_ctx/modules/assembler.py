"""P2.1 + P2.3 + P2.4 — single context assembler for live_brain_ctx.

Takes the cascade-built `context` string emitted by `_pre_llm_call_inner`,
splits it into named sections, applies:

- per-section byte caps (section_byte_limits in context_config.json)
- per-turn-lane global byte budget (lane_byte_budgets)
- priority-based dropping (lowest priority drops first when over budget)
- cross-turn deduplication (P2.3): identical section vs previous turn ⇒
  replaced with 1-line pointer

Emits an audit dict for the per-turn budget log (P2.x telemetry).

Design constraints:
- Pure-function `assemble()` is safe to call with no DB / network.
- Unknown sections are kept at default priority/cap so we never silently
  drop a section we forgot to declare.
- A section whose body is empty (just the header line) is dropped.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("live_brain_ctx.assembler")

# ---------------------------------------------------------------------------
# Defaults — used if context_config.json is unreadable or missing a key.
# ---------------------------------------------------------------------------

DEFAULT_LANE_BYTE_BUDGETS: Dict[str, int] = {
    "chit_chat": 0,
    "simple_execution": 1500,
    "deep_execution": 5000,
    "research_or_epistemic": 4000,
    "document_intake": 1500,
    "continuation_or_resume": 3500,
    "_default": 4000,
}

DEFAULT_SECTION_BYTE_LIMIT = 600

# Priority: lower = more important = kept first.
DEFAULT_SECTION_PRIORITY = 50

# Header line of a section ends in ":" (e.g. "RECENT EPISODES:"). We match
# only headers that look like the existing live_brain_ctx conventions —
# uppercase tokens, optional spaces / slashes, ≤ 50 chars total.
_HEADER_RE = re.compile(r"^([A-Z][A-Z0-9 ./_-]{1,48}):\s*$")

_CONFIG_CACHE: Dict[str, Any] = {}
_CONFIG_LOCK = threading.Lock()
_CONFIG_MTIME = 0.0
_CONFIG_PATH = Path(__file__).resolve().parent.parent / "context_config.json"

# Per-session hash store for cross-turn dedup. Keyed by session_id, holds a
# dict mapping section title -> body hash from previous turn. Bounded by
# pruning entries older than 1 hour.
_SECTION_HASHES: Dict[str, Dict[str, str]] = {}
_SECTION_HASHES_TS: Dict[str, float] = {}
_SECTION_HASHES_LOCK = threading.Lock()
_HASH_TTL_SECONDS = 3600.0


@dataclass
class Section:
    title: str
    body: str  # body is the lines under the header, excluding the header line itself
    priority: int = DEFAULT_SECTION_PRIORITY
    byte_cap: int = DEFAULT_SECTION_BYTE_LIMIT

    def rendered(self) -> str:
        if not self.body.strip():
            return ""
        return f"{self.title}:\n{self.body.rstrip()}"

    def size(self) -> int:
        return len(self.rendered())


@dataclass
class AssembleAudit:
    session_id: str = ""
    lane: str = ""
    budget: int = 0
    sections_in: List[Tuple[str, int]] = field(default_factory=list)
    sections_kept: List[Tuple[str, int]] = field(default_factory=list)
    sections_dropped: List[Tuple[str, int, str]] = field(default_factory=list)  # title, size, reason
    sections_deduped: List[str] = field(default_factory=list)
    sections_capped: List[Tuple[str, int, int]] = field(default_factory=list)  # title, before, after
    total_in: int = 0
    total_out: int = 0


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def _load_config() -> Dict[str, Any]:
    """Load context_config.json with mtime-based hot reload."""
    global _CONFIG_MTIME, _CONFIG_CACHE
    try:
        st = _CONFIG_PATH.stat()
    except OSError:
        return _CONFIG_CACHE
    with _CONFIG_LOCK:
        if st.st_mtime == _CONFIG_MTIME and _CONFIG_CACHE:
            return _CONFIG_CACHE
        try:
            with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
                _CONFIG_CACHE = json.load(f) or {}
        except Exception as exc:
            logger.warning("[assembler] failed to load context_config.json: %s", exc)
            if not _CONFIG_CACHE:
                _CONFIG_CACHE = {}
            return _CONFIG_CACHE
        _CONFIG_MTIME = st.st_mtime
        return _CONFIG_CACHE


def lane_byte_budget(lane: str) -> int:
    cfg = _load_config().get("lane_byte_budgets") or {}
    if lane in cfg:
        try:
            return int(cfg[lane])
        except (TypeError, ValueError):
            pass
    if "_default" in cfg:
        try:
            return int(cfg["_default"])
        except (TypeError, ValueError):
            pass
    return DEFAULT_LANE_BYTE_BUDGETS.get(lane, DEFAULT_LANE_BYTE_BUDGETS["_default"])


def _section_byte_cap(title: str) -> int:
    cfg = _load_config().get("section_byte_limits") or {}
    try:
        return int(cfg.get(title, DEFAULT_SECTION_BYTE_LIMIT))
    except (TypeError, ValueError):
        return DEFAULT_SECTION_BYTE_LIMIT


def _section_priority(title: str) -> int:
    cfg = _load_config().get("section_priorities") or {}
    try:
        return int(cfg.get(title, DEFAULT_SECTION_PRIORITY))
    except (TypeError, ValueError):
        return DEFAULT_SECTION_PRIORITY


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def parse_sections(context: str) -> List[Section]:
    """Split a cascade-built context string into Section objects.

    Sections are delimited by header lines matching ``HEADER:`` followed by
    body lines until the next header (or EOF). Text before the first header
    becomes an anonymous "PREAMBLE" section with priority 0 so it always
    survives — typically empty for our pipeline.
    """
    if not context or not context.strip():
        return []

    sections: List[Section] = []
    current_title: Optional[str] = None
    current_body: List[str] = []
    preamble: List[str] = []

    for raw_line in context.split("\n"):
        m = _HEADER_RE.match(raw_line.strip())
        if m:
            # flush previous
            if current_title is not None:
                sections.append(_make_section(current_title, current_body))
            else:
                if any(line.strip() for line in preamble):
                    sections.append(Section(title="PREAMBLE", body="\n".join(preamble), priority=0, byte_cap=10_000))
            current_title = m.group(1).strip()
            current_body = []
        else:
            if current_title is None:
                preamble.append(raw_line)
            else:
                current_body.append(raw_line)

    if current_title is not None:
        sections.append(_make_section(current_title, current_body))
    elif any(line.strip() for line in preamble):
        sections.append(Section(title="PREAMBLE", body="\n".join(preamble), priority=0, byte_cap=10_000))

    return sections


def _make_section(title: str, body_lines: List[str]) -> Section:
    body = "\n".join(body_lines).strip()
    return Section(
        title=title,
        body=body,
        priority=_section_priority(title),
        byte_cap=_section_byte_cap(title),
    )


# ---------------------------------------------------------------------------
# Per-section byte cap
# ---------------------------------------------------------------------------

def apply_section_caps(sections: List[Section], audit: AssembleAudit) -> None:
    """Truncate any section body whose rendered size exceeds its byte cap.

    Truncation drops complete lines from the tail of the body — never a
    half-line. The header line is always preserved. If no body line fits,
    the section is dropped from the list (caller filters empties).
    """
    for s in sections:
        size = s.size()
        if size <= s.byte_cap:
            continue
        body_lines = s.body.split("\n")
        kept: List[str] = []
        running = len(f"{s.title}:\n")
        for line in body_lines:
            line_size = len(line) + (1 if kept else 0)  # +1 for the newline
            if running + line_size > s.byte_cap:
                break
            kept.append(line)
            running += line_size
        before = size
        s.body = "\n".join(kept).strip()
        after = s.size()
        audit.sections_capped.append((s.title, before, after))


# ---------------------------------------------------------------------------
# Cross-turn dedup (P2.3)
# ---------------------------------------------------------------------------

def _hash_body(body: str) -> str:
    return hashlib.sha1(body.encode("utf-8", errors="replace")).hexdigest()[:16]


def _prune_stale_hash_entries(now: float) -> None:
    stale = [sid for sid, ts in _SECTION_HASHES_TS.items() if now - ts > _HASH_TTL_SECONDS]
    for sid in stale:
        _SECTION_HASHES.pop(sid, None)
        _SECTION_HASHES_TS.pop(sid, None)


def dedupe_against_prior_turn(
    sections: List[Section],
    session_id: str,
    audit: AssembleAudit,
) -> List[Section]:
    """For each section, if its body hash matches the prior turn's, replace
    body with a 1-line pointer. Always update the stored hash to the new
    body so the dedupe is one-shot — repeated identical content gets the
    pointer; if the body changes, full content reappears.
    """
    if not session_id:
        return sections

    now = time.time()
    with _SECTION_HASHES_LOCK:
        _prune_stale_hash_entries(now)
        prior = _SECTION_HASHES.get(session_id, {})
        new_store = dict(prior)
        out: List[Section] = []
        for s in sections:
            h = _hash_body(s.body)
            if prior.get(s.title) == h and s.body.strip():
                pointer = f"unchanged from previous turn (hash {h[:8]})"
                out.append(Section(
                    title=s.title,
                    body=pointer,
                    priority=s.priority,
                    byte_cap=s.byte_cap,
                ))
                audit.sections_deduped.append(s.title)
            else:
                out.append(s)
            new_store[s.title] = h
        _SECTION_HASHES[session_id] = new_store
        _SECTION_HASHES_TS[session_id] = now
    return out


def clear_session_hashes(session_id: str) -> None:
    """Drop dedup state for a session (e.g. when /new is issued)."""
    with _SECTION_HASHES_LOCK:
        _SECTION_HASHES.pop(session_id, None)
        _SECTION_HASHES_TS.pop(session_id, None)


# ---------------------------------------------------------------------------
# Global byte budget
# ---------------------------------------------------------------------------

def apply_global_budget(
    sections: List[Section],
    budget: int,
    audit: AssembleAudit,
) -> List[Section]:
    """Drop lowest-priority sections until total rendered size ≤ budget.

    Sections with priority < 10 (e.g. RECENT RISK ACTIVITY, UNVERIFIED
    CLAIM) are protected — even at the cost of overshooting the budget by
    a small amount — because they're corrective safety signals.
    """
    if budget <= 0:
        # Special case: budget 0 (chit_chat) — keep only protected sections.
        kept: List[Section] = []
        for s in sections:
            if s.priority < 10:
                kept.append(s)
            else:
                audit.sections_dropped.append((s.title, s.size(), "budget_zero"))
        return kept

    total = sum(s.size() + 2 for s in sections)  # +2 for inter-section "\n\n"
    if total <= budget:
        return sections

    # Sort drop candidates by priority desc (drop biggest priority number first)
    by_priority = sorted(sections, key=lambda s: (-s.priority, -s.size()))
    surviving = list(sections)
    survivor_map = {id(s): s for s in surviving}

    for victim in by_priority:
        if total <= budget:
            break
        if victim.priority < 10:
            continue  # protected
        # remove victim from surviving
        idx = next((i for i, s in enumerate(surviving) if id(s) == id(victim)), -1)
        if idx < 0:
            continue
        size = surviving[idx].size() + 2
        surviving.pop(idx)
        total -= size
        audit.sections_dropped.append((victim.title, victim.size(), "over_budget"))

    return surviving


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def assemble(
    context: str,
    *,
    turn_lane: str,
    session_id: str = "",
    dedupe: bool = True,
) -> Tuple[str, AssembleAudit]:
    """Run the full assembler pipeline.

    Returns the rebuilt context string + audit metadata.
    """
    audit = AssembleAudit(session_id=session_id, lane=turn_lane)
    sections = parse_sections(context or "")
    audit.sections_in = [(s.title, s.size()) for s in sections]
    audit.total_in = sum(s.size() + 2 for s in sections)

    apply_section_caps(sections, audit)

    if dedupe and session_id:
        sections = dedupe_against_prior_turn(sections, session_id, audit)

    budget = lane_byte_budget(turn_lane)
    audit.budget = budget

    # drop empty bodies (header w/ no content) before budget calc
    sections = [s for s in sections if s.body.strip()]

    sections = apply_global_budget(sections, budget, audit)

    # Preserve priority-sorted output (lower priority number first)
    sections.sort(key=lambda s: (s.priority, s.title))

    rendered_parts = [s.rendered() for s in sections if s.rendered()]
    out = "\n\n".join(rendered_parts)

    audit.sections_kept = [(s.title, s.size()) for s in sections]
    audit.total_out = len(out)

    return out, audit


# ---------------------------------------------------------------------------
# P2.x telemetry — budget audit log
# ---------------------------------------------------------------------------

_AUDIT_LOG_PATH: Optional[Path] = None
_AUDIT_LOG_LOCK = threading.Lock()


def _audit_log_path() -> Path:
    global _AUDIT_LOG_PATH
    if _AUDIT_LOG_PATH is not None:
        return _AUDIT_LOG_PATH
    home = os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
    log_dir = Path(home) / "logs"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    _AUDIT_LOG_PATH = log_dir / "context-budget.log"
    return _AUDIT_LOG_PATH


def log_audit(audit: AssembleAudit) -> None:
    """Append an audit record to ~/.hermes/logs/context-budget.log.

    Never raises — pure observability path. Each line is a compact JSON
    record so the file is grep-friendly *and* machine-readable.
    """
    record = {
        "ts": time.time(),
        "session": audit.session_id,
        "lane": audit.lane,
        "budget": audit.budget,
        "in_bytes": audit.total_in,
        "out_bytes": audit.total_out,
        "saved_bytes": max(0, audit.total_in - audit.total_out),
        "in_sections": [t for t, _ in audit.sections_in],
        "kept_sections": [t for t, _ in audit.sections_kept],
        "dropped": [(t, sz, reason) for t, sz, reason in audit.sections_dropped],
        "deduped": list(audit.sections_deduped),
        "capped": [(t, b, a) for t, b, a in audit.sections_capped],
    }
    try:
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
    except Exception:
        return
    path = _audit_log_path()
    try:
        with _AUDIT_LOG_LOCK:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception:
        pass
