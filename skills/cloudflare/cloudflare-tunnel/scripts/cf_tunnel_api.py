#!/usr/bin/env python3
import argparse
import json
import os
import sys
from urllib import error, parse, request

BASE = "https://api.cloudflare.com/client/v4"


def api(token, method, path, body=None, query=None):
    url = BASE + path
    if query:
        url += "?" + parse.urlencode(query)
    data = None
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    if body is not None:
        data = json.dumps(body).encode("utf-8")
    req = request.Request(url, data=data, headers=headers, method=method)
    try:
        with request.urlopen(req, timeout=45) as r:
            return json.loads(r.read().decode("utf-8"))
    except error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="ignore")
        print(raw, file=sys.stderr)
        raise


def ensure_ok(resp):
    if not resp.get("success"):
        print(json.dumps(resp, indent=2), file=sys.stderr)
        sys.exit(1)


def tunnels(token, account_id):
    resp = api(token, "GET", f"/accounts/{account_id}/cfd_tunnel")
    ensure_ok(resp)
    return resp.get("result", [])


def find_tunnel_by_name(token, account_id, name):
    for t in tunnels(token, account_id):
        if t.get("name") == name:
            return t
    return None


def create_tunnel(token, account_id, name):
    body = {"name": name, "config_src": "cloudflare"}
    resp = api(token, "POST", f"/accounts/{account_id}/cfd_tunnel", body=body)
    ensure_ok(resp)
    return resp.get("result", {})


def delete_tunnel(token, account_id, tunnel_id):
    resp = api(token, "DELETE", f"/accounts/{account_id}/cfd_tunnel/{tunnel_id}")
    ensure_ok(resp)


def get_tunnel_token(token, account_id, tunnel_id):
    resp = api(token, "GET", f"/accounts/{account_id}/cfd_tunnel/{tunnel_id}/token")
    ensure_ok(resp)
    result = resp.get("result", {})
    if isinstance(result, dict) and "token" in result:
        return result["token"]
    if isinstance(result, str):
        return result
    print(json.dumps(result, indent=2), file=sys.stderr)
    sys.exit(1)


def normalize_ingress(raw_ingress):
    ingress = raw_ingress if isinstance(raw_ingress, list) else []
    host_rules = [r for r in ingress if isinstance(r, dict) and r.get("hostname")]
    catch_all = [r for r in ingress if isinstance(r, dict) and not r.get("hostname")]
    if not catch_all:
        catch_all = [{"service": "http_status:404"}]
    return host_rules, catch_all


def get_tunnel_config(token, account_id, tunnel_id):
    resp = api(token, "GET", f"/accounts/{account_id}/cfd_tunnel/{tunnel_id}/configurations")
    ensure_ok(resp)
    result = resp.get("result", {}) or {}
    return (result.get("config", {}) or {})


def set_tunnel_config(token, account_id, tunnel_id, config):
    body = {"config": config}
    resp = api(token, "PUT", f"/accounts/{account_id}/cfd_tunnel/{tunnel_id}/configurations", body=body)
    if not resp.get("success"):
        resp = api(token, "PATCH", f"/accounts/{account_id}/cfd_tunnel/{tunnel_id}/configurations", body=body)
    ensure_ok(resp)
    return resp.get("result", {})


def upsert_route(token, account_id, tunnel_id, hostname, service):
    cfg = get_tunnel_config(token, account_id, tunnel_id)
    host_rules, catch_all = normalize_ingress(cfg.get("ingress"))
    host_rules = [r for r in host_rules if r.get("hostname") != hostname]
    host_rules.append({"hostname": hostname, "service": service})
    cfg["ingress"] = host_rules + catch_all
    return set_tunnel_config(token, account_id, tunnel_id, cfg)


def remove_route(token, account_id, tunnel_id, hostname):
    cfg = get_tunnel_config(token, account_id, tunnel_id)
    host_rules, catch_all = normalize_ingress(cfg.get("ingress"))
    host_rules = [r for r in host_rules if r.get("hostname") != hostname]
    cfg["ingress"] = host_rules + catch_all
    return set_tunnel_config(token, account_id, tunnel_id, cfg)


def get_dns_records(token, zone_id, hostname):
    list_resp = api(token, "GET", f"/zones/{zone_id}/dns_records", query={"type": "CNAME", "name": hostname})
    ensure_ok(list_resp)
    return list_resp.get("result", [])


def upsert_dns(token, zone_id, tunnel_id, hostname):
    target = f"{tunnel_id}.cfargotunnel.com"
    records = get_dns_records(token, zone_id, hostname)
    if records:
        rid = records[0].get("id")
        patch_resp = api(
            token,
            "PATCH",
            f"/zones/{zone_id}/dns_records/{rid}",
            body={"type": "CNAME", "name": hostname, "content": target, "proxied": True},
        )
        ensure_ok(patch_resp)
        return patch_resp.get("result", {})

    create_resp = api(
        token,
        "POST",
        f"/zones/{zone_id}/dns_records",
        body={"type": "CNAME", "name": hostname, "content": target, "proxied": True},
    )
    ensure_ok(create_resp)
    return create_resp.get("result", {})


def delete_dns(token, zone_id, hostname):
    records = get_dns_records(token, zone_id, hostname)
    deleted = []
    for rec in records:
        rid = rec.get("id")
        del_resp = api(token, "DELETE", f"/zones/{zone_id}/dns_records/{rid}")
        ensure_ok(del_resp)
        deleted.append(rid)
    return deleted


