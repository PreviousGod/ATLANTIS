"""Nucleus configuration — all thresholds in one place."""
from pathlib import Path

NUCLEUS_HOME = Path.home() / ".hermes" / "plugins" / "nucleus"
DATA_DIR = Path.home() / ".hermes" / "nucleus_data"
PARGOD_DB = Path.home() / ".hermes" / "nucleus" / "pargod.db"
PID_FILE = DATA_DIR / "nucleus.pid"
LOCK_FILE = DATA_DIR / "nucleus.lock"
LOG_FILE = DATA_DIR / "nucleus.log"
INSTINCTS_DIR = NUCLEUS_HOME / "instincts"

LIVE_BRAIN_DB = Path.home() / ".hermes" / "live_brain" / "live_brain.db"

# Engine
TICK_RATE = 1.0
ENTROPY_THRESHOLD = 5.0

# Sensor thresholds
THRESHOLD_CPU = 80.0
THRESHOLD_RAM = 85.0
THRESHOLD_DISK = 90.0

# Entropy weights
WEIGHT_CPU = 0.5
WEIGHT_RAM = 0.3
WEIGHT_DISK = 0.2
WEIGHT_SWAP = 0.15
WEIGHT_LOAD = 0.3

# LLM Bridge
LLM_COOLDOWN_SECONDS = 300  # 5 min between LLM calls
LLM_TIMEOUT = 120

# Web Search
WEB_SEARCH_TIMEOUT = 10.0
WEB_SEARCH_MAX_RESULTS = 5
WEB_SEARCH_MAX_BYTES_PER_SOURCE = 24000

# Instinct Guard
GUARD_TIMEOUT = 30
GUARD_MEMORY_MB = 256

# Autonomous Action
AUTO_APPROVE_RISK_THRESHOLD = 0.2
AUTO_APPROVE_WHITELIST = [
    "report_top_cpu.py",
    "report_top_ram.py",
    "check_dns.py",
    "check_service.py",
    "find_port_user.py",
]
AUTO_MAX_PER_HOUR = 3
AUTO_DEBOUNCE_SECONDS = 600

# Live Brain Sync
SYNC_INTERVAL_TICKS = 60

# Edge decay
EDGE_DECAY_RATE = 0.01  # per hour unused
EDGE_MIN_WEIGHT = 0.1
