"""LLM Bridge — direktan API pristup Hermes provideru za mutacije."""
import json
import logging
import re
import time
import urllib.request
from pathlib import Path

from .config import LLM_COOLDOWN_SECONDS, LLM_TIMEOUT

log = logging.getLogger("nucleus")

_last_call_time = 0.0
_HERMES_CONFIG = Path.home() / ".hermes" / "config.yaml"

INSTINCT_PROMPT = """\
You are Nucleus Instinct Generator. Write ONE Python script that solves the following problem on a Linux system.
Rules:
- Use ONLY stdlib (no pip packages)
- Must have a main() function called at the bottom
- Must print actionable output to stdout
- Must complete in under 30 seconds
- No subprocess, shutil, ctypes, or multiprocessing imports

Problem: {problem}
Current system state: {state}

Return ONLY the Python code, no markdown blocks, no explanation."""


def _load_credentials():
    """Read API credentials from Hermes config.yaml."""
    try:
        import yaml
        cfg = yaml.safe_load(_HERMES_CONFIG.read_text())
    except ImportError:
        # Fallback: basic yaml parsing for simple structure
        cfg = _parse_simple_yaml(_HERMES_CONFIG)
    if not cfg:
        return None, None, None
    model_cfg = cfg.get("model", {})
    provider = model_cfg.get("provider", "")
    base_url = model_cfg.get("base_url", "")
    model = model_cfg.get("model", "")
    # Find API key
    api_key = model_cfg.get("api_key", "")
    if not api_key:
        providers = cfg.get("providers", {})
        if provider in providers:
            api_key = providers[provider].get("api_key", "")
    return base_url, api_key, model


def _parse_simple_yaml(path):
    """Minimal YAML parser for flat config (no external deps)."""
    try:
        lines = path.read_text().splitlines()
    except Exception:
        return {}
    result = {}
    current_section = None
    current_sub = None
    for line in lines:
        stripped = line.rstrip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        if indent == 0 and stripped.endswith(":"):
            current_section = stripped[:-1]
            result[current_section] = {}
            current_sub = None
        elif indent == 2 and ":" in stripped:
            key, _, val = stripped.partition(":")
            key, val = key.strip(), val.strip()
            if val.endswith(":") or not val:
                current_sub = key
                if current_section:
                    result[current_section][key] = {}
            else:
                if current_section:
                    result[current_section][key] = val
        elif indent == 4 and ":" in stripped and current_section and current_sub:
            key, _, val = stripped.partition(":")
            result[current_section].setdefault(current_sub, {})[key.strip()] = val.strip()
    return result


def can_call_llm():
    """Check if cooldown has elapsed."""
    return (time.time() - _last_call_time) >= LLM_COOLDOWN_SECONDS


def generate_instinct(problem, state_json):
    """Call LLM to generate a new instinct script. Returns {success, code} or {success, error}."""
    global _last_call_time
    if not can_call_llm():
        return {"success": False, "error": "Cooldown active"}

    base_url, api_key, model = _load_credentials()
    if not base_url or not api_key:
        return {"success": False, "error": "No API credentials found in ~/.hermes/config.yaml"}

    prompt = INSTINCT_PROMPT.format(problem=problem, state=state_json)
    payload = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 2048,
        "temperature": 0.3,
    }).encode()

    url = base_url.rstrip("/") + "/chat/completions"
    req = urllib.request.Request(url, data=payload, headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    })

    _last_call_time = time.time()
    try:
        with urllib.request.urlopen(req, timeout=LLM_TIMEOUT) as resp:
            data = json.loads(resp.read().decode())
        content = data["choices"][0]["message"]["content"]
        code = _extract_code(content)
        if not code.strip():
            return {"success": False, "error": "LLM returned empty code"}
        log.info(f"LLM generated {len(code)} chars for '{problem}'")
        return {"success": True, "code": code}
    except Exception as e:
        return {"success": False, "error": str(e)}


def _extract_code(text):
    """Extract Python code from LLM response."""
    m = re.search(r"```python\n(.*?)```", text, re.DOTALL)
    if m:
        return m.group(1).strip()
    m = re.search(r"```\n(.*?)```", text, re.DOTALL)
    if m:
        return m.group(1).strip()
    return text.strip()
