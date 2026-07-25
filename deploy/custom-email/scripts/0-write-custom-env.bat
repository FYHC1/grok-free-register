@echo off
chcp 65001 >nul
echo === Write custom .env into WSL ===
wsl -d Ubuntu -u YOUR_USER -- bash -lc "cat > ~/grok-free-register/.env <<'EOF'
EMAIL_MODE=custom
EMAIL_DOMAIN=example.com
EMAIL_API=http://127.0.0.1:8080
TARGET=1
REGISTER_LOG_MODE=debug
PHYSICAL_CAP=1
S_WORKERS=1
P_WORKERS=1
C_WORKERS=1
EOF
echo --- .env ---
cat ~/grok-free-register/.env"
echo Done.
pause
