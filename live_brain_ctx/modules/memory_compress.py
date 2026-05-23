"""P2.5 — Memory block compression (plugin-only, no core patch).

The Hermes core ``MemoryStore`` (``tools/memory_tool.py``) loads MEMORY.md
and USER.md, dedups by *exact* equality, then freezes a system-prompt
snapshot. In practice the entries drift into near-duplicates ("User =
Deya/Dusa…" appears 3× with overlapping content), inflating the volatile
tier of the system prompt with no new information.

This module wraps ``MemoryStore.load_from_disk`` so that, immediately
before the snapshot is captured, entries are clustered by *containment*
(fraction of the shorter entry's tokens present in the longer one). For
each cluster we keep the longest representative and append a "(+N
similar)" suffix when N ≥ 1, so the human-curated facts stay intact but
duplicate paraphrases of the same fact collapse.

Plugin-only: registered from ``live_brain_ctx/__init__.py:register`` —
no edit to ``tools/memory_tool.py`` or ``run_agent.py``.
"""
from __future__ import annotations

import logging
import os
import re
import threading
from typing import Iterable, List, Set, Tuple

logger = logging.getLogger("live_brain_ctx.memory_compress")

_PATCH_LOCK = threading.Lock()
_PATCHED = False

_DEFAULT_THRESHOLD = 0.6
_MIN_TOKEN_LEN = 3
_MIN_TOKENS_FOR_CLUSTER = 4
_TOKEN_RE = re.compile(r"[A-Za-zÀ-ÿ0-9_]+", re.UNICODE)


def _tokens(text: str) -> Set[str]:
    if not text:
        return set()
    return {
        t.lower()
        for t in _TOKEN_RE.findall(text)
        if len(t) >= _MIN_TOKEN_LEN
    }


def _containment(a: Set[str], b: Set[str]) -> float:
    """Fraction of the smaller token set contained in the larger.

    For natural-language paraphrases of the same fact, containment ≥ 0.75
    captures "shorter entry is redundant given the longer one" while
    Jaccard is dragged down by extra wording in the longer entry.
    """
    if not a or not b:
        return 0.0
    inter = len(a & b)
    if inter == 0:
        return 0.0
    return inter / float(min(len(a), len(b)))


def _threshold() -> float:
    raw = os.environ.get("LIVE_BRAIN_CTX_MEMORY_SIMILARITY")
    if not raw:
        return _DEFAULT_THRESHOLD
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return _DEFAULT_THRESHOLD
    if 0.0 < v <= 1.0:
        return v
    return _DEFAULT_THRESHOLD


def compress_entries(entries: Iterable[str], *, threshold: float | None = None) -> Tuple[List[str], int]:
    """Cluster near-duplicate entries; keep the longest per cluster.

    Returns ``(compressed_entries, collapsed_count)`` where
    ``collapsed_count`` is the number of entries folded into a
    representative (i.e. ``len(original) - len(compressed)``).

    Order is preserved relative to the kept representatives' first
    appearance in the input. Each kept entry gets a "(+N similar)"
    suffix when N ≥ 1.
    """
    items: List[str] = [e for e in entries if isinstance(e, str) and e.strip()]
    if not items:
        return [], 0

    thr = threshold if threshold is not None else _threshold()
    tokens = [_tokens(e) for e in items]

    cluster_of: List[int] = [-1] * len(items)
    clusters: List[List[int]] = []

    for i, toks_i in enumerate(tokens):
        if cluster_of[i] != -1:
            continue
        if len(toks_i) < _MIN_TOKENS_FOR_CLUSTER:
            cluster_of[i] = len(clusters)
            clusters.append([i])
            continue
        cid = len(clusters)
        clusters.append([i])
        cluster_of[i] = cid
        # Single-link: an unassigned entry joins the cluster if it's
        # similar to ANY current member. Re-scan after each add until
        # no further candidates are found, so transitive paraphrases
        # (a≈b, b≈c, a≉c) end up in the same cluster.
        changed = True
        while changed:
            changed = False
            for j in range(i + 1, len(items)):
                if cluster_of[j] != -1:
                    continue
                if len(tokens[j]) < _MIN_TOKENS_FOR_CLUSTER:
                    continue
                for m in clusters[cid]:
                    if _containment(tokens[m], tokens[j]) >= thr:
                        clusters[cid].append(j)
                        cluster_of[j] = cid
                        changed = True
                        break

    out: List[str] = []
    collapsed = 0
    for members in clusters:
        if len(members) == 1:
            out.append(items[members[0]])
            continue
        # Keep the longest representative; tie-break by earliest index.
        best = max(members, key=lambda k: (len(items[k]), -k))
        extra = len(members) - 1
        collapsed += extra
        rep = items[best].rstrip()
        if not rep.endswith(")"):
            rep = f"{rep} (+{extra} similar)"
        else:
            rep = f"{rep} (+{extra} similar)"
        out.append(rep)

    return out, collapsed


def _patched_load_from_disk(self):  # type: ignore[no-untyped-def]
    """Replacement for ``MemoryStore.load_from_disk``.

    Same behavior as the original (read both files, exact-dedup, freeze
    snapshot) but inserts similarity-based compression between exact
    dedup and snapshot capture so the system-prompt block reflects the
    compressed view while ``memory_entries`` / ``user_entries`` (used by
    tool responses and writes) keep the full, unmodified list on disk.
    """
    from tools.memory_tool import get_memory_dir

    mem_dir = get_memory_dir()
    mem_dir.mkdir(parents=True, exist_ok=True)

    self.memory_entries = self._read_file(mem_dir / "MEMORY.md")
    self.user_entries = self._read_file(mem_dir / "USER.md")

    self.memory_entries = list(dict.fromkeys(self.memory_entries))
    self.user_entries = list(dict.fromkeys(self.user_entries))

    # Compress only the snapshot view; in-memory lists stay full so the
    # `memory` tool's read/replace/remove operations still see every
    # original entry exactly as it sits on disk.
    try:
        mem_compressed, mem_collapsed = compress_entries(self.memory_entries)
        user_compressed, user_collapsed = compress_entries(self.user_entries)
    except Exception as exc:  # pragma: no cover — never break agent boot
        logger.warning("memory compression failed, using raw entries: %s", exc)
        mem_compressed, user_compressed = self.memory_entries, self.user_entries
        mem_collapsed = user_collapsed = 0

    self._system_prompt_snapshot = {
        "memory": self._render_block("memory", mem_compressed),
        "user": self._render_block("user", user_compressed),
    }

    if mem_collapsed or user_collapsed:
        logger.info(
            "[memory_compress] collapsed %d memory + %d user entries (snapshot only)",
            mem_collapsed, user_collapsed,
        )


def install() -> bool:
    """Monkey-patch ``MemoryStore.load_from_disk``. Idempotent.

    Returns True on first successful patch, False on subsequent calls or
    when the import fails (e.g. core module renamed in a future Hermes).
    """
    global _PATCHED
    if os.environ.get("LIVE_BRAIN_CTX_MEMORY_COMPRESS", "1") == "0":
        return False
    with _PATCH_LOCK:
        if _PATCHED:
            return False
        try:
            from tools import memory_tool
        except Exception as exc:  # pragma: no cover — core unavailable
            logger.warning("[memory_compress] core memory_tool unavailable: %s", exc)
            return False
        memory_tool.MemoryStore.load_from_disk = _patched_load_from_disk  # type: ignore[assignment]
        _PATCHED = True
        logger.info("[memory_compress] MemoryStore.load_from_disk wrapped")
        return True
