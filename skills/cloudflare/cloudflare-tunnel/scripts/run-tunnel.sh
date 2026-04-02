#!/usr/bin/env bash
set -euo pipefail

# Usage:
#   scripts/run-tunnel.sh dev 3000
#   scripts/run-tunnel.sh prod

MODE="${1:-dev}"
PORT="${2:-3000}"

if ! command -v cloudflared >/dev/null 2>&1; then
  echo "cloudflared is not installed" >&2
  exit 1
fi

case "$MODE" in
  dev)
    echo "Starting temporary dev tunnel for http://localhost:${PORT}"
    exec cloudflared tunnel --url "http://localhost:${PORT}"
    ;;
  prod)
    if [[ -z "${CLOUDFLARE_TUNNEL_TOKEN:-}" ]]; then
      echo "CLOUDFLARE_TUNNEL_TOKEN is missing" >&2
      exit 1
    fi
    echo "Starting production token tunnel"
    exec cloudflared tunnel run --token "${CLOUDFLARE_TUNNEL_TOKEN}"
    ;;
  *)
    echo "Unknown mode: ${MODE}. Use: dev|prod" >&2
    exit 1
    ;;
esac
