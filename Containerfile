# TradeCentral — Claude Code sandbox
# Build:  podman build -t tradecentral .
# Run:    see bottom of this file

FROM fedora:latest

# ── System dependencies ────────────────────────────────────────────────────────
# Node.js + npm   → claude-code
# Python 3 + pip  → project deps
# WeasyPrint 61+  → needs fontconfig + fonts (no longer needs cairo/pango)
# git + openssh   → git push over SSH
RUN dnf install -y \
        nodejs npm \
        python3 python3-pip \
        fontconfig \
        liberation-fonts \
        dejavu-fonts-all \
        git \
        openssh-clients \
    && dnf clean all

# ── claude-code (global) ───────────────────────────────────────────────────────
RUN npm install -g @anthropic-ai/claude-code

# ── Python dependencies ────────────────────────────────────────────────────────
WORKDIR /app
COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

# ── Git: trust the mounted repo ───────────────────────────────────────────────
# Without this, git refuses to operate on a directory owned by a different uid
RUN git config --global --add safe.directory /app

# ── Default working directory ─────────────────────────────────────────────────
WORKDIR /app

CMD ["bash"]

# ── How to run (Option A — two containers, shared volume) ──────────────────────
#
# Step 1: Build the image (once, or after requirements change)
#   podman build -t tradecentral .
#
# Step 2: Start the alert bot (detached, always-on)
#   podman run -d --rm \
#     --name tradecentral-bot \
#     --cpus 1 --memory 512m --memory-swap 512m \
#     -v ~/Code/algotrading:/app:z \
#     --env-file ~/Code/algotrading/.env \
#     tradecentral \
#     python3 run.py
#
# Step 3: Start the Claude loop (interactive, only when you want analyses run)
#   podman run -it --rm \
#     --name tradecentral-claude \
#     --cpus 2 --memory 2g --memory-swap 2g \
#     -v ~/Code/algotrading:/app:z \
#     -v ~/.claude:/root/.claude:z \
#     -v ~/.ssh:/root/.ssh:z,ro \
#     -v ~/.gitconfig:/root/.gitconfig:z,ro \
#     --env-file ~/Code/algotrading/.env \
#     tradecentral \
#     bash -c "cd /app && claude --dangerously-skip-permissions"
#
# Then inside Claude:
#   /loop 2m Follow the instructions in LOOP_PROMPT.md
#
# Notes:
#   Both containers mount the same ~/Code/algotrading volume — file-based IPC
#   (requests/ queue) works automatically. No ports or networking needed between them.
#   ~/.claude     → carries your Claude.ai subscription auth tokens (no API key needed)
#   ~/.ssh        → needed for git push over SSH (read-only)
#   ~/.gitconfig  → carries your name/email for commits
#   --env-file    → pass your .env (Telegram token, etc.) — never COPY it into the image
#   :z            → tells SELinux to relabel the mount (required on Fedora)
#   --rm          → container auto-deletes on exit (state is safe — all in mounted volume)
#
# To stop the bot:   podman stop tradecentral-bot
# To view bot logs:  podman logs -f tradecentral-bot
