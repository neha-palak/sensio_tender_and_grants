#!/bin/bash
set -e
trap 'echo; echo "Failed -- see error above."; read -p "Press Enter to close..."' ERR
cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
    echo "Python not found. Install it from https://python.org/downloads (or 'brew install python')."
    read -p "Press Enter to close..."
    exit 1
fi

if [ ! -d .venv ]; then
    python3 -m venv .venv
fi

echo "Installing dependencies (first run only takes a while)..."
.venv/bin/pip install -q -r requirements.txt
.venv/bin/python -m playwright install chromium

# --- Redis: main.py expects one on localhost:6379 ---
if ! .venv/bin/python -c "import redis; redis.Redis(host='localhost', port=6379).ping()" >/dev/null 2>&1; then
    echo "Redis isn't running -- starting it..."
    if command -v brew >/dev/null 2>&1; then
        brew list redis >/dev/null 2>&1 || brew install redis
        brew services start redis
        sleep 2
    else
        echo "Homebrew not found. Install Redis manually, then re-run this:"
        echo "  brew install redis && brew services start redis"
        echo "(no Homebrew? install it first: https://brew.sh)"
        read -p "Press Enter to close..."
        exit 1
    fi
fi

# --- Claude CLI: llm_fallback.py uses it once Gemini keys are exhausted ---
if ! command -v claude >/dev/null 2>&1; then
    echo "Claude Code CLI not found."
    if command -v npm >/dev/null 2>&1; then
        echo "Installing it now (npm install -g @anthropic-ai/claude-code)..."
        npm install -g @anthropic-ai/claude-code || echo "Install failed -- continuing without Claude fallback this run."
    else
        echo "Install Node.js, then run: npm install -g @anthropic-ai/claude-code"
        echo "Continuing without Claude fallback for this run."
    fi
fi
if command -v claude >/dev/null 2>&1; then
    echo "(First time using Claude Code here? Run 'claude' once and log into your Pro/Max account before scraping.)"
fi

if [ ! -f .env ]; then
    echo "No .env file found -- DATABASE_URL and GEMINI_API_KEYS need to be set there."
    echo "Copy .env.example to .env and fill it in, then re-run this."
    read -p "Press Enter to close..."
    exit 1
fi

echo "Running grants scraper..."
.venv/bin/python -m Scraper_backend_grants.main

read -p "Done. Press Enter to close..."
