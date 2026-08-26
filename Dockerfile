FROM python:3.12-slim

# System deps. tmux is required by Cookbook for background downloads/serves.
# openssh-client is required for Cookbook remote server tests, setup, probes,
# downloads, and serves from Docker installs.
# git/cmake are required when Cookbook builds llama.cpp on first llama.cpp
# launch inside Docker.
# nodejs/npm provide npx for the optional built-in Browser MCP server.
# gosu lets the entrypoint drop privileges cleanly so signals still reach
# uvicorn directly (no extra shell layer like `su`/`sudo` would add).
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    cmake \
    curl \
    git \
    nodejs \
    npm \
    tmux \
    openssh-client \
    gosu \
    socat \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first (layer cache). Optional extras (PyMuPDF AGPL, etc.)
# are opt-in so the default image stays MIT-core; see requirements-optional.txt.
ARG INSTALL_OPTIONAL=false
COPY requirements.txt requirements-optional.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && if [ "$INSTALL_OPTIONAL" = "true" ]; then pip install --no-cache-dir -r requirements-optional.txt; fi

# Bundle the Omnigent agent CLI in its OWN isolated venv — its deps conflict
# with Odysseus's if installed in the same environment. Installed world-readable
# (UV_TOOL_BIN_DIR on PATH, venv under /opt) so the dropped PUID/PGID runtime
# user can launch it. The Launch button starts its server; socat (see the
# entrypoint) bridges its loopback-only web UI to the published :6868.
ENV UV_TOOL_DIR=/opt/uv/tools \
    UV_TOOL_BIN_DIR=/usr/local/bin
ARG OMNIGENT_VERSION=0.10.0
RUN pip install --no-cache-dir uv \
    && uv tool install "omnigent==${OMNIGENT_VERSION}" \
    && chmod -R a+rX /opt/uv \
    && rm -rf /root/.cache

# Claude Code + Codex CLIs so their native harnesses are available as Omnigent
# sub-agents alongside the API-model workers. Each still needs a one-time
# interactive subscription login (the API/W&B workers need no login).
ARG CLAUDE_CODE_VERSION=2.1.245
ARG CODEX_VERSION=0.150.0
RUN npm install -g \
    "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
    "@openai/codex@${CODEX_VERSION}"

# Copy app code
COPY . .

# The app runs as a non-root UID, so apply the generated-crew picker metadata
# patch while building and keep Omnigent's installed package read-only at runtime.
RUN python -c "from src.omnigent_manager import OmnigentManager; OmnigentManager(command='/usr/local/bin/omnigent')._patch_picker_metadata('/usr/local/bin/omnigent')" \
    && grep -R -q "ODYSSEUS_GENERATED_CREW_PICKER_PATCH" \
        /opt/uv/tools/omnigent/lib/python*/site-packages/omnigent/server/routes/builtin_agents.py

# Create data directory (mount a volume here for persistence)
RUN mkdir -p data logs services/cache/search

# Entrypoint that drops to PUID/PGID (default 1000:1000) and repairs
# ownership on the bind-mounted /app/data and /app/logs. Without this,
# the container runs as root and writes root-owned files into host
# bind mounts — any later non-root run (or a host user trying to
# update them) silently fails on EPERM, breaking skill extraction,
# prefs persistence, mail attachments, etc.
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

EXPOSE 7000

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "7000"]
