# cfworker 邮箱（cloudflare_temp_email）

无需本机 email_server / cloudflared 隧道。适用于 `EMAIL_MODE=cfworker`。

## 配置（`.env`）

```env
EMAIL_MODE=cfworker
CFWORKER_API_URL=https://your-worker.workers.dev
CFWORKER_ADMIN_TOKEN=your_admin_password
CFWORKER_DOMAIN=mail1.example.com
CFWORKER_DOMAINS=mail1.example.com,mail2.example.com
CFWORKER_DOMAIN_MODE=rotate
CFWORKER_ENABLE_PREFIX=1
EMAIL_DOMAIN=mail1.example.com
XAI_ENROLLER_ALLOWED_EMAIL_DOMAIN=mail1.example.com,mail2.example.com
```

请使用你自己部署的 [cloudflare_temp_email](https://github.com/dreamhunter2333/cloudflare_temp_email) Worker 与域名。

## 运行

Windows:

```powershell
.\.venv\Scripts\python.exe -u .\注册.py 1 10
```

Linux:

```bash
bash start.sh --target 10 --debug
```
