#!/usr/bin/env python3
"""ATLANTIS Installer / Updater — Cross-platform for Hermes.

Install:  python install.py [--auto]
Update:   python install.py --update
Status:   python install.py --status

Installs live_brain, live_brain_ctx, nucleus, and prefill.json.
Backs up existing plugins before overwriting.
"""
import os
import platform
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path


# ── Detection ──────────────────────────────────────────────────────────

def hermes_home() -> Path:
    env = os.environ.get("HERMES_HOME")
    if env:
        return Path(env)
    if platform.system() == "Windows":
        appdata = os.environ.get("APPDATA")
        if appdata:
            p = Path(appdata) / "hermes"
            if p.exists():
                return p
    return Path.home() / ".hermes"


def atlantis_root() -> Path:
    return Path(__file__).resolve().parent


# ── Plugin operations ──────────────────────────────────────────────────

PLUGINS = ("live_brain", "live_brain_ctx", "nucleus")


def backup_plugins(plugins_dir: Path) -> None:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_dir = plugins_dir.parent / "plugins_backup"
    backup_dir.mkdir(exist_ok=True)
    for name in PLUGINS:
        src = plugins_dir / name
        if src.exists():
            dst = backup_dir / f"{name}_backup_{ts}"
            shutil.copytree(src, dst)
            print(f"  \u2713 Backed up {name} \u2192 {dst.name}")


def install_plugin(src: Path, dst: Path, name: str) -> bool:
    if dst.exists():
        shutil.rmtree(dst)
    def _ignore(d, files):
        return [f for f in files if f in ("__pycache__", ".pytest_cache", ".git")]
    shutil.copytree(src, dst, ignore=_ignore)
    init = dst / "__init__.py"
    if not init.exists():
        print(f"  \u2717 {name}: __init__.py missing after copy!")
        return False
    py_count = sum(1 for _ in dst.rglob("*.py"))
    print(f"  \u2713 {name} installed ({py_count} .py files)")
    return True


def install_prefill(hh: Path, root: Path) -> None:
    src = root / "prefill.json"
    dst = hh / "prefill.json"
    if not src.exists():
        print("  \u26a0 prefill.json not found in repo, skipping")
        return
    if dst.exists():
        backup = hh / f"prefill.json.bak.{datetime.now():%Y%m%d_%H%M%S}"
        shutil.copy2(dst, backup)
        print(f"  \u2139 Backed up existing prefill \u2192 {backup.name}")
    shutil.copy2(src, dst)
    print("  \u2713 prefill.json installed")


# ── Dependencies ───────────────────────────────────────────────────────

def install_deps(hh: Path, root: Path) -> None:
    req = root / "live_brain" / "requirements.txt"
    if not req.exists():
        print("  \u26a0 requirements.txt not found, skipping")
        return

    python = None
    for venv_name in (".venv", "venv"):
        for sub in ("bin/python", "Scripts/python.exe"):
            p = hh / "hermes-agent" / venv_name / sub
            if p.exists():
                python = p
                break
        if python:
            break

    if not python:
        print("  \u26a0 Hermes venv not found, install manually:")
        print(f"    pip install -r {req}")
        return

    for tool in ("pip", "uv"):
        cmd = [str(python), "-m", tool, "install", "-q", "-r", str(req)]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
            if r.returncode == 0:
                print(f"  \u2713 Dependencies installed via {tool}")
                return
        except Exception:
            continue
    print(f"  \u26a0 Could not auto-install deps. Run: {python} -m pip install -r {req}")


# ── Config patching ────────────────────────────────────────────────────

CONFIG_BLOCK = """\
plugins:
  enabled:
    - live_brain
    - live_brain_ctx
    - nucleus

memory:
  provider: live_brain

context:
  engine: live_brain_ctx

agent:
  prefill_messages_file: prefill.json

tool_loop_guardrails:
  hard_stop_enabled: true
  hard_stop_after:
    exact_failure: 3
    same_tool_failure: 5
"""


def patch_config(hh: Path, auto: bool) -> None:
    config_path = hh / "config.yaml"
    if not config_path.exists():
        print(f"  \u26a0 config.yaml not found at {config_path}")
        print(f"  Add this block:\n{CONFIG_BLOCK}")
        return

    content = config_path.read_text(encoding="utf-8")
    needed = []

    if "provider: live_brain" not in content:
        needed.append("memory.provider: live_brain")
    if "engine: live_brain_ctx" not in content:
        needed.append("context.engine: live_brain_ctx")
    if "- nucleus" not in content:
        needed.append("plugins: - nucleus")
    if "prefill_messages_file: prefill.json" not in content:
        needed.append("agent.prefill_messages_file: prefill.json")
    if "hard_stop_enabled: true" not in content:
        needed.append("tool_loop_guardrails.hard_stop_enabled: true")

    if not needed:
        print("  \u2713 config.yaml already configured")
        return

    if not auto:
        print(f"  \u26a0 config.yaml needs: {', '.join(needed)}")
        print(f"  Add this block:\n{CONFIG_BLOCK}")
        return

    # Auto-patch
    lines = content.split("\n")
    out = list(lines)

    if "memory:" in content and "provider: live_brain" not in content:
        out = _patch_section(out, "memory:", "  provider: live_brain")
    if "context:" in content and "engine: live_brain_ctx" not in content:
        out = _patch_section(out, "context:", "  engine: live_brain_ctx")
    if "- nucleus" not in content:
        out = _patch_plugin(out, "nucleus")
    if "prefill_messages_file:" not in content:
        out = _patch_section(out, "agent:", "  prefill_messages_file: prefill.json")
    if "hard_stop_enabled: true" not in content:
        out = _patch_section(out, "tool_loop_guardrails:", "  hard_stop_enabled: true")

    config_path.write_text("\n".join(out) + "\n", encoding="utf-8")
    print(f"  \u2713 config.yaml patched: {', '.join(needed)}")


