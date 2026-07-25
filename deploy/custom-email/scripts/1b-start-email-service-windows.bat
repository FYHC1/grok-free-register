@echo off
chcp 65001 >nul
cd /d "D:\????\???\grok-free-register-main"
echo === Email receiver on WINDOWS host :8080 ===
echo Domain: mail.example.com
echo This is required so Cloudflare Tunnel (Windows) can reach webhook.
set EMAIL_DOMAIN=mail.example.com
set PYTHONUTF8=1
where python >nul 2>nul
if errorlevel 1 (
  echo Python not found in PATH. Install Python or use full path.
  pause
  exit /b 1
)
python -m pip install -q python-dotenv 2>nul
python -m grok_register.email_server --domain mail.example.com --port 8080
pause
