@echo off
cd /d "%~dp0"
uv run python -m kantoku serve
if errorlevel 1 pause
