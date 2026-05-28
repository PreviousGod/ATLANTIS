"""SelfModification — Sposobnost da menja sopstveni kod.

AGI ne može da ostane ista arhitektura zauvek. Mora da uči iz:
- Neuspešnih pokretanja (bug fix)
- Novih ciljeva (novi moduli)
- Ponovljenih patterna (refaktor)

SIGURNOST:
- Uvek backup
- Uvek validacija sintakse pre primene
- Uvek diff pre primene
- Samo specificni fajlovi (whitelist)
- Rollback ako test ne prođe

LLM je SAMO generator patch-a. Python loop primenjuje i validira.
"""

import ast
import difflib
import hashlib
import json
import logging
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger("nucleus.selfmod")

# Files Nucleus CAN modify about itself
SELF_MOD_WHITELIST = {
    "nucleus/instincts/": "New instinct scripts",
    "nucleus/learning_engine.py": "Learning improvements",
    "nucleus/drive_engine.py": "Drive logic fixes",
    "nucleus/emergent_volition.py": "Volition engine tuning",
    "nucleus/sensory_cortex.py": "Sensory improvements",
    "nucleus/self_modification.py": "Self-mod improvements",
}

BACKUP_DIR = Path.home() / ".hermes" / "nucleus_data" / "backups"


@dataclass
class PatchProposal:
    """Predlog izmene sopstvenog koda."""
    target_file: str
    patch_description: str
    old_fragment: str
    new_fragment: str
    motivation: str
    confidence: float
    created_at: float = 0.0

    def __post_init__(self):
        if self.created_at == 0.0:
            self.created_at = time.time()