def _patch_section(lines, header, entry):
    out = []
    for i, line in enumerate(lines):
        out.append(line)
        if line.strip().startswith(header.rstrip(":")):
            out.append(entry)
    return out


def _patch_plugin(lines, plugin_name):
    out = []
    in_enabled = False
    for line in lines:
        if line.strip() == "enabled:" and not in_enabled:
            in_enabled = True
        elif in_enabled and line.strip().startswith("- ") and plugin_name not in line:
            out.append(f"  - {plugin_name}")
            in_enabled = False
        out.append(line)
    if in_enabled:
        out.append(f"    - {plugin_name}")
    return out


# ── Status ─────────────────────────────────────────────────────────────

def show_status(hh: Path) -> None:
    plugins_dir = hh / "plugins"
    config_path = hh / "config.yaml"
    prefill_path = hh / "prefill.json"
    lb_db = hh / "live_brain" / "live_brain.db"

    print("ATLANTIS Status")
    print("=" * 44)

    for name in PLUGINS:
        init = plugins_dir / name / "__init__.py"
        if init.exists():
            size = init.stat().st_size
            print(f"  \u2713 {name:20s}  installed ({size:,} bytes)")
        else:
            print(f"  \u2717 {name:20s}  NOT installed")

    if prefill_path.exists():
        print(f"  \u2713 prefill.json          installed")
    else:
        print(f"  \u2717 prefill.json          NOT installed")

    if config_path.exists():
        content = config_path.read_text()
        has_lb = "provider: live_brain" in content
        has_ctx = "engine: live_brain_ctx" in content
        has_nuc = "- nucleus" in content
        has_pf = "prefill_messages_file:" in content
        print(f"  {'\u2713' if has_lb else '\u2717'} config: memory.provider = live_brain")
        print(f"  {'\u2713' if has_ctx else '\u2717'} config: context.engine = live_brain_ctx")
        print(f"  {'\u2713' if has_nuc else '\u2717'} config: plugins includes nucleus")
        print(f"  {'\u2713' if has_pf else '\u2717'} config: agent.prefill_messages_file")
    else:
        print("  \u2717 config.yaml not found")

    if lb_db.exists():
        print(f"  \u2713 live_brain.db         {lb_db.stat().st_size:,} bytes")
    else:
        print(f"  \u26a0 live_brain.db         not yet created (first session will create it)")


# ── Update ─────────────────────────────────────────────────────────────

def update_from_repo(root: Path) -> bool:
    """Git pull the latest ATLANTIS code."""
    if not (root / ".git").exists():
        print("  \u26a0 Not a git repo — can't auto-update")
        return False
    try:
        r = subprocess.run(
            ["git", "-C", str(root), "pull", "origin", "main"],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode == 0:
            print("  \u2713 Repo updated: " + r.stdout.strip().split("\n")[-1])
            return True
        else:
            print(f"  \u2717 Git pull failed: {r.stderr.strip()}")
            return False
    except Exception as e:
        print(f"  \u2717 Git pull error: {e}")
        return False


# ── Main ───────────────────────────────────────────────────────────────

def main():
    args = set(sys.argv[1:])

    if "--status" in args or "-s" in args:
        show_status(hermes_home())
        return

    print("\u2554" + "\u2550" * 42 + "\u2557")
    print("\u2551   ATLANTIS — Agent Intelligence System    \u2551")
    print("\u2551   live_brain + live_brain_ctx + nucleus   \u2551")
    print("\u255a" + "\u2550" * 42 + "\u255d")
    print()

    root = atlantis_root()
    hh = hermes_home()
    auto = "--auto" in args
    do_update = "--update" in args

    if do_update:
        print("[update] Pulling latest ATLANTIS...")
        update_from_repo(root)
        print()

    print(f"[1/7] Hermes at: {hh}")
    if not hh.exists():
        print(f"  \u2717 Not found. Set HERMES_HOME or install Hermes first.")
        sys.exit(1)
    print("  \u2713 Found")
    print()

    plugins_dir = hh / "plugins"
    plugins_dir.mkdir(parents=True, exist_ok=True)

    print("[2/7] Backing up existing plugins...")
    backup_plugins(plugins_dir)
    print()

    for i, name in enumerate(PLUGINS, 3):
        print(f"[{i}/7] Installing {name}...")
        src = root / name
        if not src.exists():
            print(f"  \u2717 Source not found: {src}")
            continue
        install_plugin(src, plugins_dir / name, name)
        print()

    i = 6
    print(f"[{i}/7] Installing prefill...")
    install_prefill(hh, root)
    print()

    i = 7
    print(f"[{i}/7] Installing dependencies...")
    install_deps(hh, root)
    print()

    print("[config] Configuring Hermes...")
    patch_config(hh, auto=auto)
    print()

    print("\u2550" * 44)
    print("\u2713 ATLANTIS installed!")
    print()
    if do_update:
        print("  Restart your Hermes gateway to apply:")
        print("    systemctl --user restart hermes-gateway")
    else:
        print("  Next steps:")
        print("    1. Restart Hermes gateway")
        print("    2. Start a /new session for clean state")
        print("    3. Test with a complex multi-step task")
    print()
    print(f"  python install.py --status   \u2192 check installation")
    print(f"  python install.py --update   \u2192 pull + reinstall latest")
    print()


if __name__ == "__main__":
    main()
