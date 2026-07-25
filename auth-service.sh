#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

# Default OAuth mode is Auth Code + PKCE (Grok Build), for Grok Build OAuth.
# Override only if needed: export XAI_ENROLLER_OAUTH_MODE=device
export XAI_ENROLLER_OAUTH_MODE="${XAI_ENROLLER_OAUTH_MODE:-auth_code}"

. scripts/ensure_runtime.sh
ensure_runtime

exec .venv/bin/python -m xai_enroller.service "$@"
