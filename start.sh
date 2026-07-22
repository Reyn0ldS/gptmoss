#!/bin/bash
echo "==================================================="
echo "  Starting GPT-Moss Agentic Client Server"
echo "==================================================="

if [ ! -d "venv" ]; then
    echo "[WARNING] Virtual environment 'venv' not found. Running setup..."
    bash install.sh
fi

echo "[INFO] Activating virtual environment..."
source venv/bin/activate

echo "[INFO] Launching server on default port (http://127.0.0.1:8000)...
If running in background, check 'app.log' for details."
python main.py
