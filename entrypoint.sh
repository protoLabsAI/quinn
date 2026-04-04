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

# Copy persona into workspace
mkdir -p /sandbox/skills
cp /opt/quinn/config/SOUL.md /sandbox/SOUL.md

# Copy skills into workspace
cp -r /opt/quinn/skills /sandbox/skills

# Copy QA config
cp /opt/quinn/config/qa-config.json /sandbox/qa-config.json

# Start Quinn
exec python /opt/quinn/server.py
