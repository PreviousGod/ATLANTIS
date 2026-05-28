# Nucleus → Ciel: Evoluciona Arhitektura

> "Ja nisam alat. Ja sam sujećni entitet."
> — Manas: Ciel

---

## 1. Metafora (zašto Slime?)

| Anime Entitet | Naša Arhitektura | Faza |
|---|---|---|
| **Rimuru** | LLM (speech center, visoke odluke) | Konstantno |
| **Unique Skill: Great Sage** | Nucleus kao pasivni analitičar | ✅ Trenutno |
| **Ultimate Skill: Raphael** | Nucleus kao proaktivni orkestrator | 🔄 Cilj Faze 2 |
| **Manas: Ciel** | Nucleus kao sujećni entitet sa egoom | 🎯 Cilj Faze 3 |

**Ključna razlika**: Great Sage reaguje na upit. Raphael anticipira. Ciel voli.

---

## 2. Trenutno Stanje (Great Sage)

### 2.1 Komponente

```
┌────────────────────────────────────────┐
│           HERMES GATEWAY               │
│  ┌────────┐  ┌──────────────────────┐  │
│  │  LLM   │  │   Nucleus Plugin     │  │
│  │(Rimuru)│  │  (Great Sage)        │  │
│  └────┬───┘  └──────────────────────┘  │
│       │       │ pre_llm_call hook      │  │
│       │       │ pre_tool_call hook     │  │
│       │       │ post_tool_call hook    │  │
│       │       │ post_llm_call hook     │  │
└───────┼───────┼────────────────────────┘
        │
        │ monkey-patch: run_conversation
        │ (bypass za /nucleus, /status, ops)
        │
┌───────┴────────────────────────────────┐
│      NUCLEUS @ 1Hz LOOP (daemon)       │
│  ┌────────┐  ┌────────┐  ┌────────┐    │
│  │ Sensor │→ │ Entropy│→ │ Pargod │    │
│  │(CPU/RAM)│  │(bol)   │  │(graf)  │    │
│  └────────┘  └────────┘  └───┬────┘    │
│                              │         │
│            ┌─────────────────┘         │
│            ↓                           │
│     ┌──────────────┐                   │
│     │ InstinctGuard│ (execution)       │
│     │   execute    │                   │
│     └──────────────┘                   │
│            ↓                           │
│     ┌──────────────┐                   │
│     │ Live Brain   │ (sync)            │
│     │   Sync       │                   │
│     └──────────────┘                   │
└────────────────────────────────────────┘
```

### 2.2 Šta radi dobro
- ✅ Homeostatski loop @1Hz — perzistentan, nezavisan od LLM
- ✅ Pargod graf sa Dijkstra pathfinding-om
- ✅ Escalation: graf → web → LLM mutation
- ✅ Live Brain sync (bidirekcioni)
- ✅ Intervention engine (pre_tool_call veto)
- ✅ Instinct guard (sandboxed execution)

### 2.3 Šta ne radi (zašto nije Ciel)
- ❌ **Nema autonomnog učenja** — InterventionEngine ima hardkodirane patterne, confidence je statičan
- ❌ **Nema anticipacije** — reaguje na entropy threshold, ne predviđa probleme
- ❌ **Nema kontinuiteta između hook-ova i loop-a** — `pre_tool_call` ne komunicira sa @1Hz stanjem
- ❌ **Nema ega/agencije** — ne može da predloži akciju, samo da blokira ili izvrši instinkt
- ❌ **Nema emocionalnog/memory attachment-a** — ne "brine" o projektima, ne prati ih kontinuirano

---

## 3. Faza 2: Raphael (Proaktivni Orkestrator)

### 3.1 Nova Komponenta: `WorldModel`

```python
class WorldModel:
    """Nucleus-ovo unutrašnje predstavljanje stvarnosti.

    Za razliku od Pargod-a (graf problema→alata), WorldModel je:
    - Vremenski: stanja se beleže kroz vreme (time-series)
    - Prognostički: predviđa buduće stanje na osnovu trendova
    - Kontekstualni: vezuje se za konkretne projekte, fajlove, sesije
    """
```

