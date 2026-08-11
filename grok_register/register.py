"""
Grok Free Register — CSP 异步并发架构
=============================================
单进程 asyncio + 单共享 CloakBrowser + Semaphore 背压:
  - S_Worker: 生成 Turnstile token (T)
  - P_Worker: 创建邮箱 + 发送验证码 + 轮询验证码 (Q)
  - C_Worker: claim pair 并执行注册
  - Semaphore 背压控制容量,无需中心调度器

三种邮箱模式(EMAIL_MODE):
  - tempmail (默认,零配置): 免费临时邮箱,多 provider 自动 fallback
  - custom: 自建域名邮箱,Cloudflare Email Routing → Worker → 本地 webhook
            (见 grok_register/email_server.py / cloudflare/email-worker.js)
  - cfworker: dreamhunter2333/cloudflare_temp_email 自建 API
            (cloudflare_temp_email: POST /admin/new_address + GET /admin/mails)

配置全部走环境变量 / .env(见 .env.example);CLI: --max-mem 6G --target 100
用法:
  bash start.sh          # 一键引导
"""
import os, json, random, string, time, re, secrets, base64, struct, asyncio, glob, sys, multiprocessing
from pathlib import Path
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass
import requests as req
from urllib.parse import quote
from concurrent.futures import ThreadPoolExecutor

# CSP 架构组件
from grok_register.core.admission import AdmissionGate
from grok_register.core.envelope import ResourceEnvelope
from grok_register.core.inventory import Inventory
from grok_register.core.observer import Metrics
from grok_register.ui_register import register_via_ui
from grok_register.ui_sync_register import register_via_ui_sync
from grok_register.browser_backend import (
    browser_engine as backend_browser_engine,
    browser_headless as backend_browser_headless,
    browser_proxy_server as backend_browser_proxy_server,
    context_kwargs as backend_context_kwargs,
    find_cloakbrowser_chrome,
    launch_browser_bundle,
)

os.makedirs("keys", exist_ok=True)
SITE_URL = "https://accounts.x.ai"

# ── 配置（环境变量 / .env，见 .env.example）──
def _env_int(key, default):
    try:
        return int(str(os.environ.get(key, "")).strip() or default)
    except ValueError:
        return default

def _env_int_or_none(key):
    raw = str(os.environ.get(key, "")).strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None

EMAIL_MODE      = (os.environ.get("EMAIL_MODE") or "tempmail").strip().lower()   # tempmail | custom | cfworker
if EMAIL_MODE == "mailtm":      # 兼容旧名
    EMAIL_MODE = "tempmail"
if EMAIL_MODE in ("cloudflare", "cloudflare_temp_email", "cf-worker", "cf_worker", "cf_temp_mail"):
    EMAIL_MODE = "cfworker"
LOCAL_EMAIL_API = (os.environ.get("EMAIL_API") or "http://127.0.0.1:8080").strip()
EMAIL_DOMAIN    = (os.environ.get("EMAIL_DOMAIN") or "").strip()

# cfworker / cloudflare_temp_email (cloudflare_temp_email Admin API)
CFWORKER_API_URL = (
    os.environ.get("CFWORKER_API_URL")
    or os.environ.get("CF_TEMP_MAIL_BASE")
    or os.environ.get("CLOUDFLARE_API_BASE")
    or ""
).strip().rstrip("/")
CFWORKER_ADMIN_TOKEN = (
    os.environ.get("CFWORKER_ADMIN_TOKEN")
    or os.environ.get("CF_TEMP_MAIL_ADMIN")
    or os.environ.get("CLOUDFLARE_API_KEY")
    or ""
).strip()
CFWORKER_CUSTOM_AUTH = (
    os.environ.get("CFWORKER_CUSTOM_AUTH")
    or os.environ.get("CF_TEMP_MAIL_CUSTOM_AUTH")
    or ""
).strip()
CFWORKER_DOMAIN = (
    os.environ.get("CFWORKER_DOMAIN")
    or os.environ.get("CF_TEMP_MAIL_DOMAIN")
    or EMAIL_DOMAIN
    or ""
).strip().lstrip("@").lower()
_CF_DOMAINS_RAW = (
    os.environ.get("CFWORKER_DOMAINS")
    or os.environ.get("CF_TEMP_MAIL_DOMAINS")
    or os.environ.get("CFWORKER_ENABLED_DOMAINS")
    or ""
).strip()
CFWORKER_DOMAIN_MODE = (os.environ.get("CFWORKER_DOMAIN_MODE") or "rotate").strip().lower() or "rotate"
if CFWORKER_DOMAIN_MODE not in ("fixed", "random", "rotate"):
    CFWORKER_DOMAIN_MODE = "rotate"
CFWORKER_ENABLE_PREFIX = (os.environ.get("CFWORKER_ENABLE_PREFIX") or "1").strip().lower() in ("1", "true", "yes", "on")
CFWORKER_ROTATE_STATE = (os.environ.get("CFWORKER_ROTATE_STATE") or "keys/cfworker_domain_rotate.json").strip()

def _parse_domain_list(raw):
    if not raw:
        return []
    text_raw = str(raw).strip()
    if not text_raw:
        return []
    try:
        parsed = json.loads(text_raw)
        if isinstance(parsed, list):
            items = parsed
        else:
            items = [parsed]
    except Exception:
        items = [x.strip() for x in text_raw.replace(";", ",").split(",")]
    out = []
    for item in items:
        d = str(item or "").strip().lstrip("@").lower()
        if d and d not in out:
            out.append(d)
    return out

CFWORKER_DOMAINS = _parse_domain_list(_CF_DOMAINS_RAW)
if not CFWORKER_DOMAINS and CFWORKER_DOMAIN:
    CFWORKER_DOMAINS = [CFWORKER_DOMAIN]
MIN_FREE_MEM_MB = _env_int("MIN_FREE_MEM_MB", 500)   # 自动容量派生时保留的内存(MB)
T_TARGET        = _env_int("T_TARGET", 4)            # token 池缓冲目标
Q_TARGET        = _env_int("Q_TARGET", 4)            # 就绪验证码缓冲目标
TARGET          = _env_int("TARGET", 0)              # 攒够 N 个号自动停(0=不限;--target N 可覆盖)

# CSP 容量参数
PHYSICAL_CAP    = _env_int("PHYSICAL_CAP", 0)        # 本地物理资源许可,0=自动派生
PHYSICAL_PER_CPU = _env_int("PHYSICAL_PER_CPU", 2)   # 自动派生 CPU 侧保守上限;压测可临时覆盖
PHYSICAL_MEM_MB = _env_int("PHYSICAL_MEM_MB", 512)   # 每个物理许可的保守内存预算(MB)
CAPACITY_PROFILE = (os.environ.get("CAPACITY_PROFILE") or "").strip()
T_SLOT_CAP      = _env_int("T_SLOT_CAP", 8)          # token 库存缓冲
Q_SLOT_CAP      = _env_int("Q_SLOT_CAP", 8)          # 验证码库存缓冲
Q_PENDING_CAP   = _env_int("Q_PENDING_CAP", 12)       # 外部在途 Q 请求上限
# Registration path: ui = multi-step form (page-native Castle); server_action = old Next.js action; auto = ui then fallback
REGISTER_MODE   = (os.environ.get("REGISTER_MODE") or "ui").strip().lower()
# Windows default: Sync Camoufox for UI signup (UI-compatible CF path).
# Set UI_SYNC_CAMOUFOX=0 to force legacy async page path.
_UI_SYNC_DEFAULT = "1" if sys.platform.startswith("win") else "0"
_UI_SYNC_RAW = (os.environ.get("UI_SYNC_CAMOUFOX") or _UI_SYNC_DEFAULT).strip().lower()
UI_SYNC_CAMOUFOX = _UI_SYNC_RAW in ("1", "true", "yes", "on", "sync")
if REGISTER_MODE in ("form", "browser", "playwright"):
    REGISTER_MODE = "ui"
if REGISTER_MODE in ("api", "server", "sa"):
    REGISTER_MODE = "server_action"

T_MAX_AGE       = _env_int("T_MAX_AGE", 300)          # token 最大年龄(秒)
Q_MAX_AGE       = _env_int("Q_MAX_AGE", 120)          # 验证码最大年龄(秒)
P_REQUEST_TIMEOUT = _env_int("P_REQUEST_TIMEOUT", 95) # P 等待 Q 返回超时(秒)
C_CONSUME_TIMEOUT = _env_int(
    "C_CONSUME_TIMEOUT",
    180 if REGISTER_MODE in ("ui", "auto") else 60,
) # C 消费完整 pair 超时(秒)
S_WORKERS       = _env_int("S_WORKERS", 0)            # 0=自动
P_WORKERS       = _env_int("P_WORKERS", 0)
C_WORKERS       = _env_int("C_WORKERS", 0)
C_HOT_PAGE_POOL = (os.environ.get("C_HOT_PAGE_POOL", "0").strip().lower() in ("1", "true", "yes"))
C_HOT_PAGE_POOL_SIZE = _env_int("C_HOT_PAGE_POOL_SIZE", 0)
C_SET_COOKIE_VIA_REQUEST = (
    os.environ.get("C_SET_COOKIE_VIA_REQUEST", "1" if C_HOT_PAGE_POOL else "0")
    .strip()
    .lower()
    in ("1", "true", "yes")
)

# CSP v2 局部门控/批量发送参数。水位默认在启动期结合 Physical_Sem 派生。
_T_HIGH_WATER_OVERRIDE = _env_int_or_none("T_HIGH_WATER")
_T_LOW_WATER_OVERRIDE  = _env_int_or_none("T_LOW_WATER")
_Q_HIGH_WATER_OVERRIDE = _env_int_or_none("Q_HIGH_WATER")
_Q_LOW_WATER_OVERRIDE  = _env_int_or_none("Q_LOW_WATER")
P_BATCH_MAX     = max(1, _env_int("P_BATCH_MAX", 4))
P_SEND_CAP      = _env_int("P_SEND_CAP", 0)           # >0=显式限制并发 P 发送页面;0=不额外建模
PAGE_GOTO_WAIT_UNTIL = os.environ.get("PAGE_GOTO_WAIT_UNTIL", "domcontentloaded").strip() or "domcontentloaded"
PAGE_POST_WAIT_MS = _env_int("PAGE_POST_WAIT_MS", 500)
PAGE_BLOCK_STATIC_ASSETS = (
    os.environ.get("PAGE_BLOCK_STATIC_ASSETS", "0").strip().lower()
    in ("1", "true", "yes")
)
REGISTRATION_DIAGNOSTICS = (
    os.environ.get("REGISTRATION_DIAGNOSTICS", "0").strip().lower()
    in ("1", "true", "yes")
)
REGISTRATION_RATE_LIMIT_COOLDOWN = max(
    60, _env_int("REGISTRATION_RATE_LIMIT_COOLDOWN", 60)
)
REGISTRATION_RATE_LIMIT_RECOVERY_SECONDS = max(
    1, _env_int("REGISTRATION_RATE_LIMIT_RECOVERY_SECONDS", 60)
)
REGISTRATION_RATE_LIMIT_RECOVERY_INTERVAL = max(
    1, _env_int("REGISTRATION_RATE_LIMIT_RECOVERY_INTERVAL", 3)
)
REGISTER_LOG_MODE = "user"

SITE_KEY = None
ACTION_ID = None
STATE_TREE = None

start_time = time.time()
success_count = 0
file_lock = asyncio.Lock()
STOP = asyncio.Event()

# 角色标识 + 轮询/HTTP 专用线程池（与 CPU 密集的浏览器操作解耦）
SOLVE, PRODUCE, CONSUME, IDLE = 'SOLVE', 'PRODUCE', 'CONSUME', 'IDLE'
POLL_EXECUTOR = ThreadPoolExecutor(max_workers=32)

def resolve_register_log_mode(argv=None, env=None):
    argv = list([] if argv is None else argv)
    env = dict(os.environ if env is None else env)
    mode = (env.get("REGISTER_LOG_MODE") or "user").strip().lower()
    if "--debug" in argv:
        mode = "debug"
    if mode not in {"user", "debug"}:
        raise ValueError("REGISTER_LOG_MODE must be user or debug")
    return mode


def _terminal_output(msg):
    print(msg, flush=True)


def log(msg):
    try:
        _terminal_output(msg)
    except Exception:
        return


def debug_log(msg):
    if REGISTER_LOG_MODE == "debug":
        log(msg)
def rand_str(n=15): return ''.join(random.choice(string.ascii_lowercase + string.digits) for _ in range(n))


def sanitize_terminal_error(error):
    return type(error).__name__


def format_user_registration_event(
    kind,
    *,
    task_id=None,
    count=None,
    rate_per_minute=None,
    wait_seconds=None,
    remaining=None,
):
    if kind == "service_started":
        progress = f"剩余 {remaining}" if remaining is not None else "持续运行"
        return f"[✓] 注册服务已启动 | {progress}"
    if kind == "started":
        suffix = f" | 剩余 {remaining}" if remaining is not None else ""
        return f"[→] 开始注册 #{task_id}{suffix}"
    if kind == "success":
        rate = "—" if rate_per_minute is None else f"{rate_per_minute:.1f}/分"
        return f"[✓] 注册成功 #{task_id} | 运行平均 {rate} | 累计 {count}"
    if kind == "failed":
        return f"[✗] 注册失败 #{task_id} | 已跳过，继续下一任务"
    if kind == "rate_limited":
        return f"[⏸] 触发限流 | {wait_seconds}秒后恢复探测"
    if kind == "recovered":
        return f"[▶] 限流解除 | 实际等待 {wait_seconds}秒"
    if kind == "stopped":
        return f"[■] 注册服务已停止 | 累计 {count or 0}"
    raise ValueError(f"unknown user registration event: {kind}")


class RegistrationRateLimited(RuntimeError):
    """注册提交被目标站点的限流页替代。"""


class RegistrationRateLimitCircuit:
    """在检测到注册限流后暂停新的 C 阶段提交。"""

    def __init__(
        self,
        cooldown_seconds,
        recovery_seconds=60,
        recovery_interval=3,
        clock=time.monotonic,
    ):
        self.cooldown_seconds = cooldown_seconds
        self.recovery_seconds = recovery_seconds
        self.recovery_interval = recovery_interval
        self._clock = clock
        self._blocked_until = 0.0
        self._tripped_at = None
        self._probe_active = False
        self._probe_token = None
        self._recovering_until = 0.0
        self._next_recovery_submit = 0.0

    def remaining_seconds(self):
        return max(0, int(self._blocked_until - self._clock() + 0.999))

    def is_open(self):
        return self.remaining_seconds() > 0

    def trip(self):
        starts_new_window = not self.is_open()
        if self._tripped_at is None:
            self._tripped_at = self._clock()
        self._recovering_until = 0.0
        self._next_recovery_submit = 0.0
        self._probe_active = False
        self._probe_token = None
        self._blocked_until = max(
            self._blocked_until,
            self._clock() + self.cooldown_seconds,
        )
        return starts_new_window

    async def wait(self):
        while True:
            if self.is_open():
                await asyncio.sleep(min(self.remaining_seconds(), 5))
                continue
            if self._tripped_at is None:
                if not self._recovering_until:
                    return False
                if self._probe_active:
                    await asyncio.sleep(0.2)
                    continue
                if self._clock() >= self._recovering_until:
                    self._recovering_until = 0.0
                    self._next_recovery_submit = 0.0
                    return False
                recovery_wait = self._next_recovery_submit - self._clock()
                if recovery_wait > 0:
                    await asyncio.sleep(min(recovery_wait, 0.5))
                    continue
            if not self._probe_active:
                self._probe_active = True
                self._probe_token = object()
                return self._probe_token
            await asyncio.sleep(0.2)

    def can_submit(self, probe_token=False):
        """仅允许正常态任务或当前恢复探针发起注册提交。"""
        if self._tripped_at is None and not self._recovering_until:
            return True
        return (
            probe_token is not False
            and probe_token is self._probe_token
            and not self.is_open()
        )

    def consume_recovery_seconds(self, probe_token=None):
        if self._tripped_at is None:
            self.complete_recovery_submission(probe_token)
            return None
        if probe_token is not None and probe_token is not self._probe_token:
            return None
        elapsed = self._clock() - self._tripped_at
        self._tripped_at = None
        self._blocked_until = 0.0
        self._probe_active = False
        self._probe_token = None
        self._recovering_until = self._clock() + self.recovery_seconds
        self._next_recovery_submit = self._clock() + self.recovery_interval
        return elapsed

    def complete_recovery_submission(self, probe_token):
        """完成恢复期内的一次成功提交，并按固定节奏放行下一项。"""
        if probe_token is not self._probe_token or not self._recovering_until:
            return False
        self._probe_active = False
        self._probe_token = None
        self._next_recovery_submit = self._clock() + self.recovery_interval
        return True

    def release_probe(self, probe_token):
        """提交前资源已失效时让出探针，不额外增加冷却窗口。"""
        if probe_token is self._probe_token:
            self._probe_active = False
            self._probe_token = None

    def defer_probe(self, probe_token):
        """真正的探针失败后重新进入完整冷却。"""
        if probe_token is not self._probe_token:
            return
        self._probe_active = False
        self._probe_token = None
        if self._tripped_at is not None:
            self._blocked_until = max(
                self._blocked_until,
                self._clock() + self.cooldown_seconds,
            )
        elif self._recovering_until:
            self._next_recovery_submit = self._clock() + self.recovery_interval


