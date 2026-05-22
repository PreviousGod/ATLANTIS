"""
instinct_compiler.py  —  Faza 3: Test-Driven Instinct Compiler

Noćna petlja sinteze gena. Skenira reasoning_turns, bira uspešne sesije,
generiše skill.py + test_skill.py, pokreće ih kroz instinct_guard (Arena),
i preživljavajuće inkubira u ~/.hermes/skills/auto_instincts/.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .instinct_guard import InstinctGuard

logger = logging.getLogger(__name__)

DB_PATH = os.environ.get("LIVE_BRAIN_DB", "/home/deyaan666/.hermes/live_brain.db")
AUTO_INSTINCTS_DIR = Path.home() / ".hermes" / "skills" / "auto_instincts"
FORBIDDEN_SESSION_IDS: set[str] = {"test", "debug", "sandbox", "e2e_seed"}

_guard = InstinctGuard()

# ---------------------------------------------------------------------------
# Schema for tracking instinct attempts (survivors & ruled_out)
# ---------------------------------------------------------------------------
INSTINCT_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS instinct_attempts (
    attempt_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    step_signature TEXT NOT NULL DEFAULT '',
    skill_name TEXT NOT NULL,
    skill_sha256 TEXT NOT NULL DEFAULT '',
    test_sha256 TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',   -- pending | survived | ruled_out | error
    guard_report_json TEXT NOT NULL DEFAULT '{}',
    stderr TEXT NOT NULL DEFAULT '',
    trigger_pattern TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_instinct_session ON instinct_attempts(session_id, status);
CREATE INDEX IF NOT EXISTS idx_instinct_status ON instinct_attempts(status, updated_at DESC);
"""

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------
@dataclass
class SessionTranscript:
    session_id: str
    steps: List[Dict[str, Any]] = field(default_factory=list)
    tools_used: set[str] = field(default_factory=set)
    has_synthesize: bool = False
    has_final: bool = False
    is_validated: bool = False

    def to_prompt_text(self) -> str:
        lines: List[str] = [
            f"SESSION: {self.session_id}",
            f"Tools used: {', '.join(sorted(self.tools_used))}",
            "---",
        ]
        for s in self.steps:
            lines.append(f"[{s['step_index']}] {s['step']} | tool={s['tool_name']}")
            inp = json.loads(s.get("input_json") or "{}")
            out = json.loads(s.get("output_json") or "{}")
            if inp:
                lines.append(f"  IN:  {json.dumps(inp, ensure_ascii=False)[:400]}")
            if out:
                lines.append(f"  OUT: {json.dumps(out, ensure_ascii=False)[:400]}")
        return "\n".join(lines)


@dataclass
class InstinctKit:
    skill_code: str
    test_code: str
    session_id: str
    attempt_id: str
    skill_path: Path = field(default_factory=Path)
    test_path: Path = field(default_factory=Path)


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------
def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.executescript(INSTINCT_SCHEMA_SQL)
    return conn


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# 1. TRIAGE  —  collect & score candidate sessions
# ---------------------------------------------------------------------------
def triage_candidates(
    *,
    min_tools: int = 2,
    require_synthesize: bool = True,
    lookback_hours: float = 168.0,   # last 7 days
    limit: int = 50,
) -> List[SessionTranscript]:
    """Pull sessions from reasoning_turns that look like solved problems."""
    cutoff = time.time() - lookback_hours * 3600
    conn = _db()
    try:
        rows = conn.execute(
            """
            SELECT session_id, step, step_index, tool_name, input_json, output_json, validated, created_at
            FROM reasoning_turns
            WHERE created_at >= ?
            ORDER BY session_id, step_index
            """,
            (cutoff,),
        ).fetchall()
    finally:
        conn.close()

    sessions: Dict[str, SessionTranscript] = {}
    for r in rows:
        sid = r["session_id"]
        if not sid or sid.lower() in FORBIDDEN_SESSION_IDS:
            continue
        if sid not in sessions:
            sessions[sid] = SessionTranscript(session_id=sid)
        st = sessions[sid]
        st.steps.append({k: r[k] for k in r.keys()})
        st.tools_used.add(r["tool_name"])
        if r["step"].lower() == "synthesize":
            st.has_synthesize = True
        if r["step"].lower() == "final":
            st.has_final = True
        if r["validated"]:
            st.is_validated = True

    candidates: List[SessionTranscript] = []
    for st in sessions.values():
        if len(st.tools_used) < min_tools:
            continue
        if require_synthesize and not st.has_synthesize:
            continue
        # Heuristic: if session has 'attack' followed by 'final', treat as resolved
        if not st.has_final:
            continue
        candidates.append(st)

    # Sort by richness (more tools = more complex = higher value)
    candidates.sort(key=lambda c: len(c.tools_used), reverse=True)
    return candidates[:limit]


