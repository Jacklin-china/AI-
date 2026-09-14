@echo off
cd /d "%~dp0"
uv run python -m kantoku.shells.studio
if errorlevel 1 pause