REGISTRATION_RATE_LIMIT_CIRCUIT = RegistrationRateLimitCircuit(
    REGISTRATION_RATE_LIMIT_COOLDOWN,
    recovery_seconds=REGISTRATION_RATE_LIMIT_RECOVERY_SECONDS,
    recovery_interval=REGISTRATION_RATE_LIMIT_RECOVERY_INTERVAL,
)

def _signup_response_markers(text):
    """将失败响应归类为固定标签，诊断时不输出任何服务端正文。"""
    lowered = text.lower()
    groups = {
        "challenge": ("captcha", "cf-chl", "challenge-platform"),
        "rate_limited": ("rate limit", "too many requests", "try again later"),
        "signin_page": ("sign in", "log in"),
        "signup_page": ("sign up", "create your account"),
        "next_page": ("__next", "/_next/"),
        "action_error": ("server action", "next-action", "digest"),
    }
    return ",".join(name for name, needles in groups.items() if any(x in lowered for x in needles)) or "unclassified"

def pb_varint(n):
    parts = []
    while n > 0x7f: parts.append((n & 0x7f) | 0x80); n >>= 7
    parts.append(n); return bytes(parts)
def pb_str(fid, val):
    vb = val.encode()
    return struct.pack('B', (fid << 3) | 2) + pb_varint(len(vb)) + vb
def decode_jwt_payload(token):
    parts = token.split('.')
    if len(parts) < 2: return None
    payload = parts[1] + '=' * (4 - len(parts[1]) % 4)
    try: return json.loads(base64.urlsafe_b64decode(payload))
    except Exception: return None
def find_chrome():
    # Backward-compatible alias: CloakBrowser Chromium only.
    return find_cloakbrowser_chrome()


# ──────────────────────────────────────────────
#  资源检测
# ──────────────────────────────────────────────
def get_system_resources(max_mem_arg=None):
    import subprocess
    cpu_count = multiprocessing.cpu_count()
    try:
        out = subprocess.check_output(["free", "-m"]).decode()
        for line in out.split("\n"):
            if "Mem" in line:
                parts = line.split()
                total, available = int(parts[1]), int(parts[6])
                break
        else:
            total, available = 4096, 2048
    except Exception:
        total, available = 4096, 2048

    if max_mem_arg:
        if max_mem_arg.endswith('%'):
            max_mem = int(total * float(max_mem_arg[:-1]) / 100)
        elif max_mem_arg.upper().endswith('G'):
            max_mem = int(float(max_mem_arg[:-1]) * 1024)
        elif max_mem_arg.upper().endswith('M'):
            max_mem = int(max_mem_arg[:-1])
        else:
            max_mem = int(max_mem_arg)
    else:
        max_mem = available

    return {'cpu': cpu_count, 'total_mem': total, 'available_mem': available, 'max_mem': max_mem}


def load_capacity_profile(path=CAPACITY_PROFILE):
    """读取离线校准生成的设备 profile。不存在或无效时返回空配置。"""
    if not path:
        return {}
    try:
        with open(path, "r") as f:
            data = json.load(f)
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    profile = {}
    try:
        physical_cap = int(data.get("physical_cap", 0))
    except (TypeError, ValueError):
        physical_cap = 0
    if physical_cap > 0:
        profile["physical_cap"] = physical_cap
    return profile


