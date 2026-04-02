#!/usr/bin/env bash
set -euo pipefail

TOKEN="${CLOUDFLARE_TOKEN:-}"
ACCOUNT_ID="${CF_ACCOUNT_ID:-}"
ZONE_ID="${CF_ZONE_ID:-}"

if [[ -z "$TOKEN" || -z "$ACCOUNT_ID" || -z "$ZONE_ID" ]]; then
  echo "Missing env vars. Required: CLOUDFLARE_TOKEN, CF_ACCOUNT_ID, CF_ZONE_ID" >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
python3 "$SCRIPT_DIR/registry.py" init >/dev/null

ROWS_JSON="$(python3 "$SCRIPT_DIR/registry.py" list --include-deleted)"

python3 - <<'PY' "$ROWS_JSON" "$TOKEN" "$ACCOUNT_ID" "$ZONE_ID" "$SCRIPT_DIR"
import json, subprocess, sys

rows = json.loads(sys.argv[1])
token = sys.argv[2]
account_id = sys.argv[3]
zone_id = sys.argv[4]
script_dir = sys.argv[5]


def run(cmd):
    p = subprocess.run(cmd, capture_output=True, text=True)
    return p.returncode, p.stdout.strip(), p.stderr.strip()

summary = {"checked": 0, "updated": 0, "drifted": 0, "active": 0, "deleted": 0, "details": []}

for row in rows:
    app = row.get("app_name")
    status = row.get("status")
    if status == "deleted":
        summary["deleted"] += 1
        continue

    summary["checked"] += 1
    tunnel_name = row.get("tunnel_name")
    hostname = row.get("hostname")
    container_name = row.get("container_name") or f"cloudflared-{tunnel_name}"

    rc, out, _ = run(["docker", "ps", "--format", "{{.Names}}"])
    container_running = (rc == 0 and container_name in out.splitlines())

    rc, tjson, _ = run([
        "python3", f"{script_dir}/cf_tunnel_api.py", "--token", token,
        "get-tunnel-by-name", "--account-id", account_id, "--name", tunnel_name
    ])
    tunnel_exists = False
    tunnel_id = row.get("tunnel_id") or ""
    if rc == 0 and tjson:
        try:
            obj = json.loads(tjson)
            if obj and obj.get("id"):
                tunnel_exists = True
                tunnel_id = obj.get("id")
        except Exception:
            pass

    dns_ok = False
    if hostname:
        rc, djson, _ = run([
            "python3", f"{script_dir}/cf_tunnel_api.py", "--token", token,
            "get-dns", "--zone-id", zone_id, "--hostname", hostname
        ])
        if rc == 0 and djson:
            try:
                records = json.loads(djson)
                expected = f"{tunnel_id}.cfargotunnel.com"
                dns_ok = any((r.get("content") == expected and r.get("proxied") is True) for r in records)
            except Exception:
                dns_ok = False

    if tunnel_exists and container_running and dns_ok:
        new_status = "active"
        summary["active"] += 1
        last_error = None
    else:
        new_status = "drifted"
        summary["drifted"] += 1
        reasons = []
        if not tunnel_exists:
            reasons.append("missing_tunnel")
        if not container_running:
            reasons.append("missing_container")
        if not dns_ok:
            reasons.append("dns_mismatch")
        last_error = ",".join(reasons)

    payload = {
        "app_name": app,
        "tunnel_name": tunnel_name,
        "tunnel_id": tunnel_id,
        "container_name": container_name,
        "container_id": row.get("container_id") or "",
        "hostname": hostname,
        "service_url": row.get("service_url") or "",
        "zone_id": zone_id,
        "account_id": account_id,
        "status": new_status,
        "last_error": last_error,
    }

    run(["python3", f"{script_dir}/registry.py", "upsert", "--payload", json.dumps(payload), "--action", "reconcile"])
    summary["updated"] += 1
    summary["details"].append({
        "app_name": app,
        "status": new_status,
        "tunnel_exists": tunnel_exists,
        "container_running": container_running,
        "dns_ok": dns_ok,
    })

print(json.dumps(summary, indent=2))
PY
