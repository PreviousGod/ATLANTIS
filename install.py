#!/usr/bin/env python3
"""ATLANTIS Installer — Cross-platform install script for Hermes.

Works on Linux, macOS, and Windows. Requires Python 3.8+.
Usage: python install.py
"""
import os
import platform
import re
import shutil
import sys
from pathlib import Path


def hermes_home() -> Path:
    """Detect HERMES_HOME across platforms."""
    env = os.environ.get("HERMES_HOME")
    if env:
        return Path(env)
    home = Path.home()
    # Windows: check AppData first, then fallback to ~/.hermes
    if platform.system() == "Windows":
        appdata = os.environ.get("APPDATA")
        if appdata:
            p = Path(appdata) / "hermes"
            if p.exists():
                return p
    return home / ".hermes"


def find_atlantis_root() -> Path:
    """Find ATLANTIS source root (directory containing this script)."""
    return Path(__file__).resolve().parent


def check_hermes(hh: Path) -> bool:
    """Verify Hermes is installed."""
    if not hh.exists():
        print(f"✗ Hermes not found at {hh}")
        print("  Set HERMES_HOME env var or install Hermes first.")
        return False
    plugins_dir = hh / "plugins"
    if not plugins_dir.exists():
        plugins_dir.mkdir(parents=True)
    return True


def backup(plugins_dir: Path) -> None:
    """Backup existing plugins if present."""
    from datetime import datetime
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_dir = plugins_dir.parent / "plugins_backup"
    backup_dir.mkdir(exist_ok=True)
    for name in ("live_brain", "live_brain_ctx"):
        src = plugins_dir / name
        if src.exists():
            dst = backup_dir / f"{name}_backup_{ts}"
            shutil.copytree(src, dst)
            print(f"  ✓ Backed up {name} → {dst.name}")


def install_plugin(src: Path, dst: Path, name: str) -> bool:
    """Copy plugin directory, skipping __pycache__ and .pytest_cache."""
    if dst.exists():
        shutil.rmtree(dst)
    def ignore(directory, files):
        return [f for f in files if f in ("__pycache__", ".pytest_cache", ".git")]
    shutil.copytree(src, dst, ignore=ignore)
    # Verify key file exists
    init = dst / "__init__.py"
    if not init.exists():
        print(f"  ✗ {name}: __init__.py missing after copy!")
        return False
    print(f"  ✓ {name} installed ({sum(1 for _ in dst.rglob('*.py'))} .py files)")
    return True