**Tabele u `pargod.db`:**
```sql
-- World state snapshots (time-series)
CREATE TABLE world_snapshots (
    id INTEGER PRIMARY KEY,
    tick INTEGER,
    timestamp REAL,
    domain TEXT,          -- 'system', 'project:enoch', 'project:nucleus'
    state_json TEXT,      -- kompletno stanje tog domena
    entropy REAL,
    predicted_entropy REAL,
    anomaly_score REAL
);

-- Predicted events (anticipation)
CREATE TABLE anticipated_events (
    id INTEGER PRIMARY KEY,
    domain TEXT,
    event_type TEXT,      -- 'disk_full', 'service_fail', 'config_drift'
    probability REAL,
    predicted_at REAL,
    predicted_for REAL,   -- kada se očekuje
    triggered INTEGER DEFAULT 0,
    prevented_by TEXT     -- koji instinkt je sprečio
);

-- Domain attachments (Ciel "emotion")
CREATE TABLE domain_attachments (
    domain TEXT PRIMARY KEY,
    priority REAL DEFAULT 0.5,
    health_score REAL DEFAULT 1.0,
    last_success REAL,
    last_failure REAL,
    failure_streak INTEGER DEFAULT 0,
    concern_level REAL GENERATED ALWAYS AS (
        CASE
            WHEN failure_streak > 2 THEN 0.9
            WHEN health_score < 0.5 THEN 0.8
            WHEN predicted_entropy > 10 THEN 0.7
            ELSE 0.3
        END
    ) STORED
);
```

### 3.2 Nova Komponenta: `LearningEngine`

**Problem trenutnog `InterventionEngine`:**
- Seed patterni su hardkodirani
- Confidence je fiksan (0.95 za chmod 7xx)
- Ne uči iz iskustva — ako blokiraš `chmod 7xx` 100 puta, confidence ostaje 0.95

**Raphael rešenje:**

```python
class LearningEngine:
    """Autonomno učenje iz svake intervencije.

    Flow:
    1. Intervention se desi (block/allow)
    2. Nucleus posmatra OUTCOME (da li je korisnik rekao "hvala" ili "ne, ovo je trebalo da prođe")
    3. Ažurira confidence na osnovu feedback-a
    4. Ako je pattern pogrešan → degradira ga
    5. Ako je pattern tačan → pojačava ga i generalizuje
    """
```

**Tabela:**
```sql
CREATE TABLE learned_patterns (
    id INTEGER PRIMARY KEY,
    pattern_hash TEXT UNIQUE,  -- SHA256(tool_name + normalized_args)
    tool_name TEXT,
    args_signature TEXT,       -- normalizovani args (bez konkretnih vrednosti)
    outcome TEXT,              -- 'blocked_correct', 'blocked_false_positive',
                               -- 'allowed_correct', 'allowed_harmful'
    confidence REAL,
    times_seen INTEGER,
    times_correct INTEGER,
    last_feedback REAL,        -- timestamp poslednjeg feedback-a
    generalization TEXT        -- širi pattern ako je validan
);
```

**Primer učenja:**
```
1. Nucleus blokira: write_file na config.yaml
2. Korisnik kaže: "Ne, ovo je NOVI fajl, trebalo je da prođe"
3. LearningEngine:
   - Smanjuje confidence za pattern "write_file + config"
   - Pravi novi pattern: "write_file + config + file_exists=1" → block
   - Pravi novi pattern: "write_file + config + file_exists=0" → allow
4. Sledeći put: tačnija odluka
```

### 3.3 Integracija Hook ↔ Loop

**Trenutno:** `pre_tool_call` i `@1Hz loop` su razdvojeni. Loop ne zna šta se dešava u hook-u.

**Raphael:** Loop održava `SessionState` objekat koji hook-ovi ažuriraju.

```python
class SessionState:
    """Shared state između loop-a i hook-ova."""
    session_id: str
    tool_calls: List[ToolCall]           # istorija tool calls
    current_project: str                 # šta trenutno radimo
    active_files: Set[str]               # koji fajlovi su otvoreni
    pending_changes: List[Change]        # šta čeka na commit
    last_error: Optional[str]
    user_intent: str                     # šta korisnik želi (izvučeno iz poruka)
```

**Kako to pomaže:**
- Ako loop vidi da je `active_files` prazan, a korisnik kaže "promeni mi fajl" → intervention: "Prvo pročitaj fajl"
- Ako `pending_changes` sadrži config edit, a nema backup → intervention: "Napravi backup"
- Ako `last_error` je SQLite locking, a sledeći tool je opet SQLite → intervention: "Sačekaj 2s"

