@echo off
cd /d "%~dp0"
".venv\Scripts\python.exe" -u "%~dp0授权.py" %*
