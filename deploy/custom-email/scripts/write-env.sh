#!/usr/bin/env bash
set -e
HOST=$(awk '/nameserver/{print $2; exit}' /etc/resolv.conf)
if [ -z "$HOST" ]; then
  HOST=$(ip route 2>/dev/null | awk '/default/{print $3; exit}')
fi
echo "Detected Windows host IP: $HOST"
# Prefer non-destructive switcher if present
if [ -f "/path/to/grok-free-register/scripts/switch-email-domain.sh" ]; then
  bash "/path/to/grok-free-register/scripts/switch-email-domain.sh" "mail.example.com"
  exit 0
fi
ROOT="$HOME/grok-free-register"
mkdir -p "$ROOT"
# Fallback: merge write
python3 - <<PY
from pathlib import Path
p=Path.home()/ "grok-free-register" / ".env"
host="$HOST"
raw=p.read_text(encoding="utf-8") if p.exists() else ""
data={}
order=[]
for line in raw.splitlines():
    s=line.strip()
    if not s or s.startswith("#") or "=" not in s: continue
    k,v=s.split("=",1)
    if k not in data: order.append(k)
    data[k]=v
for k,v in {
  "EMAIL_MODE":"custom",
  "EMAIL_DOMAIN":"mail.example.com",
  "EMAIL_API":f"http://{host}:8080",
  "TARGET": data.get("TARGET") or "1",
  "REGISTER_LOG_MODE": data.get("REGISTER_LOG_MODE") or "debug",
  "PHYSICAL_CAP": data.get("PHYSICAL_CAP") or "1",
  "S_WORKERS": data.get("S_WORKERS") or "1",
  "P_WORKERS": data.get("P_WORKERS") or "1",
  "C_WORKERS": data.get("C_WORKERS") or "1",
  "P_BATCH_MAX": data.get("P_BATCH_MAX") or "1",
  "Q_PENDING_CAP": data.get("Q_PENDING_CAP") or "2",
  "XAI_ENROLLER_ALLOWED_EMAIL_DOMAIN":"mail.example.com",
}.items():
    data[k]=v
    if k not in order: order.append(k)
p.write_text("\n".join(f"{k}={data[k]}" for k in order if k in data)+"\n"+ "\n".join(f"{k}={v}" for k,v in data.items() if k not in order)+"\n", encoding="utf-8")
print(p.read_text(encoding="utf-8"))
PY
echo "--- health ---"
curl -sS --connect-timeout 5 "http://${HOST}:8080/health" || echo HEALTH_FAIL
echo
