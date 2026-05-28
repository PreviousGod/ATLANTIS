"""Domain profiles for scoped Nucleus research."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse


@dataclass(frozen=True)
class DomainProfile:
    scope: str
    local_roots: tuple[Path, ...] = field(default_factory=tuple)
    include_suffixes: tuple[str, ...] = (".md", ".rst", ".txt", ".py", ".yaml", ".yml", ".json")
    exclude_parts: tuple[str, ...] = (
        ".git", "__pycache__", "node_modules", ".venv", "venv", "dist", "build",
        ".mypy_cache", ".pytest_cache", ".ruff_cache",
    )
    web_allowlist: tuple[str, ...] = field(default_factory=tuple)
    search_suffix: str = "solution"
    max_local_files: int = 240
    max_file_bytes: int = 24000

    def iter_local_sources(self):
        seen = set()
        yielded = 0
        for root in self.local_roots:
            if not root.exists():
                continue
            candidates = [root] if root.is_file() else root.rglob("*")
            for path in candidates:
                if yielded >= self.max_local_files:
                    return
                if not path.is_file():
                    continue
                if path.suffix.lower() not in self.include_suffixes:
                    continue
                parts = set(path.parts)
                if any(part in parts for part in self.exclude_parts):
                    continue
                try:
                    resolved = path.resolve()
                except OSError:
                    continue
                if resolved in seen:
                    continue
                seen.add(resolved)
                yielded += 1
                yield path

    def allows_url(self, url: str) -> bool:
        if not self.web_allowlist:
            return True
        host = urlparse(url).netloc.lower()
        if host.startswith("www."):
            host = host[4:]
        return any(host == allowed or host.endswith(f".{allowed}") for allowed in self.web_allowlist)


HERMES_ROOT = Path.home() / ".hermes" / "hermes-agent"
NUCLEUS_ROOT = Path.home() / ".hermes" / "plugins" / "nucleus"

HERMES_PROFILE = DomainProfile(
    scope="hermes",
    local_roots=(
        HERMES_ROOT / "README.md",
        HERMES_ROOT / "docs",
        HERMES_ROOT / "gateway",
        HERMES_ROOT / "hermes_cli",
        HERMES_ROOT / "plugins",
        HERMES_ROOT / "run_agent.py",
        HERMES_ROOT / "model_tools.py",
        HERMES_ROOT / "toolsets.py",
        HERMES_ROOT / "tests" / "gateway",
        NUCLEUS_ROOT,
    ),
    web_allowlist=("github.com", "raw.githubusercontent.com", "docs.github.com"),
    search_suffix="Hermes agent documentation GitHub",
    max_local_files=360,
)

LINUX_PROFILE = DomainProfile(
    scope="linux",
    local_roots=(NUCLEUS_ROOT / "instincts", NUCLEUS_ROOT / "seed_graph.json"),
    web_allowlist=(
        "kernel.org", "freedesktop.org", "man7.org", "ubuntu.com", "debian.org",
        "archlinux.org", "redhat.com", "github.com",
    ),
    search_suffix="linux troubleshooting",
)

NUCLEUS_PROFILE = DomainProfile(
    scope="nucleus",
    local_roots=(NUCLEUS_ROOT,),
    web_allowlist=("github.com", "raw.githubusercontent.com", "docs.python.org"),
    search_suffix="python sqlite linux agent",
)

_SCOPE_HINTS = {
    "hermes": (
        "hermes", "gateway", "telegram", "plugin", "plugins", "run_agent",
        "model_tools", "toolset", "live brain", "live_brain", "context", "ctx",
    ),
    "linux": (
        "cpu", "ram", "memory", "disk", "port", "dns", "systemd", "service",
        "oom", "swap", "inode", "network", "process", "zombie", "time_wait",
    ),
}


def detect_scope(problem: str) -> str:
    text = (problem or "").lower()
    for scope, hints in _SCOPE_HINTS.items():
        if any(hint in text for hint in hints):
            return scope
    return "nucleus"


def get_domain_profile(problem: str | None = None, scope: str | None = None) -> DomainProfile:
    selected = (scope or detect_scope(problem or "")).lower()
    if selected == "hermes":
        return HERMES_PROFILE
    if selected == "linux":
        return LINUX_PROFILE
    return NUCLEUS_PROFILE
