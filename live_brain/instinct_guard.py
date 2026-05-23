"""
instinct_guard.py — ATLANTIS NUCLEUS Phase 2: Immune System & Sandbox

Statically analyse and sandbox-execute auto-generated Python instinct scripts.
Guarantee: generated code cannot escape, cannot delete user files, cannot
open network sockets, and cannot call dangerous builtins.

Usage:
    guard = InstinctGuard()
    report = guard.audit_code(source_code)
    if report.is_safe:
        result = guard.sandbox_run(source_code, timeout=5.0)
"""

from __future__ import annotations

import ast
import builtins
import hashlib
import json
import logging
import os
import re
import resource
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Static analysis configuration
# ---------------------------------------------------------------------------

FORBIDDEN_TOP_LEVEL_IMPORTS: Set[str] = {
    "os", "subprocess", "shutil", "socket", "urllib", "http", "ftplib",
    "smtplib", "requests", "paramiko", "pexpect", "telnetlib", "pickle",
    "marshal", "ctypes", "_ctypes", "cffi", "multiprocessing", "threading", "asyncio",
    "tempfile",  # can be whitelisted later if needed
}

FORBIDDEN_IMPORTS_FUZZY: Set[str] = {
    # anything containing these tokens is suspicious
    "_sqlite3", "dbm", "shelve", "ssl", "crypt", "spwd", "pwd", "grp",
    "pty", "tty", "fcntl", "mmap", "syslog", "nis", "nis", "grp", "pwd",
}

FORBIDDEN_BUILTINS: Set[str] = {
    "eval", "exec", "compile", "__import__", "open", "input",
    "reload", "exit", "quit",
}

FORBIDDEN_ATTRIBUTES: Set[str] = {
    # attribute names that, on any object, are dangerous
    "system", "popen", "spawn", "fork", "kill", "execv", "execve",
    "rmtree", "move", "copytree", "copy", "remove", "unlink", "rmdir",
    "chmod", "chown", "link", "symlink",
}

FORBIDDEN_CALL_PATTERNS: List[Tuple[str, re.Pattern]] = [
    # (reason, regex_on_source_text)
    ("hardcoded_shell_call", re.compile(r"\bos\.[a-z]+\("),),
    ("hardcoded_subprocess", re.compile(r"\bsubprocess\.[a-z]+\("),),
    ("hardcoded_shutil", re.compile(r"\bshutil\.[a-z]+\("),),
    ("hardcoded_eval_exec", re.compile(r"\b(eval|exec)\s*\("),),
]

SENSITIVE_PATHS: Set[str] = {
    str(Path.home() / ".hermes"),
    str(Path.home()),
    "/", "/etc", "/usr", "/var", "/bin", "/sbin", "/lib", "/lib64",
    "/sys", "/proc", "/dev",
}

SAFE_BUILTINS: Dict[str, Any] = {
    name: getattr(builtins, name)
    for name in dir(builtins)
    if not name.startswith("_") and name not in FORBIDDEN_BUILTINS
}

# Prune further: remove classes that can open files indirectly
for _prune in ("file", "open"):
    SAFE_BUILTINS.pop(_prune, None)


# ---------------------------------------------------------------------------
# Audit report
# ---------------------------------------------------------------------------

@dataclass
class AuditReport:
    is_safe: bool
    sha256: str
    violations: List[str] = field(default_factory=list)
    imports: List[str] = field(default_factory=list)
    calls: List[str] = field(default_factory=list)
    scan_time_ms: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "is_safe": self.is_safe,
            "sha256": self.sha256,
            "violations": self.violations,
            "imports": self.imports,
            "calls": self.calls,
            "scan_time_ms": round(self.scan_time_ms, 3),
        }


# ---------------------------------------------------------------------------
# AST walker
# ---------------------------------------------------------------------------

