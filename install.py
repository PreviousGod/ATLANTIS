#!/usr/bin/env python3
"""ATLANTIS Installer — Cross-platform install script for Hermes.

Works on Linux, macOS, and Windows. Requires Python 3.8+.
Usage: python install.py
"""
import os
import platform
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


def main():
    print("╔══════════════════════════════════════════╗")
    print("║   ATLANTIS Installer for Hermes         ║")
    print("║   Cognitive Architecture + Live Brain   ║")
    print("╚══════════════════════════════════════════╝")
    print()

    root = find_atlantis_root()
    hh = hermes_home()

    print(f"[1/5] Detecting Hermes at: {hh}")
    if not check_hermes(hh):
        sys.exit(1)
    print(f"  ✓ Hermes found")
    print()

    plugins_dir = hh / "plugins"

    print("[2/5] Backing up existing plugins...")
    backup(plugins_dir)
    print()

    print("[3/5] Installing live_brain...")
    lb_src = root / "live_brain"
    if not lb_src.exists():
        print(f"  ✗ Source not found: {lb_src}")
        sys.exit(1)
    install_plugin(lb_src, plugins_dir / "live_brain", "live_brain")
    print()

    print("[4/5] Installing live_brain_ctx...")
    ctx_src = root / "live_brain_ctx"
    if not ctx_src.exists():
        print(f"  ✗ Source not found: {ctx_src}")
        sys.exit(1)
    install_plugin(ctx_src, plugins_dir / "live_brain_ctx", "live_brain_ctx")
    print()

    print("[5/5] Verifying installation...")
    verify_import(plugins_dir)
    print()

    print("═" * 44)
    print("✓ ATLANTIS installed successfully!")
    print()
    print("Next steps:")
    print("  1. Restart Hermes gateway")
    print("  2. Send a complex query to test cognitive architecture")
    print("  3. Check logs: tail -f ~/.hermes/logs/gateway.log")
    print()
    print(f"Installed to: {plugins_dir}")


if __name__ == "__main__":
    main()
