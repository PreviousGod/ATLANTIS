"""Shared state constants for live_brain_ctx.

All module-level globals that used to live in `live_brain_ctx/__init__.py` now
live here so helper modules can import them without circular references.

Some values are mutable — they are rebound by ``apply_context_config()`` based
on the merged JSON config at init time. After that they are effectively
read-only.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List


# ---------------------------------------------------------------------------
# Runtime counters / globals
# ---------------------------------------------------------------------------
LAST_MAINTENANCE_TIME = 0.0
MAINTENANCE_INTERVAL = 3600.0  # run DB maintenance at most once per hour
LAST_CONTEXT_METADATA: Dict[str, Any] = {'recipe_ids': []}

# ---------------------------------------------------------------------------
# TTL + size budgets
# ---------------------------------------------------------------------------
CONSTRAINT_TTL_DAYS = 7
MAX_ACTIVE_EPISODES = 3
MAX_FACT_LEN = 200

# ---------------------------------------------------------------------------
# Mutable configuration (rebound by apply_context_config)
# ---------------------------------------------------------------------------
CHIT_CHAT_PATTERNS: set[str] = {
    # Serbian / Croatian / Bosnian
    'zdravo', 'cao', 'ćao', 'ajde', 'nastavi', 'cekaj', 'čekaj', 'naravno',
    'sta ima', 'šta ima', 'kako si', 'da', 'ne',
    'hvala', 'hvala ti', 'fala', 'super', 'odlično', 'odlicno', 'extra',
    'top', 'svaka cast', 'svaka čast', 'bravo',
    # Russian
    'privet', 'привет', 'zdorovo', 'здорово', 'da net', 'да нет',
    'kak dela', 'как дела', 'ladno', 'ладно', 'horosho', 'хорошо',
    'ponyatno', 'понятно', 'prodolzhai', 'продолжай', 'prodolzai',
    'davai', 'давай', 'poka', 'пока', 'spasibo', 'спасибо',
    'okey', 'окей', 'ага', 'aga', 'uhu', 'угу',
    'otlichno', 'отлично', 'super', 'супер', 'kruto', 'круто',
    'klass', 'класс', 'krasava', 'красава', 'molodets', 'молодец',
    # Ukrainian
    'pryvit', 'привіт', 'zdorov', 'здоров', 'dobre', 'добре',
    'harazd', 'гаразд', 'zvichaino', 'звічайно', 'prodovzhui', 'продовжуй',
    'diakuiu', 'дякую', 'zrozumilo', 'зрозуміло',
    'tak', 'так', 'ni', 'ні', 'chekai', 'чекай',
    'vidminno', 'відмінно', 'super', 'супер', 'kruto', 'круто',
    'klas', 'клас', 'krasavchyk', 'красавчик', 'molodets', 'молодець',
    # English
    'hello', 'hi', 'hey', 'ok', 'hmm', 'hm',
}

LOW_SIGNAL_WORDS: set[str] = {
    'problem', 'plugin', 'memory', 'brain', 'generation', 'generate',
    # Serbian / Croatian / Bosnian
    'napravi', 'uradi', 'kako', 'sta', 'šta', 'radi', 'recap',
    'poslednje', 'uradjeno', 'urađeno', 'gde', 'gdje', 'dje',
    'stali', 'stao', 'stala', 'rekao', 'rekla', 'rekli', 'sam', 'smo',
    'odgovori', 'odgovor', 'sećanja', 'secanja', 'secanca',
    'traži', 'trazi', 'ponavljam', 'ponavljati', 'ponovi',
    # Russian
    'sdelai', 'сделай', 'sdelat', 'сделать', 'kak', 'как', 'chto', 'что',
    'gde', 'где', 'kuda', 'куда', 'zachem', 'зачем', 'pochemu', 'почему',
    'kakoj', 'какой', 'skazhi', 'скажи', 'rasskazhi', 'расскажи',
    'otvet', 'ответ', 'povtori', 'повтори', 'pomniu', 'помню',
    'proshlyj', 'прошлый', 'poslednij', 'последний', ' bylo', 'было',
    'sdelano', 'сделано', 'pomogite', 'помогите',
    # Ukrainian
    'zroby', 'зроби', 'zrobyty', 'зробити', 'iak', 'як', 'shcho', 'що',
    'de', 'де', 'kudy', 'куди', 'chomu', 'чому', 'navishcho', 'навіщо',
    'iakii', 'який', 'skazhy', 'скажи', 'rozpovisy', 'розповіси',
    'vidpovid', 'відповідь', 'povtory', 'повтори', 'pamiataiu', 'пам\'ятаю',
    'mynulyi', 'минулий', 'ostannii', 'останній', 'bulo', 'було',
    'zrobleno', 'зроблено', 'dopomozhit', 'допоможіть',
    # English
    'what', 'which', 'with', 'how', 'where', 'why', 'when', 'who',
}

MEDIA_DOMAIN_WORDS: set[str] = {
    'video', 'image', 'audio', 'render', 'export', 'ffmpeg', 'media', 'file',
}

SECTION_LIMITS: Dict[str, int] = {
    'MUST FOLLOW': 3,
    'VERIFIED ARTIFACTS': 5,
    'ACTIVE TASK': 1,
    'KNOWN FACTS': 4,
    'OPEN BUG': 2,
    'PROVEN FIX': 3,
    'NEXT REQUIRED ACTION': 1,
    'RECENT EPISODES': 3,
    'PENDING APPROVAL': 3,
    'EPISTEMIC STATUS': 8,
    'CONTINUITY MEMORY': 5,
}

AUTO_SURFACE_PENDING_APPROVALS = True

# ---------------------------------------------------------------------------
# Recall-pattern vocabularies
# ---------------------------------------------------------------------------
RECALL_QUERY_WORDS: set[str] = {
    'gde', 'gdje', 'dje', 'dokle', 'stali', 'stao', 'stala', 'ostali',
    'dosli', 'došli', 'rekao', 'rekla', 'rekli', 'told', 'where', 'were',
    'leave', 'left', 'off', 'odgovori', 'odgovor', 'sećanja', 'secanja',
    'traži', 'trazi', 'ponavljam', 'ponovi',
}

MUSIC_MEMORY_ALIASES: tuple[str, ...] = (
    'pesm', 'pjesm', 'song', 'songs', 'music', 'muzik', 'cover', 'flamenco',
    'triler', 'trileri', 'trilerima', 'serbezovski', 'esmeralda', 'lyrics',
    'romska', 'romski', 'spanski', 'španski', 'spanish', 'gitar', 'gitara',
    'reference', 'referenca',
)

REVIEW_ONLY_TERMS: tuple[str, ...] = (
    # Serbian / Croatian / Bosnian
    'review', 'pregled', 'recenz', 'verdikt', 'analiziraj', 'analiza',
    'oceni', 'ocjena', 'ocena', 'šta fali', 'sta fali', 'šta još fali',
    'sta jos fali',
    # Russian
    'prover', 'провер', 'otsen', 'оцен', 'analizirui', 'анализируй',
    'analiz', 'анализ', 'retse', 'реце', 'verdikt', 'вердикт',
    'chego ne khvataet', 'чего не хватает', 'chto dumaesh', 'что думаешь',
    'kak tebe', 'как тебе', 'otsenka', 'оценка', 'smotri', 'смотри',
    # Ukrainian
    'perevis', 'перевір', 'otsiny', 'оціни', 'analizui', 'аналізуй',
    'analiz', 'аналіз', 'retsenz', 'реценз', 'verdikt', 'вердикт',
    'choho ne vystachaie', 'чого не вистачає', 'shcho dumaiesh', 'що думаєш',
    'iak tobi', 'як тобі', 'otsinka', 'оцінка', 'dyvys', 'дивись',
    # English
    'analyze', 'analysis', 'rate', 'rating', 'score',
    'what is missing', 'what do you think',
)

CHANGE_INTENT_TERMS: tuple[str, ...] = (
    # Serbian / Croatian / Bosnian
    'implement', 'patch', 'fix', 'sredi', 'poprav', 'change', 'promeni',
    'promijeni', 'dodaj', 'odradi', 'uradi posao', 'reši', 'resi', 'resolve',
    'apply', 'edit', 'update code',
    # Russian
    'isprav', 'исправ', 'poprav', 'sdelai', 'сделай', 'dobav', 'добав',
    'izmeni', 'измени', 'pomeniai', 'поменяй', 'realizui', 'реализуй',
    'vnedri', 'внедри', 'napishi', 'напиши', 'kod', 'код',
    'primeni', 'примени', 'obnovi', 'обнови', 'zameni', 'замени',
    # Ukrainian
    'vyprav', 'виправ', 'zroby', 'зроби', 'dodai', 'додай',
    'zminy', 'зміни', 'pominiai', 'поміняй', 'realizui', 'реалізуй',
    'vprovad', 'впровадь', 'napyshy', 'напиши', 'kod', 'код',
    'zastosui', 'застосуй', 'onovy', 'онови', 'zaminy', 'заміни',
)

# ---------------------------------------------------------------------------
# Regex pattern library
# ---------------------------------------------------------------------------
SECRET_RE = re.compile(
    r'\b(?:sk-[A-Za-z0-9_-]{12,}|sk-or-v1-[A-Za-z0-9_-]{12,}|'
    r'[A-Za-z0-9_]*(?:api[_-]?key|token|secret)[A-Za-z0-9_]*\s*[:=]\s*\S+)',
    re.IGNORECASE,
)

NOISY_MEMORY_RE = re.compile(
    r'(##\s*summary|###\s*situacija|the user sent an image|'
    r'the user sent a voice message|selfie photo|personal trust|'
    r'gave me his selfie|openrouter api key|api key \(active|client_secret|'
    r'review the conversation above)',
    re.IGNORECASE,
)

LOW_VALUE_FACT_RE = re.compile(
    r'(dobra pitanje|refaktorisao live brain|evo kako bih|'
    r'na osnovu memory context)',
    re.IGNORECASE,
)

SYNTHETIC_MEMORY_RE = re.compile(
    r'\b(?:ack-seed|ack-infer|live_brain_human_memory_seed|'
    r'memory_sync_fix_test|lbmemsync-|hmem-|kestrel\s+harbor|'
    r'live_brain_capability_e2e|upamti\s+ovo\s+kao\s+stvarno\s+pravilo)\b',
    re.IGNORECASE,
)

CONTINUATION_QUERY_RE = re.compile(
    # Serbian/Croatian/Bosnian
    r'\b(?:gde|gdje|đe|dje|dokle|where)\b.{0,80}\b(?:stali|stao|stala|ostali|došli|dosli|were|left|off)\b|'
    r'\b(?:šta|sta|što|sto|what)\b.{0,80}\b(?:rekao|rekla|rekli|told|radili|radimo|dogovorili)\b|'
    r'\b(?:nastavi|continue|where\s+were\s+we|where\s+did\s+we\s+leave\s+off)\b|'
    # Russian
    r'\b(?:gde|где|kuda|куда|dokuda|докуда)\b.{0,80}\b(?:ostanovilis|остановились|ostalis|остались|byli|были)\b|'
    r'\b(?:chto|что|chego|чего)\b.{0,80}\b(?:skazal|сказал|skazala|сказала|skazali|сказали|delali|делали|dogovorilis|договорились)\b|'
    r'\b(?:prodolzhai|продолжай|prodolzai|davai dalshe|давай дальше|na chom ostanovilis|на чём остановились)\b|'
    # Ukrainian
    r'\b(?:de|де|kudy|куди|dokudy|докуди)\b.{0,80}\b(?:zupynylys|зупинились|zalyshylys|залишились|buly|були)\b|'
    r'\b(?:shcho|що|choho|чого)\b.{0,80}\b(?:skazav|сказав|skazala|сказала|skazaly|сказали|robyly|робили|domovylys|домовились)\b|'
    r'\b(?:prodovzhui|продовжуй|davai dali|давай далі|na chomu zupynylys|на чому зупинились)\b',
    re.IGNORECASE | re.DOTALL,
)

RUN_MARKER_RE = re.compile(r'\b(?:run|lbcap|codename)[-_][a-z0-9]+\b', re.IGNORECASE)

DESTRUCTIVE_MEMORY_RE = re.compile(
    r'\b(?:'
    # Serbian / Croatian / Bosnian
    r'izbriši|izbrisi|obriši|obrisi|briši|brisi|'
    # Russian
    r'udali|удали|udali|удали|udalyat|удалять|ubrat|убрать|ubirai|убирай|'
    r'sotri|сотри|steret|стереть|snesti|снести|'
    # Ukrainian
    r'vydaly|видали|vydaliaty|видаляти|prybraty|прибрати|'
    r'zitry|зітри|sterty|стерти|znesty|знести|'
    # English
    r'delete|remove|rm'
    r')\b',
    re.IGNORECASE,
)

NEGATED_DESTRUCTIVE_RE = re.compile(
    r"\b(?:ne|nemoj|never|do\s+not|don'?t|dont)\s+(?:da\s+)?"
    r"(?:izbriši|izbrisi|obriši|obrisi|briši|brisi|delete|remove|rm)\b",
    re.IGNORECASE,
)

MEDIA_PROJECT_MEMORY_RE = re.compile(
    r'\b(?:enoch|media\s+delivery|messagemediadocument|artifact\s+selection|'
    r'wrong\s+artifact|video\s+attachments?|video\s+delivery|mp4|'
    r'pošalji\s+mi\s+ona\s+dva|posalji\s+mi\s+ona\s+dva)\b',
    re.IGNORECASE,
)

MEDIA_PROJECT_QUERY_RE = re.compile(
    r'\b(?:enoch|media|video|mp4|attachment|artifact|artefact|delivery|'
    r'messagemediadocument|pošalji|posalji)\b',
    re.IGNORECASE,
)

MUSIC_DOMAIN_RE = re.compile(
    r'\b(?:pesm\w*|pjesm\w*|song|songs|music|muzik\w*|cover|lyrics|'
    r'aran[žz]man\w*|[cč]ujem|[cč]uje[sš]|25-30%?|triler\w*|flamenco|'
    r'serbezovski|esmeralda|romsk\w*|[sš]pansk\w*|spanish|gitar\w*|suno)\b',
    re.IGNORECASE,
)

VOICE_TTS_DOMAIN_RE = re.compile(
    r'\b(?:tts|voice|glas|piper|xtts|mms|qwen3tts|obliteratus|'
    r'abliteration-config|voiceover|speech|audio)\b|templates/[^\s]+\.ya?ml',
    re.IGNORECASE,
)

PATH_CONFIG_RE = re.compile(
    r'(?:(?:^|\s)(?:\.?/|/)[^\s]+|\b[^\s]+\.(?:ya?ml|json|toml|py|wav|mp3|mp4)\b)',
    re.IGNORECASE,
)

PATH_CONFIG_QUERY_RE = re.compile(
    r'\b(?:path|putanja|file|fajl|config|konfig|yaml|json|repo|skript|'
    r'script|code|kod|template)\b',
    re.IGNORECASE,
)

RAW_TOOL_FACT_RE = re.compile(
    r'\b(?:successfully\s+used\s+tool|tool_result|browser_scroll|'
    r'browser_navigate|execute_code)\b|'
    r'[{}]["\']?(?:success|ok|proposals|tool_calls)["\']?\s*:',
    re.IGNORECASE,
)

RAW_TOOL_QUERY_RE = re.compile(
    r'\b(?:tool|alat|debug|trace|raw|json|payload|browser|command|komand|'
    r'code|kod|repo)\b',
    re.IGNORECASE,
)

OPEN_LOOP_FACT_RE = re.compile(
    r'\b(?:active\s+open\s+loop|open\s+loops?|current\s+objective|'
    r'safe\s+next\s+action)\b',
    re.IGNORECASE,
)

OPEN_LOOP_QUERY_RE = re.compile(
    r'\b(?:open\s+loops?|unfinished|nezavr|zavr[šs]|krenuo|stali|objective|'
    r'status|dashboard|link|blok|blocker)\b',
    re.IGNORECASE,
)

META_WORK_ITEM_RE = re.compile(
    r'\b(?:review\s+only|oceni|ocena|analiziraj|analysis|review|'
    r'gateway\s+restartovan|restartovan|restartovao|'
    r'patch(?:-eva|evi|ovan|ovano)?\s+(?:je\s+)?(?:primenjen|primijenjen|applied)|'
    r'codex\s+je\s+patchovao|drugi\s+krug|tre[cć]i\s+krug)\b',
    re.IGNORECASE,
)