# ---------------------------------------------------------------------------
# 2. GOD PROMPT  —  build the prompt that wakes the 8B architect
# ---------------------------------------------------------------------------
def build_god_prompt(transcript: SessionTranscript) -> str:
    """Construct the exact prompt we feed to the model."""
    return (
        "Ti si arhitekta sistema. Korisnik je imao ovaj problem, i ti si ga rešio nizom koraka. "
        "Sada taj proces mora postati autonoman instinkt.\n\n"
        "Transkript sesije:\n"
        f"{transcript.to_prompt_text()}\n\n"
        "Zadatak:\n"
        "1. Napiši skill.py koji sadrži glavnu funkciju run_instinct(data). "
        "Kod mora biti univerzalan i otporan na greške. "
        "Dozvoljene biblioteke: math, re, json, datetime, collections, itertools, statistics.\n"
        "2. Napiši test_skill.py koji koristi assert da proveri ivice (edge cases) tvoje funkcije.\n"
        "3. Formatiraj izlaz striktno u tagove:\n"
        "   <skill_code>\n   ...\n   </skill_code>\n"
        "   <test_code>\n   ...\n   </test_code>\n"
        "Nemoj dodavati bilo kakav objašnjavajući tekst izvan tagova."
    )


# ---------------------------------------------------------------------------
# 3. ARENA  —  parse model output, save, run through instinct_guard
# ---------------------------------------------------------------------------
def parse_model_output(raw: str) -> Tuple[str, str]:
    """Extract skill_code and test_code from model response."""
    skill_pat = re.compile(r"<skill_code>\s*(.*?)\s*</skill_code>", re.S | re.I)
    test_pat = re.compile(r"<test_code>\s*(.*?)\s*</test_code>", re.S | re.I)
    skill_m = skill_pat.search(raw)
    test_m = test_pat.search(raw)
    if not skill_m or not test_m:
        raise ValueError("Model output missing <skill_code> or <test_code> tags")
    return skill_m.group(1).strip(), test_m.group(1).strip()


def prepare_arena(skill_code: str, test_code: str, session_id: str) -> InstinctKit:
    """Write files to a temp sandbox and return paths."""
    attempt_id = _sha256(f"{session_id}:{time.time()}")
    tmpdir = Path(tempfile.gettempdir()) / f"instinct_arena_{attempt_id}"
    tmpdir.mkdir(parents=True, exist_ok=True)
    skill_path = tmpdir / "skill.py"
    test_path = tmpdir / "test_skill.py"
    skill_path.write_text(skill_code, encoding="utf-8")
    test_path.write_text(test_code, encoding="utf-8")
    return InstinctKit(
        skill_code=skill_code,
        test_code=test_code,
        session_id=session_id,
        attempt_id=attempt_id,
        skill_path=skill_path,
        test_path=test_path,
    )


def arena_battle(kit: InstinctKit) -> Dict[str, Any]:
    """Run the test file through instinct_guard and return full report."""
    # First audit BOTH files (belt & suspenders)
    skill_audit = _guard.audit_code(kit.skill_code)
    test_audit = _guard.audit_code(kit.test_code)
    if not skill_audit.is_safe:
        return {
            "status": "ruled_out",
            "reason": "skill_audit_failed",
            "guard_report": skill_audit.to_dict(),
            "stdout": "",
            "stderr": json.dumps(skill_audit.to_dict()["violations"], ensure_ascii=False),
            "returncode": -1,
        }
    if not test_audit.is_safe:
        return {
            "status": "ruled_out",
            "reason": "test_audit_failed",
            "guard_report": test_audit.to_dict(),
            "stdout": "",
            "stderr": json.dumps(test_audit.to_dict()["violations"], ensure_ascii=False),
            "returncode": -1,
        }

    # Run test through sandbox
    report = _guard.sandbox_run(kit.test_code)
    return report