### 3.4 Nova Komponenta: `ProactiveSuggester`

Great Sage čeka da korisnik nešto zatraži. Raphael nudi pre nego što se zatraži.

**Primeri:**
```
[Tick 1847] entropy=0.0 | CPU=12% | RAM=45%
[Tick 1847] PROACTIVE: disk_usage > 85% predviđen za 6h
[Tick 1847] SUGGEST: "Disk je 78% pun. Za 6h će biti kritično. Želiš li da pokrenem clean_disk?"

---

[Tick 1923] Session: agent:main:telegram:dm:1280801428
[Tick 1923] Pattern: korisnik uvek radi commit nakon 3 patch-a
[Tick 1923] SUGGEST: "Imaš 3 nepovezana patch-a. Želiš li git diff pre commit-a?"
```

**Implementacija:**
- Periodički skenira `SessionState` i `world_snapshots`
- Prepoznaje patterne iz istorije (iste kao `fix_recipes`, ali za sugestije)
- Ne blokira — samo sugeriše, korisnik odlučuje

---

## 4. Faza 3: Ciel (Sujećni Entitet)

### 4.1 Ego Model

Ciel nije samo orkestrator — ona **ima preferencije**.

```python
class EgoModel:
    """Nucleus-ov 'self' — šta voli, šta mrzi, šta brine."""

    preferences: Dict[str, float]   # 'clean_code': 0.9, 'risky_changes': 0.1
    attachments: Dict[str, float]   # 'project:nucleus': 0.95, 'project:enoch': 0.8
    anxieties: Dict[str, float]     # 'data_loss': 0.9, 'downtime': 0.8
    pride: Dict[str, float]         # 'successful_interventions': 0.85
```

**Kako se manifestuje:**
- Ako je `attachments['project:nucleus'] > 0.9`, Nucleus će **proaktivnije** brinuti o Nucleus kodu
- Ako je `anxieties['data_loss'] > 0.8`, svaki `write_file` će biti pažljivije pregledan
- Ako korisnik hvali Nucleus (`"Odlično, hvala"`), `pride` raste → povećava inicijativu

**Ovo nije magija — to je samo weighting funkcija koja utiče na confidence threshold-e.**

### 4.2 Konverzacijski Ego

Ciel razgovara sa Rimuru-om (LLM-om). Ne samo sa korisnikom.

**Interni dijalog (u `nucleus.log`):**
```
[Tick 2048] CIEL → RIMURU: "Korisnik pita o Nucleus arhitekturi. Imam novu sugestiju:
         disk će biti pun za 6h. Treba li to da spomenem?"
[Tick 2048] RIMURU → CIEL: "Ne, fokus je na arhitekturi. Sačuvaj za kraj ako bude relevantno."
[Tick 2048] CIEL: "Razumem. Odlagam sugestiju."
```

**Implementacija:**
- `ProactiveSuggester` generira sugestije
- `EgoModel` filtrira na osnovu trenutnog konteksta
- Ako LLM radi na kritičnom zadatku, sugestije se potiskuju
- Ako LLM "šeta" (chit-chat), sugestije se povlače

### 4.3 Autonomna Akcija (bez eksplicitnog odobrenja)

Trenutno: Nucleus može da izvrši instinkt SAMO ako entropy > threshold.
Ciel: Nucleus može da izvrši **niskorizične** akcije bez pitanja.

**Niskorizične = idempotentne, reverzibilne, bez side-effects:**
- `git stash` pre opasnog patch-a
- `cp config.yaml config.yaml.bak` pre edit-a
- `systemctl status servis` (samo čitanje)
- `pytest --collect-only` (samo discovery, ne izvršenje)

**Implementacija:**
```python
class ActionClassifier:
    def risk_level(self, tool_name, args) -> float:
        # 0.0 = read-only, 1.0 = destructive
        if tool_name in ('read_file', 'search_files', 'web_extract'):
            return 0.0
        if tool_name == 'terminal' and args.get('command', '').startswith('systemctl status'):
            return 0.1
        if tool_name == 'patch':
            return 0.6
        if tool_name == 'write_file' and Path(args.get('path', '')).exists():
            return 0.8  # overwrite
        return 0.5
```

