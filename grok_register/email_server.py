"""
邮件接收 API 服务器
====================
接收 Cloudflare Email Routing 转发的邮件，供注册服务查询。

端点:
  POST /webhook          — Cloudflare 转发邮件到这里
  GET  /check/<email>    — 查询某邮箱的验证码
  GET  /domains          — 返回可用域名
  GET  /health           — 健康检查

用法:
  bash start.sh --email-service
  EMAIL_DOMAIN=your.domain bash start.sh --email-service --port 8080
"""
import os, re, json, time, sys
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass
from http.server import HTTPServer, ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, unquote
from threading import Lock

# 配置
DEFAULT_DOMAIN = os.environ.get("EMAIL_DOMAIN", "")
DEFAULT_PORT = 8080

# 存储
emails = {}  # {email_address: [{"code": "ABC123", "time": timestamp, "raw": "..."}]}
emails_lock = Lock()

# 清理过期邮件（5 分钟）
def cleanup_old():
    now = time.time()
    with emails_lock:
        for addr in list(emails.keys()):
            emails[addr] = [e for e in emails[addr] if now - e['time'] < 300]
            if not emails[addr]:
                del emails[addr]


def normalize_email(value):
    """提取纯邮箱地址，兼容 Name <a@b.com> / 多地址 / 大小写。"""
    if value is None:
        return ""
    if isinstance(value, list):
        for item in value:
            got = normalize_email(item)
            if got:
                return got
        return ""
    if isinstance(value, dict):
        return normalize_email(value.get("address") or value.get("email") or value.get("value") or "")
    text = str(value).strip()
    m = re.search(r'([A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,})', text, re.I)
    return (m.group(1) if m else text).strip().lower()


def extract_code(text):
    """从邮件内容提取验证码（兼容 x.ai / HTML / 纯文本）"""
    if not text:
        return None
    # 常见: ABC-DEF / ABCDEF
    for pat in (
        r'>([A-Z0-9]{3}-[A-Z0-9]{3})<',
        r'>([A-Z0-9]{6})<',
        r'\b([A-Z0-9]{3}-[A-Z0-9]{3})\b',
        r'(?i)(?:code|验证码|verification code|one[- ]time)[^\nA-Z0-9]{0,40}([A-Z0-9]{3}-?[A-Z0-9]{3})\b',
        r'(?i)(?:code|验证码|verification)[^\nA-Z0-9]{0,40}([A-Z0-9]{6})\b',
        r'(?<![A-Z0-9])([A-Z0-9]{6})(?![A-Z0-9])',
    ):
        m = re.search(pat, text)
        if m:
            return m.group(1).replace('-', '').upper()
    return None
    # 格式1: ABC-DEF
    m = re.search(r'>([A-Z0-9]{3}-[A-Z0-9]{3})<', text)
    if m:
        return m.group(1).replace('-', '')
    # 格式2: 直接 6 位
    m = re.search(r'>([A-Z0-9]{6})<', text)
    if m:
        return m.group(1)
    # 格式3: 正文中的 6 位
    m = re.search(r'\b([A-Z0-9]{3}-?[A-Z0-9]{3})\b', text)
    if m:
        return m.group(1).replace('-', '')
    # 格式4: 纯数字/字母验证码常见 6 位
    m = re.search(r'(?i)(?:code|验证码|verification)[^\nA-Z0-9]{0,20}([A-Z0-9]{6})\b', text)
    if m:
        return m.group(1).upper()
    return None


class EmailHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        print(f'[HTTP] POST hit path={self.path!r}', flush=True)
        if self.path == '/webhook':
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length).decode('utf-8', errors='replace')

            try:
                data = json.loads(body)
            except Exception:
                data = {}

            to_addr = normalize_email(data.get('to', data.get('recipient', '')))
            from_addr = normalize_email(data.get('from', data.get('sender', '')))
            subject = data.get('subject', '') or ''
            text = data.get('text', '') or ''
            html = data.get('html', '') or ''

            content = f"{subject}\n{text}\n{html}"
            code = extract_code(content)

            print(
                f'[HTTP] webhook received to={to_addr!r} from={from_addr!r} '
                f'subject={subject!r} code={code!r} body_len={len(body)}',
                flush=True,
            )

            if to_addr and code:
                with emails_lock:
                    if to_addr not in emails:
                        emails[to_addr] = []
                    emails[to_addr].append({
                        'code': code,
                        'time': time.time(),
                        'from': from_addr,
                        'subject': subject,
                    })
                print(f'[+] {to_addr} code={code}', flush=True)
            elif to_addr and not code:
                print(f'[!] no code extracted for {to_addr}', flush=True)

            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({"ok": True, "code": code, "to": to_addr}).encode())
        else:
            self.send_response(404)
            self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == '/health':
            self._json({"status": "ok", "emails": len(emails)})

        elif path == '/domains':
            self._json({"domains": [DEFAULT_DOMAIN]})

        elif path.startswith('/check/'):
            addr = normalize_email(unquote(path[7:]))
            cleanup_old()
            with emails_lock:
                items = emails.get(addr, [])
                if items:
                    latest = items[-1]
                    self._json({"code": latest['code'], "from": latest['from']})
                else:
                    self._json({"code": None})

        elif path == '/list':
            cleanup_old()
            with emails_lock:
                result = {addr: len(msgs) for addr, msgs in emails.items()}
            self._json(result)

        else:
            self.send_response(404)
            self.end_headers()

    def _json(self, data):
        body = json.dumps(data).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        print(f"[HTTP] {args[0] if args else format}", flush=True)


def main():
    global DEFAULT_DOMAIN
    port = DEFAULT_PORT
    domain = DEFAULT_DOMAIN

    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == '--port' and i + 1 < len(args):
            port = int(args[i + 1]); i += 2; continue
        if args[i] == '--domain' and i + 1 < len(args):
            domain = args[i + 1]; i += 2; continue
        i += 1

    DEFAULT_DOMAIN = domain

    print(f"[*] Email server starting on :{port}", flush=True)
    print(f"[*] Domain: {domain}", flush=True)
    print(f"[*] Webhook: http://0.0.0.0:{port}/webhook", flush=True)
    print(f"[*] Check: http://localhost:{port}/check/<email>", flush=True)

    server = ThreadingHTTPServer(('0.0.0.0', port), EmailHandler)
    server.serve_forever()


if __name__ == "__main__":
    main()