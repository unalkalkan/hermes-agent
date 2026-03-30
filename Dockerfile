# ============================================================================
# Hermes Agent — Docker Image (NVIDIA CUDA + Docker-in-Docker)
# ============================================================================
# Build:  docker compose build
# Run:    docker compose up -d
# Shell:  docker compose exec hermes bash
# Chat:   docker compose exec hermes hermes
# Setup:  docker compose exec hermes hermes setup
#
# GPU:    Requires NVIDIA drivers + nvidia-container-toolkit on host.
#         Override CUDA version at build time:
#           docker compose build --build-arg CUDA_BASE=nvidia/cuda:12.6.3-cudnn-runtime-ubuntu22.04
# ============================================================================

# ── Build arg for CUDA base image (must be before any FROM) ──────────────────
ARG CUDA_BASE=nvidia/cuda:12.8.0-cudnn-runtime-ubuntu22.04

FROM docker:27-dind AS dind

# ---------------------------------------------------------------------------
# Main image — NVIDIA CUDA runtime (Ubuntu 22.04 base)
# ---------------------------------------------------------------------------
# Adapt the base image to match your GPU / driver:
#   RTX 30xx/40xx  → 12.4, 12.6, or 12.8
#   RTX 50xx       → 12.8 or 13.0 (when available)
# Fall back to the newest CUDA runtime your driver supports.
FROM ${CUDA_BASE}

# Avoid interactive prompts during package installation
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    # Make NVIDIA runtime visible inside the container
    NVIDIA_VISIBLE_DEVICES=all \
    NVIDIA_DRIVER_CAPABILITIES=compute,utility

# ── System packages ──────────────────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
        # Essentials
        ca-certificates curl wget git openssh-client gnupg lsb-release \
        # Build tools (needed by some Python C-extension wheels + flash-attn)
        build-essential python3-dev libffi-dev \
        # Useful CLI utilities
        ripgrep jq less procps vim-tiny \
        # Audio / TTS
        ffmpeg sox libsndfile1 \
        # iptables for Docker-in-Docker networking
        iptables \
        # supervisor for managing dockerd alongside our entrypoint
        supervisor \
    && rm -rf /var/lib/apt/lists/*

# ── Docker CLI + dockerd from official DinD image ───────────────────────────
COPY --from=dind /usr/local/bin/docker          /usr/local/bin/docker
COPY --from=dind /usr/local/bin/dockerd         /usr/local/bin/dockerd
COPY --from=dind /usr/local/bin/docker-init     /usr/local/bin/docker-init
COPY --from=dind /usr/local/bin/docker-proxy    /usr/local/bin/docker-proxy
COPY --from=dind /usr/local/bin/containerd      /usr/local/bin/containerd
COPY --from=dind /usr/local/bin/containerd-shim-runc-v2 /usr/local/bin/containerd-shim-runc-v2
COPY --from=dind /usr/local/bin/ctr             /usr/local/bin/ctr
COPY --from=dind /usr/local/bin/runc            /usr/local/bin/runc

# Docker compose plugin
COPY --from=docker/compose-bin:latest /docker-compose /usr/local/lib/docker/cli-plugins/docker-compose

# ── Node.js 22 LTS ──────────────────────────────────────────────────────────
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# ── uv (fast Python package manager) ────────────────────────────────────────
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# ── Python 3.11 via uv ──────────────────────────────────────────────────────
RUN uv python install 3.11

# ── Hermes Agent source ─────────────────────────────────────────────────────
WORKDIR /opt/hermes-agent
COPY . .

# Initialize mini-swe-agent submodule if present (may not be in COPY context)
RUN if [ -f "mini-swe-agent/pyproject.toml" ]; then \
        echo "mini-swe-agent submodule found"; \
    else \
        echo "mini-swe-agent submodule not found — will be fetched at runtime if .git exists"; \
    fi

# ── Virtual environment + dependencies ───────────────────────────────────────
# Venv lives OUTSIDE the source tree so a bind-mount of the host repo onto
# /opt/hermes-agent at runtime does not clobber installed packages.
RUN uv venv /opt/hermes-venv --python 3.11

ENV VIRTUAL_ENV=/opt/hermes-venv
ENV PATH="/opt/hermes-venv/bin:$PATH"

# Install the main package with all extras
RUN uv pip install -e ".[all]" || uv pip install -e "."

# Install mini-swe-agent if available
RUN if [ -f "mini-swe-agent/pyproject.toml" ]; then \
        uv pip install -e "./mini-swe-agent"; \
    fi

# Install Node.js deps (browser tools)
RUN if [ -f "package.json" ]; then npm install --silent 2>/dev/null || true; fi

# Install Playwright Chromium for browser automation
RUN npx playwright install --with-deps chromium 2>/dev/null || true

# ── HERMES_HOME → /data/hermes (mounted volume) ─────────────────────────────
ENV HERMES_HOME=/data/hermes

# Make hermes available system-wide
RUN ln -sf /opt/hermes-venv/bin/hermes /usr/local/bin/hermes

# ── Supervisor config for dockerd ────────────────────────────────────────────
RUN mkdir -p /etc/supervisor/conf.d /var/log/supervisor
COPY docker/supervisord.conf /etc/supervisor/supervisord.conf

# ── Entrypoint ───────────────────────────────────────────────────────────────
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Default working directory for agent sessions
WORKDIR /workspace

ENTRYPOINT ["/entrypoint.sh"]
CMD ["hermes.py"]
