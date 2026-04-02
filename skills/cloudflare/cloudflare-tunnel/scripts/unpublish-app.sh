#!/usr/bin/env bash
set -euo pipefail

# Usage:
#   ./unpublish-app.sh <tunnel-name> <hostname>

TUNNEL_NAME="${1:-}"
HOSTNAME="${2:-}"
ACCOUNT_ID="${CF_ACCOUNT_ID:-}"
ZONE_ID="${CF_ZONE_ID:-}"
TOKEN="${CLOUDFLARE_TOKEN:-}"

if [[ -z "$TUNNEL_NAME" || -z "$HOSTNAME" ]]; then
  echo "Usage: $0 <tunnel-name> <hostname>" >&2
  exit 1
fi
if [[ -z "$TOKEN" || -z "$ACCOUNT_ID" || -z "$ZONE_ID" ]]; then
  echo "Missing env vars. Required: CLOUDFLARE_TOKEN, CF_ACCOUNT_ID, CF_ZONE_ID" >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_NAME="$TUNNEL_NAME"
CONTAINER_NAME="cloudflared-${TUNNEL_NAME}"

python3 "$SCRIPT_DIR/registry.py" init >/dev/null
python3 "$SCRIPT_DIR/registry.py" status --app-name "$APP_NAME" --status deprovisioning --action unpublish_start >/dev/null || true

TUNNEL_JSON="$(python3 "$SCRIPT_DIR/cf_tunnel_api.py" --token "$TOKEN" get-tunnel-by-name --account-id "$ACCOUNT_ID" --name "$TUNNEL_NAME")"
TUNNEL_ID="$(python3 - <<'PY' "$TUNNEL_JSON"
import json,sys
obj=json.loads(sys.argv[1])
print(obj.get('id',''))
PY
)"

if [[ -n "$TUNNEL_ID" ]]; then
  python3 "$SCRIPT_DIR/cf_tunnel_api.py" \
    --token "$TOKEN" \
    unpublish-app \
    --account-id "$ACCOUNT_ID" \
    --zone-id "$ZONE_ID" \
    --tunnel-id "$TUNNEL_ID" \
    --hostname "$HOSTNAME" >/tmp/cf_unpublish_result.json

  (docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1) || true

  DELETED=false
  for i in 1 2 3 4 5 6 7 8; do
    if python3 "$SCRIPT_DIR/cf_tunnel_api.py" --token "$TOKEN" delete-tunnel --account-id "$ACCOUNT_ID" --tunnel-id "$TUNNEL_ID" >/tmp/cf_delete_tunnel_result.json 2>/tmp/cf_delete_tunnel_err.log; then
      DELETED=true
      break
    fi
    sleep 5
  done

  FINAL_STATUS="deleted"
  LAST_ERROR=""
  if [[ "$DELETED" != "true" ]]; then
    FINAL_STATUS="drifted"
    LAST_ERROR="Tunnel delete did not complete (active connection delay)"
  fi

  PAYLOAD="$(python3 - <<'PY' "$APP_NAME" "$TUNNEL_NAME" "$TUNNEL_ID" "$CONTAINER_NAME" "$HOSTNAME" "$ZONE_ID" "$ACCOUNT_ID" "$FINAL_STATUS" "$LAST_ERROR"
import json,sys
print(json.dumps({
  "app_name": sys.argv[1],
  "tunnel_name": sys.argv[2],
  "tunnel_id": sys.argv[3],
  "container_name": sys.argv[4],
  "container_id": "",
  "hostname": sys.argv[5],
  "service_url": "",
  "zone_id": sys.argv[6],
  "account_id": sys.argv[7],
  "status": sys.argv[8],
  "last_error": sys.argv[9] if sys.argv[9] else None
}))
PY
)"
  python3 "$SCRIPT_DIR/registry.py" upsert --payload "$PAYLOAD" --action unpublish_complete >/dev/null

  python3 - <<'PY' "$TUNNEL_NAME" "$TUNNEL_ID" "$HOSTNAME" "$CONTAINER_NAME" "$DELETED"
import json,sys
print(json.dumps({
  "ok": True,
  "unpublished": True,
  "deleted_tunnel": sys.argv[5].lower() == 'true',
  "tunnel_name": sys.argv[1],
  "tunnel_id": sys.argv[2],
  "hostname": sys.argv[3],
  "removed_container": sys.argv[4]
}, indent=2))
PY
else
  python3 "$SCRIPT_DIR/registry.py" status --app-name "$APP_NAME" --status drifted --last-error "Tunnel not found by name during unpublish" --action unpublish_error >/dev/null || true
  echo "Tunnel not found by name: $TUNNEL_NAME" >&2
  exit 1
fi