# ---------------------------------------------------------------------------
# 4. DARWIN FILTER  —  survive → skills/ ; die → ruled_out with stderr
# ---------------------------------------------------------------------------
def darwin_filter(
    kit: InstinctKit,
    report: Dict[str, Any],
    conn: sqlite3.Connection,
) -> Dict[str, Any]:
    """Record result and promote survivors to permanent storage."""
    now = time.time()
    guard_report = report.get("audit", report.get("guard_report", {}))
    stdout = report.get("stdout", "")
    stderr = report.get("stderr", "")
    returncode = report.get("returncode", -999)

    survived = (
        report.get("status") == "ok"
        and returncode == 0
        and guard_report.get("is_safe", False)
    )

    status = "survived" if survived else "ruled_out"

    # Derive a trigger pattern from session steps (first 3 tool names + step labels)
    trigger_bits: List[str] = []
    for s in kit.skill_code.splitlines()[:20]:
        if "def run_instinct" in s:
            trigger_bits.append("run_instinct")
            break
    # Simple keyword heuristic from skill source
    keywords = re.findall(r'"([^"]{5,30})"', kit.skill_code)
    trigger_pattern = " | ".join(keywords[:3]) if keywords else "auto_instinct"

    conn.execute(
        """
        INSERT OR REPLACE INTO instinct_attempts
        (attempt_id, session_id, step_signature, skill_name, skill_sha256, test_sha256,
         status, guard_report_json, stderr, trigger_pattern, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            kit.attempt_id,
            kit.session_id,
            "",  # step_signature can be enriched later
            f"instinct_{kit.session_id}_{kit.attempt_id[:8]}",
            _sha256(kit.skill_code),
            _sha256(kit.test_code),
            status,
            json.dumps(guard_report, ensure_ascii=False, sort_keys=True),
            stderr,
            trigger_pattern,
            now,
            now,
        ),
    )
    conn.commit()

    if survived:
        target_dir = AUTO_INSTINCTS_DIR / kit.attempt_id
        target_dir.mkdir(parents=True, exist_ok=True)
        (target_dir / "skill.py").write_text(kit.skill_code, encoding="utf-8")
        (target_dir / "test_skill.py").write_text(kit.test_code, encoding="utf-8")
        # Write a minimal SKILL.md so Hermes can load it
        skill_md = target_dir / "SKILL.md"
        skill_md.write_text(
            f"""---
name: instinct_{kit.session_id}_{kit.attempt_id[:8]}
description: Auto-generated instinct from session {kit.session_id}
trigger: {trigger_pattern}
---

# Auto Instinct

Generated by instinct_compiler at {time.strftime('%Y-%m-%d %H:%M', time.localtime(now))}.

## Usage

```python
from skill import run_instinct
result = run_instinct(data)
```
""",
            encoding="utf-8",
        )
        logger.info("[instinct_compiler] SURVIVOR %s → %s", kit.attempt_id, target_dir)
        return {"status": "survived", "attempt_id": kit.attempt_id, "path": str(target_dir)}
    else:
        logger.info(
            "[instinct_compiler] RULED_OUT %s | rc=%s stderr=%s",
            kit.attempt_id,
            returncode,
            stderr[:200],
        )
        return {
            "status": "ruled_out",
            "attempt_id": kit.attempt_id,
            "reason": stderr,
            "guard_report": guard_report,
        }


# ---------------------------------------------------------------------------
# 5. MAIN LOOP  —  end-to-end compiler run
# ---------------------------------------------------------------------------
def compile_instincts(
    *,
    model_runner: Optional[Any] = None,
    dry_run: bool = False,
    limit: int = 10,
) -> List[Dict[str, Any]]:
    """
    Full nightly pipeline.

    *model_runner* — callable(prompt: str) -> str that talks to the 8B model.
    If None, prompts are dumped to stdout and you must pipe model output back in.
    """
    AUTO_INSTINCTS_DIR.mkdir(parents=True, exist_ok=True)
    candidates = triage_candidates(limit=limit)
    if not candidates:
        logger.info("[instinct_compiler] No candidate sessions found.")
        return []

    results: List[Dict[str, Any]] = []
    conn = _db()
    try:
        for cand in candidates:
            prompt = build_god_prompt(cand)
            if dry_run:
                results.append({"session_id": cand.session_id, "dry_run": True, "prompt": prompt[:500]})
                continue

            if model_runner is None:
                # Headless mode: write prompt to file, expect operator or cron to feed it to model
                prompt_file = AUTO_INSTINCTS_DIR / f"prompt_{cand.session_id}.txt"
                prompt_file.write_text(prompt, encoding="utf-8")
                results.append({
                    "session_id": cand.session_id,
                    "status": "prompt_written",
                    "path": str(prompt_file),
                })
                continue

            try:
                raw_output = model_runner(prompt)
                skill_code, test_code = parse_model_output(raw_output)
            except Exception as exc:
                logger.warning("[instinct_compiler] parse error for %s: %s", cand.session_id, exc)
                results.append({"session_id": cand.session_id, "status": "parse_error", "error": str(exc)})
                continue

            kit = prepare_arena(skill_code, test_code, cand.session_id)
            report = arena_battle(kit)
            verdict = darwin_filter(kit, report, conn)
            results.append({"session_id": cand.session_id, **verdict})
    finally:
        conn.close()

    return results


# ---------------------------------------------------------------------------
# CLI entrypoint for cron / manual invocation
# ---------------------------------------------------------------------------
def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="Instinct Compiler — Faza 3 NUCLEUS")
    parser.add_argument("--dry-run", action="store_true", help="Only triage and show prompts")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--model-script", type=str, default="", help="Path to script that accepts prompt on stdin and returns model output on stdout")
    args = parser.parse_args()

    model_runner = None
    if args.model_script:
        def _runner(prompt: str) -> str:
            proc = subprocess.run(
                [sys.executable, args.model_script],
                input=prompt,
                capture_output=True,
                text=True,
                timeout=300,
            )
            if proc.returncode != 0:
                raise RuntimeError(f"Model script failed: {proc.stderr}")
            return proc.stdout
        model_runner = _runner

    results = compile_instincts(model_runner=model_runner, dry_run=args.dry_run, limit=args.limit)
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
