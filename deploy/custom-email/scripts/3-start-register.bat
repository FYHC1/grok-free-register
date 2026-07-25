@echo off
chcp 65001 >nul
echo === Register (debug) ===
wsl -d Ubuntu -u YOUR_USER -- bash -lc "cd ~/grok-free-register && bash start.sh --debug"
