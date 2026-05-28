"""ActionClassifier — Ciel Risk Assessment.

Procenjuje rizik Python skripte na osnovu AST analize.
Koristi se pre autonomnog izvršavanja da odredi da li akcija
zahteva odobrenje ili može auto-approve.

Risk scale:
  0.0-0.2: Read-only diagnostic → AUTO-APPROVE
  0.2-0.5: Low-risk write (backup, stash) → AUTO-APPROVE (configurable)
  0.5-0.7: Medium-risk (file delete, service restart) → PENDING APPROVAL
  0.7-0.9: High-risk (systemctl stop, mass delete) → PENDING APPROVAL
  0.9-1.0: Destructive (rm -rf, eval, os.system) → BLOCKED
"""
from __future__ import annotations

import ast
import logging
from pathlib import Path
from typing import Dict, List, Optional

log = logging.getLogger("nucleus")

# Destructive operations that are always blocked
_BLOCKED_CALLS = {
    "eval", "exec", "compile",
    "os.system", "os.popen", "os.execl", "os.execle", "os.execlp",
    "os.execv", "os.execve", "os.execvp", "os.execvpe",
    "os.spawnl", "os.spawnle", "os.spawnlp", "os.spawnlpe",
    "os.spawnv", "os.spawnve", "os.spawnvp", "os.spawnvpe",
}

# Write operations that increase risk
_WRITE_CALLS = {
    "os.unlink", "os.remove", "os.rmdir", "os.removedirs",
    "os.rename", "os.replace",
    "shutil.rmtree", "shutil.move", "shutil.copy",
}

# Subprocess calls
_SUBPROCESS_CALLS = {
    "subprocess.run", "subprocess.call", "subprocess.check_call",
    "subprocess.check_output", "subprocess.Popen",
}

# Service-affecting terminal commands (detected via string analysis)
_SERVICE_COMMANDS = [
    "systemctl restart", "systemctl stop", "systemctl disable",
    "systemctl start", "kill ", "pkill ", "killall ",
]


