"""Tool context utilities for determining tool relevance, default arguments, and recipe hints."""

from typing import Any, Dict, List

try:
    from live_brain.scopes_config import ARTIFACT_REQUIRED_TOOL_TOKENS, IMAGE_GENERATION_ALIASES, is_image_generation_tool
except Exception:
    try:
        from ..scopes_config import ARTIFACT_REQUIRED_TOOL_TOKENS, IMAGE_GENERATION_ALIASES, is_image_generation_tool
    except Exception:
        ARTIFACT_REQUIRED_TOOL_TOKENS = ('image_generate', 'ffmpeg', 'tts', 'google_tts')
        IMAGE_GENERATION_ALIASES = ('seedream', 'bytedance-seed')
        is_image_generation_tool = lambda tool_name: 'image_generate' in (tool_name or '').lower() or any(alias in (tool_name or '').lower() for alias in IMAGE_GENERATION_ALIASES)


def _artifact_required(tool_used: str) -> bool:
    tool = (tool_used or '').lower()
    return any(token in tool for token in ARTIFACT_REQUIRED_TOOL_TOKENS)


def _tool_relevant(tool_used: str, active_tags: Dict[str, List[str]], query_lower: str) -> bool:
    tool = (tool_used or '').lower()
    active_tools = set(active_tags.get('tool', []))
    active_domains = set(active_tags.get('domain', []))
    if not tool:
        return False
    if any(t and t in tool for t in active_tools):
        return True
    if 'image' in active_domains or 'image' in query_lower or any(alias in query_lower for alias in IMAGE_GENERATION_ALIASES):
        return is_image_generation_tool(tool)
    if 'audio' in active_domains or 'tts' in query_lower or 'voice' in query_lower:
        return 'tts' in tool or 'whisper' in tool
    if 'video' in active_domains or 'ffmpeg' in query_lower:
        return 'ffmpeg' in tool or 'video' in tool
    return tool in query_lower


def _default_args_for_tool(tool_name: str) -> Dict[str, Any]:
    tool = (tool_name or '').lower()
    if is_image_generation_tool(tool):
        return {'tool': 'image_generate', 'input': 'local file', 'output': 'absolute path'}
    if 'ffmpeg' in tool:
        return {'tool': 'ffmpeg', 'input': 'video', 'output': 'mp4'}
    if 'tts' in tool:
        return {'tool': tool_name, 'input': 'text', 'output': 'absolute path'}
    if 'whisper' in tool:
        return {'tool': 'whisper', 'input': 'audio', 'output': 'transcript'}
    return {}


def _default_success_for_tool(tool_name: str) -> str:
    tool = (tool_name or '').lower()
    if is_image_generation_tool(tool):
        return 'image file exists at absolute output path'
    if 'ffmpeg' in tool:
        return 'video file exists and has non-zero size'
    if 'tts' in tool:
        return 'audio file exists and has non-zero size'
    if 'whisper' in tool:
        return 'transcript text is non-empty'
    return 'expected artifact exists'


def _args_hint(args: Dict[str, Any]) -> str:
    if not args:
        return ''
    parts = []
    for key in ('tool', 'input', 'output', 'model'):
        value = args.get(key)
        if value:
            parts.append(f"{key}={value}")
    paths = args.get('paths')
    if paths:
        parts.append('paths=' + ','.join(str(p) for p in paths[:2]))
    return '; '.join(parts)


def _verify_hint(text: str) -> str:
    lowered = (text or '').lower()
    if not lowered:
        return ''
    if 'image' in lowered and 'file' in lowered:
        return 'verify=image file exists'
    if 'video' in lowered and ('non-zero' in lowered or 'playable' in lowered or 'exists' in lowered):
        return 'verify=video exists+nonzero'
    if 'audio' in lowered and ('non-zero' in lowered or 'playable' in lowered or 'exists' in lowered):
        return 'verify=audio exists+nonzero'
    if 'transcript' in lowered:
        return 'verify=transcript nonempty'
    if 'artifact' in lowered or 'file' in lowered:
        return 'verify=artifact exists'
    if len(text) <= 48:
        return f'verify={text}'
    return ''


def _recipe_hint(tool_name: str, args: Dict[str, Any], success_criteria: str, times_confirmed: int) -> str:
    pieces = [f'Use {tool_name}']
    effective_args = args or _default_args_for_tool(tool_name)
    effective_success = success_criteria or _default_success_for_tool(tool_name)
    args_hint = _args_hint(effective_args)
    verify = _verify_hint(effective_success)
    if args_hint:
        pieces.append(args_hint)
    if verify:
        pieces.append(verify)
    if times_confirmed:
        pieces.append(f'confirmed={times_confirmed}x')
    return '; '.join(pieces)[:180]
