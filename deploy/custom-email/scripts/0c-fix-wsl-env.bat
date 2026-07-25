@echo off
chcp 65001 >nul
echo === Fix WSL .env EMAIL_API to Windows host ===
wsl -d Ubuntu -u YOUR_USER -- bash "/path/to/grok-free-register/deploy/custom-email/scripts/write-env.sh"
echo.
pause