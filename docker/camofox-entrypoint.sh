#!/usr/bin/env sh
set -eu

mkdir -p /data/camofox/.cache /data/camofox/.camofox/cookies

# Running container as root is common in this stack; Camoufox/Firefox requires
# HOME to be owned by the current user. Ensure ownership matches runtime UID.
if [ "$(id -u)" = "0" ]; then
  chown -R root:root /data/camofox || true
fi

# Seed persistent volume with pre-fetched camoufox binaries on first run
if [ ! -f /data/camofox/.cache/camoufox/version.json ] && [ -f /opt/camoufox-seed/version.json ]; then
  echo "[camofox-entrypoint] Seeding Camoufox binaries into persistent volume..."
  mkdir -p /data/camofox/.cache/camoufox
  cp -a /opt/camoufox-seed/. /data/camofox/.cache/camoufox/
fi

export HOME=/data/camofox

exec "$@"
