@echo off
chcp 65001 >nul
echo === Cloudflare Tunnel hook.example.com -^> 127.0.0.1:8080 ===
"cloudflared" tunnel --config "%~dp0..\cloudflared\config.yml" run
