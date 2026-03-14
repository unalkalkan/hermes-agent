#!/bin/bash
# ============================================================================
# Hermes Agent — Docker Entrypoint
# ============================================================================
# Handles first-run onboarding, starts Docker-in-Docker, then runs the
# requested command (default: hermes interactive CLI).
# ============================================================================

set -e

# ── Colors ───────────────────────────────────────────────────────────────────
GREEN='\033[0;32m'
CYAN='\033[0;36m'
YELLOW='\033[0;33m'
NC='\033[0m'

log_info()    { echo -e "${CYAN}→${NC} $1"; }
log_success() { echo -e "${GREEN}✓${NC} $1"; }
log_warn()    { echo -e "${YELLOW}⚠${NC} $1"; }

# ── Start Docker-in-Docker via supervisor ────────────────────────────────────
start_dockerd() {
    # Only start dockerd if we're running with --privileged (cgroup check)
    if [ -d /sys/fs/cgroup ] && [ -w /sys/fs/cgroup ]; then
        log_info "Starting Docker daemon (Docker-in-Docker)..."
        supervisord -c /etc/supervisor/supervisord.conf

        # Wait for dockerd to become ready (max 30s)
        local retries=30
        while [ $retries -gt 0 ]; do
            if docker info >/dev/null 2>&1; then
                log_success "Docker daemon ready"
                return 0
            fi
            retries=$((retries - 1))
            sleep 1
        done
        log_warn "Docker daemon did not start — container may not have --privileged flag"
    else
        log_warn "Docker-in-Docker not available (container needs --privileged)"
    fi
}

# ── First-run onboarding ────────────────────────────────────────────────────
first_run_setup() {
    local hermes_home="${HERMES_HOME:-/data/hermes}"

    # Marker file to detect first run
    if [ -f "$hermes_home/.initialized" ]; then
        return 0
    fi

    log_info "First run detected — setting up Hermes Agent..."

    # Create directory structure (mirrors install.sh copy_config_templates)
    mkdir -p "$hermes_home"/{cron,sessions,logs,pairing,hooks,image_cache,audio_cache,memories,skills,whatsapp/session,skins}

    # Create .env from template if not already present (user may have mounted one)
    if [ ! -f "$hermes_home/.env" ]; then
        if [ -f /opt/hermes-agent/.env.example ]; then
            cp /opt/hermes-agent/.env.example "$hermes_home/.env"
            log_success "Created $hermes_home/.env from template"
        else
            touch "$hermes_home/.env"
            log_success "Created $hermes_home/.env"
        fi
    else
        log_info "$hermes_home/.env already exists (mounted from host)"
    fi

    # Create config.yaml from template if not already present
    if [ ! -f "$hermes_home/config.yaml" ]; then
        if [ -f /opt/hermes-agent/cli-config.yaml.example ]; then
            cp /opt/hermes-agent/cli-config.yaml.example "$hermes_home/config.yaml"
            log_success "Created $hermes_home/config.yaml from template"
        fi
    else
        log_info "$hermes_home/config.yaml already exists (mounted from host)"
    fi

    # Create SOUL.md
    if [ ! -f "$hermes_home/SOUL.md" ]; then
        cat > "$hermes_home/SOUL.md" << 'SOUL_EOF'
# Hermes Agent Persona

<!--
This file defines the agent's personality and tone.
The agent will embody whatever you write here.
Edit this to customize how Hermes communicates with you.

Examples:
  - "You are a warm, playful assistant who uses kaomoji occasionally."
  - "You are a concise technical expert. No fluff, just facts."
  - "You speak like a friendly coworker who happens to know everything."

This file is loaded fresh each message -- no restart needed.
Delete the contents (or this file) to use the default personality.
-->
SOUL_EOF
        log_success "Created $hermes_home/SOUL.md"
    fi

    # Sync bundled skills
    if [ -d /opt/hermes-agent/skills ]; then
        log_info "Syncing bundled skills..."
        if python /opt/hermes-agent/tools/skills_sync.py 2>/dev/null; then
            log_success "Skills synced"
        else
            cp -rn /opt/hermes-agent/skills/* "$hermes_home/skills/" 2>/dev/null || true
            log_success "Skills copied"
        fi
    fi

    # Mark as initialized
    date -Iseconds > "$hermes_home/.initialized"
    log_success "Hermes Agent initialized — data stored in $hermes_home/"
    echo ""
}

# ── Re-link editable install from mounted source ────────────────────────────
relink_source() {
    # When the host repo is bind-mounted onto /opt/hermes-agent, the venv's
    # editable .pth link (created at build time from the COPY'd source) points
    # at the correct path but the egg-info may be stale.  Re-run a fast
    # editable install so the venv picks up the live mounted source.
    if [ -d /opt/hermes-agent/.git ]; then
        log_info "Live source mount detected — re-linking editable install..."
        cd /opt/hermes-agent

        # Mark the repo safe for git (container uid may differ from host)
        git config --global --get-all safe.directory | grep -qxF /opt/hermes-agent \
            || git config --global --add safe.directory /opt/hermes-agent

        # Fast re-link (uv is very quick for no-op / editable re-installs)
        uv pip install --quiet -e ".[all]" 2>/dev/null \
            || uv pip install --quiet -e "." 2>/dev/null \
            || log_warn "Editable re-install failed — hermes may use stale code"

        # Re-link mini-swe-agent if present
        if [ -f "mini-swe-agent/pyproject.toml" ]; then
            uv pip install --quiet -e "./mini-swe-agent" 2>/dev/null || true
        fi

        # Install node_modules if missing (not in the image when source is mounted)
        if [ -f "package.json" ] && [ ! -d "node_modules" ]; then
            log_info "Installing Node.js dependencies..."
            npm install --silent 2>/dev/null || true
        fi

        log_success "Source linked — edits in /opt/hermes-agent are live"
    fi
}

# ── Main ─────────────────────────────────────────────────────────────────────

# Run first-time setup before starting supervised services so log/config paths
# exist and gateway can inherit variables from the persisted .env file.
first_run_setup

# Re-link source if host repo is mounted
relink_source

# Source .env into the environment so hermes and supervised services pick up API keys
if [ -f "${HERMES_HOME:-/data/hermes}/.env" ]; then
    set -a
    # shellcheck disable=SC1091
    . "${HERMES_HOME:-/data/hermes}/.env"
    set +a
fi

# Start Docker-in-Docker and other supervised background services (gateway)
start_dockerd

# If the user passed a command, run it; otherwise start hermes CLI
if [ $# -eq 0 ]; then
    exec hermes
else
    exec "$@"
fi
