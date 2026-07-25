@echo off
chcp 65001 >nul
echo Writing .env so WSL register polls Windows-host email server...
echo Domain: mail.example.com
wsl -d Ubuntu -u YOUR_USER -- bash -lc "HOST=$(grep -m1 nameserver /etc/resolv.conf | awk '{print $2}'); echo WindowsHost=$HOST; cat > ~/grok-free-register/.env <<EOF
EMAIL_MODE=custom
EMAIL_DOMAIN=mail.example.com
EMAIL_API=http://$HOST:8080
TARGET=1
REGISTER_LOG_MODE=debug
PHYSICAL_CAP=1
S_WORKERS=1
P_WORKERS=1
C_WORKERS=1
P_BATCH_MAX=1
Q_PENDING_CAP=2
EOF
cat ~/grok-free-register/.env
echo --- probe ---
curl -sS --connect-timeout 5 http://$HOST:8080/health || echo PROBE_FAIL"
pause
