@echo off
chcp 65001 >nul
echo === Email receiver (8080) ===
wsl -d Ubuntu -u YOUR_USER -- bash -lc "cd ~/grok-free-register && bash start.sh --email-service"