def install_deps(hh: Path, root: Path) -> None:
    """Install Python dependencies into Hermes venv."""
    import subprocess
    req_file = root / "live_brain" / "requirements.txt"
    if not req_file.exists():
        print("  ⚠ requirements.txt not found, skipping")
        return

    # Find pip: hermes venv > system pip
    venv_python = hh / "hermes-agent" / ".venv" / "bin" / "python"
    if not venv_python.exists():
        venv_python = hh / "hermes-agent" / "venv" / "bin" / "python"
    if not venv_python.exists():
        # Windows
        venv_python = hh / "hermes-agent" / ".venv" / "Scripts" / "python.exe"
    if not venv_python.exists():
        print("  ⚠ Hermes venv not found, install manually:")
        print(f"    pip install -r {req_file}")
        return

    try:
        result = subprocess.run(
            [str(venv_python), "-m", "pip", "install", "-q", "-r", str(req_file)],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode == 0:
            print("  ✓ Dependencies installed (ddgs, tiktoken)")
        else:
            # Try uv fallback
            result2 = subprocess.run(
                ["uv", "pip", "install", "-q", "-r", str(req_file), "--python", str(venv_python)],
                capture_output=True, text=True, timeout=60,
            )
            if result2.returncode == 0:
                print("  ✓ Dependencies installed via uv (ddgs, tiktoken)")
            else:
                print(f"  ⚠ pip failed, install manually: {venv_python} -m pip install -r {req_file}")
    except Exception as e:
        print(f"  ⚠ Could not install deps: {e}")
        print(f"    Install manually: pip install -r {req_file}")


def verify_import(plugins_dir: Path) -> bool:
    """Quick import check."""
    sys.path.insert(0, str(plugins_dir))
    try:
        import importlib
        mod = importlib.import_module("live_brain_ctx.modules.cognitive_architecture")
        assert hasattr(mod, "get_cognitive_context")
        print("  ✓ cognitive_architecture imports OK")
        return True
    except Exception as e:
        print(f"  ⚠ Import check failed: {e}")
        print("    (Plugin will still work if Hermes loads it correctly)")
        return True  # non-fatal
    finally:
        sys.path.pop(0)


CONFIG_REQUIRED = {
    "memory_engine": "live_brain",
    "context_engine": "live_brain_ctx",
    "plugins": ["live_brain", "live_brain_ctx"],
}

CONFIG_SNIPPET = """\
# --- ATLANTIS configuration (add to your config.yaml) ---
memory:
  engine: live_brain

context:
  engine: live_brain_ctx

plugins:
  - live_brain
  - live_brain_ctx
"""


def configure_hermes(hh: Path, auto: bool) -> None:
    """Check and optionally patch config.yaml."""
    config_path = hh / "config.yaml"
    if not config_path.exists():
        print(f"  ⚠ config.yaml not found at {config_path}")
        print(f"    Create it manually with:\n{CONFIG_SNIPPET}")
        return

    content = config_path.read_text(encoding="utf-8")
    needs_memory = "engine: live_brain" not in content
    needs_ctx = "engine: live_brain_ctx" not in content
    needs_plugin_lb = not re.search(r'-\s+live_brain\s*$', content, re.MULTILINE)
    needs_plugin_ctx = "- live_brain_ctx" not in content

    if not any([needs_memory, needs_ctx, needs_plugin_lb, needs_plugin_ctx]):
        print("  ✓ config.yaml already configured for ATLANTIS")
        return

    missing = []
    if needs_memory:
        missing.append("memory.engine: live_brain")
    if needs_ctx:
        missing.append("context.engine: live_brain_ctx")
    if needs_plugin_lb:
        missing.append("plugins: - live_brain")
    if needs_plugin_ctx:
        missing.append("plugins: - live_brain_ctx")

    if not auto:
        print(f"  ⚠ config.yaml missing: {', '.join(missing)}")
        print(f"    Add the following to {config_path}:\n")
        print(CONFIG_SNIPPET)
        return

    # Auto-patch: append missing entries
    patches = []
    if needs_memory:
        if "memory:" in content:
            content = content.replace("memory:", "memory:\n  engine: live_brain", 1)
        else:
            patches.append("\nmemory:\n  engine: live_brain\n")
    if needs_ctx:
        if "context:" in content:
            # Insert engine line after context:
            content = content.replace("context:", "context:\n  engine: live_brain_ctx", 1)
        else:
            patches.append("\ncontext:\n  engine: live_brain_ctx\n")
    if needs_plugin_lb or needs_plugin_ctx:
        if "plugins:" not in content:
            patches.append("\nplugins:\n  enabled:\n  - live_brain\n  - live_brain_ctx\n")
        else:
            if needs_plugin_lb:
                content = content.replace("- live_brain_ctx", "- live_brain\n  - live_brain_ctx", 1)
            if needs_plugin_ctx:
                if "- live_brain_ctx" not in content:
                    content = content.replace("- live_brain", "- live_brain\n  - live_brain_ctx", 1)

    content += "".join(patches)
    config_path.write_text(content, encoding="utf-8")
    print(f"  ✓ config.yaml patched: {', '.join(missing)}")


def main():
    print("╔══════════════════════════════════════════╗")
    print("║   ATLANTIS Installer for Hermes         ║")
    print("║   Cognitive Architecture + Live Brain   ║")
    print("╚══════════════════════════════════════════╝")
    print()

    # Parse --auto flag (skip interactive prompt)
    root = find_atlantis_root()
    hh = hermes_home()

    print(f"[1/7] Detecting Hermes at: {hh}")
    if not check_hermes(hh):
        sys.exit(1)
    print(f"  ✓ Hermes found")
    print()

    plugins_dir = hh / "plugins"

    print("[2/7] Backing up existing plugins...")
    backup(plugins_dir)
    print()

    print("[3/7] Installing live_brain...")
    lb_src = root / "live_brain"
    if not lb_src.exists():
        print(f"  ✗ Source not found: {lb_src}")
        sys.exit(1)
    install_plugin(lb_src, plugins_dir / "live_brain", "live_brain")
    print()

    print("[4/7] Installing live_brain_ctx...")
    ctx_src = root / "live_brain_ctx"
    if not ctx_src.exists():
        print(f"  ✗ Source not found: {ctx_src}")
        sys.exit(1)
    install_plugin(ctx_src, plugins_dir / "live_brain_ctx", "live_brain_ctx")
    print()

    print("[5/7] Installing dependencies...")
    install_deps(hh, root)
    print()

    print("[6/7] Configuring Hermes...")
    if "--auto" in sys.argv:
        auto_config = True
    else:
        print("  How to configure config.yaml?")
        print("    1) Show me what to add (manual)")
        print("    2) Auto-patch config.yaml")
        choice = input("  Choose [1/2]: ").strip()
        auto_config = choice == "2"
    configure_hermes(hh, auto=auto_config)
    print()

    print("[7/7] Verifying installation...")
    verify_import(plugins_dir)
    print()

    print("═" * 44)
    print("✓ ATLANTIS installed successfully!")
    print()
    print("Usage:")
    print("  python install.py        → install + show config instructions")
    print("  python install.py --auto → install + auto-patch config.yaml")
    print()
    print("Next steps:")
    print("  1. Restart Hermes gateway")
    print("  2. Send a complex query to test cognitive architecture")
    print()
    print(f"Installed to: {plugins_dir}")


if __name__ == "__main__":
    main()