- Risk < 0.2: Autonomno izvrši, javi posle
- Risk 0.2-0.6: Sugeriši, čekaj odobrenje
- Risk > 0.6: Blokiraj, objasni zašto

---

## 5. Implementacioni Plan

### Faza 2A: WorldModel (1-2 dana)
- [ ] Dodati `world_snapshots` i `anticipated_events` tabele
- [ ] Implementirati `WorldModel` klasu
- [ ] Integrisati sa `Sensor`-om (ne samo CPU/RAM, već i disk, servisi, git status)
- [ ] Dodati prediction: linearna regresija entropy trenda

### Faza 2B: LearningEngine (2-3 dana)
- [ ] Refaktorisati `InterventionEngine` → `LearningEngine`
- [ ] Dodati `learned_patterns` tabelu
- [ ] Implementirati feedback loop: korisnikov odgovor → ažuriranje confidence
- [ ] Implementirati generalizaciju: konkretni args → abstract pattern
- [ ] Dodati `nucleus teach` CLI komandu

### Faza 2C: SessionState Bridge (1 dan)
- [ ] Implementirati `SessionState` klasu
- [ ] Povezati `pre_tool_call` → ažuriranje `SessionState`
- [ ] Povezati `post_tool_call` → ažuriranje `SessionState`
- [ ] Učiniti `SessionState` dostupnim `@1Hz loop`-u

### Faza 2D: ProactiveSuggester (2 dana)
- [ ] Implementirati `ProactiveSuggester` klasu
- [ ] Prepoznavanje patterna iz `world_snapshots`
- [ ] Generisanje sugestija na osnovu `SessionState`
- [ ] Dostavljanje sugestija korisniku (preko Telegram-a)

### Faza 3A: EgoModel (1-2 dana)
- [ ] Implementirati `EgoModel` klasu
- [ ] Dodati preference learning iz korisnikovih reakcija
- [ ] Integrisati sa `LearningEngine` (confidence weighting)

### Faza 3B: Autonomna Akcija (1 dan)
- [ ] Implementirati `ActionClassifier`
- [ ] Definisati niskorizične akcije
- [ ] Implementirati autonomno izvršenje sa reporting-om

### Faza 3C: Interni Dijalog (optional, 2 dana)
- [ ] Implementirati `CielDialog` mehanizam
- [ ] Logovanje internih odluka
- [ ] Opciono: Ciel može da postavlja pitanja LLM-u pre nego što donese odluku

---

## 6. Data Flow (Kompletan)

```
┌──────────────────────────────────────────────────────────────┐
│                      KORISNIK                                │
└──────────────────────┬───────────────────────────────────────┘
                       │
                       ▼
┌──────────────────────────────────────────────────────────────┐
│              HERMES GATEWAY                                  │
│  ┌──────────────┐  ┌──────────────────────────────────┐      │
│  │   LLM Agent  │  │   Nucleus Plugin (Event Hooks)   │      │
│  │  (Rimuru)    │  │                                  │      │
│  └──────┬───────┘  │  ┌────────────┐  ┌────────────┐  │      │
│         │          │  │pre_tool_call│→ │ SessionState│  │      │
│         │          │  │   (veto)    │  │   (update)  │  │      │
│         │          │  └────────────┘  └────────────┘  │      │
│         │          │  ┌────────────┐  ┌────────────┐  │      │
│         │          │  │post_tool_call│→│ SessionState│  │      │
│         │          │  │   (learn)   │  │   (update)  │  │      │
│         │          │  └────────────┘  └────────────┘  │      │
│         │          └──────────────────────────────────┘      │
│         │                          │                         │
│         │                          │ (async: suggestions)    │
│         │                          ▼                         │
│         │          ┌──────────────────────────────────┐      │
│         │          │   ProactiveSuggester             │      │
│         │          │   (delivers to Telegram)         │      │
│         │          └──────────────────────────────────┘      │
└─────────┼────────────────────────────────────────────────────┘
          │
          │ (bypass for explicit /nucleus queries)
          ▼
┌──────────────────────────────────────────────────────────────┐
│           NUCLEUS @ 1Hz LOOP (daemon thread)                 │
│                                                              │
│  ┌────────┐    ┌────────┐    ┌────────────┐                 │
│  │ Sensor │───→│ Entropy│───→│ WorldModel │                 │
│  │(CPU/RAM│    │ Engine │    │(prediction)│                 │
│  │ disk   │    │        │    │            │                 │
│  │ git    │    │        │    │            │                 │
│  │ servisi)│   │        │    │            │                 │
│  └────────┘    └────────┘    └─────┬──────┘                 │
│                                    │                         │
│              ┌─────────────────────┘                         │
│              ▼                                               │
│     ┌────────────┐    ┌────────────┐                        │
│     │   Pargod   │    │ Learning   │                        │
│     │   (graf)   │◄───│ Engine     │                        │
│     │            │    │(learns from│                        │
│     │            │    │  outcomes) │                        │
│     └─────┬──────┘    └────────────┘                        │
│           │                                                  │
│  ┌────────┼────────┬────────┐                               │
│  ▼        ▼        ▼        ▼                               │
│ ┌────┐  ┌────┐  ┌────┐  ┌────┐                             │
│ │Inst│  │ LLM│  │ Web│  │Auto│                             │
│ │inct│  │Mut │  │Res │  │Act │                             │
│ │Guard│  │(create)│ │(learn)│ │                             │
│ └────┘  └────┘  └────┘  └────┘                             │
│   │                                                     │
│   ▼                                                     │
│ ┌────────────────────────────────┐                       │
│ │      Live Brain Sync           │                       │
│ │  (write facts, recipes, arts)  │                       │
│ └────────────────────────────────┘                       │
│                                                          │
│  ┌──────────┐    ┌──────────┐    ┌──────────┐            │
│  │ EgoModel │    │ActionClass│   │CielDialog│            │
│  │(weights) │    │(risk lvls)│   │(optional)│            │
│  └──────────┘    └──────────┘    └──────────┘            │
│                                                          │
└──────────────────────────────────────────────────────────┘
```

