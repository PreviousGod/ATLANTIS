from __future__ import annotations

from typing import Mapping

# Canonical tool aliases used for scope extraction and legacy transcript backfill.
# Add concrete tool/model/provider names here when they should resolve to the same
# executable tool in recipe learning and context retrieval.
TOOL_TERMS: Mapping[str, str] = {
    'ffmpeg': 'ffmpeg',
    'seedream': 'image_generate',
    'bytedance-seed': 'image_generate',
    'image_generate': 'image_generate',
    'seedance': 'video_generate',
    'vision_analyze': 'vision_analyze',
    'whisper': 'whisper',
    'faster_whisper': 'whisper',
    'google_tts': 'google_tts',
    'gemini-3.1-flash-tts': 'google_tts',
    'tts': 'tts',
    'telegram': 'telegram',
}

# Natural-language domain aliases. Add words here when they describe the problem
# domain, not the exact executable tool. Example: "slika" -> image belongs here;
# "image_generate" belongs in TOOL_TERMS.
DOMAIN_TERMS: Mapping[str, str] = {
    'image': 'image',
    'slika': 'image',
    'picture': 'image',
    'photo': 'image',
    'png': 'image',
    'jpg': 'image',
    'video': 'video',
    'mp4': 'video',
    'audio': 'audio',
    'voice': 'audio',
    'glas': 'audio',
    'tts': 'audio',
    'lyrics': 'music',
    'pesm': 'music',
    'pjesm': 'music',
    'song': 'music',
    'music': 'music',
    'muzik': 'music',
    'cover': 'music',
    'aranzman': 'music',
    'aranžman': 'music',
    'triler': 'music',
    'flamenco': 'music',
    'romsk': 'music',
    'spanish': 'music',
    'gitara': 'music',
    'memory': 'memory',
    'brain': 'memory',
    'plugin': 'plugin',
    'database': 'database',
    'sqlite': 'database',
    'bug': 'debugging',
    'error': 'debugging',
    'problem': 'debugging',
    'issue': 'debugging',
    'ne radi': 'debugging',
    'not working': 'debugging',
}

IMAGE_GENERATION_ALIASES = tuple(alias for alias, tool in TOOL_TERMS.items() if tool == 'image_generate')
TOOL_SIGNAL_TERMS = dict(TOOL_TERMS)
ARTIFACT_REQUIRED_TOOL_TOKENS = ('image_generate', 'ffmpeg', 'tts', 'google_tts')
RECIPE_TOOL_TOKENS = ARTIFACT_REQUIRED_TOOL_TOKENS + ('whisper', 'vision_analyze')


def contains_any(text: str, aliases: tuple[str, ...]) -> bool:
    lowered = (text or '').lower()
    return any(alias in lowered for alias in aliases)


def is_image_generation_tool(tool_name: str) -> bool:
    return contains_any(tool_name, ('image_generate', *IMAGE_GENERATION_ALIASES))


def tool_domain(tool_name: str) -> str:
    tool = (tool_name or '').lower()
    if is_image_generation_tool(tool):
        return 'image'
    if 'ffmpeg' in tool or 'video' in tool:
        return 'video'
    if 'tts' in tool or 'whisper' in tool or 'audio' in tool:
        return 'audio'
    if 'vision' in tool:
        return 'image'
    return ''
