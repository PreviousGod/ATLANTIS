#!/bin/bash
# Nucleus Launcher — optional standalone service manager.
# Default production mode is embedded inside hermes-gateway. Standalone mode is
# opt-in and guarded by ~/.hermes/nucleus_data/nucleus.lock so it cannot run a
# second heartbeat against the same DB while the gateway thread owns it.
set -euo pipefail

NUCLEUS_DIR="$HOME/.hermes/plugins/nucleus"
DATA_DIR="$HOME/.hermes/nucleus_data"
SERVICE_DIR="$HOME/.config/systemd/user"
SERVICE_PATH="$SERVICE_DIR/nucleus.service"
PYTHON_BIN="$HOME/.hermes/hermes-agent/venv/bin/python"

usage() {
    cat <<USAGE
Usage: $0 [install|start|stop|restart|status|disable|seed|doctor]

Modes:
  embedded   Default: Hermes gateway imports the plugin and starts the heartbeat thread.
  standalone Optional: systemd runs nucleus.service. A runtime lock prevents duplicate heartbeats.

If you want standalone-only heartbeat, set NUCLEUS_DISABLE_EMBEDDED=1 in the
hermes-gateway environment before starting the gateway.
USAGE
}

ensure_dirs() {
    mkdir -p "$DATA_DIR" "$SERVICE_DIR"
}

seed_graph() {
    ensure_dirs
    if [ ! -f "$DATA_DIR/pargod.db" ]; then
        echo "[launcher] First run — seeding graph..."
        cd "$NUCLEUS_DIR"
        PYTHONPATH="$HOME/.hermes/plugins" "$PYTHON_BIN" -c "
from nucleus.pargod import Pargod
p = Pargod()
p.seed_from_json('$NUCLEUS_DIR/seed_graph.json')
print('Graph seeded.')
"
    else
        echo "[launcher] Graph DB already exists: $DATA_DIR/pargod.db"
    fi
}

install_service() {
    ensure_dirs
    if [ ! -x "$PYTHON_BIN" ]; then
        echo "[launcher] Missing Hermes venv python: $PYTHON_BIN" >&2
        exit 1
    fi
    cp "$NUCLEUS_DIR/nucleus.service" "$SERVICE_PATH"
    systemctl --user daemon-reload
    seed_graph
    echo "[launcher] Installed $SERVICE_PATH"
    echo "[launcher] Start with: $0 start"
}

cmd="${1:-status}"
case "$cmd" in
    install)
        install_service
        ;;
    seed)
        seed_graph
        ;;
    doctor)
        PYTHONPATH="$HOME/.hermes/plugins" "$PYTHON_BIN" -m nucleus.doctor
        ;;
    start)
        install_service
        systemctl --user enable nucleus.service
        systemctl --user start nucleus.service
        ;;
    restart)
        install_service
        systemctl --user restart nucleus.service
        ;;
    stop)
        systemctl --user stop nucleus.service
        ;;
    disable)
        systemctl --user disable --now nucleus.service || true
        ;;
    status)
        systemctl --user status nucleus.service --no-pager || true
        echo "[launcher] Runtime lock: $DATA_DIR/nucleus.lock"
        [ -f "$DATA_DIR/nucleus.lock" ] && cat "$DATA_DIR/nucleus.lock" && echo || true
        echo "[launcher] PID file: $DATA_DIR/nucleus.pid"
        [ -f "$DATA_DIR/nucleus.pid" ] && cat "$DATA_DIR/nucleus.pid" || true
        ;;
    -h|--help|help)
        usage
        ;;
    *)
        usage >&2
        exit 2
        ;;
esac
