#!/usr/bin/env bash
set -euo pipefail

# Usage:
#   ./publish-app.sh <tunnel-name> <hostname> <service-url>
# Example:
#   ./publish-app.sh hello-world hello.example.com http://127.0.0.1:8088

TUNNEL_NAME="${1:-}"
HOSTNAME="${2:-}"
SERVICE="${3:-}"
ACCOUNT_ID="${CF_ACCOUNT_ID:-}"
ZONE_ID="${CF_ZONE_ID:-}"
TOKEN="${CLOUDFLARE_TOKEN:-}"

if [[ -z "$TUNNEL_NAME" || -z "$HOSTNAME" || -z "$SERVICE" ]]; then
  echo "Usage: $0 <tunnel-name> <hostname> <service-url>" >&2
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
python3 "$SCRIPT_DIR/registry.py" status --app-name "$APP_NAME" --status provisioning --action publish_start >/dev/null || true

# Ensure tunnel exists
ENSURE_JSON="$(python3 "$SCRIPT_DIR/cf_tunnel_api.py" --token "$TOKEN" ensure-tunnel --account-id "$ACCOUNT_ID" --name "$TUNNEL_NAME")"
TUNNEL_ID="$(python3 - <<'PY' "$ENSURE_JSON"
import json,sys
obj=json.loads(sys.argv[1])
t=obj.get('tunnel',{})
print(t.get('id',''))
PY
)"
if [[ -z "$TUNNEL_ID" ]]; then
  python3 "$SCRIPT_DIR/registry.py" status --app-name "$APP_NAME" --status drifted --last-error "Failed to resolve tunnel id" --action publish_error >/dev/null || true
  echo "Failed to resolve tunnel id" >&2
  exit 1
fi

# Get connector token for this tunnel
CONNECTOR_TOKEN="$(python3 "$SCRIPT_DIR/cf_tunnel_api.py" --token "$TOKEN" get-tunnel-token --account-id "$ACCOUNT_ID" --tunnel-id "$TUNNEL_ID")"
if [[ -z "$CONNECTOR_TOKEN" ]]; then
  python3 "$SCRIPT_DIR/registry.py" status --app-name "$APP_NAME" --status drifted --last-error "Failed to get connector token" --action publish_error >/dev/null || true
  echo "Failed to get connector token" >&2
  exit 1
fi

# Publish hostname route + DNS
python3 "$SCRIPT_DIR/cf_tunnel_api.py" \
  --token "$TOKEN" \
  publish-app \
  --account-id "$ACCOUNT_ID" \
  --zone-id "$ZONE_ID" \
  --tunnel-id "$TUNNEL_ID" \
  --hostname "$HOSTNAME" \
  --service "$SERVICE" >/tmp/cf_publish_result.json

# Start dedicated cloudflared container for this tunnel (persistent)
(docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1) || true

docker run -d \
  --name "$CONTAINER_NAME" \
  --network host \
  --restart unless-stopped \
  cloudflare/cloudflared:latest \
  tunnel run --token "$CONNECTOR_TOKEN" >/tmp/cf_container_id.txt

CONTAINER_ID="$(cat /tmp/cf_container_id.txt)"

PAYLOAD="$(python3 - <<'PY' "$APP_NAME" "$TUNNEL_NAME" "$TUNNEL_ID" "$CONTAINER_NAME" "$CONTAINER_ID" "$HOSTNAME" "$SERVICE" "$ZONE_ID" "$ACCOUNT_ID"
import json,sys
print(json.dumps({
  "app_name": sys.argv[1],
  "tunnel_name": sys.argv[2],
  "tunnel_id": sys.argv[3],
  "container_name": sys.argv[4],
  "container_id": sys.argv[5].strip(),
  "hostname": sys.argv[6],
  "service_url": sys.argv[7],
  "zone_id": sys.argv[8],
  "account_id": sys.argv[9],
  "status": "active",
  "last_error": None
}))
PY
)"
python3 "$SCRIPT_DIR/registry.py" upsert --payload "$PAYLOAD" --action publish_success >/dev/null

python3 - <<'PY' "$TUNNEL_NAME" "$TUNNEL_ID" "$HOSTNAME" "$SERVICE" "$CONTAINER_NAME" "$CONTAINER_ID"
import json,sys
print(json.dumps({
  "ok": True,
  "tunnel_name": sys.argv[1],
  "tunnel_id": sys.argv[2],
  "hostname": sys.argv[3],
  "service": sys.argv[4],
  "cloudflared_container": sys.argv[5],
  "container_id": sys.argv[6].strip()
}, indent=2))
PY