def cmd_list_tunnels(args):
    print(json.dumps(tunnels(args.token, args.account_id), indent=2))


def cmd_get_tunnel_by_name(args):
    t = find_tunnel_by_name(args.token, args.account_id, args.name)
    print(json.dumps(t or {}, indent=2))


def cmd_ensure_tunnel(args):
    existing = find_tunnel_by_name(args.token, args.account_id, args.name)
    if existing:
        print(json.dumps({"created": False, "tunnel": existing}, indent=2))
        return
    created = create_tunnel(args.token, args.account_id, args.name)
    print(json.dumps({"created": True, "tunnel": created}, indent=2))


def cmd_create_tunnel(args):
    print(json.dumps(create_tunnel(args.token, args.account_id, args.name), indent=2))


def cmd_delete_tunnel(args):
    delete_tunnel(args.token, args.account_id, args.tunnel_id)
    print(json.dumps({"deleted": True, "tunnel_id": args.tunnel_id}, indent=2))


def cmd_get_tunnel_token(args):
    print(get_tunnel_token(args.token, args.account_id, args.tunnel_id))


def cmd_get_config(args):
    cfg = get_tunnel_config(args.token, args.account_id, args.tunnel_id)
    print(json.dumps(cfg, indent=2))


def cmd_get_dns(args):
    print(json.dumps(get_dns_records(args.token, args.zone_id, args.hostname), indent=2))


def cmd_create_dns(args):
    print(json.dumps(upsert_dns(args.token, args.zone_id, args.tunnel_id, args.hostname), indent=2))


def cmd_delete_dns(args):
    print(json.dumps({"deleted_dns_ids": delete_dns(args.token, args.zone_id, args.hostname)}, indent=2))


def cmd_publish_app(args):
    upsert_route(args.token, args.account_id, args.tunnel_id, args.hostname, args.service)
    dns = upsert_dns(args.token, args.zone_id, args.tunnel_id, args.hostname)
    print(json.dumps({
        "published": True,
        "hostname": args.hostname,
        "service": args.service,
        "tunnel_id": args.tunnel_id,
        "dns_record": dns,
    }, indent=2))


def cmd_unpublish_app(args):
    remove_route(args.token, args.account_id, args.tunnel_id, args.hostname)
    deleted_dns_ids = delete_dns(args.token, args.zone_id, args.hostname)
    print(json.dumps({
        "unpublished": True,
        "hostname": args.hostname,
        "tunnel_id": args.tunnel_id,
        "deleted_dns_ids": deleted_dns_ids,
    }, indent=2))


def main():
    p = argparse.ArgumentParser(description="Cloudflare Tunnel/API helper")
    p.add_argument("--token", default=os.getenv("CLOUDFLARE_TOKEN", ""))
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("list-tunnels")
    s.add_argument("--account-id", required=True)
    s.set_defaults(func=cmd_list_tunnels)

    s = sub.add_parser("get-tunnel-by-name")
    s.add_argument("--account-id", required=True)
    s.add_argument("--name", required=True)
    s.set_defaults(func=cmd_get_tunnel_by_name)

    s = sub.add_parser("ensure-tunnel")
    s.add_argument("--account-id", required=True)
    s.add_argument("--name", required=True)
    s.set_defaults(func=cmd_ensure_tunnel)

    s = sub.add_parser("create-tunnel")
    s.add_argument("--account-id", required=True)
    s.add_argument("--name", required=True)
    s.set_defaults(func=cmd_create_tunnel)

    s = sub.add_parser("delete-tunnel")
    s.add_argument("--account-id", required=True)
    s.add_argument("--tunnel-id", required=True)
    s.set_defaults(func=cmd_delete_tunnel)

    s = sub.add_parser("get-tunnel-token")
    s.add_argument("--account-id", required=True)
    s.add_argument("--tunnel-id", required=True)
    s.set_defaults(func=cmd_get_tunnel_token)

    s = sub.add_parser("get-config")
    s.add_argument("--account-id", required=True)
    s.add_argument("--tunnel-id", required=True)
    s.set_defaults(func=cmd_get_config)

    s = sub.add_parser("get-dns")
    s.add_argument("--zone-id", required=True)
    s.add_argument("--hostname", required=True)
    s.set_defaults(func=cmd_get_dns)

    s = sub.add_parser("create-dns")
    s.add_argument("--zone-id", required=True)
    s.add_argument("--tunnel-id", required=True)
    s.add_argument("--hostname", required=True)
    s.set_defaults(func=cmd_create_dns)

    s = sub.add_parser("delete-dns")
    s.add_argument("--zone-id", required=True)
    s.add_argument("--hostname", required=True)
    s.set_defaults(func=cmd_delete_dns)

    s = sub.add_parser("publish-app")
    s.add_argument("--account-id", required=True)
    s.add_argument("--zone-id", required=True)
    s.add_argument("--tunnel-id", required=True)
    s.add_argument("--hostname", required=True)
    s.add_argument("--service", required=True)
    s.set_defaults(func=cmd_publish_app)

    s = sub.add_parser("unpublish-app")
    s.add_argument("--account-id", required=True)
    s.add_argument("--zone-id", required=True)
    s.add_argument("--tunnel-id", required=True)
    s.add_argument("--hostname", required=True)
    s.set_defaults(func=cmd_unpublish_app)

    args = p.parse_args()
    if not args.token:
        print("CLOUDFLARE_TOKEN (or --token) is required", file=sys.stderr)
        sys.exit(2)
    args.func(args)


if __name__ == "__main__":
    main()