class ActionClassifier:
    """Classify the risk level of an instinct script before execution."""

    def classify_script(self, script_path: str) -> Dict[str, any]:
        """
        Analyze a Python script and return risk assessment.

        Returns dict:
            risk: float (0.0-1.0)
            reason: str (human-readable explanation)
            can_auto_approve: bool
            operations: List[str] (detected operations)
        """
        path = Path(script_path)
        if not path.exists():
            return {
                "risk": 1.0,
                "reason": "Script not found",
                "can_auto_approve": False,
                "operations": [],
            }

        try:
            with open(script_path) as f:
                source = f.read()
            tree = ast.parse(source)
        except SyntaxError as e:
            return {
                "risk": 1.0,
                "reason": f"SyntaxError: {e}",
                "can_auto_approve": False,
                "operations": [],
            }
        except Exception as e:
            return {
                "risk": 1.0,
                "reason": f"Parse failed: {e}",
                "can_auto_approve": False,
                "operations": [],
            }

        risk = 0.0
        reasons: List[str] = []
        ops: List[str] = []

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue

            func_name = self._resolve_func(node.func)

            # BLOCKED: eval/exec/compile
            if func_name in _BLOCKED_CALLS:
                risk = 1.0
                reasons.append(f"DESTRUCTIVE: {func_name}")
                ops.append(func_name)
                break  # Can't get worse than 1.0

            # BLOCKED: os.system / os.popen
            if func_name in ("os.system", "os.popen"):
                risk = 1.0
                reasons.append(f"SYSTEM CALL: {func_name}")
                ops.append(func_name)
                break

            # HIGH RISK: subprocess
            if func_name in _SUBPROCESS_CALLS:
                risk = max(risk, 0.8)
                reasons.append(f"Subprocess: {func_name}")
                ops.append(func_name)
                # Check command string for service operations
                cmd = self._extract_first_arg(node)
                if cmd and isinstance(cmd, str):
                    for svc_cmd in _SERVICE_COMMANDS:
                        if svc_cmd in cmd.lower():
                            risk = max(risk, 0.85)
                            reasons.append(f"Service command: {svc_cmd.strip()}")
                            ops.append(f"service:{svc_cmd.strip()}")

            # MEDIUM RISK: file deletion
            if func_name in _WRITE_CALLS:
                risk = max(risk, 0.6)
                reasons.append(f"File write/delete: {func_name}")
                ops.append(func_name)

            # LOW RISK: file write via open()
            if func_name == "open":
                mode = self._extract_open_mode(node)
                if mode in ("w", "a", "w+", "a+", "x", "x+"):
                    risk = max(risk, 0.5)
                    reasons.append(f"File write: open(mode='{mode}')")
                    ops.append(f"open:{mode}")
                elif mode in ("r", "rb", "r+"):
                    # Read-only, no risk increase
                    ops.append(f"open:{mode}")

            # LOW RISK: os.makedirs / os.mkdir
            if func_name in ("os.makedirs", "os.mkdir"):
                risk = max(risk, 0.3)
                reasons.append(f"Directory create: {func_name}")
                ops.append(func_name)

            # LOW RISK: os.walk (used by clean_disk)
            if func_name == "os.walk":
                risk = max(risk, 0.3)
                reasons.append("Directory traversal")
                ops.append("os.walk")

        # Also scan string literals for dangerous shell commands
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                val = node.value.lower()
                for svc_cmd in _SERVICE_COMMANDS:
                    if svc_cmd in val:
                        risk = max(risk, 0.75)
                        reasons.append(f"String contains: {svc_cmd.strip()}")
                        ops.append(f"string:{svc_cmd.strip()}")

        result = {
            "risk": round(risk, 2),
            "reason": "; ".join(reasons) if reasons else "Read-only diagnostic",
            "can_auto_approve": risk < 0.2,
            "operations": ops,
        }
        log.debug("CLASSIFY %s → risk=%.2f approve=%s ops=%s",
                  Path(script_path).name, result["risk"], result["can_auto_approve"], ops)
        return result

    def classify_tool_call(self, tool_name: str, args: Dict[str, any]) -> Dict[str, any]:
        """
        Classify risk of a Hermes tool call (for future use with tool-level autonomy).

        Risk levels:
            read_file, search_files, web_extract, web_search: 0.0
            terminal + 'systemctl status': 0.1
            terminal + 'systemctl restart': 0.4
            terminal + 'rm ': 0.9
            write_file + exists: 0.8
            write_file + new: 0.7
            patch: 0.6
        """
        risk = 0.5
        reasons = []

        if tool_name in ("read_file", "search_files", "web_extract", "web_search"):
            risk = 0.0
            reasons.append("Read-only")

        elif tool_name == "terminal":
            cmd = args.get("command", "")
            if cmd.startswith(("systemctl status", "ps ", "top", "free", "df ", "cat ", "ls ")):
                risk = 0.1
                reasons.append("Read-only system query")
            elif cmd.startswith(("systemctl restart", "systemctl stop", "systemctl start")):
                risk = 0.4
                reasons.append("Service control")
            elif cmd.startswith(("cp ", "mv ", "mkdir ")):
                risk = 0.3
                reasons.append("File operation")
            elif "rm " in cmd or "rm -" in cmd:
                risk = 0.9
                reasons.append("DELETE command")
            elif "chmod 777" in cmd or "chmod 000" in cmd:
                risk = 0.9
                reasons.append("Dangerous chmod")
            else:
                risk = 0.5
                reasons.append("Unknown terminal command")

        elif tool_name == "write_file":
            path = args.get("path", "")
            if Path(path).exists():
                risk = 0.8
                reasons.append("Overwrite existing file")
            else:
                risk = 0.7
                reasons.append("Create new file")

        elif tool_name == "patch":
            risk = 0.6
            reasons.append("Code modification")

        elif tool_name == "execute_code":
            risk = 0.7
            reasons.append("Arbitrary code execution")

        return {
            "risk": round(risk, 2),
            "reason": "; ".join(reasons),
            "can_auto_approve": risk < 0.2,
            "tool": tool_name,
        }

    # ── Helpers ──────────────────────────────────────────────────────────────────────────

    def _resolve_func(self, node) -> str:
        """Resolve an AST call target to a dotted name like 'os.unlink'."""
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            base = self._resolve_func(node.value)
            return f"{base}.{node.attr}" if base else node.attr
        return ""

    def _extract_first_arg(self, node: ast.Call) -> Optional[str]:
        """Extract the first positional argument if it's a string constant."""
        if node.args and isinstance(node.args[0], ast.Constant):
            return str(node.args[0].value)
        return None

    def _extract_open_mode(self, node: ast.Call) -> str:
        """Extract the mode argument from an open() call."""
        # open(path) → default 'r'
        if len(node.args) < 2:
            return "r"
        # open(path, mode)
        if isinstance(node.args[1], ast.Constant):
            return str(node.args[1].value)
        # open(path, mode='w')
        for kw in node.keywords:
            if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
                return str(kw.value.value)
        return "r"
