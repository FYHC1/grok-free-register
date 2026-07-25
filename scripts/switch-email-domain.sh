#!/usr/bin/env bash
set -euo pipefail
# Switch register email domain in WSL without wiping CPA settings.
# Usage:
#   bash switch-email-domain.sh mail.example.com
#   bash switch-email-domain.sh mail.example.com 5

DOMAIN="${1:-mail.example.com}"
TARGET="${2:-}"
ROOT="${HOME}/grok-free-register"
ENV_FILE="${ROOT}/.env"
mkdir -p "${ROOT}"
touch "${ENV_FILE}"

HOST=$(awk '/nameserver/{print $2; exit}' /etc/resolv.conf)
if [ -z "${HOST}" ]; then
  HOST=$(ip route 2>/dev/null | awk '/default/{print $3; exit}')
fi
if [ -z "${HOST}" ]; then
  HOST="127.0.0.1"
fi

python3 - <<PY
from pathlib import Path
env_path = Path(${ENV_FILE@Q})
domain = ${DOMAIN@Q}
target = ${TARGET@Q}
host = ${HOST@Q}
raw = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
data = {}
order = []
for line in raw.splitlines():
    s=line.strip()
    if not s or s.startswith("#") or "=" not in s:
        continue
    k,v=s.split("=",1)
    if k not in data:
        order.append(k)
    data[k]=v

defaults = {
    "EMAIL_MODE": "custom",
    "EMAIL_DOMAIN": domain,
    "EMAIL_API": f"http://{host}:8080",
    "TARGET": target or data.get("TARGET") or "1",
    "REGISTER_LOG_MODE": data.get("REGISTER_LOG_MODE") or "debug",
    "PHYSICAL_CAP": data.get("PHYSICAL_CAP") or "1",
    "S_WORKERS": data.get("S_WORKERS") or "1",
    "P_WORKERS": data.get("P_WORKERS") or "1",
    "C_WORKERS": data.get("C_WORKERS") or "1",
    "P_BATCH_MAX": data.get("P_BATCH_MAX") or "1",
    "Q_PENDING_CAP": data.get("Q_PENDING_CAP") or "2",
    "XAI_ENROLLER_ALLOWED_EMAIL_DOMAIN": domain,
}
for k,v in defaults.items():
    data[k]=v
    if k not in order:
        order.append(k)

# keep any existing CPA / other keys
lines=[]
for k in order:
    lines.append(f"{k}={data[k]}")
for k,v in data.items():
    if k not in order:
        lines.append(f"{k}={v}")
env_path.write_text("\n".join(lines)+"\n", encoding="utf-8")
print(env_path.read_text(encoding="utf-8"))
PY

echo "--- health ${HOST}:8080 ---"
curl -sS --connect-timeout 5 "http://${HOST}:8080/health" || echo "HEALTH_FAIL (start email server first)"
echo
