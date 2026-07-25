# grok-free-register

Windows / Linux 可用的 Grok 免费账号**注册**与 **OAuth 授权**工具。

本仓库是基于原项目 [hechuyi/grok-free-register](https://github.com/hechuyi/grok-free-register) 的**二开修改版**，在原有 CSP 注册架构上补齐了 Windows 一键注册、Authorization Code 授权、CPA 兼容产物与台账跳过等能力。

> 本仓库**不包含**任何个人密钥、域名、邮箱、服务器地址或私有教程。请只把私密信息放在本地 `.env`（已 gitignore）。

## 开源协议

本项目以 **MIT License** 开源，详见根目录 [`LICENSE`](LICENSE)。

- 可自由使用、修改、分发
- 需保留版权与许可声明
- **按“原样”提供，作者不对使用后果承担责任**（见下方免责声明）

上游原项目： [hechuyi/grok-free-register](https://github.com/hechuyi/grok-free-register)

## 免责声明

1. 本工具仅供**学习、研究与个人自用**自动化技术交流，请遵守 xAI / Grok 服务条款与当地法律法规。
2. 使用本项目产生的账号、Token、请求行为与后果由使用者自行承担；作者与贡献者不承担任何直接或间接责任。
3. 请勿用于批量滥用、绕过风控牟利、侵犯他人权益或任何违法用途。
4. 上游接口、风控策略、OAuth 流程随时可能变更，本仓库**不保证**长期可用或与第三方网关兼容。
5. 仓库文档中的示例 URL、密钥均为占位符；请勿把真实 `.env`、`keys/`、`auth-local/` 推送到公开仓库。

## 与原版对比（本二开改了什么）

| 方面 | 原版 [hechuyi/grok-free-register](https://github.com/hechuyi/grok-free-register) | 本二开 |
|------|----------------------------------|--------|
| 注册入口 | Linux/`start.sh` + CSP 多 worker 为主 | 保留原能力，并增加 Windows **UI Sync（Camoufox）** 一键 `注册.py` |
| OAuth | 偏 Device Flow / 认证服务导出 | 默认 **Authorization Code + PKCE**（`referrer=grok-build`），避免常见 `invalid_grant` |
| 产物 | SSO / 认证服务 claimed 文件 | 直接生成 **CPA 兼容** `auth-local/authenticated/xai-*.json`，可选上传 CLIProxyAPI |
| 日常命令 | 环境变量 + shell 参数较长 | Windows：`注册.py [并发] [数量]`、`授权.py [数量]`；已授权台账自动跳过 |
| 邮箱 | tempmail / 自建等 | 增加 **cfworker**（`cloudflare_temp_email` Admin API）等配置示例 |
| 文档 | 上游文档 | 公开文档脱敏；个人本地教程默认 **不入库** |

> 说明：二开目标是“注册 → 授权 → 可导入 CPA/同类网关”的闭环；原版 CSP 注册链路仍保留，可按环境选用。

## 项目优势

- **注册 + 授权一体**：从 SSO 会话到 OAuth `access_token` / `refresh_token` 一条龙
- **Windows 友好**：中文/英文入口脚本，参数短，适合日常批量
- **Auth Code 路径**：对齐 Grok Build OAuth，比 Device Flow 更稳
- **CPA 兼容 JSON**：本地 `xai-*.json` 可导入 CLIProxyAPI 管理端
- **台账跳过**：`keys/cpa-enroll-ledger.jsonl` 记住成功项，重复跑不会反复烧号
- **密钥默认不入库**：`.env` / `keys/` / `auth-local/` 已在 `.gitignore`

## 限制与已知问题

- 依赖第三方页面与风控（Turnstile / Castle 等），成功率随 IP、指纹、邮箱域名变化
- 部分邮箱域名会被判定为临时邮，OAuth 出现 `access_denied`（需换可信域名）
- Device Flow 路径仍保留作兼容/实验，**默认不推荐**（易 `invalid_grant`）
- 需要可用代理或稳定出口时，请在本机 Clash 等自行管理节点；仓库不捆绑代理配置
- Camoufox / Playwright 浏览器体积较大，首次运行需下载
- 不保证与所有第三方网关版本永久兼容；接口变更时需自行适配

## 快速开始

```bash
git clone <your-fork-or-repo-url>
cd grok-free-register
python -m venv .venv

# Windows
.\.venv\Scripts\pip install -r requirements.txt
copy .env.example .env

# Linux / macOS
# .venv/bin/pip install -r requirements.txt
# cp .env.example .env
```

编辑 `.env`（示例均为占位符）：

```env
EMAIL_MODE=tempmail
# EMAIL_MODE=cfworker
# CFWORKER_API_URL=https://your-worker.workers.dev
# CFWORKER_ADMIN_TOKEN=your_admin_password
# CFWORKER_DOMAINS=mail1.example.com,mail2.example.com
# XAI_ENROLLER_ALLOWED_EMAIL_DOMAIN=mail1.example.com,mail2.example.com
# XAI_ENROLLER_CPA_BASE_URL=https://your-cpa.example
# XAI_ENROLLER_CPA_MANAGEMENT_SECRET=your_management_key
```

### Windows 日常命令

```powershell
# 注册：并发 数量（数量 0 = 无限，Ctrl+C 停止）
.\.venv\Scripts\python.exe -u .\注册.py 2 10
.\.venv\Scripts\python.exe -u .\注册.py 0 0

# 授权：已成功自动跳过台账；0 = 全部剩余
.\.venv\Scripts\python.exe -u .\授权.py
.\.venv\Scripts\python.exe -u .\授权.py 50
.\.venv\Scripts\python.exe -u .\授权.py --force
```

英文入口：`register.py` / `auth.py`（或 `register.cmd` / `auth.cmd`）。

### Linux

```bash
bash start.sh --target 10
.venv/bin/python scripts/sso_auth_code_enroll.py \
  --source-file keys/auth-sessions.jsonl \
  --count 0 --upload-cpa --allow-no-proxy
```

## 产物（默认不入库）

| 路径 | 说明 |
|------|------|
| `keys/auth-sessions.jsonl` | 注册产出的 SSO 会话 |
| `auth-local/authenticated/xai-*.json` | OAuth 授权文件（CPA 兼容） |
| `keys/cpa-enroll-ledger.jsonl` | 授权台账（跳过已成功） |

## 文档

- [注册](docs/guides/registration.md)
- [OAuth 授权码流程](docs/guides/oauth-auth-code.md)
- [认证服务](docs/guides/auth-service.md)
- [排障](docs/guides/runtime-troubleshooting.md)
- [cfworker 邮箱](docs/cfworker-email.md)
- [架构](docs/architecture.md)

## 常见问题

**Q: 注册成功但授权 `access_denied`？**  
A: 多为邮箱域名信誉问题。换更可信的域名或邮箱方案后再注册授权；连续失败请先停，避免空烧。

**Q: 授权 `invalid_grant`？**  
A: 确认走的是 **Auth Code + PKCE（grok-build）**，不要用 Device Flow 批量烧号。检查账号是否仍可登录、代理是否稳定。

**Q: 已经授权过的号会重复做吗？**  
A: 默认不会。台账 `keys/cpa-enroll-ledger.jsonl` 与本地/CPA 已有文件会跳过；强制重做用 `授权.py --force`。

**Q: 如何只授权某一个邮箱？**  
A: 使用核心脚本 `--email you@example.com`，或先筛到单独 jsonl 再跑 `授权.py`。

**Q: 可以不上传 CPA，只留本地文件吗？**  
A: 可以。本地始终写入 `auth-local/authenticated/`；上传需配置 CPA 并带 `--upload-cpa`（`授权.py` 默认会按脚本逻辑处理，以当前脚本参数为准）。

**Q: 要不要提交 `.env` 和 keys？**  
A: **不要**。推送前自查：无真实 `sk-`、Worker 域名、CPA URL、内网 IP、邮箱、教程里的私人路径。

**Q: 和原版仓库关系？**  
A: 本仓库基于 [hechuyi/grok-free-register](https://github.com/hechuyi/grok-free-register) 修改；问题排查时可对照上游更新，但本二开的授权默认路径与 Windows 入口以本仓库文档为准。

## 安全说明（推送前自检）

请确认以下内容**不会**出现在 git 提交中：

- `.env`、真实 management key / admin token
- `keys/`、`auth-local/`、日志与 SSO/OAuth JSON
- 个人域名、Worker 地址、CPA 实例 URL、内网 IP
- 本地私人教程（本仓库默认 gitignore：`完整使用教程.md`、`本地部署使用说明.md`、`一页速查.md` 等）

## 致谢

- 原项目：[hechuyi/grok-free-register](https://github.com/hechuyi/grok-free-register)
- 依赖生态：Camoufox、Playwright、httpx、curl_cffi 等开源组件

## 贡献

欢迎 Issue / PR：修 bug、补文档、适配接口变更。请勿在 PR 中提交任何真实密钥或账号数据。
