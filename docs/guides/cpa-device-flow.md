# CPA 驱动的 xAI Device Flow 授权（注册后）

网友目标链路：

```text
本地过盾
  → 密码/注册登录 CreateSession → SSO
  → device-flow 授权 (user_code=xxxx)   ← 由 CPA 发起
  → CPA 换 access/refresh token 并落盘 Auth
  → 本机同步 SUB/Auth JSON
  → 可选写入 Grok2API
```

## 为什么 device-flow 要用 CPA？

CLIProxyAPI（CPA）已内置 xAI Device OAuth：

| API | 方法 | 作用 |
|-----|------|------|
| `/v0/management/xai-auth-url` | GET | CPA 向 `auth.x.ai` 申请 `device_code/user_code`，返回授权 URL，并在后台轮询 token |
| `/v0/management/get-auth-status?state=...` | GET | 查询本次 OAuth：`wait` / `ok` / `error` |
| `/v0/management/auth-files` | GET | 列出已保存凭据 |
| `/v0/management/auth-files/download?name=...` | GET | 下载 `xai-*.json` |
| `/v0/management/auth-files` | POST | 本地已有 token 时上传（备用） |

源码（你本机 CPA 7-24）：

- 路由：`internal/api/server.go` → `mgmt.GET("/xai-auth-url", RequestXAIToken)`
- 实现：`internal/api/handlers/management/auth_files.go` → `RequestXAIToken`
- 协议：`internal/auth/xai/xai.go`（`ClientID=b1a00492-...`，与本地 enroller 相同）

**分工：**

1. **CPA**：持有 `device_code`、轮询 `/oauth2/token`、写入 CPA Auth 库  
2. **本机**：用注册得到的 SSO，在浏览器打开 `user_code` 页面点 Allow  
3. **本机**：CPA 成功后把新 `xai-*.json` 下载到 `auth-local/authenticated/`（SUB/CPA 兼容格式）  
4. **可选**：导入本地 Grok2API

> 注意：CPA 与本地 enroller 使用**同一个** public client_id。  
> 若 xAI 对 token 返回 `invalid_grant: Access denied`，CPA 手动 OAuth 也会失败——这不是“没走 CPA”的问题。

## 前置条件

1. 注册机已产出 SSO：
   - `keys/auth-sessions.jsonl` 或
   - `keys/accounts.txt`（`email:password:sso`）
2. 云端 CPA 可访问，且 management key 正确  
3. 需要代理时自己开 Clash（默认不必系统全局代理）  
4. 本机已装项目 venv + Playwright/CloakBrowser（与 `auth-service` 相同）

## 配置 `.env`

```env
XAI_ENROLLER_CPA_BASE_URL=https://你的CPA域名
XAI_ENROLLER_CPA_MANAGEMENT_SECRET=你的management_key

# 可选代理
# HTTP_PROXY=http://127.0.0.1:7897
# HTTPS_PROXY=http://127.0.0.1:7897

# 可选：成功后导入本地 Grok2API
# GROK2API_ADMIN_USER=admin
# GROK2API_ADMIN_PASS=...
```

## 一键试跑（先 1 个账号）

PowerShell：

```powershell
cd "YOUR_PROJECT_DIR"

# 若在 WSL 跑认证，把 source 指到 WSL 同步过来的 jsonl 也可以
wsl -d Ubuntu -u YOUR_USER -- bash -lc "cd ~/grok-free-register && .venv/bin/python scripts/cpa_xai_device_enroll.py --source-file keys/auth-sessions.jsonl --index 0 --count 1 --headed --json-out /tmp/cpa_enroll_once.json"
```

Windows venv（若本机已装依赖）：

```powershell
cd "YOUR_PROJECT_DIR"
.\.venv\Scripts\python.exe scripts\cpa_xai_device_enroll.py `
  --source-file keys\auth-sessions.jsonl `
  --index 0 --count 1 --headed `
  --json-out keys\cpa_enroll_once.json
```

成功标志：

- 日志出现 `[CPA] browser authorized; waiting CPA token save...`
- `get-auth-status` 最终 `status=ok`
- `auth-local/authenticated/xai-*.json` 有新文件
- CPA 管理页 Auth Files 增加一条 xAI

失败且含 `invalid_grant` / `Access denied`：

- **立刻停批**，不要继续烧号
- 说明 token 交换仍被 xAI 拒绝（与之前 Path B / CPA 手动一致）

## 批量 + 双写 Grok2API

```powershell
.\.venv\Scripts\python.exe scripts\cpa_xai_device_enroll.py `
  --source-file keys\auth-sessions.jsonl `
  --index 0 --count 10 `
  --import-grok2api
```

## 和旧路径对比

| 路径 | device_code 在哪 | token 轮询 | 落盘 |
|------|-----------------|------------|------|
| 旧 `auth-service` / Path B | 本机 | 本机 | 本机 `xai-*.json`，再手动上传 CPA |
| **新 CPA device-flow** | **CPA 服务器** | **CPA 服务器** | CPA 先有；本机再下载 + 可选 Grok2API |

浏览器批准 UI 仍在 `accounts.x.ai`；`auth.x.ai` 只做 discovery / device_code / token。

## 相关文件

- 脚本：`scripts/cpa_xai_device_enroll.py`
- 浏览器批准复用：`xai_enroller/executors.py`（PlaywrightExecutor）
- 本地 CPA 兼容格式：`xai_enroller/sinks.py` / `auth-local/authenticated/`
- 旧本地闭环（不经 CPA 发起）：更新包 `scripts/device_flow_browser_complete.py`
