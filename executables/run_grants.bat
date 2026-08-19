@echo off
cd /d "%~dp0\.."

where python >nul 2>nul
if %errorlevel%==0 (
    set PYLAUNCHER=python
) else (
    where py >nul 2>nul
    if %errorlevel%==0 (
        set PYLAUNCHER=py
    ) else (
        echo Python not found. Install it from https://python.org/downloads and check "Add python.exe to PATH".
        pause
        exit /b 1
    )
)

if not exist .venv (
    %PYLAUNCHER% -m venv .venv
)

echo Installing dependencies (first run only takes a while)...
.venv\Scripts\pip install -q -r requirements.txt
.venv\Scripts\python -m playwright install chromium

REM --- Redis: main.py expects one on localhost:6379. No native Windows
REM     package, so this only checks -- it doesn't try to auto-install one.
.venv\Scripts\python -c "import redis; redis.Redis(host='localhost', port=6379).ping()" >nul 2>nul
if not %errorlevel%==0 (
    echo Redis isn't reachable on localhost:6379.
    echo Windows has no native Redis package. Easiest options:
    echo   1. WSL:      wsl --install   then inside WSL: sudo apt install redis-server ^&^& redis-server
    echo   2. Memurai (Redis-compatible for Windows^): https://www.memurai.com
    echo Start one of those, then re-run this.
    pause
    exit /b 1
)

REM --- Claude CLI: llm_fallback.py uses it once Gemini keys are exhausted ---
where claude >nul 2>nul
if not %errorlevel%==0 (
    echo Claude Code CLI not found.
    where npm >nul 2>nul
    if %errorlevel%==0 (
        echo Installing it now...
        npm install -g @anthropic-ai/claude-code
    ) else (
        echo Install Node.js, then run: npm install -g @anthropic-ai/claude-code
        echo Continuing without Claude fallback for this run.
    )
)
where claude >nul 2>nul
if %errorlevel%==0 (
    echo First time using Claude Code here? Run 'claude' once and log into your Pro/Max account before scraping.
)

if not exist .env (
    echo No .env file found -- DATABASE_URL and GEMINI_API_KEYS need to be set there.
    echo Copy .env.example to .env and fill it in, then re-run this.
    pause
    exit /b 1
)

echo Running grants scraper...
.venv\Scripts\python -m Scraper_backend_grants.main

pause
