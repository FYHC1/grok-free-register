# OAuth 授权（注册后 → CPA 可用文件）

## 路径说明

```text
keys/auth-sessions.jsonl   (注册产出 SSO)
        │
        ▼  scripts/sso_auth_code_enroll.py
  Authorization Code + PKCE (referrer=grok-build)
        │
        ├─ auth-local/authenticated/xai-*.json   ← CPA 兼容
        └─ POST CPA /v0/management/auth-files    ← --upload-cpa
```

**不要用 device-flow。** device-flow 容易 `invalid_grant`。

## 一键（推荐）

Windows:

```powershell
.\.venv\Scripts\python.exe -u .\授权.py
.\.venv\Scripts\python.exe -u .\授权.py 50
.\.venv\Scripts\python.exe -u .\授权.py --force
```

或直接调用核心脚本：

```powershell
.\.venv\Scripts\python.exe -u scripts\sso_auth_code_enroll.py `
  --source-file keys\auth-sessions.jsonl `
  --index 0 --count 0 --interval 3 `
  --upload-cpa --allow-no-proxy
```

Linux:

```bash
.venv/bin/python scripts/sso_auth_code_enroll.py \
  --source-file keys/auth-sessions.jsonl \
  --count 0 --interval 3 --upload-cpa --allow-no-proxy
```

## 常用参数

| 参数 | 含义 |
|------|------|
| `--index N` | 从第 N 个 SSO 开始 |
| `--count N` | 处理 N 个；`0`=全部剩余 |
| `--interval S` | 每个间隔秒 |
| `--upload-cpa` | 本地保存后上传 CPA |
| `--force-reauth` | 已有也重做 |
| `--retry-failed` | 重试台账失败号 |
| `--allow-no-proxy` | 无 HTTP 代理时也跑（会自动探测常见 Clash 端口） |
| `--email xx@yy` | 只处理一个邮箱 |

## 成功标志

- 日志：`[OK] ... referrer=grok-build path=.../xai-....json`
- 本地：`auth-local/authenticated/xai-*.json`
- CPA 管理页 Auth Files 出现同名
- 台账：`keys/cpa-enroll-ledger.jsonl` 记 `status=ok`（下次自动跳过）

## `.env` 需要

```env
XAI_ENROLLER_CPA_BASE_URL=https://your-cpa.example
XAI_ENROLLER_CPA_MANAGEMENT_SECRET=your_management_key
XAI_ENROLLER_ALLOWED_EMAIL_DOMAIN=mail1.example.com,mail2.example.com
```

## 注意

- 注册成功后再跑授权
- 连续 `access_denied` 先停，检查域名/代理/账号状态
- 已成功账号会写入台账，默认自动跳过
