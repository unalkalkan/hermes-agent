---
name: cloudflare-tunnel
description: Create and manage Cloudflare Tunnel endpoints to expose local or containerized apps to the internet for development and production. Use this skill whenever the user asks to expose an app, create a public URL, set up Cloudflare Tunnel, map a domain/subdomain to a local service, or run secure ingress without opening firewall ports.
---

# Cloudflare Tunnel

Use this skill to publish local services through Cloudflare Tunnel using only `CLOUDFLARE_TOKEN` for API-driven tunnel lifecycle management (create token, create DNS, configure routes), then run dedicated `cloudflared` connector containers per tunnel.

## When to use

Trigger this skill when the user asks for any of the following:
- Expose localhost app to the internet
- Create a public URL for testing/dev
- Configure Cloudflare Tunnel for production
- Route `app.example.com` to a local/container service
- Avoid opening inbound firewall ports

## Prerequisites

- Docker available to run `cloudflare/cloudflared` connector containers
- `CLOUDFLARE_TOKEN` available in environment (Cloudflare API token, not tunnel connector token)
- Token must have at least: Account Cloudflare Tunnel Edit + Zone DNS Edit on target resources
- `CF_ACCOUNT_ID` and `CF_ZONE_ID` set in environment
- For production hostname routing: domain is in Cloudflare DNS

Check quickly:

```bash
docker --version
printenv CLOUDFLARE_TOKEN | wc -c
printenv CF_ACCOUNT_ID | wc -c
printenv CF_ZONE_ID | wc -c
```

If token length is 1 or 0, token is missing.

## Mode A: Fast development URL (temporary)

Use this for quick demos and local development when a random public URL is acceptable.

```bash
cloudflared tunnel --url http://localhost:3000
```

Notes:
- Creates a `*.trycloudflare.com` URL
- Good for temporary testing
- Not suitable for stable production hostname

## Mode B: API-managed persistent tunnel per app (recommended here)

Use API flow to create tunnel + connector token dynamically, then run a dedicated `cloudflared` container per tunnel.

### 1) Publish app (create tunnel if needed, configure route+DNS, start connector)

```bash
scripts/publish-app.sh myapp myapp.example.com http://127.0.0.1:3000
```

### 2) Verify

- `docker ps` shows `cloudflared-myapp`
- `docker logs cloudflared-myapp --tail=50`
- `curl -I https://myapp.example.com`

### 3) Remove app exposure cleanly

```bash
scripts/unpublish-app.sh myapp myapp.example.com
```

This removes route + DNS, stops/removes connector container, and deletes tunnel.

## Docker patterns

### Expose app running in same Docker network

If cloudflared runs in one container and app in another, target service by container name and port:
- `http://my-app:3000`

If app runs on host and cloudflared in container, use host gateway if supported:
- `http://host.docker.internal:3000`

## Systemd production pattern (VM/bare metal)

Create service:

```ini
[Unit]
Description=Cloudflare Tunnel
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
Environment=CLOUDFLARE_TUNNEL_TOKEN=REPLACE_ME
ExecStart=/usr/local/bin/cloudflared tunnel run --token ${CLOUDFLARE_TUNNEL_TOKEN}
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now cloudflared
sudo systemctl status cloudflared --no-pager
```

## Troubleshooting

1) `cloudflared: command not found`
- Install cloudflared in the runtime where you start the tunnel.

2) Authentication/token errors
- Re-check `CLOUDFLARE_TUNNEL_TOKEN`
- Regenerate connector token from tunnel settings
- Ensure no extra quotes/whitespace in `.env`

3) Hostname not resolving
- Confirm tunnel has a Public Hostname entry
- Verify DNS zone is managed by Cloudflare
- Check proxy status in DNS/hostname config

4) 502/connection refused from tunnel
- Confirm origin app is listening on expected host/port
- Validate target URL in hostname config
- Test from same runtime: `curl -I http://localhost:3000`

## Multi-tunnel architecture (important)

- One `cloudflared` process/container runs one tunnel connector session at a time.
- This skill uses one dedicated `cloudflared-<tunnel-name>` container per app/tunnel for persistent isolation.
- That means multiple persistent apps => multiple cloudflared containers (one each).
- Publish/unpublish lifecycle is fully API-driven from `CLOUDFLARE_TOKEN`.

## Safe defaults

- Prefer temporary `trycloudflare` URL for quick dev demos
- For persistent app endpoints, use one tunnel per app with dedicated connector container
- Keep `CLOUDFLARE_TOKEN`, `CF_ACCOUNT_ID`, `CF_ZONE_ID` in `.env`, never hardcode into repos
- Use separate naming conventions for dev/prod tunnels (e.g., `app-dev`, `app-prod`)

## API automation (create/delete on the fly)

Use bundled script: `scripts/cf_tunnel_api.py`

Common examples:

```bash
# List tunnels
python3 scripts/cf_tunnel_api.py list-tunnels --account-id <ACCOUNT_ID>

# Create tunnel
python3 scripts/cf_tunnel_api.py create-tunnel --account-id <ACCOUNT_ID> --name app-dev-123

# Get connector token for a tunnel
python3 scripts/cf_tunnel_api.py get-tunnel-token --account-id <ACCOUNT_ID> --tunnel-id <TUNNEL_ID>

# Create DNS route app.example.com -> <tunnel_id>.cfargotunnel.com
python3 scripts/cf_tunnel_api.py create-dns --zone-id <ZONE_ID> --tunnel-id <TUNNEL_ID> --hostname app.example.com

# Delete DNS route
python3 scripts/cf_tunnel_api.py delete-dns --zone-id <ZONE_ID> --hostname app.example.com

# Delete tunnel
python3 scripts/cf_tunnel_api.py delete-tunnel --account-id <ACCOUNT_ID> --tunnel-id <TUNNEL_ID>
```

Recommended pattern for reliability:
- Keep tunnel-name stable per app (`myapp`, `api-prod`, etc.)
- Run one connector container per tunnel (`cloudflared-<tunnel-name>`)
- Use publish/unpublish wrappers so route + DNS + container lifecycle stay in sync

Convenience wrappers:

```bash
# Publish (create tunnel if missing, add route+DNS, start connector)
scripts/publish-app.sh myapp myapp.example.com http://127.0.0.1:3000

# Unpublish (remove route+DNS, stop connector, delete tunnel)
scripts/unpublish-app.sh myapp myapp.example.com

# Show persisted managed inventory (from /data/hermes/cloudflare-tunnels/registry.db)
scripts/list-managed.sh

# Reconcile persisted inventory against Docker + Cloudflare and mark drift
scripts/reconcile-managed.sh
```

## Persistent state and restart safety

- Registry DB path: `/data/hermes/cloudflare-tunnels/registry.db`
- Registry survives Hermes container restarts because `/data/hermes` is persistent.
- `publish-app.sh` and `unpublish-app.sh` both write lifecycle state/events.
- Status values used: `provisioning`, `active`, `deprovisioning`, `deleted`, `drifted`.
- Use `scripts/reconcile-managed.sh` after config changes or manual dashboard edits.

## Quick response template

When asked to expose an app:
1) Ask app port and desired hostname (if unknown)
2) Run `publish-app.sh <tunnel-name> <hostname> <service-url>`
3) Confirm reachable public URL
4) Show `list-managed.sh` output for persisted state
5) Share validation checklist (HTTP status + app health endpoint)
