#!/bin/bash
set -e

echo "==================================================="
echo "  GPT-Moss Agentic Client Setup (Linux/macOS)"
echo "==================================================="

# 1. Check Python
if ! command -v python3 &> /dev/null; then
    echo "[ERROR] python3 could not be found. Please install Python 3.10+."
    exit 1
fi

# 2. Check virtualenv module
if ! python3 -m venv --help &> /dev/null; then
    echo "[ERROR] python3-venv module is missing. Install it using 'sudo apt-get install python3-venv' or equivalent."
    exit 1
fi

# 3. Create venv
if [ ! -d "venv" ]; then
    echo "[INFO] Creating virtual environment in 'venv'..."
    python3 -m venv venv
else
    echo "[INFO] Virtual environment 'venv' already exists."
fi

# 4. Install dependencies
echo "[INFO] Activating virtual environment and installing dependencies..."
source venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

# 5. Copy templates
if [ ! -f ".env" ]; then
    echo "[INFO] Initializing .env config file..."
    cp .env.template .env
fi

if [ ! -d "workspace" ]; then
    mkdir workspace
fi

if [ ! -f "workspace/config.json" ]; then
    echo "[INFO] Initializing config.json in workspace..."
    cp config.json.template workspace/config.json
fi

# 6. Make start script executable
if [ -f "start.sh" ]; then
    chmod +x start.sh
fi

echo "==================================================="
echo "[SUCCESS] Setup completed successfully!"
echo "Run './start.sh' to start the application."
echo "==================================================="