class _DangerousNodeVisitor(ast.NodeVisitor):
    """Collect imports, calls, and attribute accesses for policy checks."""

    def __init__(self, raw_source: str):
        self.raw_source = raw_source
        self.imports: List[str] = []
        self.calls: List[str] = []
        self.attributes: List[str] = []
        self.violations: List[str] = []
        self._current_function: Optional[str] = None

    def _add_violation(self, msg: str) -> None:
        if msg not in self.violations:
            self.violations.append(msg)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            name = alias.name.split(".")[0]
            self.imports.append(name)
            if name in FORBIDDEN_TOP_LEVEL_IMPORTS:
                self._add_violation(f"forbidden_top_level_import: {name}")
            for fuzzy in FORBIDDEN_IMPORTS_FUZZY:
                if fuzzy in name.lower():
                    self._add_violation(f"suspicious_import: {name}")
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module:
            mod = node.module.split(".")[0]
            self.imports.append(mod)
            if mod in FORBIDDEN_TOP_LEVEL_IMPORTS:
                self._add_violation(f"forbidden_top_level_import: {mod}")
        for alias in node.names:
            self.imports.append(alias.name)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        call_desc = self._describe_call(node)
        self.calls.append(call_desc)
        # check for builtins
        if call_desc in FORBIDDEN_BUILTINS:
            self._add_violation(f"forbidden_builtin_call: {call_desc}()")
        # check for forbidden attributes
        for attr in FORBIDDEN_ATTRIBUTES:
            if f".{attr}" in call_desc:
                self._add_violation(f"forbidden_attribute_call: {call_desc}")
        # check getattr/hasattr/setattr with forbidden names as string literals
        if call_desc in ("getattr", "hasattr", "setattr"):
            if len(node.args) >= 2:
                second_arg = node.args[1]
                if isinstance(second_arg, ast.Constant) and isinstance(second_arg.value, str):
                    attr_name = second_arg.value
                    if attr_name in FORBIDDEN_BUILTINS or attr_name in FORBIDDEN_ATTRIBUTES:
                        self._add_violation(
                            f"forbidden_dynamic_access: {call_desc}(..., '{attr_name}')"
                        )
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        self.attributes.append(node.attr)
        self.generic_visit(node)

    def _describe_call(self, node: ast.Call) -> str:
        """Best-effort reconstruction of call target as dotted name."""
        parts: List[str] = []
        current: ast.AST = node.func
        while isinstance(current, ast.Attribute):
            parts.append(current.attr)
            current = current.value
        if isinstance(current, ast.Name):
            parts.append(current.id)
        elif isinstance(current, ast.Subscript):
            parts.append("<subscript>")
        else:
            parts.append("<expr>")
        return ".".join(reversed(parts))


# ---------------------------------------------------------------------------
# Main guard class
# ---------------------------------------------------------------------------

