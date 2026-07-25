# 自建邮箱 + Cloudflare Tunnel（域名 example.com）

## 架构
邮件 → CF Email Routing → Worker
  → https://hook.example.com/webhook
  → cloudflared 隧道
  → 本机 127.0.0.1:8080（WSL email_server）
  → 注册机取验证码

## 一次性配置（按顺序做）

### 1) Cloudflare 开启 Email Routing
浏览器：Cloudflare → example.com → Email → Email Routing → Enable  
按提示加 MX/TXT。

### 2) 登录并创建隧道

可双击：`scripts\\setup-tunnel.bat`  
或 PowerShell：
```powershell
& "cloudflared" tunnel login
& "cloudflared" tunnel create grok-mail-hook
& "cloudflared" tunnel route dns grok-mail-hook hook.example.com
```
- `login` 会打开浏览器，选中托管 example.com 的账号并授权
- `create` 会打印 **Tunnel ID**，并生成  
  `C:\Users\YOUR_USER\.cloudflared\<TunnelID>.json`
- 编辑本目录 `cloudflared\config.yml`：
  - 把两处 `TUNNEL_ID` 都换成真实 ID

### 3) 部署 Email Worker（PowerShell）
```powershell
cd "YOUR_PROJECT_DIR\deploy\custom-email\worker"
cmd /c "npm install"
cmd /c "npx wrangler login"
cmd /c "npx wrangler secret put WEBHOOK_URL"
# 粘贴下面这一行后回车：
# https://hook.example.com/webhook
cmd /c "npx wrangler deploy"
```

### 4) Catch-all 指向 Worker
Cloudflare → Email → Email Routing → Routing rules  
→ Catch-all → Action: **Send to a Worker** → 选 `mailfree` → Save

### 5) 写入本机 custom 配置
双击：
`deploy\custom-email\scripts\0-write-custom-env.bat`

## 每次运行（开 3 个窗口，顺序固定）
1. `scripts\1-start-email-service.bat`  （收信）
2. `scripts\2-start-tunnel.bat`         （隧道）
3. `scripts\3-start-register.bat`       （注册）

## 自检
```powershell
# 收信服务
wsl -d Ubuntu -u YOUR_USER -- bash -lc "curl -sS http://127.0.0.1:8080/health"

# 隧道 + 公网域名（隧道跑起来后）
curl.exe -sS https://hook.example.com/health

# 模拟一封验证码邮件
curl.exe -sS -X POST https://hook.example.com/webhook -H "content-type: application/json" -d "{\"to\":\"test@example.com\",\"from\":\"a@b.com\",\"subject\":\"code\",\"text\":\"ABC-DEF\",\"html\":\"\"}"
```
收信窗口应出现：`[+] test@example.com code=ABCDEF`

## 成功信号
- 收信窗口：`[+] xxx@example.com code=...`
- 注册 debug：`q_sent>0` → `Q>0` → `[→] 开始注册 #1`



