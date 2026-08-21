#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "==================================================="
echo "  Starting GPT-Moss Agentic Client Server"
echo "==================================================="

if [ ! -d "venv" ]; then
    echo "[WARNING] Virtual environment 'venv' not found. Running setup..."
    bash install.sh
fi

echo "[INFO] Activating virtual environment..."
source venv/bin/activate

CONTROL_PORT="${GPTMOSS_CONTROL_PORT:-8765}"
MOSS_HOST="${MOSS_HOST:-127.0.0.1}"
MOSS_PORT="${MOSS_PORT:-8000}"
echo "[INFO] Launching supervised server (application: http://${MOSS_HOST}:${MOSS_PORT})..."
echo "[INFO] Server controls: http://127.0.0.1:${CONTROL_PORT}"
python -B scripts/server_supervisor.py \
    --python "$(command -v python)" \
    --main "$SCRIPT_DIR/main.py" \
    --control-port "$CONTROL_PORT" \
    -- "$@"