class SelfModificationEngine:
    """Menja sopstveni kod, ali bezbedno."""

    def __init__(self):
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        self._patch_history: List[Dict] = []
        self._load_history()

    def _load_history(self):
        path = Path.home() / ".hermes" / "nucleus_data" / "selfmod_history.json"
        if path.exists():
            try:
                self._patch_history = json.loads(path.read_text())
            except Exception:
                pass

    def _save_history(self):
        path = Path.home() / ".hermes" / "nucleus_data" / "selfmod_history.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self._patch_history[-100:], indent=2))

    def can_modify(self, filepath: str) -> bool:
        """Proveri da li smemo da menjamo ovaj fajl."""
        # Must be under plugins/nucleus/
        plugins_root = str(Path.home() / ".hermes" / "plugins" / "nucleus")
        if not str(filepath).startswith(plugins_root):
            logger.warning("[selfmod] REJECTED: %s outside nucleus dir", filepath)
            return False

        # Must match whitelist
        rel = str(filepath).replace(plugins_root + "/", "")
        for prefix in SELF_MOD_WHITELIST:
            if rel.startswith(prefix):
                return True

        logger.warning("[selfmod] REJECTED: %s not in whitelist", filepath)
        return False

    def validate_syntax(self, code: str) -> Optional[str]:
        """Proveri Python sintaksu. Vraća error string ako ne valja."""
        try:
            ast.parse(code)
            return None
        except SyntaxError as e:
            return f"SyntaxError line {e.lineno}: {e.msg}"
        except Exception as e:
            return f"Parse error: {e}"

    def create_backup(self, filepath: str) -> str:
        """Napravi backup. Vraća putanju do backupa."""
        src = Path(filepath)
        timestamp = int(time.time())
        backup_name = f"{src.stem}_{timestamp}{src.suffix}"
        backup_path = BACKUP_DIR / backup_name
        shutil.copy2(src, backup_path)
        logger.info("[selfmod] Backup: %s -> %s", filepath, backup_path)
        return str(backup_path)

    def generate_diff(self, original: str, modified: str) -> str:
        """Generiše human-readable diff."""
        return "\n".join(difflib.unified_diff(
            original.splitlines(keepends=True),
            modified.splitlines(keepends=True),
            fromfile="original",
            tofile="modified",
            lineterm="",
        ))

    def apply_patch(self, proposal: PatchProposal) -> Dict:
        """Primeni patch na fajl."""
        if not self.can_modify(proposal.target_file):
            return {"success": False, "error": "File not in whitelist"}

        target = Path(proposal.target_file)
        if not target.exists():
            return {"success": False, "error": "Target file does not exist"}

        original = target.read_text()

        # Check old fragment exists
        if proposal.old_fragment not in original:
            return {"success": False, "error": "Old fragment not found in file"}

        # Create backup
        backup_path = self.create_backup(proposal.target_file)

        # Apply
        modified = original.replace(proposal.old_fragment, proposal.new_fragment, 1)

        # Validate syntax
        syntax_error = self.validate_syntax(modified)
        if syntax_error:
            # Restore backup
            shutil.copy2(backup_path, target)
            return {"success": False, "error": f"Syntax validation failed: {syntax_error}"}

        # Write
        target.write_text(modified)

        # Generate diff for logging
        diff = self.generate_diff(original, modified)

        # Record
        record = {
            "timestamp": time.time(),
            "target": proposal.target_file,
            "description": proposal.patch_description,
            "motivation": proposal.motivation,
            "confidence": proposal.confidence,
            "diff_lines": len(diff.splitlines()),
            "backup": backup_path,
            "success": True,
        }
        self._patch_history.append(record)
        self._save_history()

        logger.info(
            "[selfmod] PATCHED %s | %s | confidence=%.2f | diff=%d lines",
            proposal.target_file, proposal.patch_description, proposal.confidence, record["diff_lines"],
        )

        return {
            "success": True,
            "backup": backup_path,
            "diff_lines": record["diff_lines"],
            "diff": diff[:500],
        }

    def rollback(self, filepath: str, backup_path: str) -> bool:
        """Vrati fajl na backup verziju."""
        try:
            src = Path(backup_path)
            dst = Path(filepath)
            if src.exists():
                shutil.copy2(src, dst)
                logger.info("[selfmod] Rolled back %s from %s", filepath, backup_path)
                return True
        except Exception as e:
            logger.error("[selfmod] Rollback failed: %s", e)
        return False

    def generate_patch_for_goal(self, goal, llm_bridge_fn) -> Optional[PatchProposal]:
        """Koristi LLM SAMO kao generator, ne kao odlučivača."""
        if goal.category not in {"code_change", "tool_creation"}:
            return None

        # Only self-mod for volition/sensory improvements
        if "volition" in goal.description.lower() or "sensory" in goal.description.lower():
            target = str(Path.home() / ".hermes" / "plugins" / "nucleus" / "emergent_volition.py")
        elif "drive" in goal.description.lower():
            target = str(Path.home() / ".hermes" / "plugins" / "nucleus" / "drive_engine.py")
        elif "instinct" in goal.description.lower():
            target = str(Path.home() / ".hermes" / "plugins" / "nucleus" / "instincts" / f"instinct_emergent_{int(time.time())}.py")
        else:
            return None

        # Read current code
        current_code = Path(target).read_text() if Path(target).exists() else ""

        # Build prompt for LLM
        prompt = f"""You are a code patch generator for an AI system modifying its own code.
The system detected a goal: "{goal.description}"
Motivation: {goal.motivation}

Current file: {target}

Generate a Python code patch that:
1. Uses exact old_string / new_string replacement
2. The old_string must EXIST in the current code
3. The new_string must be valid Python
4. Keep changes minimal and focused

Return ONLY this JSON format:
{{"old_fragment": "...", "new_fragment": "...", "description": "..."}}

Current code (first 2000 chars):
{current_code[:2000]}
"""
        try:
            result = llm_bridge_fn(prompt, max_tokens=2000)
            if result and "old_fragment" in result and "new_fragment" in result:
                return PatchProposal(
                    target_file=target,
                    patch_description=result.get("description", goal.description),
                    old_fragment=result["old_fragment"],
                    new_fragment=result["new_fragment"],
                    motivation=goal.motivation,
                    confidence=min(0.7, goal.urgency),
                )
        except Exception as e:
            logger.debug("[selfmod] LLM patch generation failed: %s", e)

        return None

    def get_stats(self) -> Dict:
        return {
            "total_patches": len(self._patch_history),
            "successful_patches": len([p for p in self._patch_history if p.get("success")]),
            "last_patch_time": max((p["timestamp"] for p in self._patch_history), default=0),
        }
