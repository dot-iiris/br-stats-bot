@echo off
cd /d "%~dp0"
if not exist .env (
  copy .env.example .env >nul
  echo Created .env - fill in DISCORD_TOKEN and BOT_API_KEY, then run this again.
  notepad .env
  pause
  exit /b
)
if not exist .venv\Scripts\python.exe (
  echo Setting up Python environment...
  py -3 -m venv .venv 2>nul || python -m venv .venv
)
.venv\Scripts\python.exe -m pip install --disable-pip-version-check -q -r requirements.txt
title BR Stats bot
.venv\Scripts\python.exe bot.py
pause