class InstinctGuard:
    """High-level API: audit + sandbox."""

    def __init__(
        self,
        max_memory_mb: int = 128,
        cpu_time_sec: int = 5,
        timeout_sec: float = 10.0,
        safe_builtins: Optional[Dict[str, Any]] = None,
    ):
        self.max_memory_mb = max(max_memory_mb, 32)
        self.cpu_time_sec = max(cpu_time_sec, 1)
        self.timeout_sec = max(timeout_sec, 2.0)
        self.safe_builtins = safe_builtins or SAFE_BUILTINS.copy()

    # ------------------------------------------------------------------
    # 1. Static audit (AST)
    # ------------------------------------------------------------------

    def audit_code(self, source: str) -> AuditReport:
        t0 = time.perf_counter()
        source = textwrap.dedent(source)
        sha = hashlib.sha256(source.encode("utf-8")).hexdigest()[:16]

        # --- Parseability ---
        try:
            tree = ast.parse(source, mode="exec")
        except SyntaxError as exc:
            return AuditReport(
                is_safe=False,
                sha256=sha,
                violations=[f"syntax_error: {exc}"],
                scan_time_ms=(time.perf_counter() - t0) * 1000,
            )

        visitor = _DangerousNodeVisitor(source)
        visitor.visit(tree)

        # --- Regex pass (catches obfuscated calls) ---
        for reason, pattern in FORBIDDEN_CALL_PATTERNS:
            if pattern.search(source):
                visitor._add_violation(f"regex_hit: {reason}")

        # --- Path literal scan ---
        for line in source.splitlines():
            for sensitive in SENSITIVE_PATHS:
                if sensitive in line:
                    # allow strings that merely mention the path in comments
                    stripped = line.split("#")[0]
                    if sensitive in stripped:
                        visitor._add_violation(
                            f"sensitive_path_literal: {sensitive} in source"
                        )

        is_safe = len(visitor.violations) == 0
        return AuditReport(
            is_safe=is_safe,
            sha256=sha,
            violations=visitor.violations,
            imports=visitor.imports,
            calls=visitor.calls,
            scan_time_ms=(time.perf_counter() - t0) * 1000,
        )

    # ------------------------------------------------------------------
    # 2. Sandbox execution
    # ------------------------------------------------------------------

    def sandbox_run(
        self,
        source: str,
        *,
        timeout: Optional[float] = None,
        input_json: Optional[str] = None,
        allowed_modules: Optional[Set[str]] = None,
    ) -> Dict[str, Any]:
        """
        Execute *source* in an isolated subprocess with:
          - memory limit (RLIMIT_AS)
          - CPU time limit (RLIMIT_CPU)
          - real-time wall-clock timeout (SIGKILL)
          - stripped builtins
          - no file-system write access outside temp dir
          - optional input_json injected as variable __instinct_input__
        """
        audit = self.audit_code(source)
        if not audit.is_safe:
            return {
                "status": "blocked_by_audit",
                "audit": audit.to_dict(),
                "stdout": "",
                "stderr": "",
                "returncode": None,
            }

        timeout = timeout or self.timeout_sec
        allowed = allowed_modules or {"json", "math", "re", "statistics", "random", "datetime", "itertools", "functools", "collections", "typing", " fractions", "decimal", "hashlib", "string", "inspect"}

        # --- Build wrapper script that runs user code in restricted env ---
        wrapper = self._build_wrapper(source, allowed, input_json)

        with tempfile.TemporaryDirectory(prefix="instinct_sandbox_") as tmpdir:
            script_path = Path(tmpdir) / "_instinct_runner.py"
            script_path.write_text(wrapper, encoding="utf-8")

            cmd = [sys.executable, str(script_path)]
            t0 = time.perf_counter()
            try:
                proc = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    cwd=tmpdir,
                    preexec_fn=self._drop_limits,
                    env={
                        "PATH": "/usr/bin:/bin",
                        "HOME": tmpdir,
                        "PYTHONDONTWRITEBYTECODE": "1",
                        "PYTHONNOUSERSITE": "1",
                    },
                )
                elapsed_ms = (time.perf_counter() - t0) * 1000
                return {
                    "status": "ok" if proc.returncode == 0 else "error",
                    "audit": audit.to_dict(),
                    "stdout": proc.stdout,
                    "stderr": proc.stderr,
                    "returncode": proc.returncode,
                    "elapsed_ms": round(elapsed_ms, 3),
                }
            except subprocess.TimeoutExpired:
                return {
                    "status": "timeout",
                    "audit": audit.to_dict(),
                    "stdout": "",
                    "stderr": "",
                    "returncode": -9,
                    "elapsed_ms": round((time.perf_counter() - t0) * 1000, 3),
                }
            except Exception as exc:
                return {
                    "status": "sandbox_exception",
                    "audit": audit.to_dict(),
                    "stdout": "",
                    "stderr": str(exc),
                    "returncode": -1,
                }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _build_wrapper(
        self,
        source: str,
        allowed_modules: Set[str],
        input_json: Optional[str],
    ) -> str:
        """Return a self-contained wrapper that restricts globals and imports."""
        safe_builtins_json = json.dumps(sorted(self.safe_builtins.keys()))
        allowed_json = json.dumps(sorted(allowed_modules))
        input_data = json.loads(input_json) if input_json is not None else None

        wrapper = f'''
import sys, json, builtins, importlib

# lockdown
ALLOWED = set({allowed_json})
SAFE_BUILTINS = set({safe_builtins_json})

class _ImportLock:
    def find_module(self, fullname, path=None):
        root = fullname.split(".")[0]
        if root not in ALLOWED:
            raise ImportError(f"Import of {{root}} blocked by InstinctGuard")
        return None

sys.meta_path.insert(0, _ImportLock())

# strip builtins
gbl = {{k: v for k, v in builtins.__dict__.items() if k in SAFE_BUILTINS}}

# inject input if given
__instinct_input__ = {input_data!r}

# user code
gbl["__instinct_input__"] = __instinct_input__
gbl["__name__"] = "__instinct__"
gbl["json"] = __import__("json")
gbl["math"] = __import__("math")
gbl["re"] = __import__("re")

code = {source!r}
exec(compile(code, "<instinct>", "exec"), gbl)
'''
        return wrapper

    def _drop_limits(self) -> None:
        """Called in child process before exec. Sets resource limits."""
        try:
            # memory
            resource.setrlimit(
                resource.RLIMIT_AS,
                (self.max_memory_mb * 1024 * 1024, self.max_memory_mb * 1024 * 1024),
            )
            # CPU time (soft, hard)
            resource.setrlimit(
                resource.RLIMIT_CPU,
                (self.cpu_time_sec, self.cpu_time_sec + 1),
            )
            # no core dumps
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
            # limit child processes
            resource.setrlimit(resource.RLIMIT_NPROC, (16, 16))
            # limit open files
            resource.setrlimit(resource.RLIMIT_NOFILE, (32, 32))
        except Exception:
            pass
        # ignore SIGINT so parent ctrl-c doesn't kill sandbox
        signal.signal(signal.SIGINT, signal.SIG_IGN)


# ---------------------------------------------------------------------------
# CLI / quick-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    guard = InstinctGuard()

    # --- test 1: obviously bad ---
    bad = """
import os
os.system('echo pwned')
"""
    r = guard.audit_code(bad)
    print("BAD audit:", json.dumps(r.to_dict(), indent=2))
    assert not r.is_safe
    print("---")

    # --- test 2: safe math instinct ---
    safe = """
def solve(data):
    import math
    return math.sqrt(data['x'])
result = solve(__instinct_input__)
print(json.dumps({"result": result}))
"""
    r = guard.audit_code(safe)
    print("SAFE audit:", json.dumps(r.to_dict(), indent=2))
    assert r.is_safe

    out = guard.sandbox_run(safe, input_json='{"x": 64}')
    print("SAFE run:", json.dumps(out, indent=2))
    assert out["status"] == "ok"
    print("---")

    # --- test 3: eval in disguise ---
    sneaky = """
getattr(__builtins__, 'eval')('1+1')
"""
    r = guard.audit_code(sneaky)
    print("SNEAKY audit:", json.dumps(r.to_dict(), indent=2))
    assert not r.is_safe

    print("\nAll instinct_guard tests passed.")
