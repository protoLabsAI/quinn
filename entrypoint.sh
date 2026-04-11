#!/bin/bash
# Quinn — container entrypoint
# Secrets are injected by `infisical run` wrapping this script.
# See docker-compose.yml for the infisical run command.

echo "[entrypoint] Starting Quinn"

# Create dirs inside tmpfs home
mkdir -p /home/sandbox/.local

# Ensure persistent volume dirs exist
mkdir -p /sandbox/audit /sandbox/knowledge

# Map Quinn-specific secret names to standard env vars
if [ -n "$DISCORD_BOT_QUINN" ] && [ -z "$DISCORD_BOT_TOKEN" ]; then
    export DISCORD_BOT_TOKEN="$DISCORD_BOT_QUINN"
    echo "[entrypoint] Mapped DISCORD_BOT_QUINN -> DISCORD_BOT_TOKEN"
fi

# ── GitHub App authentication ────────────────────────────────────────────────
# Quinn needs to post formal PR reviews as @protoquinn[bot]. That requires
# an installation token minted from the Quinn GitHub App credentials (not a
# plain PAT, which would post as whoever owns the token). Mint one now so
# gh_cli.py has something to read immediately, then spawn a background
# refresher that keeps the token file fresh every 45 minutes.
if [ -n "$QUINN_APP_ID" ] && [ -n "$QUINN_APP_PRIVATE_KEY" ]; then
    echo "[entrypoint] Minting initial GitHub App installation token..."
    if python /opt/quinn/tools/github_app_auth.py once; then
        echo "[entrypoint] Token minted, starting refresher in background"
        python /opt/quinn/tools/github_app_auth.py daemon &
    else
        echo "[entrypoint] WARNING: initial token mint failed — gh calls will fall back to GITHUB_TOKEN env var"
    fi
else
    echo "[entrypoint] QUINN_APP_ID / QUINN_APP_PRIVATE_KEY not set — gh calls will use GITHUB_TOKEN env var directly"
fi

# Copy persona into workspace
mkdir -p /sandbox/skills
cp /opt/quinn/config/SOUL.md /sandbox/SOUL.md

# Copy skills into workspace
cp -r /opt/quinn/skills /sandbox/skills

# Copy QA config
cp /opt/quinn/config/qa-config.json /sandbox/qa-config.json

# Start Quinn
exec python /opt/quinn/server.py
