@echo off
chcp 65001 >nul
set CF=cloudflared
echo [1/3] login (browser will open)...
"%CF%" tunnel login
echo.
echo [2/3] create tunnel grok-mail-hook...
"%CF%" tunnel create grok-mail-hook
echo.
echo [3/3] route DNS hook.example.com...
"%CF%" tunnel route dns grok-mail-hook hook.example.com
echo.
echo Done. Now:
echo  1) Open C:\Users\YOUR_USER\.cloudflared\ and copy the long Tunnel ID from the .json filename
echo  2) Edit deploy\custom-email\cloudflared\config.yml and replace TUNNEL_ID (2 places)
echo  3) Continue Worker deploy steps in README
pause

