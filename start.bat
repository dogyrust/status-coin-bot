@echo off
REM First run sets up a virtual environment and installs dependencies.
cd /d "%~dp0"

if not exist venv (
    echo Creating virtual environment...
    py -m venv venv
)

call venv\Scripts\activate.bat
echo Installing/updating dependencies...
python -m pip install --upgrade pip >nul
pip install -r requirements.txt

if not exist .env (
    echo.
    echo [!] No .env file found. Copy .env.example to .env and add your DISCORD_TOKEN.
    copy .env.example .env >nul
    echo Created .env for you - open it and paste your token, then run start.bat again.
    pause
    exit /b
)

echo Starting bot...
python bot.py
pause
