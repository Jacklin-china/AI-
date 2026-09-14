@echo off
cd /d "%~dp0"
uv run python -m kantoku.shells.web_studio
if errorlevel 1 pause
