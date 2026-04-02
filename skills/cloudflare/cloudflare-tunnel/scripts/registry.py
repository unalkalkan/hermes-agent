#!/usr/bin/env python3
import argparse
import json
import os
import sqlite3
from datetime import datetime, timezone

REGISTRY_DIR = os.getenv("CF_TUNNEL_REGISTRY_DIR", "/data/hermes/cloudflare-tunnels")
REGISTRY_DB = os.path.join(REGISTRY_DIR, "registry.db")


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def ensure_db():
    os.makedirs(REGISTRY_DIR, exist_ok=True)
    conn = sqlite3.connect(REGISTRY_DB)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS managed_apps (
            app_name TEXT PRIMARY KEY,
            tunnel_name TEXT NOT NULL,
            tunnel_id TEXT,
            container_name TEXT,
            container_id TEXT,
            hostname TEXT,
            service_url TEXT,
            zone_id TEXT,
            account_id TEXT,
            status TEXT NOT NULL,
            last_error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            app_name TEXT,
            action TEXT NOT NULL,
            success INTEGER NOT NULL,
            details_json TEXT
        )
        """
    )
    conn.commit()
    return conn


def add_event(conn, app_name, action, success, details):
    conn.execute(
        "INSERT INTO events (ts, app_name, action, success, details_json) VALUES (?, ?, ?, ?, ?)",
        (now_iso(), app_name, action, 1 if success else 0, json.dumps(details or {})),
    )
    conn.commit()


def upsert_managed(conn, payload):
    ts = now_iso()
    existing = conn.execute("SELECT app_name, created_at FROM managed_apps WHERE app_name=?", (payload["app_name"],)).fetchone()
    created_at = existing["created_at"] if existing else ts

    conn.execute(
        """
        INSERT INTO managed_apps (
            app_name, tunnel_name, tunnel_id, container_name, container_id, hostname,
            service_url, zone_id, account_id, status, last_error, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(app_name) DO UPDATE SET
            tunnel_name=excluded.tunnel_name,
            tunnel_id=excluded.tunnel_id,
            container_name=excluded.container_name,
            container_id=excluded.container_id,
            hostname=excluded.hostname,
            service_url=excluded.service_url,
            zone_id=excluded.zone_id,
            account_id=excluded.account_id,
            status=excluded.status,
            last_error=excluded.last_error,
            updated_at=excluded.updated_at
        """,
        (
            payload["app_name"],
            payload.get("tunnel_name"),
            payload.get("tunnel_id"),
            payload.get("container_name"),
            payload.get("container_id"),
            payload.get("hostname"),
            payload.get("service_url"),
            payload.get("zone_id"),
            payload.get("account_id"),
            payload.get("status", "active"),
            payload.get("last_error"),
            created_at,
            ts,
        ),
    )
    conn.commit()


def set_status(conn, app_name, status, last_error=None):
    conn.execute(
        "UPDATE managed_apps SET status=?, last_error=?, updated_at=? WHERE app_name=?",
        (status, last_error, now_iso(), app_name),
    )
    conn.commit()


def list_managed(conn, include_deleted=False):
    if include_deleted:
        rows = conn.execute("SELECT * FROM managed_apps ORDER BY updated_at DESC").fetchall()
    else:
        rows = conn.execute("SELECT * FROM managed_apps WHERE status != 'deleted' ORDER BY updated_at DESC").fetchall()
    return [dict(r) for r in rows]


def get_app(conn, app_name):
    row = conn.execute("SELECT * FROM managed_apps WHERE app_name=?", (app_name,)).fetchone()
    return dict(row) if row else None


def parse_payload_arg(s):
    return json.loads(s)


def cmd_init(args):
    conn = ensure_db()
    print(json.dumps({"ok": True, "db": REGISTRY_DB}, indent=2))
    conn.close()


def cmd_upsert(args):
    conn = ensure_db()
    payload = parse_payload_arg(args.payload)
    upsert_managed(conn, payload)
    add_event(conn, payload.get("app_name"), args.action, True, payload)
    print(json.dumps({"ok": True, "app_name": payload.get("app_name")}, indent=2))
    conn.close()


def cmd_status(args):
    conn = ensure_db()
    set_status(conn, args.app_name, args.status, args.last_error)
    add_event(conn, args.app_name, args.action, True, {"status": args.status, "last_error": args.last_error})
    print(json.dumps({"ok": True, "app_name": args.app_name, "status": args.status}, indent=2))
    conn.close()


def cmd_list(args):
    conn = ensure_db()
    rows = list_managed(conn, include_deleted=args.include_deleted)
    print(json.dumps(rows, indent=2))
    conn.close()


def cmd_get(args):
    conn = ensure_db()
    row = get_app(conn, args.app_name)
    print(json.dumps(row or {}, indent=2))
    conn.close()


def cmd_event(args):
    conn = ensure_db()
    details = parse_payload_arg(args.details) if args.details else {}
    add_event(conn, args.app_name, args.action, args.success, details)
    print(json.dumps({"ok": True}, indent=2))
    conn.close()


def main():
    p = argparse.ArgumentParser(description="Persistent registry for Cloudflare tunnel-managed apps")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init")
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("upsert")
    s.add_argument("--payload", required=True, help="JSON object")
    s.add_argument("--action", default="upsert")
    s.set_defaults(func=cmd_upsert)

    s = sub.add_parser("status")
    s.add_argument("--app-name", required=True)
    s.add_argument("--status", required=True)
    s.add_argument("--last-error", default=None)
    s.add_argument("--action", default="status")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("list")
    s.add_argument("--include-deleted", action="store_true")
    s.set_defaults(func=cmd_list)

    s = sub.add_parser("get")
    s.add_argument("--app-name", required=True)
    s.set_defaults(func=cmd_get)

    s = sub.add_parser("event")
    s.add_argument("--app-name", default=None)
    s.add_argument("--action", required=True)
    s.add_argument("--success", action="store_true")
    s.add_argument("--details", default=None, help="JSON object")
    s.set_defaults(func=cmd_event)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