def derive_capacity(
    cpu_count,
    max_mem_mb,
    *,
    physical_cap=None,
    profile_physical_cap=None,
    physical_per_cpu=None,
    physical_mem_mb=None,
    min_free_mem_mb=None,
):
    """启动期静态容量派生:显式配置 > 设备 profile > CPU/内存保守自动值。"""
    configured_physical = PHYSICAL_CAP if physical_cap is None else physical_cap
    profiled_physical = profile_physical_cap or 0
    per_cpu = PHYSICAL_PER_CPU if physical_per_cpu is None else physical_per_cpu
    mem_per_physical = PHYSICAL_MEM_MB if physical_mem_mb is None else physical_mem_mb
    reserve_mem = MIN_FREE_MEM_MB if min_free_mem_mb is None else min_free_mem_mb

    cpu_cap = max(1, cpu_count * max(1, per_cpu))
    usable_mem = max(0, max_mem_mb - reserve_mem)
    mem_cap = max(1, usable_mem // max(1, mem_per_physical))
    auto_cap = max(1, min(cpu_cap, mem_cap))

    if configured_physical > 0:
        physical = configured_physical
    elif profiled_physical > 0:
        physical = max(1, min(profiled_physical, mem_cap))
    else:
        physical = auto_cap

    s_workers = S_WORKERS if S_WORKERS > 0 else physical + 2
    p_workers = P_WORKERS if P_WORKERS > 0 else Q_PENDING_CAP + 2
    c_workers = C_WORKERS if C_WORKERS > 0 else physical + 2
    return physical, s_workers, p_workers, c_workers


def derive_admission_watermarks(
    physical_cap,
    *,
    t_slot_cap=None,
    q_pending_cap=None,
    t_target=None,
    q_target=None,
    t_high_override=None,
    t_low_override=None,
    q_high_override=None,
    q_low_override=None,
):
    """派生 CSP v2 局部门控水位。

    T 的默认高水位跟随 Physical_Sem,避免物理并发提高后仍只允许少量 T
    in-progress。显式环境变量覆盖仍然优先。
    """
    t_slot = T_SLOT_CAP if t_slot_cap is None else t_slot_cap
    q_pending = Q_PENDING_CAP if q_pending_cap is None else q_pending_cap
    t_goal = T_TARGET if t_target is None else t_target
    q_goal = Q_TARGET if q_target is None else q_target

    t_high_cfg = _T_HIGH_WATER_OVERRIDE if t_high_override is None else t_high_override
    t_low_cfg = _T_LOW_WATER_OVERRIDE if t_low_override is None else t_low_override
    q_high_cfg = _Q_HIGH_WATER_OVERRIDE if q_high_override is None else q_high_override
    q_low_cfg = _Q_LOW_WATER_OVERRIDE if q_low_override is None else q_low_override

    if t_high_cfg is None:
        t_high = min(max(1, t_slot), max(1, t_goal, physical_cap))
    else:
        t_high = max(1, min(max(1, t_slot), t_high_cfg))

    if t_low_cfg is None:
        t_low = max(0, min(t_high, t_high // 2))
    else:
        t_low = max(0, min(t_high, t_low_cfg))

    if q_high_cfg is None:
        q_high = max(1, q_pending)
    else:
        q_high = max(1, min(max(1, q_pending), q_high_cfg))

    if q_low_cfg is None:
        q_low = max(0, min(q_high, q_goal, q_high // 2))
    else:
        q_low = max(0, min(q_high, q_low_cfg))

    return {
        "t_low": t_low,
        "t_high": t_high,
        "q_low": q_low,
        "q_high": q_high,
    }


def derive_c_hot_page_pool_size(physical_cap, c_workers, configured_size=None):
    """启动期静态派生 C 热页池容量；显式配置优先。"""
    configured = C_HOT_PAGE_POOL_SIZE if configured_size is None else configured_size
    if configured and configured > 0:
        return configured
    return max(1, min(max(1, physical_cap), max(1, c_workers)))


# ──────────────────────────────────────────────
#  配置获取
# ──────────────────────────────────────────────
async def fetch_config():
    global SITE_KEY, ACTION_ID, STATE_TREE
    debug_log('[*] Fetching config...')
    env_key = (os.environ.get("TURNSTILE_SITEKEY") or os.environ.get("SITE_KEY") or "").strip()
    if REGISTER_MODE in ("ui",) and (env_key or SITE_KEY):
        if env_key:
            SITE_KEY = env_key
        debug_log(f"[+] SITE_KEY (cached/env): {SITE_KEY}")
        return
    async with launch_browser_bundle(log=debug_log) as bundle:
        browser = bundle.browser
        try:
            page = await browser.new_page()
            await page.goto(f'{SITE_URL}/sign-up?redirect=grok-com', timeout=30000)
            await page.wait_for_timeout(5000)
            html = await page.content()
            m = re.search(r'0x4AAAAAAA[a-zA-Z0-9_-]+', html)
            if m: SITE_KEY = m.group(0); debug_log(f'[+] SITE_KEY: {SITE_KEY}')
            for chunk in re.findall(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)', html, re.DOTALL):
                if 'sign-up' not in chunk: continue
                decoded = chunk.replace('\\"', '"')
                f_match = re.search(r'"f":\[\[\[', decoded)
                if not f_match: continue
                f_start = f_match.start() + 5
                end_idx = decoded.find('"$undefined"', f_start)
                if end_idx < 0: continue
                STATE_TREE = quote(decoded[f_start:end_idx].replace('\\\\"', '"').replace('\\', ''), safe='')
                debug_log(f'[+] STATE_TREE: {STATE_TREE[:50]}...')
                break
            js_urls = re.findall(r'src="(/_next/static/[^"]+\.js[^"]*)"', html)
            for js_url in js_urls[:50]:
                try:
                    js = await page.evaluate(f"(async()=>{{return await fetch('{js_url}').then(r=>r.text()).catch(()=>\"\" )}})()")
                    if not js: continue
                    if not any(kw in js for kw in ['createUser','registerUser','emailValidation']): continue
                    hexes = re.findall(r'[a-fA-F0-9]{40,50}', js)
                    if hexes: ACTION_ID = hexes[0]; break
                except asyncio.CancelledError:
                    raise
                except Exception:
                    continue
            if ACTION_ID: debug_log(f'[+] ACTION_ID: {ACTION_ID}')
        finally:
            await browser.close()
    if not all([SITE_KEY, ACTION_ID, STATE_TREE]):
        if "Blocked due to abusive traffic" in html or "challenge-platform" in html:
            raise RuntimeError(
                "Config fetch failed: Cloudflare blocked this proxy IP (abusive traffic). "
                "换代理节点或等待风控解除后重试。"
            )
        raise RuntimeError(
            f"Config fetch failed: SITE_KEY={bool(SITE_KEY)} ACTION_ID={bool(ACTION_ID)} "
            f"STATE_TREE={bool(STATE_TREE)} page_len={len(html)}"
        )


# ──────────────────────────────────────────────
#  异步操作
# ──────────────────────────────────────────────
async def grpc_create_code(page, email):
    inner = pb_str(1, email)
    frame = b'\x00' + struct.pack('>I', len(inner)) + inner
    fb64 = base64.b64encode(frame).decode()
    s = await page.evaluate(f"(async()=>{{var fb=Uint8Array.from(atob('{fb64}'),c=>c.charCodeAt(0));var r=await fetch('{SITE_URL}/auth_mgmt.AuthManagement/CreateEmailValidationCode',{{method:'POST',headers:{{'content-type':'application/grpc-web+proto','x-grpc-web':'1','x-user-agent':'connect-es/2.1.1'}},body:fb.buffer}});return r.headers.get('grpc-status')||'0';}})()")
    return s == '0'


async def _prepare_signup_page(page, *, redirect=True, timeout=30000):
    if PAGE_BLOCK_STATIC_ASSETS:
        await page.route("**/*", _route_static_asset_filter)
    url = f'{SITE_URL}/sign-up?redirect=grok-com' if redirect else f'{SITE_URL}/sign-up'
    await page.goto(url, timeout=timeout, wait_until=PAGE_GOTO_WAIT_UNTIL)
    if PAGE_POST_WAIT_MS > 0:
        await page.wait_for_timeout(PAGE_POST_WAIT_MS)
    # Warm Castle early so request tokens are not cold/invalid at submit time.
    if ENABLE_CASTLE:
        try:
            await ensure_castle_sdk(page)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass


async def _route_static_asset_filter(route):
    req = route.request
    if (
        req.resource_type in ("image", "font", "media", "stylesheet")
        or "/_next/static/" in req.url
        or "analytics" in req.url
    ):
        await route.abort()
        return
    await route.continue_()


async def grpc_verify_code(page, email, code):
    inner = pb_str(1, email) + pb_str(2, code)
    frame = b'\x00' + struct.pack('>I', len(inner)) + inner
    fb64 = base64.b64encode(frame).decode()
    s = await page.evaluate(f"(async()=>{{var fb=Uint8Array.from(atob('{fb64}'),c=>c.charCodeAt(0));var r=await fetch('{SITE_URL}/auth_mgmt.AuthManagement/VerifyEmailValidationCode',{{method:'POST',headers:{{'content-type':'application/grpc-web+proto','x-grpc-web':'1','x-user-agent':'connect-es/2.1.1'}},body:fb.buffer}});return r.headers.get('grpc-status')||'0';}})()")
    if REGISTRATION_DIAGNOSTICS and s != '0':
        debug_log('[C] verify rejected')
    return s == '0'


def auth_cookie_snapshot(cookies):
    """保留认证所需 Cookie 的原始作用域；不写入邮箱密码。"""
    fields = ("name", "value", "domain", "path", "expires", "httpOnly", "secure", "sameSite")
    return [
        {field: cookie[field] for field in fields if field in cookie}
        for cookie in cookies
        if cookie.get("name") in {"sso", "sso-rw"}
    ]


# Castle.io bot signal. Missing token at registration marks account:
# botFlagSource=BOT_FLAG_SOURCE_CASTLE / castle_token:no_token → OAuth access_denied.
CASTLE_PK = (
    os.environ.get("CASTLE_PK")
    or os.environ.get("XAI_CASTLE_PK")
    or "pk_p8GGWvD3TmFJZRsX3BQcqAv9aFVispNz"
).strip()
ENABLE_CASTLE = (
    os.environ.get("ENABLE_CASTLE", "1").strip().lower() not in ("0", "false", "no", "off")
)
# When castle token cannot be created, abort signup instead of creating bot-flagged accounts.
CASTLE_REQUIRED = (
    os.environ.get("CASTLE_REQUIRED", "1").strip().lower() not in ("0", "false", "no", "off")
)
CASTLE_TIMEOUT_MS = max(2000, _env_int("CASTLE_TIMEOUT_MS", 12000))


def _account_is_bot_flagged(details) -> bool:
    """True when get-user reports Castle/bot flags that block OAuth."""
    if not isinstance(details, dict):
        return False
    src = str(details.get("bot_flag") or details.get("botFlagSource") or "").strip()
    det = str(details.get("bot_flag_details") or details.get("botFlagDetails") or "").strip().lower()
    if not src and not det:
        return False
    if "CASTLE" in src.upper():
        return True
    if any(k in det for k in ("invalid_token", "no_token", "castle_token", "castle")):
        return True
    return bool(src)


def _persist_dirty_account(email, password, sso, details):
    """Keep bot-flagged SSO out of success files; record for analysis only."""
    try:
        document = json.dumps(
            {
                "email": email,
                "password": password,
                "sso": (sso or "")[:32] + ("..." if sso and len(sso) > 32 else ""),
                "bot_flag": (details or {}).get("bot_flag") or "",
                "bot_flag_details": (details or {}).get("bot_flag_details") or "",
                "ts": int(time.time()),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        _append_registration_line(
            "keys/dirty-botflag.jsonl",
            document + "\n",
            mode=0o600,
            durable=True,
        )
    except Exception as exc:
        debug_log(f"[C] dirty_persist_error={type(exc).__name__}")



def _load_local_castle_js() -> str:
    """Optional offline fallback for cdn.castle.io (scripts/_castle.js)."""
    candidates = []
    try:
        here = Path(__file__).resolve()
        candidates.append(here.parent.parent / "scripts" / "_castle.js")
    except Exception:
        pass
    candidates.append(Path.cwd() / "scripts" / "_castle.js")
    for path in candidates:
        try:
            if path.is_file():
                text = path.read_text(encoding="utf-8", errors="ignore").strip()
                if text:
                    return text
        except Exception:
            continue
    return ""


_LOCAL_CASTLE_JS = _load_local_castle_js()


def browser_proxy_settings():
    """Playwright proxy dict, or None.

    In WSL, Clash on Windows usually needs Allow LAN, e.g.
    BROWSER_PROXY=http://127.0.0.1:7897
    On pure Windows host, use http://127.0.0.1:7897
    """
    server = backend_browser_proxy_server()
    return {"server": server} if server else None


def browser_headless() -> bool:
    return backend_browser_headless()


def launch_browser_kwargs():
    """Legacy CloakBrowser launch kwargs (kept for tooling/tests)."""
    kwargs = {"executable_path": find_chrome(), "headless": browser_headless()}
    proxy = browser_proxy_settings()
    if proxy:
        kwargs["proxy"] = proxy
    return kwargs


def new_browser_context_kwargs():
    """Engine-aware context options (Camoufox prefers no_viewport)."""
    return backend_context_kwargs(backend_browser_engine())


async def ensure_castle_sdk(page, *, timeout_ms=None):
    """Load/init Castle early on the signup page so it can collect signals."""
    if not ENABLE_CASTLE or not CASTLE_PK:
        return {"ok": False, "reason": "disabled"}
    if timeout_ms is None:
        timeout_ms = min(8000, CASTLE_TIMEOUT_MS)
    try:
        # light humanization helps Castle fingerprint quality
        try:
            await page.mouse.move(random.randint(40, 260), random.randint(40, 220))
            await page.mouse.move(random.randint(120, 480), random.randint(80, 360), steps=6)
        except Exception:
            pass
        info = await page.evaluate(
            """async ({pk, timeoutMs, localJs}) => {
              const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
              const hasApi = () => typeof window._castle === 'function'
                || !!(window.Castle && typeof window.Castle.createRequestToken === 'function');
              if (hasApi()) return {ok:true, source:'existing', has_underscore: typeof window._castle === 'function'};
              const loadSrc = (src) => new Promise((resolve, reject) => {
                const s = document.createElement('script');
                s.src = src; s.async = true;
                s.onload = () => resolve(true);
                s.onerror = () => reject(new Error('load_failed'));
                (document.head || document.documentElement).appendChild(s);
              });
              let err = '';
              try {
                const already = [...document.scripts].some((s) => (s.src || '').includes('cdn.castle.io'));
                if (!already) {
                  await loadSrc('https://cdn.castle.io/v2/castle.js?key=' + encodeURIComponent(pk));
                }
              } catch (e) {
                err = String((e && e.message) || e);
                if (localJs) {
                  try {
                    const s = document.createElement('script');
                    s.type = 'text/javascript';
                    s.text = localJs;
                    (document.head || document.documentElement).appendChild(s);
                    err = err ? err + '|local_js' : 'local_js';
                  } catch (e2) {
                    err += '|' + String((e2 && e2.message) || e2);
                  }
                }
              }
              const deadline = Date.now() + (timeoutMs || 8000);
              while (Date.now() < deadline && !hasApi()) await sleep(100);
              if (typeof window._castle === 'function') {
                try { window._castle('setAppId', pk); } catch (e) {}
              }
              // allow internal bootstrap
              await sleep(400);
              return {
                ok: hasApi(),
                source: err.includes('local') ? 'local' : 'cdn',
                has_underscore: typeof window._castle === 'function',
                has_Castle: !!(window.Castle && window.Castle.createRequestToken),
                err,
              };
            }""",
            {"pk": CASTLE_PK, "timeoutMs": int(timeout_ms), "localJs": _LOCAL_CASTLE_JS},
        )
        if info and info.get("ok"):
            debug_log(
                f"[C] castle_sdk ready source={info.get('source')} "
                f"_castle={info.get('has_underscore')} err={info.get('err') or '-'}"
            )
        else:
            debug_log(f"[C] castle_sdk not ready info={info}")
        return info or {"ok": False}
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        debug_log(f"[C] castle_sdk_error={type(exc).__name__}: {exc}")
        return {"ok": False, "reason": type(exc).__name__}


async def obtain_castle_request_token(page, *, timeout_ms=None):
    """Create Castle request token from an already-warmed SDK when possible.

    Important: xAI validates token server-side. Fresh cold inject often yields
    botFlagDetails=castle_token:invalid_token. Warm the SDK early, humanize,
    then create immediately before signup submit.
    """
    if not ENABLE_CASTLE or not CASTLE_PK:
        return ""
    if timeout_ms is None:
        timeout_ms = CASTLE_TIMEOUT_MS
    await ensure_castle_sdk(page, timeout_ms=min(timeout_ms, 8000))
    try:
        try:
            await page.mouse.move(random.randint(80, 420), random.randint(60, 300), steps=5)
            await page.mouse.wheel(0, random.randint(40, 160))
            await asyncio.sleep(0.15 + random.random() * 0.25)
        except Exception:
            pass
        result = await page.evaluate(
            """async ({pk, timeoutMs}) => {
              const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
              const wrap = (thenable) => new Promise((resolve, reject) => {
                try {
                  if (thenable == null) { resolve(''); return; }
                  if (typeof thenable.then === 'function') {
                    thenable.then((v) => resolve(v || ''), (e) => reject(e));
                    return;
                  }
                  resolve(thenable);
                } catch (e) { reject(e); }
              });
              const tryCreate = async (budgetMs) => {
                try {
                  const el = document.querySelector('input[name="castle_request_token"]');
                  if (el && el.value) return String(el.value);
                } catch (e) {}
                if (typeof window._castle === 'function') {
                  try { window._castle('setAppId', pk); } catch (e) {}
                  try {
                    const t = await Promise.race([
                      wrap(window._castle('createRequestToken')),
                      sleep(Math.max(500, budgetMs || 2000)).then(() => ''),
                    ]);
                    if (t) return String(t);
                  } catch (e) {}
                }
                if (window.Castle && typeof window.Castle.createRequestToken === 'function') {
                  try {
                    const t = await Promise.race([
                      wrap(window.Castle.createRequestToken()),
                      sleep(Math.max(500, budgetMs || 2000)).then(() => ''),
                    ]);
                    if (t) return String(t);
                  } catch (e) {}
                }
                return '';
              };
              // brief settle for fingerprint buffers
              await sleep(300);
              const deadline = Date.now() + (timeoutMs || 12000);
              let token = '';
              while (Date.now() < deadline) {
                token = await tryCreate(Math.min(3000, Math.max(800, deadline - Date.now())));
                if (token) break;
                await sleep(150);
              }
              return {
                token: token || '',
                has_underscore: typeof window._castle === 'function',
                has_Castle: !!(window.Castle && window.Castle.createRequestToken),
              };
            }""",
            {"pk": CASTLE_PK, "timeoutMs": int(timeout_ms)},
        )
        if isinstance(result, dict):
            token = str(result.get("token") or "").strip()
            debug_log(
                f"[C] castle_token len={len(token)} "
                f"_castle={bool(result.get('has_underscore'))} "
                f"Castle={bool(result.get('has_Castle'))}"
            )
            return token
        token = str(result or "").strip()
        debug_log(f"[C] castle_token_len={len(token)}")
        return token
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        debug_log(f"[C] castle_token_error={type(exc).__name__}: {exc}")
        return ""


def _random_birth_date_iso():
    age = random.randint(22, 35)
    year = time.localtime().tm_year - age
    month = random.randint(1, 12)
    day = random.randint(1, 28)
    hour = random.randint(0, 23)
    minute = random.randint(0, 59)
    return f"{year:04d}-{month:02d}-{day:02d}T{hour:02d}:{minute:02d}:00.000Z"


async def finalize_registered_account(page, sso):
    """Post-signup activation: land grok.com + birth_date + TOS + NSFW.

    Finalizes account activation after signup. Soft-fail: SSO still returned.
    Returns (sso, cookies, details). details may include bot_flag*.
    """
    sso = (sso or "").strip()
    if not sso:
        return sso, [], {"bot_flag": "", "bot_flag_details": ""}
    details = {"birth_ok": False, "tos_ok": False, "nsfw_ok": False, "grok_sso": False}
    try:
        # Ensure SSO cookies exist for both auth and grok domains before activation.
        await page.context.add_cookies(
            [
                {"name": "sso", "value": sso, "domain": ".x.ai", "path": "/"},
                {"name": "sso-rw", "value": sso, "domain": ".x.ai", "path": "/"},
                {"name": "sso", "value": sso, "domain": ".grok.com", "path": "/"},
                {"name": "sso-rw", "value": sso, "domain": ".grok.com", "path": "/"},
            ]
        )
    except Exception:
        pass
    try:
        await page.goto("https://grok.com/", timeout=30000, wait_until="domcontentloaded")
        await asyncio.sleep(1.0)
    except asyncio.CancelledError:
        raise
    except Exception:
        pass

    birth = _random_birth_date_iso()
    try:
        birth_status = await page.evaluate(
            """async (birthDate) => {
              try {
                const r = await fetch('https://grok.com/rest/auth/set-birth-date', {
                  method: 'POST',
                  headers: {'content-type': 'application/json', 'accept': 'application/json'},
                  body: JSON.stringify({birthDate}),
                  credentials: 'include',
                });
                return r.status;
              } catch (e) {
                return -1;
              }
            }""",
            birth,
        )
        details["birth_ok"] = int(birth_status or 0) in (200, 204) or int(birth_status or 0) == 429
    except Exception:
        pass

    # TOS endpoint is on accounts.x.ai; prefer APIRequestContext (more stable than page.evaluate
    # after cross-site navigation). Fallback to browser fetch on accept-tos.
    try:
        # protobuf: field 2 varint = 1  => bytes 0x10 0x01
        tos_payload = bytes([0x10, 0x01])
        tos_frame = b"\x00" + struct.pack(">I", len(tos_payload)) + tos_payload
        tos_headers = {
            "content-type": "application/grpc-web+proto",
            "x-grpc-web": "1",
            "x-user-agent": "connect-es/2.1.1",
            "origin": "https://accounts.x.ai",
            "referer": "https://accounts.x.ai/accept-tos",
        }
        tos_status = -1
        try:
            resp = await page.context.request.post(
                "https://accounts.x.ai/auth_mgmt.AuthManagement/SetTosAcceptedVersion",
                data=tos_frame,
                headers=tos_headers,
                timeout=15000,
            )
            tos_status = int(getattr(resp, "status", 0) or 0)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            debug_log(f"[C] tos_request_error={type(exc).__name__}")
            tos_status = -1

        if not (200 <= tos_status < 300):
            try:
                await page.goto(
                    "https://accounts.x.ai/accept-tos",
                    timeout=20000,
                    wait_until="domcontentloaded",
                )
                await asyncio.sleep(0.8)
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            try:
                tos_status = await page.evaluate(
                    """async () => {
                      try {
                        const payload = new Uint8Array([0x10, 0x01]);
                        const frame = new Uint8Array(5 + payload.length);
                        frame[0] = 0;
                        frame[1] = 0; frame[2] = 0; frame[3] = 0; frame[4] = payload.length;
                        frame.set(payload, 5);
                        const r = await fetch('https://accounts.x.ai/auth_mgmt.AuthManagement/SetTosAcceptedVersion', {
                          method: 'POST',
                          headers: {
                            'content-type': 'application/grpc-web+proto',
                            'x-grpc-web': '1',
                            'x-user-agent': 'connect-es/2.1.1',
                            'origin': 'https://accounts.x.ai',
                            'referer': 'https://accounts.x.ai/accept-tos',
                          },
                          body: frame,
                          credentials: 'include',
                        });
                        return r.status;
                      } catch (e) {
                        return -1;
                      }
                    }"""
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                debug_log(f"[C] tos_eval_error={type(exc).__name__}: {exc}")
                tos_status = -1

        details["tos_ok"] = 200 <= int(tos_status or 0) < 300
        if not details["tos_ok"]:
            debug_log(f"[C] tos_status={tos_status}")
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        debug_log(f"[C] tos_error={type(exc).__name__}: {exc}")

    try:
        nsfw_status = await page.evaluate(
            """async () => {
              try {
                // always_show_nsfw_content grpc-web frame (for Grok Build OAuth)
                const field1 = new Uint8Array([0x0a, 0x02, 0x10, 0x01]);
                const nsfw = new TextEncoder().encode('always_show_nsfw_content');
                const field2Inner = new Uint8Array(2 + nsfw.length);
                field2Inner[0] = 0x0a; field2Inner[1] = nsfw.length; field2Inner.set(nsfw, 2);
                const field2 = new Uint8Array(2 + field2Inner.length);
                field2[0] = 0x12; field2[1] = field2Inner.length; field2.set(field2Inner, 2);
                const payload = new Uint8Array(field1.length + field2.length);
                payload.set(field1, 0); payload.set(field2, field1.length);
                const frame = new Uint8Array(5 + payload.length);
                frame[0] = 0;
                frame[1] = (payload.length >>> 24) & 0xff;
                frame[2] = (payload.length >>> 16) & 0xff;
                frame[3] = (payload.length >>> 8) & 0xff;
                frame[4] = payload.length & 0xff;
                frame.set(payload, 5);
                const r = await fetch('https://grok.com/auth_mgmt.AuthManagement/UpdateUserFeatureControls', {
                  method: 'POST',
                  headers: {
                    'content-type': 'application/grpc-web+proto',
                    'x-grpc-web': '1',
                    'origin': 'https://grok.com',
                    'referer': 'https://grok.com/',
                  },
                  body: frame,
                  credentials: 'include',
                });
                return r.status;
              } catch (e) {
                return -1;
              }
            }"""
        )
        details["nsfw_ok"] = 200 <= int(nsfw_status or 0) < 300
    except Exception:
        pass

    cookies = []
    try:
        cookies = await page.context.cookies()
        grok_sso = next(
            (
                c.get("value")
                for c in cookies
                if c.get("name") == "sso" and "grok.com" in str(c.get("domain") or "")
            ),
            None,
        )
        if grok_sso:
            sso = grok_sso
            details["grok_sso"] = True
    except Exception:
        cookies = []

    # Probe bot flag so we know immediately if Castle token was accepted.
    try:
        user_info = await page.evaluate(
            """async () => {
              try {
                const r = await fetch('https://grok.com/rest/auth/get-user', {
                  method: 'GET',
                  credentials: 'include',
                  headers: {'accept': 'application/json'},
                });
                if (!r.ok) return {status: r.status};
                const j = await r.json();
                return {
                  status: r.status,
                  botFlagSource: j.botFlagSource || '',
                  botFlagDetails: j.botFlagDetails || '',
                  riskLevel: j.riskLevel || '',
                  personalTeamId: j.personalTeamId || j.teamId || '',
                };
              } catch (e) {
                return {status: -1, err: String(e)};
              }
            }"""
        )
        if isinstance(user_info, dict):
            debug_log(
                f"[C] get-user botFlag={user_info.get('botFlagSource') or '-'} "
                f"details={user_info.get('botFlagDetails') or '-'} "
                f"risk={user_info.get('riskLevel') or '-'} "
                f"status={user_info.get('status')}"
            )
            details["bot_flag"] = user_info.get("botFlagSource") or ""
            details["bot_flag_details"] = user_info.get("botFlagDetails") or ""
    except Exception as exc:
        debug_log(f"[C] get-user_error={type(exc).__name__}")

    if REGISTRATION_DIAGNOSTICS or True:
        debug_log(
            f"[C] activate birth={details['birth_ok']} tos={details['tos_ok']} "
            f"nsfw={details['nsfw_ok']} grok_sso={details['grok_sso']}"
        )
    return sso, auth_cookie_snapshot(cookies) if cookies else [], details


async def server_action_register(
    page,
    email,
    password,
    code,
    turnstile_token,
    *,
    include_session=False,
):
    # Warm SDK first; actual token is created inside the submit evaluate so the
    # request uses a just-minted token from the same page context.
    if ENABLE_CASTLE:
        await ensure_castle_sdk(page)
    pre_token = await obtain_castle_request_token(page)
    if ENABLE_CASTLE and not pre_token:
        debug_log(
            "[C] castle token missing; account would be bot-flagged and OAuth-denied"
        )
        if CASTLE_REQUIRED:
            debug_log("[C] CASTLE_REQUIRED=1 → abort signup (no bot-flagged account)")
            return None

    given = random.choice(["James","John","Robert","Michael","William","David","Richard","Joseph","Thomas","Charles"])
    family = random.choice(["Smith","Johnson","Williams","Brown","Jones","Garcia","Miller","Davis","Rodriguez","Martinez"])
    # NOTE: castle token is injected at submit-time inside fetch_js (fresh mint).
    body_obj = {
        "emailValidationCode": code,
        "createUserAndSessionRequest": {
            "email": email,
            "givenName": given,
            "familyName": family,
            "clearTextPassword": password,
            "tosAcceptedVersion": "$undefined",
        },
        "turnstileToken": turnstile_token,
        "promptOnDuplicateEmail": True,
    }

    payload = json.dumps([body_obj])
    pb64 = base64.b64encode(payload.encode()).decode()

    fetch_js = f"""(async()=>{{
      const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
      const wrap = (thenable) => new Promise((resolve, reject) => {{
        try {{
          if (thenable == null) {{ resolve(''); return; }}
          if (typeof thenable.then === 'function') thenable.then(v => resolve(v || ''), reject);
          else resolve(thenable);
        }} catch (e) {{ reject(e); }}
      }});
      let castle = '';
      try {{
        if (typeof window._castle === 'function') {{
          try {{ window._castle('setAppId', {json.dumps(CASTLE_PK)}); }} catch (e) {{}}
          castle = await Promise.race([
            wrap(window._castle('createRequestToken')),
            sleep(4000).then(() => ''),
          ]);
          castle = castle ? String(castle) : '';
        }} else if (window.Castle && typeof window.Castle.createRequestToken === 'function') {{
          castle = await Promise.race([
            wrap(window.Castle.createRequestToken()),
            sleep(4000).then(() => ''),
          ]);
          castle = castle ? String(castle) : '';
        }}
      }} catch (e) {{ castle = ''; }}
      // Fallback to pre-minted token if live mint failed
      if (!castle) castle = {json.dumps(pre_token or '')};

      // Rebuild body with fresh castle fields (top-level + nested).
      let bodyArr;
      try {{
        bodyArr = JSON.parse(atob('{pb64}'));
      }} catch (e) {{
        return JSON.stringify({{status:0,retryAfter:'',text:'',castleLen:castle?castle.length:0,err:'body_parse'}});
      }}
      if (Array.isArray(bodyArr) && bodyArr[0] && castle) {{
        bodyArr[0].castleRequestToken = castle;
        bodyArr[0].castle_request_token = castle;
        if (bodyArr[0].createUserAndSessionRequest) {{
          bodyArr[0].createUserAndSessionRequest.castleRequestToken = castle;
          bodyArr[0].createUserAndSessionRequest.castle_request_token = castle;
        }}
      }}
      const bodyText = JSON.stringify(bodyArr);
      const headers = {{
        'accept':'text/x-component',
        'content-type':'text/plain;charset=UTF-8',
        'next-router-state-tree':'{STATE_TREE}',
        'next-action':'{ACTION_ID}'
      }};
      if (castle) {{
        headers['X-Castle-Request-Token'] = castle;
        headers['x-castle-request-token'] = castle;
      }}
      const r = await fetch('{SITE_URL}/sign-up', {{method:'POST', headers, body: bodyText, credentials:'include'}});
      return JSON.stringify({{status:r.status,retryAfter:r.headers.get('retry-after')||'',text:await r.text(),castleLen:castle?castle.length:0}});
    }})()"""

    if REGISTRATION_DIAGNOSTICS:
        diagnostic_json = await page.evaluate(fetch_js)
        diagnostic = json.loads(diagnostic_json)
        result_text = diagnostic['text']
    else:
        diagnostic = None
        raw = await page.evaluate(fetch_js)
        try:
            parsed = json.loads(raw)
            result_text = parsed.get("text") or ""
            diagnostic = parsed
        except Exception:
            result_text = raw if isinstance(raw, str) else ""
    # 注册响应里带一个 set-cookie 重定向 URL,必须访问它,x.ai 才会下发真正的 sso cookie(152 字符 JWT)。
    # 注意:直接解 q= 里的 JWT 取 config.token 是错的——那是 120 字符内部 blob,不是 sso 凭证。
    text = result_text.replace('\\/', '/')  # RSC 里 / 被转义成 \/
    m = re.search(r'(https://[^" \s\\]+set-cookie\?q=[^:" \s\\]+)1:', text)
    if not m:
        m = re.search(r'(https://[^" \s\\]+set-cookie\?q=[A-Za-z0-9_.\-]+)', text)
    if not m:
        markers = _signup_response_markers(result_text)
        if diagnostic is not None:
            debug_log(
                f"[C] signup no session http_status={diagnostic['status']} "
                f"retry_after={diagnostic['retryAfter'] or '-'} response_bytes={len(result_text)} "
                f"markers={markers}"
            )
        if "rate_limited" in markers:
            raise RegistrationRateLimited("signup_rate_limited")
        return None
    url = m.group(1)
    if C_SET_COOKIE_VIA_REQUEST:
        try:
            await page.context.request.get(url, timeout=15000)
            cookies = await page.context.cookies()
            sso = next((c['value'] for c in cookies if c['name'] == 'sso'), None)
            if sso:
                try:
                    sso2, cookies2, act_details = await finalize_registered_account(page, sso)
                    if sso2:
                        sso = sso2
                    if cookies2:
                        cookies = cookies2
                    if CASTLE_REQUIRED and _account_is_bot_flagged(act_details):
                        debug_log(
                            f"[C] bot-flagged after signup botFlag={act_details.get('bot_flag') or '-'} "
                            f"details={act_details.get('bot_flag_details') or '-'} → discard"
                        )
                        try:
                            _persist_dirty_account(email, password, sso, act_details)
                        except Exception:
                            pass
                        return None
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    debug_log(f"[C] activate warn: {type(exc).__name__}")
                if include_session:
                    return sso, auth_cookie_snapshot(cookies) if cookies else []
                return sso
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    # 首方导航访问该 URL,浏览器正常落 sso cookie(跨域 fetch 会被 CORS/三方cookie 拦)
    try:
        await page.goto(url, timeout=15000, wait_until='domcontentloaded')
    except asyncio.CancelledError:
        raise
    except Exception:
        pass
    cookies = await page.context.cookies()
    sso = next((c['value'] for c in cookies if c['name'] == 'sso'), None)
    if REGISTRATION_DIAGNOSTICS and not sso:
        debug_log('[C] signup set-cookie completed without sso cookie')
    if not sso:
        return None
    # Activate + prefer grok.com SSO before persist (upstream UI flow parity).
    try:
        sso2, cookies2, act_details = await finalize_registered_account(page, sso)
        if sso2:
            sso = sso2
        if cookies2:
            cookies = cookies2
        if CASTLE_REQUIRED and _account_is_bot_flagged(act_details):
            debug_log(
                f"[C] bot-flagged after signup botFlag={act_details.get('bot_flag') or '-'} "
                f"details={act_details.get('bot_flag_details') or '-'} → discard"
            )
            try:
                _persist_dirty_account(email, password, sso, act_details)
            except Exception:
                pass
            return None
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        debug_log(f"[C] activate warn: {type(exc).__name__}")
    if include_session:
        return sso, auth_cookie_snapshot(cookies) if cookies else []
    return sso

# solver 预热页面池:复用已停在 sign-up 的页面,省掉每次 page.goto 的重型 SPA 加载
# SOLVER_REUSE=0 可关闭(用于 A/B 对比 goto 优化的增益)
SOLVER_REUSE = (os.environ.get("SOLVER_REUSE", "1").strip().lower() not in ("0", "false", "no"))
_solver_pool = []
_solver_lock = asyncio.Lock()
MAX_SOLVER_REUSE = _env_int("MAX_SOLVER_REUSE", 25)
SOLVER_INITIAL_WAIT_MS = _env_int("SOLVER_INITIAL_WAIT_MS", 500)
SOLVER_POLL_INTERVAL_MS = _env_int("SOLVER_POLL_INTERVAL_MS", 500)
SOLVER_POLL_ATTEMPTS = _env_int("SOLVER_POLL_ATTEMPTS", 100)
SOLVER_HARD_TIMEOUT = max(10, _env_int("SOLVER_HARD_TIMEOUT", 90))
SOLVER_CLEANUP_TIMEOUT = max(1, _env_int("SOLVER_CLEANUP_TIMEOUT", 5))
SOLVER_FAST_CLICK = (os.environ.get("SOLVER_FAST_CLICK", "1").strip().lower() not in ("0", "false", "no"))
SOLVER_MOUSE_CLICK_RETRIES = _env_int("SOLVER_MOUSE_CLICK_RETRIES", 3)
SOLVER_MOUSE_CLICK_INTERVAL_MS = _env_int("SOLVER_MOUSE_CLICK_INTERVAL_MS", 600)
SOLVER_TIMELINE_TRACE = (os.environ.get("SOLVER_TIMELINE_TRACE", "0").strip().lower() in ("1", "true", "yes"))
SOLVER_TIMELINE_SAMPLE = _env_int("SOLVER_TIMELINE_SAMPLE", 8)
_solver_timeline_emitted = 0
_solver_timeline_next_id = 0


def _new_solver_timeline(*, enabled=None):
    global _solver_timeline_next_id
    enabled = SOLVER_TIMELINE_TRACE if enabled is None else enabled
    if not enabled:
        return None
    _solver_timeline_next_id += 1
    return {"start": time.time(), "solve_id": _solver_timeline_next_id, "events": []}


def _trace_solver_event(timeline, event, **fields):
    if timeline is None:
        return
    item = {"t": round(time.time() - timeline["start"], 4), "event": event}
    if "solve_id" in timeline:
        item["solve_id"] = timeline["solve_id"]
    item.update(fields)
    timeline["events"].append(item)


def _new_solver_poll_stats():
    return {
        "poll_attempts": 0,
        "first_token_attempt": None,
        "poll_read_ms_total": 0.0,
        "poll_read_ms_max": 0.0,
        "poll_read_count": 0,
        "poll_retry_click_count": 0,
    }


def _finish_solver_poll_stats(stats):
    if not stats:
        return {}
    read_count = stats.pop("poll_read_count", 0)
    total = stats.pop("poll_read_ms_total", 0.0)
    stats["poll_read_ms_avg"] = round(total / read_count, 1) if read_count else 0.0
    stats["poll_read_ms_max"] = round(stats.get("poll_read_ms_max", 0.0), 1)
    return stats


async def _turnstile_frame_count(p):
    try:
        return await p.evaluate(
            "() => document.querySelectorAll('iframe[src*=turnstile], iframe[src*=challenges.cloudflare.com]').length"
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        return 0


async def _turnstile_dom_snapshot(p):
    try:
        return await p.evaluate(
            """() => {
                const clip = (value, n = 96) => String(value || "").slice(0, n);
                const rectInfo = (el) => {
                    if (!el) return null;
                    const r = el.getBoundingClientRect();
                    return {
                        x: Math.round(r.left),
                        y: Math.round(r.top),
                        w: Math.round(r.width),
                        h: Math.round(r.height),
                        visible: r.width >= 1 && r.height >= 1
                    };
                };
                const elemInfo = (el) => {
                    if (!el) return null;
                    return {
                        tag: clip(el.tagName),
                        id: clip(el.id, 64),
                        class: clip(el.className, 96),
                        is_iframe: el.tagName === "IFRAME"
                    };
                };
                const urlInfo = (src) => {
                    try {
                        const u = new URL(src || "", location.href);
                        return {host: clip(u.host, 96), path: clip(u.pathname, 96)};
                    } catch (_) {
                        return {host: "", path: ""};
                    }
                };
                const widget = document.querySelector(".cf-turnstile");
                const wr = rectInfo(widget);
                const center = wr ? {
                    x: Math.round(wr.x + wr.w / 2),
                    y: Math.round(wr.y + wr.h / 2)
                } : null;
                const centerEl = center ? document.elementFromPoint(center.x, center.y) : null;
                const iframes = Array.from(document.querySelectorAll("iframe"));
                const iframeSummaries = iframes.slice(0, 8).map((f) => {
                    const info = Object.assign(urlInfo(f.getAttribute("src") || ""), rectInfo(f) || {});
                    info.in_widget = widget ? widget.contains(f) : false;
                    return info;
                });
                const isTurnstileFrame = (f) => {
                    const src = f.getAttribute("src") || "";
                    return src.includes("turnstile") || src.includes("challenges.cloudflare.com");
                };
                const response = document.querySelector('input[name="cf-turnstile-response"]');
                return {
                    __csp_solver_snapshot: true,
                    ready_state: document.readyState,
                    viewport: {w: window.innerWidth, h: window.innerHeight},
                    widget: Object.assign({present: Boolean(widget)}, wr || {}),
                    click_center: center,
                    element_at_center: elemInfo(centerEl),
                    all_iframe_count: iframes.length,
                    turnstile_iframe_count: iframes.filter(isTurnstileFrame).length,
                    iframe_summaries: iframeSummaries,
                    turnstile_loaded: Boolean(window.turnstile),
                    response_input: {
                        present: Boolean(response),
                        token_len: response && response.value ? response.value.length : 0
                    }
                };
            }"""
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        return {}


async def _turnstile_page_trace(p):
    try:
        return await p.evaluate(
            "() => window.__cspTurnstileTrace ? Object.assign({}, window.__cspTurnstileTrace) : null"
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        return None


async def _safe_set_viewport(page, width=800, height=600):
    try:
        await page.set_viewport_size({"width": width, "height": height})
    except Exception:
        # Camoufox/Firefox contexts may run with no_viewport=True
        pass


async def _get_solver_page(browser):
    if SOLVER_REUSE:
        async with _solver_lock:
            if _solver_pool:
                item = _solver_pool.pop()
                item["reused"] = True
                item["goto_s"] = 0.0
                return item
    p = await browser.new_page()
    await _safe_set_viewport(p, 800, 600)
    goto_started = time.time()
    await p.goto(f'{SITE_URL}/sign-up', timeout=20000)
    await p.wait_for_timeout(1000)
    return {"page": p, "n": 0, "reused": False, "goto_s": time.time() - goto_started}

async def _put_solver_page(item, ok):
    p = item["page"]
    item["n"] += 1
    if SOLVER_REUSE and ok and item["n"] < MAX_SOLVER_REUSE:
        try:  # 清理本次注入痕迹,留待复用
            await asyncio.wait_for(
                p.evaluate("document.querySelectorAll('.cf-turnstile').forEach(e=>e.remove());var i=document.querySelector('input[name=\"cf-turnstile-response\"]');if(i)i.remove();"),
                timeout=SOLVER_CLEANUP_TIMEOUT,
            )
            async with _solver_lock:
                _solver_pool.append(item)
            return
        except asyncio.CancelledError:
            await _close_solver_page(item)
            raise
        except Exception:
            pass
    await _close_solver_page(item)


async def _close_solver_page(item):
    """Bound cleanup so a wedged renderer cannot trap the worker in finally."""
    page = item["page"]
    try:
        await asyncio.wait_for(page.close(), timeout=SOLVER_CLEANUP_TIMEOUT)
    except asyncio.CancelledError:
        # Solver cancellation is expected at the hard deadline.  Give cleanup
        # its own bounded task so the page is not returned to the reuse pool.
        cleanup = asyncio.create_task(page.close())
        try:
            await asyncio.wait_for(cleanup, timeout=SOLVER_CLEANUP_TIMEOUT)
        except BaseException:
            cleanup.cancel()
        raise
    except Exception:
        pass

async def _inject_turnstile_widget(p, *, timeline=False):
    if not timeline:
        await p.evaluate(f"""var d=document.createElement('div');d.className='cf-turnstile';d.setAttribute('data-sitekey','{SITE_KEY}');d.style.cssText='position:fixed;top:10px;left:10px;z-index:99999;background:white;padding:12px;border:2px solid red;border-radius:6px;width:300px;height:70px';document.body.appendChild(d);function __r(){{window.turnstile&&window.turnstile.render(d,{{sitekey:'{SITE_KEY}',callback:function(t){{var i=document.querySelector('input[name="cf-turnstile-response"]');if(!i){{i=document.createElement('input');i.type='hidden';i.name='cf-turnstile-response';document.body.appendChild(i);}}i.value=t;}}}})}}if(window.turnstile){{__r()}}else{{var s=document.createElement('script');s.src='https://challenges.cloudflare.com/turnstile/v0/api.js';s.onload=function(){{setTimeout(__r,1000)}};document.head.appendChild(s);}}""")
        return
    await p.evaluate(f"""var __trace=window.__cspTurnstileTrace={{created_at:performance.now(),script_inserted_at:null,script_loaded_at:null,render_called_at:null,render_returned_at:null,token_written_at:null,token_len:0,error:null}};var d=document.createElement('div');d.className='cf-turnstile';d.setAttribute('data-sitekey','{SITE_KEY}');d.style.cssText='position:fixed;top:10px;left:10px;z-index:99999;background:white;padding:12px;border:2px solid red;border-radius:6px;width:300px;height:70px';document.body.appendChild(d);function __r(){{try{{if(!window.turnstile)return;__trace.render_called_at=performance.now();var __ret=window.turnstile.render(d,{{sitekey:'{SITE_KEY}',callback:function(t){{var i=document.querySelector('input[name="cf-turnstile-response"]');if(!i){{i=document.createElement('input');i.type='hidden';i.name='cf-turnstile-response';document.body.appendChild(i);}}i.value=t;__trace.token_written_at=performance.now();__trace.token_len=t?t.length:0;}}}});__trace.render_returned_at=performance.now();__trace.render_return_type=typeof __ret;}}catch(e){{__trace.error=e&&e.name?e.name:String(e);}}}}if(window.turnstile){{__r()}}else{{var s=document.createElement('script');s.src='https://challenges.cloudflare.com/turnstile/v0/api.js';s.onload=function(){{__trace.script_loaded_at=performance.now();setTimeout(__r,1000)}};__trace.script_inserted_at=performance.now();document.head.appendChild(s);}}""")


async def _has_visible_turnstile_frame(p):
    try:
        return await p.evaluate(
            """() => Array.from(document.querySelectorAll('iframe')).some((f) => {
                const r = f.getBoundingClientRect();
                return r.width >= 20 && r.height >= 20;
            })"""
        )
    except Exception:
        return False


async def _read_turnstile_token(p):
    try:
        return await p.evaluate('document.querySelector("input[name=\\"cf-turnstile-response\\"]")?.value||""')
    except asyncio.CancelledError:
        raise
    except Exception:
        return ""


async def _mouse_click_turnstile_center(p):
    clicked, _trace = await _mouse_click_turnstile_center_trace(p)
    return clicked


async def _mouse_click_turnstile_center_trace(p):
    trace = {}
    started = time.time()
    box = await p.evaluate(
        """() => {
            const e = document.querySelector('.cf-turnstile');
            if (!e) return null;
            const r = e.getBoundingClientRect();
            return {x: r.left + r.width / 2, y: r.top + r.height / 2};
        }"""
    )
    trace["box_eval_ms"] = round((time.time() - started) * 1000, 1)
    if not box:
        return False, trace
    x = float(box["x"])
    y = float(box["y"])
    trace["click_x"] = round(x, 1)
    trace["click_y"] = round(y, 1)
    started = time.time()
    await p.mouse.move(max(0, x - 25), max(0, y - 8))
    trace["mouse_move1_ms"] = round((time.time() - started) * 1000, 1)
    started = time.time()
    await p.mouse.move(x, y, steps=8)
    trace["mouse_move2_ms"] = round((time.time() - started) * 1000, 1)
    started = time.time()
    await p.mouse.down()
    trace["mouse_down_ms"] = round((time.time() - started) * 1000, 1)
    await asyncio.sleep(0.05)
    started = time.time()
    await p.mouse.up()
    trace["mouse_up_ms"] = round((time.time() - started) * 1000, 1)
    return True, trace


async def _repeat_mouse_click_turnstile(p, *, timeline=None):
    retries = max(0, SOLVER_MOUSE_CLICK_RETRIES)
    if retries <= 0:
        return False
    clicked = False
    interval = max(50, SOLVER_MOUSE_CLICK_INTERVAL_MS) / 1000.0
    for i in range(retries):
        token = await _read_turnstile_token(p)
        dom = await _turnstile_dom_snapshot(p) if timeline is not None else None
        iframe_count = dom.get("turnstile_iframe_count", 0) if dom else 0
        _trace_solver_event(
            timeline, "click_before", attempt=i + 1,
            token_len=len(token or ""), iframe_count=iframe_count, dom=dom
        )
        if token and len(token) > 10:
            return clicked
        click_started = time.time()
        click_error = None
        try:
            if timeline is not None:
                attempt_clicked, click_trace = await _mouse_click_turnstile_center_trace(p)
            else:
                attempt_clicked = await _mouse_click_turnstile_center(p)
                click_trace = {}
            clicked = attempt_clicked or clicked
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            click_error = type(exc).__name__
            attempt_clicked = False
            click_trace = {}
        click_call_ms = round((time.time() - click_started) * 1000, 1)
        token = await _read_turnstile_token(p) if timeline is not None else ""
        dom = await _turnstile_dom_snapshot(p) if timeline is not None else None
        iframe_count = dom.get("turnstile_iframe_count", 0) if dom else 0
        _trace_solver_event(
            timeline, "click_after", attempt=i + 1, clicked=clicked,
            attempt_clicked=attempt_clicked,
            token_len=len(token or ""), iframe_count=iframe_count,
            click_call_ms=click_call_ms, click_error=click_error,
            click_trace=click_trace, dom=dom
        )
        if i != retries - 1:
            await asyncio.sleep(interval)
    return clicked


async def _click_turnstile_if_possible(p, *, fast=False, timeline=None):
    if SOLVER_MOUSE_CLICK_RETRIES > 0:
        return await _repeat_mouse_click_turnstile(p, timeline=timeline)
    visible = await _has_visible_turnstile_frame(p)
    _trace_solver_event(timeline, "visible_check", visible=visible)
    if fast and not visible:
        return visible
    click_timeout = 500 if fast else 3000
    for sel in ["iframe[src*='challenges.cloudflare.com']","iframe[src*='turnstile']",".cf-turnstile iframe"]:
        try:
            fr = p.frame_locator(sel).first
            await fr.locator("#checkbox, .checkbox, input[type=checkbox], body").first.click(timeout=click_timeout)
            break
        except asyncio.CancelledError:
            raise
        except Exception:
            continue
    return visible


async def _poll_turnstile_token(p, *, stats=None):
    for i in range(SOLVER_POLL_ATTEMPTS):
        await asyncio.sleep(max(50, SOLVER_POLL_INTERVAL_MS) / 1000)
        if stats is not None:
            stats["poll_attempts"] = i + 1
            read_started = time.time()
        try:
            t = await _read_turnstile_token(p)
            if stats is not None:
                read_ms = (time.time() - read_started) * 1000
                stats["poll_read_count"] += 1
                stats["poll_read_ms_total"] += read_ms
                stats["poll_read_ms_max"] = max(stats["poll_read_ms_max"], read_ms)
            if t and len(t) > 10:
                if stats is not None and stats["first_token_attempt"] is None:
                    stats["first_token_attempt"] = i + 1
                return t
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        retry_every = max(1, int(10000 / max(50, SOLVER_POLL_INTERVAL_MS)))
        if i > 0 and i % retry_every == 0:
            try:
                await p.locator(".cf-turnstile").first.click(timeout=1000)
                if stats is not None:
                    stats["poll_retry_click_count"] += 1
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
    return None


async def _start_turnstile_challenge(browser, *, fast_click=False):
    global _solver_timeline_emitted
    item = await _get_solver_page(browser)
    p = item["page"]
    trace_timeline = SOLVER_TIMELINE_TRACE and _solver_timeline_emitted < SOLVER_TIMELINE_SAMPLE
    if trace_timeline:
        _solver_timeline_emitted += 1
    timeline = _new_solver_timeline(enabled=trace_timeline)
    trace = {
        "goto_s": item.get("goto_s", 0.0),
        "reused": bool(item.get("reused", False)),
        "reuse_count": item.get("n", 0),
        "inject_s": 0.0,
        "initial_s": 0.0,
        "click_s": 0.0,
        "wait_s": 0.0,
        "visible_frame": False,
    }
    item["trace"] = trace
    item["timeline"] = timeline
    try:
        stage_started = time.time()
        _trace_solver_event(timeline, "inject_start")
        await _inject_turnstile_widget(p, timeline=timeline is not None)
        trace["inject_s"] = time.time() - stage_started
        _trace_solver_event(timeline, "inject_done")
        if timeline is not None:
            _trace_solver_event(
                timeline, "page_trace_after_inject",
                page_trace=await _turnstile_page_trace(p)
            )
        stage_started = time.time()
        await p.wait_for_timeout(SOLVER_INITIAL_WAIT_MS)
        trace["initial_s"] = time.time() - stage_started
        _trace_solver_event(timeline, "initial_done")
        if timeline is not None:
            _trace_solver_event(
                timeline, "page_trace_after_initial",
                page_trace=await _turnstile_page_trace(p)
            )
        stage_started = time.time()
        trace["visible_frame"] = await _click_turnstile_if_possible(
            p, fast=fast_click, timeline=timeline
        )
        trace["click_s"] = time.time() - stage_started
        _trace_solver_event(timeline, "click_stage_done", clicked=bool(trace["visible_frame"]))
        if timeline is not None:
            _trace_solver_event(
                timeline, "page_trace_after_click",
                page_trace=await _turnstile_page_trace(p)
            )
        return item
    except BaseException:
        await _put_solver_page(item, False)
        raise


async def _wait_turnstile_challenge(item):
    ok = False
    try:
        wait_started = time.time()
        timeline = item.get("timeline")
        poll_stats = _new_solver_poll_stats() if timeline is not None else None
        _trace_solver_event(timeline, "poll_start")
        token = await _poll_turnstile_token(item["page"], stats=poll_stats)
        item.get("trace", {})["wait_s"] = time.time() - wait_started
        ok = token is not None
        page_trace = await _turnstile_page_trace(item["page"]) if timeline is not None else None
        _trace_solver_event(
            timeline, "poll_done", ok=ok, token_len=len(token or ""),
            page_trace=page_trace, **_finish_solver_poll_stats(poll_stats)
        )
        return token
    except asyncio.CancelledError:
        raise
    except Exception:
        return None
    finally:
        timeline = item.get("timeline")
        if timeline is not None:
            debug_log("[solver_timeline] " + json.dumps(timeline["events"], separators=(",", ":")))
        await _put_solver_page(item, ok)


async def solve_one_turnstile(browser):
    token, _trace = await solve_one_turnstile_with_trace(browser)
    return token


async def solve_one_turnstile_with_trace(browser):
    item = await _start_turnstile_challenge(browser, fast_click=SOLVER_FAST_CLICK)
    token = await _wait_turnstile_challenge(item)
    return token, item.get("trace", {})


def _record_solver_trace(metrics, trace, total_seconds, token):
    metrics.t_solve_count += 1
    metrics.t_solve_seconds += total_seconds
    if token is None:
        metrics.t_solve_failed += 1
    if not trace:
        return
    metrics.solver_goto_seconds += trace.get("goto_s", 0.0)
    metrics.solver_inject_seconds += trace.get("inject_s", 0.0)
    metrics.solver_initial_seconds += trace.get("initial_s", 0.0)
    metrics.solver_click_seconds += trace.get("click_s", 0.0)
    metrics.solver_wait_seconds += trace.get("wait_s", 0.0)
    if trace.get("reused"):
        metrics.solver_reused_count += 1
    if trace.get("visible_frame"):
        metrics.solver_visible_frame_count += 1


# ──────────────────────────────────────────────
#  邮箱服务:custom(自建 webhook) / tempmail(免费临时邮箱,多 provider fallback)
# ──────────────────────────────────────────────
# 免 key 的公共临时邮箱 provider(实测可用,互为 fallback 消灭单点):
#  - mail.tm 同协议:mail.tm / mail.gw / duckmail.sbs
#  - 独立 API:tempmail.lol
# handle 编码 provider,供 poll_code 分派;新增 provider 只要在这两处加一段即可。
TEMPMAIL_BASES = ["https://api.mail.tm", "https://api.mail.gw", "https://api.duckmail.sbs"]

def _extract_code(text):
    """多层兜底提取验证码,抗邮件模板变化。"""
    for pat in (r'>([A-Z0-9]{3}-[A-Z0-9]{3})<', r'>([A-Z0-9]{6})<', r'\b([A-Z0-9]{3}-?[A-Z0-9]{3})\b'):
        m = re.search(pat, text)
        if m:
            return m.group(1).replace('-', '')
    return None


_CFWORKER_STATE = {}  # email -> metadata for poll

def _cfworker_headers():
    h = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "x-admin-auth": CFWORKER_ADMIN_TOKEN,
    }
    if CFWORKER_CUSTOM_AUTH:
        h["x-custom-auth"] = CFWORKER_CUSTOM_AUTH
    return h

def _cfworker_pick_domain():
    domains = list(CFWORKER_DOMAINS or ([] if not CFWORKER_DOMAIN else [CFWORKER_DOMAIN]))
    if not domains:
        raise RuntimeError("cfworker 未配置域名: 设置 CFWORKER_DOMAIN 或 CFWORKER_DOMAINS")
    mode = CFWORKER_DOMAIN_MODE
    if mode == "fixed" or len(domains) == 1:
        return domains[0]
    if mode == "random":
        return random.choice(domains)
    path = Path(CFWORKER_ROTATE_STATE)
    path.parent.mkdir(parents=True, exist_ok=True)
    idx = 0
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8") or "{}")
            idx = int(data.get("index") or 0)
    except Exception:
        idx = 0
    domain = domains[idx % len(domains)]
    try:
        path.write_text(
            json.dumps({"index": (idx + 1) % len(domains), "last": domain}, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception:
        pass
    return domain

def _cfworker_local_name():
    prefix = "".join(random.choices(string.ascii_lowercase, k=6))
    suffix = "".join(random.choices(string.digits, k=4))
    return f"{prefix}{suffix}"

def _cfworker_create():
    if not CFWORKER_API_URL:
        raise RuntimeError("cfworker 未配置 CFWORKER_API_URL / CF_TEMP_MAIL_BASE")
    if not CFWORKER_ADMIN_TOKEN:
        raise RuntimeError("cfworker 未配置 CFWORKER_ADMIN_TOKEN / CF_TEMP_MAIL_ADMIN")
    domain = _cfworker_pick_domain()
    name = _cfworker_local_name()
    payload = {
        "enablePrefix": bool(CFWORKER_ENABLE_PREFIX),
        "name": name,
        "domain": domain,
    }
    r = req.post(
        f"{CFWORKER_API_URL}/admin/new_address",
        headers=_cfworker_headers(),
        json=payload,
        timeout=20,
    )
    if r.status_code >= 400:
        raise RuntimeError(f"cfworker new_address HTTP {r.status_code}: {(r.text or '')[:200]}")
    data = r.json() if r.content else {}
    email = str(data.get("address") or data.get("email") or "").strip().lower()
    jwt = str(data.get("jwt") or data.get("token") or "").strip()
    address_id = data.get("address_id")
    if not email:
        raise RuntimeError(f"cfworker new_address 无 email: {data}")
    _CFWORKER_STATE[email] = {
        "jwt": jwt,
        "address_id": address_id,
        "domain": domain,
        "seen": set(),
    }
    return f"cf|{email}", email

def _cfworker_list_mails(email: str):
    st = _CFWORKER_STATE.get(email) or {}
    r = req.get(
        f"{CFWORKER_API_URL}/admin/mails",
        headers=_cfworker_headers(),
        params={"limit": 20, "offset": 0, "address": email},
        timeout=15,
    )
    if r.status_code >= 400 and st.get("jwt"):
        r = req.get(
            f"{CFWORKER_API_URL}/api/mails",
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {st['jwt']}",
            },
            params={"limit": 20, "offset": 0},
            timeout=15,
        )
    if r.status_code >= 400:
        raise RuntimeError(f"cfworker mails HTTP {r.status_code}: {(r.text or '')[:200]}")
    data = r.json() if r.content else {}
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return data.get("results") or data.get("mails") or data.get("items") or []
    return []

def _cfworker_fetch_text(email: str):
    mails = _cfworker_list_mails(email)
    if not mails:
        return None
    st = _CFWORKER_STATE.setdefault(email, {"seen": set()})
    seen = st.setdefault("seen", set())
    chunks = []
    for mail in mails:
        mid = str(mail.get("id") or mail.get("message_id") or "")
        raw = str(mail.get("raw") or "")
        subject = str(mail.get("subject") or "")
        body = str(mail.get("text") or mail.get("body") or "")
        html = str(mail.get("html") or "")
        source = str(mail.get("source") or mail.get("from") or "")
        chunks.append("\n".join([subject, source, body, html, raw]))
        if mid:
            seen.add(mid)
    return "\n".join(chunks) if chunks else None

def _mailtm_create(base, password):
    """mail.tm 同协议建箱;返回 (handle, email)。"""
    d = req.get(f'{base}/domains', timeout=12).json()
    d = d.get('hydra:member', d) if isinstance(d, dict) else d
    doms = [x['domain'] for x in d if x.get('isActive', True) and not x.get('isPrivate', False)]
    if not doms:
        raise RuntimeError('no domain')
    email = f'oc{secrets.token_hex(5)}@{doms[0]}'
    req.post(f'{base}/accounts', json={'address': email, 'password': password}, timeout=12)
    tok = req.post(f'{base}/token', json={'address': email, 'password': password}, timeout=12).json().get('token', '')
    if not tok:
        raise RuntimeError('no token')
    return f'mt|{base}|{tok}', email

def _lol_create():
    """tempmail.lol 建箱;返回 (handle, email)。"""
    r = req.post('https://api.tempmail.lol/v2/inbox/create', timeout=12).json()
    addr, tok = r.get('address', ''), r.get('token', '')
    if not addr or not tok:
        raise RuntimeError('lol create failed')
    return f'lol|{tok}', addr

def create_email():
    """custom / cfworker / tempmail 建箱。"""
    password = rand_str()
    if EMAIL_MODE == 'custom':
        email = f'oc{secrets.token_hex(5)}@{EMAIL_DOMAIN}'
        return email, email, password  # 地址即用,验证码经 CF Worker POST 到本地 webhook
    if EMAIL_MODE == 'cfworker':
        handle, email = _cfworker_create()
        return handle, email, password

    # 优先用已跑通的 mail.tm,其余按序仅作 fallback
    makers = [(lambda b=b: _mailtm_create(b, password)) for b in TEMPMAIL_BASES] + [_lol_create]
    for make in makers:
        try:
            handle, email = make()
            return handle, email, password
        except Exception:
            continue
    raise RuntimeError('所有临时邮箱 provider 均不可用')

def _tempmail_fetch(handle):
    """按 handle 前缀分派,取该邮箱当前邮件全文(subject+text+html);无则 None。"""
    kind = handle.split('|', 1)[0]
    if kind == 'cf':
        email = handle.split('|', 1)[1].strip().lower()
        return _cfworker_fetch_text(email)
    if kind == 'lol':
        tok = handle.split('|', 1)[1]
        data = req.get(f'https://api.tempmail.lol/v2/inbox?token={tok}', timeout=10).json()
        items = data.get('emails') or data.get('messages') or []
        if not items:
            return None
        return '\n'.join(f"{i.get('subject','')}\n{i.get('body','')}\n{i.get('html','')}"
                         for i in items if isinstance(i, dict))
    # mail.tm 同协议:handle = "mt|base|token"
    _, base, tok = handle.split('|', 2)
    hdr = {'Accept': 'application/json', 'Authorization': f'Bearer {tok}'}
    data = req.get(f'{base}/messages', headers=hdr, timeout=10).json()
    msgs = data if isinstance(data, list) else data.get('hydra:member', [])
    if not msgs:
        return None
    mid = str(msgs[0].get('id') or '')
    detail = req.get(f'{base}/messages/{mid}', headers=hdr, timeout=10).json()
    parts = [str(detail.get(k, '')) for k in ['subject', 'intro', 'text', 'html']]
    if isinstance(detail.get('html'), list):
        parts.append('\n'.join(str(x) for x in detail['html']))
    return '\n'.join(parts)

def poll_code(handle, max_wait=90):
    """轮询验证码:custom 查本地 webhook /check;tempmail 按 provider 取信。"""
    if EMAIL_MODE == 'custom':
        for _ in range(max_wait):
            time.sleep(1)
            try:
                resp = req.get(f'{LOCAL_EMAIL_API}/check/{handle}', timeout=5)
                if resp.status_code == 200 and resp.json().get('code'):
                    return resp.json()['code']
            except Exception:
                pass
        return None

    for _ in range(max_wait):
        time.sleep(1)
        try:
            text = _tempmail_fetch(handle)
            if text:
                code = _extract_code(text)
                if code:
                    return code
        except Exception:
            pass
    return None


async def _create_email_async(loop):
    """在线程池中创建邮箱,避免阻塞 asyncio 事件循环。"""
    return await loop.run_in_executor(POLL_EXECUTOR, create_email)


async def _poll_code_async(loop, handle):
    """在线程池中轮询验证码。"""
    return await loop.run_in_executor(POLL_EXECUTOR, poll_code, handle)


async def _acquire_many(sem, count):
    """一次预留多个许可；取消或异常时回滚已获取许可。"""
    acquired = 0
    try:
        for _ in range(count):
            await sem.acquire()
            acquired += 1
        return acquired
    except BaseException:
        for _ in range(acquired):
            sem.release()
        raise


class _NoopAsyncSemaphore:
    async def acquire(self):
        return True

    def release(self):
        return None


async def _send_q_request_batch(browser, physical_sem, p_send_sem, requests, metrics=None):
    """使用一个页面发送一批 Q 请求。

    返回每个请求的 sent 状态。等待 Q 返回不在此函数内发生,因此这里释放
    Physical_Sem 后不会占用本地重资源。
    """
    p_send_acquired = False
    physical_acquired = False
    physical_wait_started = None
    physical_hold_started = None
    page = None
    await p_send_sem.acquire()
    p_send_acquired = True
    try:
        physical_wait_started = time.time()
        await physical_sem.acquire()
        physical_acquired = True
        physical_hold_started = time.time()
        if metrics is not None:
            metrics.p_physical_count += 1
            metrics.p_physical_wait_seconds += physical_hold_started - physical_wait_started
        page = await browser.new_page()
        await _safe_set_viewport(page, 800, 600)
        try:
            stage_started = time.time()
            await _prepare_signup_page(page, redirect=True, timeout=30000)
            if metrics is not None:
                metrics.p_page_prepare_count += 1
                metrics.p_page_prepare_seconds += time.time() - stage_started
        except asyncio.CancelledError:
            raise
        except Exception:
            return [{**item, "sent": False} for item in requests]

        results = []
        send_started = time.time()
        for item in requests:
            sent = False
            try:
                sent = await grpc_create_code(page, item["email"])
            except asyncio.CancelledError:
                raise
            except Exception:
                sent = False
            results.append({**item, "sent": sent})
        if metrics is not None:
            metrics.p_send_count += 1
            metrics.p_send_seconds += time.time() - send_started
        return results
    finally:
        if page is not None:
            try:
                await page.close()
            except Exception:
                pass
        if physical_acquired:
            if metrics is not None and physical_hold_started is not None:
                metrics.p_physical_hold_seconds += time.time() - physical_hold_started
            physical_sem.release()
        if p_send_acquired:
            p_send_sem.release()


async def _poll_and_admit_q(
    request,
    inventory,
    q_pending_sem,
    q_slot_sem,
    metrics,
    *,
    q_batch_lease=None,
    admission_gate=None,
):
    """等待单个 Q 返回并入库；每个请求独立释放 pending/inflight。"""
    loop = asyncio.get_event_loop()
    release_reservation = True
    try:
        try:
            code = await asyncio.wait_for(
                _poll_code_async(loop, request["handle"]),
                timeout=P_REQUEST_TIMEOUT,
            )
        except asyncio.CancelledError:
            # poll_code 仍在运行时取消，底层轮询可能继续持有该请求；此处
            # 不能提前归还 pending/inflight，否则新请求会超量进入。
            release_reservation = False
            raise
        except asyncio.TimeoutError:
            code = None

        if code is None:
            metrics.q_discarded += 1
            return False

        metrics.q_returned += 1
        returned_at = time.time()
        q_env = None
        try:
            q_env = await ResourceEnvelope.create_with_slot(
                'Q',
                {
                    'email': request["email"],
                    'password': request["password"],
                    'code': code,
                    'handle': request.get("handle"),
                },
                q_slot_sem,
                expires_at=returned_at + Q_MAX_AGE,
            )
            await inventory.put_q(q_env)
            debug_log('[P] verification code admitted')
            return True
        except asyncio.CancelledError:
            if q_env is not None and not q_env.released:
                q_env.discard()
            raise
        except Exception:
            if q_env is not None and not q_env.released:
                q_env.discard()
            metrics.q_discarded += 1
            return False
    finally:
        if release_reservation:
            # poll 已终止（含超时/网络/解析异常），或 Q 已返回后任务在
            # 入库阶段取消：请求所有权已经回到本协程，必须归还许可。
            q_pending_sem.release()
            if q_batch_lease is not None:
                await q_batch_lease.release_one()
            if admission_gate is not None:
                await admission_gate.notify_changed()


def _observe_background_task(task):
    """Consume detached task failures so background settlement is not silent."""
    try:
        task.result()
    except asyncio.CancelledError:
        pass
    except Exception as e:
        debug_log(f'[P] background settle err: {sanitize_terminal_error(e)}')


# ──────────────────────────────────────────────
#  CSP Worker
# ──────────────────────────────────────────────

class _CHotPageLease:
    def __init__(self, browser, metrics=None):
        self.browser = browser
        self.metrics = metrics
        self.context = None
        self.page = None

    async def __aenter__(self):
        started = time.time()
        try:
            self.context, self.page = await _acquire_c_page(self.browser, self.metrics)
            return self.page
        finally:
            if self.metrics is not None:
                self.metrics.c_page_acquire_count += 1
                self.metrics.c_page_acquire_seconds += time.time() - started

    async def __aexit__(self, exc_type, exc, tb):
        await _release_c_page(self.context, self.page, healthy=exc_type is None)
        self.context = None
        self.page = None
        return False


_c_hot_page_pool = []
_c_hot_page_lock = asyncio.Lock()
_c_hot_page_pool_size = derive_c_hot_page_pool_size(PHYSICAL_CAP or 1, C_WORKERS or 1)


async def _new_c_hot_page(browser):
    context = await browser.new_context(**new_browser_context_kwargs())
    page = await context.new_page()
    await _prepare_signup_page(page, redirect=True, timeout=30000)
    return context, page


async def _acquire_c_page(browser, metrics=None):
    """Create an isolated page for C.

    Camoufox can hang forever on new_context/new_page. Always bound with timeout
    and prefer direct page order: browser.new_page(no_viewport=True) first.
    """
    if browser is None:
        raise RuntimeError("browser is None; cannot acquire C page")

    if C_HOT_PAGE_POOL:
        async with _c_hot_page_lock:
            if _c_hot_page_pool:
                if metrics is not None:
                    metrics.c_hot_page_hits += 1
                return _c_hot_page_pool.pop()
        if metrics is not None:
            metrics.c_hot_page_misses += 1
        return await asyncio.wait_for(_new_c_hot_page(browser), timeout=45)

    page_timeout = float(os.environ.get("C_PAGE_ACQUIRE_TIMEOUT", "45") or "45")

    async def _create():
        debug_log("[C] page-acquire start")
        kwargs = new_browser_context_kwargs()
        # 1) browser-native direct new_page(no_viewport=True)
        try:
            page = await browser.new_page(no_viewport=True)
            context = page.context
            debug_log("[C] page-acquire via browser.new_page(no_viewport=True)")
        except TypeError:
            page = None
            context = None
        except Exception as exc:
            debug_log(f"[C] page-acquire direct new_page err={type(exc).__name__}")
            page = None
            context = None

        # 2) context -> page
        if page is None:
            try:
                context = await browser.new_context(**kwargs)
                page = await context.new_page()
                debug_log("[C] page-acquire via new_context+new_page")
            except TypeError:
                try:
                    slim = dict(kwargs)
                    slim.pop("no_viewport", None)
                    slim.pop("viewport", None)
                    context = await browser.new_context(**slim)
                    page = await context.new_page()
                    debug_log("[C] page-acquire via slim new_context")
                except Exception as exc:
                    debug_log(f"[C] page-acquire context err={type(exc).__name__}")
                    context = await browser.new_context()
                    page = await context.new_page()
                    debug_log("[C] page-acquire via bare new_context")
            except Exception as exc:
                debug_log(f"[C] page-acquire context fallback err={type(exc).__name__}")
                page = await browser.new_page()
                context = page.context
                debug_log("[C] page-acquire via bare browser.new_page")

        # UI mode owns navigation in register_via_ui; warm-up goto can crash Camoufox
        # and waste the only browser process. Keep warm-up for server_action path only.
        if REGISTER_MODE not in ("ui",):
            await _prepare_signup_page(page, redirect=True, timeout=30000)
        debug_log("[C] page-acquire ready")
        return context, page

    try:
        return await asyncio.wait_for(_create(), timeout=max(10.0, page_timeout))
    except asyncio.TimeoutError:
        debug_log(f"[C] page-acquire TIMEOUT after {page_timeout}s")
        raise RuntimeError(f"C page acquire timeout after {page_timeout}s")


async def _release_c_page(context, page, *, healthy):
    if not C_HOT_PAGE_POOL or context is None:
        try:
            if context is not None:
                await context.close()
            else:
                await page.close()
        except Exception:
            try:
                await page.close()
            except Exception:
                pass
        return

    if healthy:
        try:
            if "/sign-up" not in (getattr(page, "url", "") or ""):
                healthy = False
            else:
                await context.clear_cookies()
                await page.evaluate(
                    "() => { try { localStorage.clear(); sessionStorage.clear(); } catch (e) {} }"
                )
                async with _c_hot_page_lock:
                    if len(_c_hot_page_pool) < _c_hot_page_pool_size:
                        _c_hot_page_pool.append((context, page))
                        return
        except asyncio.CancelledError:
            try:
                await context.close()
            except Exception:
                pass
            raise
        except Exception:
            pass

    try:
        await context.close()
    except Exception:
        try:
            await page.close()
        except Exception:
            pass


def _c_page_lease(browser, metrics=None):
    return _CHotPageLease(browser, metrics)


async def _close_c_hot_page_pool():
    async with _c_hot_page_lock:
        items = list(_c_hot_page_pool)
        _c_hot_page_pool.clear()
    for context, page in items:
        try:
            await context.close()
        except Exception:
            try:
                await page.close()
            except Exception:
                pass


async def s_worker(wid, browser, inventory, physical_sem, t_slot_sem, metrics, admission_gate=None):
    """S_Worker: 生成 T 并入库。"""
    while not STOP.is_set():
        t_lease = None
        try:
            if admission_gate is not None:
                t_lease = await admission_gate.acquire_t_production()

            physical_wait_started = time.time()
            await physical_sem.acquire()
            physical_hold_started = time.time()
            metrics.s_physical_count += 1
            metrics.s_physical_wait_seconds += physical_hold_started - physical_wait_started
            token = None
            trace = {}
            solve_started = time.time()
            try:
                try:
                    # UI path: page-native Turnstile/Castle. Dummy T only unblocks claim_pair.
                    if REGISTER_MODE in ("ui",):
                        token = f"ui-native-{secrets.token_hex(8)}"
                        trace = {
                            "goto_s": 0.0,
                            "inject_s": 0.0,
                            "initial_s": 0.0,
                            "click_s": 0.0,
                            "wait_s": 0.0,
                            "reused": False,
                            "visible_frame": True,
                        }
                        await asyncio.sleep(0.05)
                    else:
                        token, trace = await asyncio.wait_for(
                            solve_one_turnstile_with_trace(browser),
                            timeout=SOLVER_HARD_TIMEOUT,
                        )
                except asyncio.TimeoutError:
                    debug_log(f'[S] {wid} solver timeout after {SOLVER_HARD_TIMEOUT}s')
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    debug_log(f'[S] {wid} solver error: {sanitize_terminal_error(exc)}')
            finally:
                solve_elapsed = time.time() - solve_started
                _record_solver_trace(metrics, trace, solve_elapsed, token)
                metrics.s_physical_hold_seconds += time.time() - physical_hold_started
                physical_sem.release()

            if token is None:
                metrics.t_discarded += 1
                if t_lease is not None:
                    await t_lease.release()
                    t_lease = None
                await asyncio.sleep(0.5)
                continue

            metrics.t_produced += 1
            now = time.time()
            t_env = None
            try:
                t_env = await ResourceEnvelope.create_with_slot(
                    'T', token, t_slot_sem, expires_at=now + T_MAX_AGE
                )
                await inventory.put_t(t_env)
                if admission_gate is not None:
                    await admission_gate.notify_changed()
            except asyncio.CancelledError:
                if t_env is not None and not t_env.released:
                    t_env.discard()
                metrics.t_discarded += 1
                raise
            except Exception:
                if t_env is not None and not t_env.released:
                    t_env.discard()
                metrics.t_discarded += 1
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # A single browser/page failure must not terminate the permanent
            # producer.  The next iteration acquires a fresh solver page.
            metrics.t_discarded += 1
            debug_log(f'[S] {wid} worker error: {sanitize_terminal_error(exc)}')
        finally:
            if t_lease is not None:
                await t_lease.release()
        await asyncio.sleep(0.2)


async def p_worker(
    wid,
    browser,
    inventory,
    physical_sem,
    q_pending_sem,
    q_slot_sem,
    metrics,
    admission_gate=None,
    p_send_sem=None,
    max_batch=1,
):
    """P_Worker: 创建邮箱 + 发码 + 轮询 + 入库。"""
    loop = asyncio.get_event_loop()
    p_send_sem = p_send_sem or _NoopAsyncSemaphore()
    while not STOP.is_set():
        q_lease = None
        pending_owned = 0
        settle_tasks = []
        try:
            if admission_gate is not None:
                q_lease = await admission_gate.acquire_q_batch(max_batch=max_batch)
                batch_count = q_lease.count
            else:
                batch_count = 1

            pending_owned = await _acquire_many(q_pending_sem, batch_count)

            requests = []
            for _ in range(batch_count):
                email_started = time.time()
                try:
                    handle, email, password = await _create_email_async(loop)
                    metrics.p_email_create_count += 1
                    metrics.p_email_create_seconds += time.time() - email_started
                    requests.append({"handle": handle, "email": email, "password": password})
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    metrics.p_email_create_count += 1
                    metrics.p_email_create_seconds += time.time() - email_started
                    debug_log(f'[P] {wid} create email err: {sanitize_terminal_error(e)}')
                    metrics.q_discarded += 1
                    q_pending_sem.release()
                    pending_owned -= 1
                    if q_lease is not None:
                        await q_lease.release_one()

            if not requests:
                continue

            # UI mode: mailbox only. Form submit issues the verification code so
            # page-native Castle can mint a valid request token on real signup.
            if REGISTER_MODE in ("ui",):
                for item in requests:
                    admitted = False
                    try:
                        returned_at = time.time()
                        q_env = await ResourceEnvelope.create_with_slot(
                            'Q',
                            {
                                'email': item["email"],
                                'password': item["password"],
                                'code': '',
                                'handle': item.get("handle"),
                            },
                            q_slot_sem,
                            expires_at=returned_at + Q_MAX_AGE,
                        )
                        await inventory.put_q(q_env)
                        metrics.q_sent += 1
                        metrics.q_returned += 1
                        pending_owned -= 1
                        q_pending_sem.release()
                        if q_lease is not None:
                            await q_lease.release_one()
                        if admission_gate is not None:
                            await admission_gate.notify_changed()
                        admitted = True
                        debug_log('[P] ui-mode mailbox admitted (code deferred to C-UI)')
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        debug_log(f'[P] {wid} ui mailbox admit err: {sanitize_terminal_error(e)}')
                        metrics.q_discarded += 1
                        if not admitted:
                            q_pending_sem.release()
                            pending_owned -= 1
                            if q_lease is not None:
                                await q_lease.release_one()
                continue

            results = await _send_q_request_batch(
                browser, physical_sem, p_send_sem, requests, metrics
            )
            metrics.q_send_batches += 1
            metrics.q_send_batch_items += len(results)

            for item in results:
                if not item["sent"]:
                    metrics.q_discarded += 1
                    q_pending_sem.release()
                    pending_owned -= 1
                    if q_lease is not None:
                        await q_lease.release_one()
                    continue

                metrics.q_sent += 1
                pending_owned -= 1
                task = asyncio.create_task(
                    _poll_and_admit_q(
                        item,
                        inventory,
                        q_pending_sem,
                        q_slot_sem,
                        metrics,
                        q_batch_lease=q_lease,
                        admission_gate=admission_gate,
                    )
                )
                task.add_done_callback(_observe_background_task)
                settle_tasks.append(task)

            if settle_tasks:
                await asyncio.gather(*(asyncio.shield(task) for task in settle_tasks))

        except asyncio.CancelledError:
            for _ in range(pending_owned):
                q_pending_sem.release()
                if q_lease is not None:
                    await q_lease.release_one()
            raise
        except Exception as e:
            debug_log(f'[P] {wid} err: {sanitize_terminal_error(e)}')
            metrics.q_discarded += 1
            for _ in range(pending_owned):
                q_pending_sem.release()
                if q_lease is not None:
                    await q_lease.release_one()
        await asyncio.sleep(0.2)


def _pair_is_expired(pair, now=None):
    """检查已 claim 的 T/Q 是否在等待期间失效。"""
    now = time.time() if now is None else now
    return any(
        bool(check(now))
        for envelope in (pair.t, pair.q)
        if (check := getattr(envelope, "is_expired", None)) is not None
    )


def _append_registration_line(path, line, mode=None, *, durable=False):
    """追加一行到 keys/ 文件。带跨进程文件锁，防止多 worker / 双批次并发写交错。"""
    try:
        import scripts.append_locked as al

        al.locked_append(path, line, durable=durable)
    except Exception:
        # 锁不可用（scripts 不在 sys.path 等）时回退普通追加，不阻塞注册
        with open(path, "a") as stream:
            stream.write(line)
            if durable:
                stream.flush()
                os.fsync(stream.fileno())
    if mode is not None:
        os.chmod(path, mode)


def _persist_registration(email, password, sso, session_cookies):
    if session_cookies or sso:
        document = json.dumps(
            {
                "email": email,
                "sso": sso,
                "cookies": session_cookies or [],
            },
            separators=(",", ":"),
        )
        _append_registration_line(
            "keys/auth-sessions.jsonl",
            document + "\n",
            mode=0o600,
            durable=True,
        )
    _append_registration_line("keys/accounts.txt", f"{email}:{password}:{sso}\n")
    _append_registration_line("keys/grok.txt", sso + "\n")


async def _consume_pair(
    browser,
    physical_sem,
    pair,
    metrics,
    task_id=None,
    recovery_probe=None,
):
    """执行一次 C 消费。返回 True 表示业务成功,False 表示消费失败。"""
    global success_count
    email = pair.q.value['email']
    password = pair.q.value['password']
    code = pair.q.value['code']
    token = pair.t.value

    # c_worker 在单次消费超时开始前完成冷却等待。保留这里的默认路径，
    # 供直接调用者使用，同时避免把 60 秒冷却计入 60 秒消费超时。
    if recovery_probe is None:
        recovery_probe = await REGISTRATION_RATE_LIMIT_CIRCUIT.wait()

    # 直接调用路径也可能在 circuit.wait() 中跨过资源有效期。尚未开始
    # 注册时只让出探针，不把本地过期误判成一次恢复探测失败。
    if _pair_is_expired(pair):
        if recovery_probe:
            REGISTRATION_RATE_LIMIT_CIRCUIT.release_probe(recovery_probe)
        return False

    physical_acquired = False
    physical_hold_started = None
    recovery_completed = False
    try:
        physical_wait_started = time.time()
        await physical_sem.acquire()
        physical_acquired = True
        physical_hold_started = time.time()
        metrics.c_physical_count += 1
        metrics.c_physical_wait_seconds += physical_hold_started - physical_wait_started
        debug_log("[C] physical acquired")
        t0 = time.time()
        sso = None
        session_cookies = []
        try:
            # UI Sync path must NOT open shared AsyncCamoufox pages first.
            # That page-acquire was hanging forever (phys:0, no [C-UI] logs).
            use_ui = REGISTER_MODE in ("ui", "auto")
            use_sync_first = use_ui and UI_SYNC_CAMOUFOX
            handle = None
            try:
                handle = pair.q.value.get("handle")
            except Exception:
                handle = None
            loop = asyncio.get_event_loop()

            async def _refresh_code():
                if not handle:
                    return None
                try:
                    return await loop.run_in_executor(
                        POLL_EXECUTOR,
                        lambda: poll_code(handle, max_wait=8),
                    )
                except Exception:
                    return None

            def _refresh_code_sync():
                if not handle:
                    return None
                try:
                    return poll_code(handle, max_wait=8)
                except Exception:
                    return None

            registration = None
            verified = True
            if not use_ui:
                # server_action still needs a page for grpc verify + submit
                async with _c_page_lease(browser, metrics) as page:
                    verify_started = time.time()
                    try:
                        verified = await grpc_verify_code(page, email, code)
                    finally:
                        metrics.c_verify_count += 1
                        metrics.c_verify_seconds += time.time() - verify_started
                    if (
                        verified
                        and not _pair_is_expired(pair)
                        and REGISTRATION_RATE_LIMIT_CIRCUIT.can_submit(recovery_probe)
                    ):
                        register_started = time.time()
                        try:
                            registration = await server_action_register(
                                page,
                                email,
                                password,
                                code,
                                token,
                                include_session=True,
                            )
                            if registration:
                                sso, session_cookies = registration
                        finally:
                            metrics.c_register_count += 1
                            metrics.c_register_seconds += time.time() - register_started
            else:
                metrics.c_verify_count += 1
                if (
                    verified
                    and not _pair_is_expired(pair)
                    and REGISTRATION_RATE_LIMIT_CIRCUIT.can_submit(recovery_probe)
                ):
                    register_started = time.time()
                    try:
                        ui_sso = None
                        if use_sync_first:
                            debug_log("[C-UI] using Sync Camoufox path (no shared page lease)")
                            ui_sso = await loop.run_in_executor(
                                None,
                                lambda: register_via_ui_sync(
                                    email=email,
                                    password=password,
                                    code=code or "",
                                    handle=str(handle or ""),
                                    refresh_code=_refresh_code_sync if handle else None,
                                    log=debug_log,
                                ),
                            )
                            if ui_sso:
                                debug_log(f"[C-UI] sync sso len={len(ui_sso)}")
                            else:
                                debug_log("[C-UI] sync path failed; fallback async UI")

                        if not ui_sso:
                            if browser is None:
                                debug_log("[C-UI] no async browser for fallback")
                            else:
                                async with _c_page_lease(browser, metrics) as page:
                                    ui_sso = await register_via_ui(
                                        page,
                                        email=email,
                                        password=password,
                                        code=code or "",
                                        turnstile_token=token,
                                        refresh_code=_refresh_code if handle else None,
                                        log=debug_log,
                                    )
                                    if ui_sso:
                                        sso2, cookies2, act_details = await finalize_registered_account(
                                            page, ui_sso
                                        )
                                        if CASTLE_REQUIRED and _account_is_bot_flagged(act_details):
                                            debug_log(
                                                f"[C] UI bot-flagged botFlag={act_details.get('bot_flag') or '-'} "
                                                f"details={act_details.get('bot_flag_details') or '-'} -> discard"
                                            )
                                            try:
                                                _persist_dirty_account(
                                                    email, password, sso2 or ui_sso, act_details
                                                )
                                            except Exception:
                                                pass
                                            ui_sso = None
                                        else:
                                            registration = (sso2 or ui_sso, cookies2 or [])
                                            ui_sso = None  # already packaged

                        if ui_sso and registration is None:
                            # Sync succeeded: finalize on a short-lived page if possible,
                            # otherwise keep raw SSO so we do not lose a good account.
                            finalized = False
                            if browser is not None:
                                try:
                                    async with _c_page_lease(browser, metrics) as page:
                                        sso2, cookies2, act_details = await finalize_registered_account(
                                            page, ui_sso
                                        )
                                        if CASTLE_REQUIRED and _account_is_bot_flagged(act_details):
                                            debug_log(
                                                f"[C] UI bot-flagged botFlag={act_details.get('bot_flag') or '-'} "
                                                f"details={act_details.get('bot_flag_details') or '-'} -> discard"
                                            )
                                            try:
                                                _persist_dirty_account(
                                                    email, password, sso2 or ui_sso, act_details
                                                )
                                            except Exception:
                                                pass
                                        else:
                                            registration = (sso2 or ui_sso, cookies2 or [])
                                            finalized = True
                                except Exception as exc:
                                    debug_log(
                                        f"[C-UI] finalize page skipped: {type(exc).__name__}; keep raw sso"
                                    )
                            if not finalized and registration is None:
                                registration = (ui_sso, [])

                        if registration is None and REGISTER_MODE == "auto":
                            # last-resort server_action if UI failed
                            if browser is not None:
                                async with _c_page_lease(browser, metrics) as page:
                                    local_code = code or ""
                                    if not local_code and handle:
                                        try:
                                            local_code = await loop.run_in_executor(
                                                POLL_EXECUTOR,
                                                lambda: poll_code(handle, max_wait=30),
                                            ) or ""
                                        except Exception:
                                            local_code = ""
                                    if local_code:
                                        try:
                                            await grpc_verify_code(page, email, local_code)
                                        except Exception:
                                            pass
                                    registration = await server_action_register(
                                        page,
                                        email,
                                        password,
                                        local_code,
                                        token,
                                        include_session=True,
                                    )

                        if registration:
                            sso, session_cookies = registration
                    finally:
                        metrics.c_register_count += 1
                        metrics.c_register_seconds += time.time() - register_started
        except RegistrationRateLimited:
            if REGISTRATION_RATE_LIMIT_CIRCUIT.trip():
                log(
                    format_user_registration_event(
                        "rate_limited",
                        wait_seconds=REGISTRATION_RATE_LIMIT_CIRCUIT.remaining_seconds(),
                    )
                )
            return False

        if sso:
            elapsed = time.time() - t0
            async with file_lock:
                _persist_registration(email, password, sso, session_cookies)
                metrics.record_success()
                success_count = metrics.success_count
                count = metrics.success_count
            log(
                format_user_registration_event(
                    "success",
                    task_id=task_id,
                    count=count,
                    rate_per_minute=metrics.runtime_average_success_rate(),
                )
            )
            if recovery_probe:
                recovered_after = REGISTRATION_RATE_LIMIT_CIRCUIT.consume_recovery_seconds(
                    recovery_probe
                )
                recovery_completed = True
                if recovered_after is not None:
                    log(
                        format_user_registration_event(
                            "recovered", wait_seconds=round(recovered_after)
                        )
                    )
            return True

        log(format_user_registration_event("failed", task_id=task_id))
        return False
    finally:
        if recovery_probe and not recovery_completed:
            REGISTRATION_RATE_LIMIT_CIRCUIT.defer_probe(recovery_probe)
        if physical_acquired:
            metrics.c_physical_hold_seconds += time.time() - physical_hold_started
            physical_sem.release()



_BROWSER_DEAD_STREAK = 0
_BROWSER_DEAD_STOP_AFTER = int(os.environ.get("BROWSER_DEAD_STOP_AFTER") or "2")


def _is_browser_closed_error(exc) -> bool:
    name = type(exc).__name__
    msg = str(exc or "")
    low = msg.lower()
    return (
        "TargetClosed" in name
        or "Target closed" in msg
        or "browser has been closed" in low
        or "context or browser has been closed" in low
        or "connection closed" in low
        or "target page, context or browser has been closed" in low
    )


async def c_worker(wid, browser, inventory, physical_sem, metrics, admission_gate=None):
    """C_Worker: claim pair 并执行注册。"""
    global _BROWSER_DEAD_STREAK
    while not STOP.is_set():
        recovery_probe = False
        task_id = None
        try:
            async with inventory.claim_pair() as pair:
                if admission_gate is not None:
                    await admission_gate.notify_changed()

                # 必须先 claim，再通过冷却闸门。否则多个 worker 会在限流前
                # 通过闸门，随后于冷却期陆续拿到 pair 并漏闸。等待发生在
                # wait_for 之外，因此不计入单次 C 消费超时。
                recovery_probe = await REGISTRATION_RATE_LIMIT_CIRCUIT.wait()
                if _pair_is_expired(pair):
                    if recovery_probe:
                        REGISTRATION_RATE_LIMIT_CIRCUIT.release_probe(recovery_probe)
                        recovery_probe = False
                    continue

                task_id = metrics.next_registration_task()
                log(
                    format_user_registration_event(
                        "started",
                        task_id=task_id,
                        remaining=max(TARGET - metrics.success_count, 0) if TARGET else None,
                    )
                )
                try:
                    ok = await asyncio.wait_for(
                        _consume_pair(
                            browser,
                            physical_sem,
                            pair,
                            metrics,
                            task_id=task_id,
                            recovery_probe=recovery_probe,
                        ),
                        timeout=C_CONSUME_TIMEOUT,
                    )
                    if ok:
                        metrics.pair_consumed_ok += 1
                        _BROWSER_DEAD_STREAK = 0
                    else:
                        metrics.pair_consumed_fail += 1
                except asyncio.TimeoutError:
                    metrics.pair_consumed_fail += 1
                    log(format_user_registration_event("failed", task_id=task_id))

        except asyncio.CancelledError:
            raise
        except Exception as e:
            if task_id is not None:
                log(format_user_registration_event("failed", task_id=task_id))
            debug_log(f'[C] {wid} err: {sanitize_terminal_error(e)}')
            metrics.pair_consumed_fail += 1
            if _is_browser_closed_error(e):
                _BROWSER_DEAD_STREAK += 1
                debug_log(
                    f"[C] browser/page closed streak={_BROWSER_DEAD_STREAK}/"
                    f"{_BROWSER_DEAD_STOP_AFTER}"
                )
                if _BROWSER_DEAD_STREAK >= max(1, _BROWSER_DEAD_STOP_AFTER):
                    log(
                        "[!] 浏览器已崩溃/关闭，停止继续注册，避免空转烧邮箱。"
                        "请重新运行 start.sh / register。"
                    )
                    STOP.set()
            else:
                _BROWSER_DEAD_STREAK = 0
        finally:
            if recovery_probe:
                # 覆盖等待 pair、获取物理许可或其他边界处的取消/异常。
                # 已成功或已延期的 probe 在这里会是幂等 no-op。
                REGISTRATION_RATE_LIMIT_CIRCUIT.defer_probe(recovery_probe)
        await asyncio.sleep(0.2)


# ──────────────────────────────────────────────
#  只读监控
# ──────────────────────────────────────────────
async def monitor(inventory, sems, metrics, interval=8):
    """定期输出系统状态。"""
    while not STOP.is_set():
        await asyncio.sleep(interval)
        if REGISTER_LOG_MODE == "debug":
            log(metrics.snapshot(inventory, sems))
        if TARGET and metrics.success_count >= TARGET:
            log(f'[*] 已达目标 {TARGET} 个,停止。'); STOP.set()


# ──────────────────────────────────────────────
#  主入口
# ──────────────────────────────────────────────
async def main():
    global TARGET, _c_hot_page_pool_size
    max_mem_arg = None
    for i, arg in enumerate(sys.argv[1:], 1):
        if arg == '--max-mem' and i + 1 < len(sys.argv):
            max_mem_arg = sys.argv[i + 1]
        elif arg == '--target' and i + 1 < len(sys.argv):
            TARGET = int(sys.argv[i + 1])

    resources = get_system_resources(max_mem_arg)
    cpu = resources['cpu']
    capacity_profile = load_capacity_profile()

    # 自动派生容量
    physical_cap, s_workers, p_workers, c_workers = derive_capacity(
        cpu,
        resources['max_mem'],
        profile_physical_cap=capacity_profile.get("physical_cap"),
    )
    p_send_cap = P_SEND_CAP if P_SEND_CAP > 0 else 0
    admission_watermarks = derive_admission_watermarks(physical_cap)
    _c_hot_page_pool_size = derive_c_hot_page_pool_size(physical_cap, c_workers)

    # 校验邮箱模式配置
    if EMAIL_MODE not in ('tempmail', 'custom', 'cfworker'):
        log("[!] 配置错误：EMAIL_MODE 应为 tempmail / custom / cfworker"); return 2
    if EMAIL_MODE == 'custom' and not EMAIL_DOMAIN:
        log("[!] 配置错误：custom 模式需在 .env 设置 EMAIL_DOMAIN"); return 2
    if EMAIL_MODE == 'cfworker':
        if not CFWORKER_API_URL:
            log("[!] 配置错误：cfworker 模式需 CFWORKER_API_URL 或 CF_TEMP_MAIL_BASE"); return 2
        if not CFWORKER_ADMIN_TOKEN:
            log("[!] 配置错误：cfworker 模式需 CFWORKER_ADMIN_TOKEN 或 CF_TEMP_MAIL_ADMIN"); return 2
        if not CFWORKER_DOMAINS and not CFWORKER_DOMAIN:
            log("[!] 配置错误：cfworker 模式需 CFWORKER_DOMAIN 或 CFWORKER_DOMAINS"); return 2

    debug_log("=" * 50)
    debug_log(f"  Grok Free Register (CSP Architecture)")
    debug_log(f"  CPU: {cpu} cores  Memory: {resources['available_mem']}/{resources['total_mem']}MB")
    debug_log(f"  MaxMemForAuto: {resources['max_mem']}MB  MemReserve: {MIN_FREE_MEM_MB}MB  PhysicalMemBudget: {PHYSICAL_MEM_MB}MB")
    if capacity_profile:
        debug_log(f"  CapacityProfile: {CAPACITY_PROFILE} physical_cap={capacity_profile.get('physical_cap')}")
    debug_log(f"  EmailMode: {EMAIL_MODE}")
    debug_log(f"  RegisterMode: {REGISTER_MODE}")
    debug_log(f"  BrowserEngine: {backend_browser_engine()}")
    if EMAIL_MODE == 'cfworker':
        debug_log(
            f"  CFWorker: {CFWORKER_API_URL} domains={','.join(CFWORKER_DOMAINS or [CFWORKER_DOMAIN])} "
            f"mode={CFWORKER_DOMAIN_MODE} prefix={int(CFWORKER_ENABLE_PREFIX)}"
        )
    elif EMAIL_MODE == 'custom':
        debug_log(f"  CustomEmail: domain={EMAIL_DOMAIN} api={LOCAL_EMAIL_API}")
    debug_log(f"  Physical_Sem={physical_cap}  T_Slot={T_SLOT_CAP}  Q_Slot={Q_SLOT_CAP}  Q_Pending={Q_PENDING_CAP}")
    debug_log(
        f"  Admission: T_LOW/HIGH={admission_watermarks['t_low']}/{admission_watermarks['t_high']}  "
        f"Q_LOW/HIGH={admission_watermarks['q_low']}/{admission_watermarks['q_high']}"
    )
    debug_log(f"  P_BatchMax={P_BATCH_MAX}  P_Send_Sem={'disabled' if p_send_cap == 0 else p_send_cap}")
    debug_log(
        f"  C_HotPagePool={'on' if C_HOT_PAGE_POOL else 'off'}"
        f" size={_c_hot_page_pool_size if C_HOT_PAGE_POOL else 0}"
        f" setCookieViaRequest={'on' if C_SET_COOKIE_VIA_REQUEST else 'off'}"
    )
    debug_log(f"  Workers: S={s_workers} P={p_workers} C={c_workers}")
    debug_log(
        f"  Timeouts: Solver={SOLVER_HARD_TIMEOUT}s SolverCleanup={SOLVER_CLEANUP_TIMEOUT}s "
        f"P_Request={P_REQUEST_TIMEOUT}s C_Consume={C_CONSUME_TIMEOUT}s"
    )
    debug_log(
        f"  SolverMouseClick: retries={SOLVER_MOUSE_CLICK_RETRIES} "
        f"interval={SOLVER_MOUSE_CLICK_INTERVAL_MS}ms"
    )
    if TARGET:
        debug_log(f"  Target: {TARGET}")
    debug_log("=" * 50)

    await fetch_config()

    # UI Sync path uses its own Sync Camoufox. Avoid launching a shared
    # AsyncCamoufox that can hang on page-acquire and contend with Sync.
    skip_shared_browser = REGISTER_MODE in ("ui",) and UI_SYNC_CAMOUFOX

    class _NullBrowserCM:
        async def __aenter__(self):
            return None
        async def __aexit__(self, *args):
            return False

    browser_cm = (
        _NullBrowserCM()
        if skip_shared_browser
        else launch_browser_bundle(log=debug_log)
    )

    async with browser_cm as bundle:
        if bundle is None:
            browser = None
            debug_log("[*] Browser skipped (UI_SYNC_CAMOUFOX owns Camoufox)")
        else:
            browser = bundle.browser
            debug_log(
                f"[*] Browser engine={bundle.engine} headless={browser_headless()} "
                f"proxy={(browser_proxy_settings() or {}).get('server') or 'off'}"
            )

        metrics = Metrics()
        inventory = Inventory(metrics=metrics)
        admission_gate = AdmissionGate(
            inventory,
            t_low=admission_watermarks["t_low"],
            t_high=admission_watermarks["t_high"],
            q_low=admission_watermarks["q_low"],
            q_high=admission_watermarks["q_high"],
        )
        physical_sem = asyncio.Semaphore(physical_cap)
        p_send_sem = asyncio.Semaphore(p_send_cap) if p_send_cap > 0 else None
        t_slot_sem = asyncio.Semaphore(T_SLOT_CAP)
        q_slot_sem = asyncio.Semaphore(Q_SLOT_CAP)
        q_pending_sem = asyncio.Semaphore(Q_PENDING_CAP)

        sems = {
            'physical': physical_sem,
            't_slot': t_slot_sem,
            'q_slot': q_slot_sem,
            'q_pending': q_pending_sem,
            'admission': admission_gate,
        }
        if p_send_sem is not None:
            sems['p_send'] = p_send_sem

        tasks = []

        # S_Workers
        for i in range(s_workers):
            tasks.append(asyncio.create_task(
                s_worker(i, browser, inventory, physical_sem, t_slot_sem, metrics, admission_gate)
            ))

        # P_Workers
        for i in range(p_workers):
            tasks.append(asyncio.create_task(
                p_worker(
                    i,
                    browser,
                    inventory,
                    physical_sem,
                    q_pending_sem,
                    q_slot_sem,
                    metrics,
                    admission_gate,
                    p_send_sem,
                    P_BATCH_MAX,
                )
            ))

        # C_Workers
        for i in range(c_workers):
            tasks.append(asyncio.create_task(
                c_worker(i, browser, inventory, physical_sem, metrics, admission_gate)
            ))

        # Monitor
        tasks.append(asyncio.create_task(monitor(inventory, sems, metrics)))

        debug_log(f'[*] CSP up: S={s_workers} P={p_workers} C={c_workers} workers')
        log(
            format_user_registration_event(
                "service_started",
                remaining=max(TARGET - metrics.success_count, 0) if TARGET else None,
            )
        )

        try:
            await asyncio.gather(*tasks)
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            for t in tasks:
                t.cancel()
            await _close_c_hot_page_pool()
            # browser lifecycle owned by launch_browser_bundle
            log(format_user_registration_event("stopped", count=metrics.success_count))
    return 0

if __name__ == "__main__":
    try:
        REGISTER_LOG_MODE = resolve_register_log_mode(sys.argv[1:])
    except ValueError:
        log("[!] 配置错误：REGISTER_LOG_MODE 应为 user 或 debug")
        raise SystemExit(2)
    try:
        exit_code = asyncio.run(main())
    except ValueError as exc:
        log(f"[!] 配置错误：{sanitize_terminal_error(exc)}")
        exit_code = 2
    raise SystemExit(exit_code)
