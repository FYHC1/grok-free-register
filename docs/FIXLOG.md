# 修复记录（FIXLOG）

> 按时间倒序记录已定位/已修复的问题。每条包含：症状、根因、修复、验证、状态。

---

## 2026-08-12 · 6. 代码 review 发现并修复 6 类问题（含优化计划清单）

- **状态**：✅ 已修复并验证；优化计划保留待实施
- **背景**：对全项目（入口脚本 / 注册模块 / CPA 模块 / xai_enroller 核心）并行代码 review，发现并修复以下问题：

### 已修复
| # | 文件 | 问题 | 修复 |
|---|------|------|------|
| 1 | `scripts/cpa_xai_device_enroll.py` `maybe_import_grok2api` | **镜像副本的 `--auth-dir+--limit` bug**——传入精确 paths 却被目录扫描覆盖，导入取错文件（与 sso 曾修的问题同源） | 改用 `--files` 传精确路径 |
| 2 | `grok_register/register.py` `_append_registration_line` | **无锁追加写** keys/*.jsonl / accounts.txt / grok.txt，多 worker 并发可致行交错/损坏（双批次事故底层根因） | 接入跨平台锁 `scripts/append_locked.py` |
| 3 | `scripts/sso_auth_code_enroll.py` `append_ledger` | 台账追加写无锁 | 接入锁 |
| 4 | `scripts/cpa_xai_device_enroll.py` `append_enroll_ledger` + `save_local_auth_json` | 台账 + oauth_credentials.jsonl + refresh_tokens.txt 追加写无锁 | 接入锁 |
| 5 | `scripts/single_instance.py` `_pid_alive` | tasklist 用 `text=True` 默认 locale 解码，中文 Windows（GBK）输出可能崩溃/误判 | 显式 `encoding="utf-8", errors="replace"` |
| 6 | `xai_enroller/auth_code.py` `sso_to_token` | `session = new_session()` 从不 close，批量授权时每个账号泄漏连接/TLS 上下文 | try/finally 确保 close |

- **新增** `scripts/append_locked.py`：跨平台追加锁（POSIX `fcntl.flock` / Windows `msvcrt.locking`），锁文件 `.{name}.lock`，带 fsync。
- **验证**：全部 7 文件 `py_compile` 通过；锁工具并发单测 4 进程 × 20 条 = 80 行完整无交错 PASS。

### 设计优化计划（保留待实施）
- **P0 架构统一**
  1. 锁体系统一：`single_instance.py`（Python wrapper）与 `start.sh` 的 `flock` 互不感知；`auth-service.sh`、`start-auth-cpa-device.ps1`、custom-email `*.bat` 全部补锁
  2. 台账/凭证集中化：三处 jsonl 追加写已加锁，但读-改-写仍非原子；建议封装 `LedgerStore` 或统一到 SQLite（`xai_enroller/ledger.py` 已用 SQLite）
- **P1 健壮性**
  3. OAuth 错误分类：`auth_code.py:541` 只查状态码范围，400/401（invalid_grant）与 5xx 处理相同
  4. Grok2API 重复账号治理：现存 129 条重复 email 记录
  5. 浏览器进程兜底清理：`executors.py` close 超时可能留孤儿 chromium
  6. httpx 重试：cpa 模块多处 httpx 调用无重试
- **P2 工程卫生**
  7. `.gitattributes` + autocrlf（本次推送已实施 .gitattributes，见仓库根目录）
  8. `.cmd` 加 `chcp 65001`（中文文件名 + 参数）
  9. 异常路径记台账：cpa 模块捕获 Exception 分支不写 ledger
  10. `setup.sh` / `ensure_runtime.sh` 锁加陈旧检测
- **P3 可选**
  11. `auth_code.py` 的 `_debug_dump_consent_html` 写 /tmp 可能含 SSO 片段，应脱敏或默认关闭
  12. SQLite `_connect` 无 `timeout=`，建议加 `timeout=30` + WAL

---

## 2026-08-11 · 4. grok2api 导入失败 `admin login failed: 502`（导入脚本代理泄漏）

- **状态**：✅ 已修复并验证
- **文件**：`scripts/import_authenticated_to_grok2api.py`
- **症状**：`授权.py --import-grok2api` 执行导入时报 `[grok2api] skipped: import_failed:admin login failed: 502`，Grok2API 中看不到凭证。
- **根因**：
  1. `授权.py` 的 grok2api 导入是**可选开关**（`--import-grok2api`，默认关闭），此前运行未带该参数 → 导入从未执行。这是"凭证没进 grok2api"的直接原因。
  2. 即使带参数，`import_authenticated_to_grok2api.py` 的 `admin_login`（`httpx.post`，约69行）和 `post_import`（`httpx.Client`，约202行）都未设 `trust_env=False`，与问题 2 同类：`.env` 的 `HTTP_PROXY/HTTPS_PROXY` 被 load_dotenv 注入后，httpx 静默走 Clash 代理访问本机/局域网 Grok2API admin → 502。
- **修复**：两处 httpx 调用加 `trust_env=False`；顺带修复 268 行 `log("...%s", auth_dir)` 传参错误（log 只收一个参数）。
- **验证**：运行 `scripts/import_authenticated_to_grok2api.py` → `admin login OK` → `import complete: created=73 updated=0 skipped=0 synced=73 syncFailed=0`，73 个本地 `xai-*.json` 全部导入 Grok2API。
- **使用方式**：`授权.py --import-grok2api`（不带 `--force` 时跳过重授，自动扫描 `auth-local/authenticated/` 下全部已有文件导入）。

---

## 2026-08-11 · 3. cpa_xai_device_enroll.py 代理劫持 CPA 请求（可选修复项，待确认）

- **状态**：⏳ 待处理（可选，用户暂缓）
- **文件**：`scripts/cpa_xai_device_enroll.py:317-320`
- **症状**：该脚本的 `CPAClient` 访问 CPA（本机/局域网服务）时，若环境配置了代理，请求被发往代理导致 502。
- **根因**：`proxy = proxy_for_httpx()` 读取 `HTTPS_PROXY/HTTP_PROXY/ALL_PROXY` 后，`httpx.Client(proxy=proxy, ...)` **显式**把代理传给 CPA 客户端。CPA 是私有地址，Clash 等代理对其返回 502（与问题 2 同类，但这里比问题 2 更直接：连 trust_env 都不依赖）。
- **修复方案**（未实施）：CPA 客户端不传 `proxy=`，并加 `trust_env=False` 强制直连。
- **备注**：与问题 2 的 `sso_auth_code_enroll.py` 修复保持一致；改前需确认该脚本没有依赖代理访问公网 CPA 的场景（README 示例 `XAI_ENROLLER_CPA_BASE_URL=https://你的CPA域名`，若 CPA 是公网域名且必须走代理，则需按域名区分，不能一刀切）。

---

## 2026-08-11 · 2. 授权.py cpa_upload 502：httpx 默认 trust_env 吞掉环境变量代理

- **状态**：✅ 已修复并验证
- **文件**：`scripts/sso_auth_code_enroll.py` `CPAClient.__init__`（约 320-333 行）
- **症状**：`授权.py` 授权成功（产出 `xai-*.json`），但上传 CPA 全部失败：
  `[FAIL] xxx@foxserver404.dpdns.org stage=cpa_upload_failed error=cpa_upload:upload xai-xxx.json failed: 502`
- **根因**：`load_dotenv` 把 `.env` 的 `HTTP_PROXY/HTTPS_PROXY=http://127.0.0.1:7890` 注入 `os.environ`；`httpx.Client()` 默认 `trust_env=True` 会**静默读取环境变量代理**。上传目标 `192.168.2.14`（局域网私有地址）被发往 Clash → Clash 返回 502。原注释「明确不设 proxy 即可」是误解：不传 `proxy=` 参数 ≠ 不走代理。
- **修复**：`httpx.Client(..., trust_env=False)`，并修正注释说明。
- **验证**：
  - 修复前：直连 401（服务器可达、接口存在）、走代理 502（复现用户日志）。
  - 修复后：网络层已通，服务器正常返回 JSON；剩余 401 为配置问题（见下）。
- **遗留问题（已解决）**：修复后一度出现 `{"error":"invalid management key"}`（本地 `.env` 与 CPA 服务器 management key 不一致）——已在服务器侧同步 key 后解决，后续 `授权.py --force` 上传全部成功。

---

## 2026-08-11 · 1. register.py `RuntimeError: Config fetch failed`：ACTION_ID 正则失配

- **状态**：✅ 已修复并验证
- **文件**：`grok_register/register.py:627`（`fetch_config()` 内）
- **症状**：运行 `注册.py` 报 `RuntimeError: Config fetch failed`。日志有 `[+] SITE_KEY`、`[+] STATE_TREE`，但没有 `[+] ACTION_ID`。
- **根因**：提取 Next.js chunk URL 的正则 `src="(/_next/static/[^"]+\.js)"` 要求 URL 以 `.js"` 结尾，但 xAI 的 Vercel 部署给所有 chunk 加了 `?dpl=...` 查询参数（实测 89 个 script 全部带参），导致 `js_urls` 为空 → ACTION_ID 提取循环不执行 → `if not all([SITE_KEY, ACTION_ID, STATE_TREE])` 抛错。ACTION_ID 用于注册表单提交的 `next-action` 请求头。
- **修复**：正则改为 `src="(/_next/static/[^"]+\.js[^"]*)"`（允许查询参数）。
- **验证**：修正后匹配 41 个 chunk；从 `2_ddy-5_nvmbg.js` 成功提取 ACTION_ID `7fb80c3e3a9e9084b6dfd13b67fce9804c2a4fb9c7`；`py_compile` 通过。