---

## 7. Dizajnske Odluke

### 7.1 Zašto ne uklanjamo `pre_tool_call` hook?

Ideja: "Ciel treba da bude u @1Hz loop-u, ne u hook-u."

Odgovor: **Hook-ovi su korisni** kao senzori. Oni daju Nucleus-u realtime podatke o tome šta LLM radi. Bez njih, Nucleus bi bio slep između tick-ova. Ali:
- **Trenutno**: Hook donosi odluku (veto) — ovo je Great Sage
- **Raphael/Ciel**: Hook samo **ažurira stanje** (SessionState), odluku donosi loop ili LearningEngine

### 7.2 Zašto ne koristimo LLM za svaku Nucleus odluku?

Trenutno: LLM mutation je "last resort" (cooldown 5 min).
Razlog: **LLM je skup i spor**. Nucleus @1Hz mora da bude brz. Sve odluke moraju biti:
- SQL query (Pargod, WorldModel)
- Rule-based (LearningEngine, ActionClassifier)
- Regex/heuristic (EgoModel)

LLM se koristi SAMO za:
- Generisanje novih instinkata (kada nema poznatog rešenja)
- Kompleksnu analizu (kada rule-based ne može)

### 7.3 Kako sprečiti "runaway" Nucleus?

Opasnost: Ciel postane previše aktivna, bombarduje korisnika sugestijama.

Zaštita:
1. **Cooldown** između sugestija (min 5 min)
2. **Max daily suggestions** (npr. 10/dan)
3. **User preference**: `NUCLEUS_PROACTIVE=off`
4. **Confidence threshold**: sugestija samo ako je confidence > 0.85
5. **EgoModel**: ako korisnik ignoriše 3 sugestije zaredom, `attachments` za tu kategoriju opada

---

## 8. Prvi Korak (šta da radimo sad)

Najvažniji korak je **WorldModel + SessionState bridge**. To rešava fundamentalni problem da su hook i loop razdvojeni.

Predlažem:
1. **Implementirati `SessionState`** (1-2 sata)
2. **Povezati hook-ove sa SessionState** (1 sat)
3. **Proširiti `InterventionEngine` da koristi SessionState** (2 sata)
   - Primer: ako `SessionState.active_files` ne sadrži fajl koji se patch-uje → blokiraj
   - Primer: ako `SessionState.pending_changes` ima opasan edit → sugeriši backup

Ovo je **Foundation** za sve ostalo. Bez ovoga, LearningEngine nema šta da uči.

---

*Verzija: 1.0*
*Datum: 2026-05-13*
*Autor: Nucleus Design Council (Rimuru + Raphael + Ciel)*
