"""Instinct Guard — sandbox izvršavanje sa resource limitima i AST provjером."""
import ast
import os
import resource
import subprocess
import sys

from .config import GUARD_TIMEOUT, GUARD_MEMORY_MB

BLOCKED_IMPORTS = {"subprocess", "shutil", "ctypes", "multiprocessing"}
BLOCKED_CALLS = {
    "eval", "exec", "compile", "__import__",
    "importlib.import_module",
    "os.system", "os.popen",
    "os.execl", "os.execle", "os.execlp", "os.execlpe",
    "os.execv", "os.execve", "os.execvp", "os.execvpe",
    "os.spawnl", "os.spawnle", "os.spawnlp", "os.spawnlpe",
    "os.spawnv", "os.spawnve", "os.spawnvp", "os.spawnvpe",
}
def _collect_import_aliases(tree):
    aliases = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in BLOCKED_IMPORTS:
                    return None, f"Blocked import: {alias.name}"
                aliases[alias.asname or root] = alias.name
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if root in BLOCKED_IMPORTS:
                return None, f"Blocked import: {node.module}"
            for alias in node.names:
                imported = f"{node.module}.{alias.name}" if node.module else alias.name
                aliases[alias.asname or alias.name] = imported
                if imported in BLOCKED_CALLS:
                    return None, f"Blocked import: {imported}"
    return aliases, ""


def _resolve_expr(expr, aliases):
    if isinstance(expr, ast.Name):
        return aliases.get(expr.id, expr.id)
    if isinstance(expr, ast.Attribute):
        base = _resolve_expr(expr.value, aliases)
        return f"{base}.{expr.attr}" if base else expr.attr
    if isinstance(expr, ast.Call):
        func_name = _resolve_expr(expr.func, aliases)
        if func_name == "__import__" and expr.args and isinstance(expr.args[0], ast.Constant):
            return str(expr.args[0].value)
        if func_name == "importlib.import_module" and expr.args and isinstance(expr.args[0], ast.Constant):
            return str(expr.args[0].value)
    return ""


def _blocked_getattr_call(node, aliases):
    if not isinstance(node.func, ast.Name) or node.func.id != "getattr":
        return ""
    if len(node.args) < 2 or not isinstance(node.args[1], ast.Constant):
        return ""
    base = _resolve_expr(node.args[0], aliases)
    attr = str(node.args[1].value)
    name = f"{base}.{attr}" if base else attr
    return name if name in BLOCKED_CALLS else ""


def _check_ast(script_path):
    """Scan AST for dangerous imports/calls. Returns (safe, reason)."""
    try:
        with open(script_path) as f:
            source = f.read()
        tree = ast.parse(source)
    except SyntaxError as e:
        return False, f"SyntaxError: {e}"
    aliases, reason = _collect_import_aliases(tree)
    if aliases is None:
        return False, reason
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            getattr_name = _blocked_getattr_call(node, aliases)
            if getattr_name:
                return False, f"Blocked call: {getattr_name}"
            if isinstance(node.func, ast.Call):
                nested_getattr_name = _blocked_getattr_call(node.func, aliases)
                if nested_getattr_name:
                    return False, f"Blocked call: {nested_getattr_name}"
            name = _resolve_expr(node.func, aliases)
            if name in BLOCKED_CALLS:
                return False, f"Blocked call: {name}"
    return True, ""


def _set_limits(memory_mb=None):
    """Called in subprocess to set resource limits."""
    mem = (memory_mb or GUARD_MEMORY_MB) * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_AS, (mem, mem))
    resource.setrlimit(resource.RLIMIT_NPROC, (32, 32))


class InstinctGuard:
    def __init__(self, timeout=None, memory_mb=None):
        self.timeout = timeout or GUARD_TIMEOUT
        self.memory_mb = memory_mb or GUARD_MEMORY_MB

    def execute(self, script_path):
        if not os.path.exists(script_path):
            return {"success": False, "error": "Script not found"}
        # AST safety check
        safe, reason = _check_ast(script_path)
        if not safe:
            return {"success": False, "error": f"BLOCKED: {reason}"}
        try:
            result = subprocess.run(
                [sys.executable, script_path],
                capture_output=True, text=True,
                timeout=self.timeout,
                preexec_fn=lambda: _set_limits(self.memory_mb),
            )
            return {
                "success": result.returncode == 0,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "returncode": result.returncode,
                "error": result.stderr.strip() if result.returncode != 0 else None,
            }
        except subprocess.TimeoutExpired:
            return {"success": False, "error": f"Timeout ({self.timeout}s)"}
        except Exception as e:
            return {"success": False, "error": str(e)}
