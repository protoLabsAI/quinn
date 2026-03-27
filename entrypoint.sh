#!/bin/bash
# Quinn — container entrypoint

echo "[entrypoint] Starting Quinn"

# Create dirs inside tmpfs home
mkdir -p /home/sandbox/.local

# Ensure persistent volume dirs exist
mkdir -p /sandbox/audit /sandbox/knowledge

# --- Infisical secret injection ---
# Pull secrets from Infisical at startup if configured.
# Requires INFISICAL_TOKEN (service token or machine identity) in the environment.
if [ -n "$INFISICAL_TOKEN" ] && command -v infisical &>/dev/null; then
    echo "[entrypoint] Pulling secrets from Infisical..."
    INFISICAL_DOMAIN="${INFISICAL_API_URL:-https://secrets.proto-labs.ai}"
    INFISICAL_PROJECT="${INFISICAL_PROJECT_ID:-11e172e0-a1f6-41d5-9464-df72779a7063}"
    INFISICAL_ENV="${INFISICAL_ENVIRONMENT:-prod}"

    # Export secrets as env vars for this process tree
    eval "$(infisical export \
        --domain "$INFISICAL_DOMAIN" \
        --projectId "$INFISICAL_PROJECT" \
        --env "$INFISICAL_ENV" \
        --format dotenv \
        --token "$INFISICAL_TOKEN" 2>/dev/null | sed 's/^/export /')" \
    && echo "[entrypoint] Secrets loaded from Infisical ($INFISICAL_ENV)" \
    || echo "[entrypoint] WARNING: Infisical secret fetch failed, falling back to env vars"
else
    echo "[entrypoint] Infisical not configured, using direct env vars"
fi

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

# --- Claude credentials ---
mkdir -p /home/sandbox/.claude

if [ -n "$CLAUDE_OAUTH_CREDENTIALS" ]; then
    echo "$CLAUDE_OAUTH_CREDENTIALS" > /home/sandbox/.claude/.credentials.json
    chmod 600 /home/sandbox/.claude/.credentials.json
    echo "[entrypoint] Claude credentials loaded from env var"
elif [ -f /opt/claude-creds/.credentials.json ]; then
    cp /opt/claude-creds/.credentials.json /home/sandbox/.claude/.credentials.json
    chmod 600 /home/sandbox/.claude/.credentials.json
    echo "[entrypoint] Claude credentials loaded from mounted volume"
fi

# --- CLIProxyAPI — OpenAI-compatible proxy for Claude OAuth ---
mkdir -p /opt/.cliproxy
cp /opt/quinn/config/cliproxy-config.yaml /opt/.cliproxy/config.yaml

# Inject OAuth token into CLIProxyAPI config
inject_token() {
    python3 -c "
import json, yaml, time, sys

with open('/opt/claude-creds/.credentials.json') as f:
    creds = json.load(f)

oauth = creds.get('claudeAiOauth', {})
token = oauth.get('accessToken', '')
if not token:
    print('[token-refresh] No token found in credentials')
    sys.exit(0)

with open('/opt/.cliproxy/config.yaml') as f:
    cfg = yaml.safe_load(f)

old_token = ''
if cfg.get('claude-api-key'):
    old_token = cfg['claude-api-key'][0].get('api-key', '')

cfg['claude-api-key'] = [{'api-key': token}]

with open('/opt/.cliproxy/config.yaml', 'w') as f:
    yaml.dump(cfg, f, default_flow_style=False)

if token != old_token:
    print(f'[token-refresh] New OAuth token injected at {time.strftime(\"%H:%M:%S\")}')
else:
    print('[token-refresh] Token unchanged')
"
}

inject_token

cli-proxy-api --config /opt/.cliproxy/config.yaml &
echo "[entrypoint] CLIProxyAPI started on port 8317"

# Wait for CLIProxyAPI to be ready
for i in $(seq 1 15); do
    MODEL_COUNT=$(curl -sf http://127.0.0.1:8317/v1/models -H "Authorization: Bearer quinn-internal" 2>/dev/null | python3 -c "import sys,json; print(len(json.loads(sys.stdin.read()).get('data',[])))" 2>/dev/null || echo "0")
    if [ "$MODEL_COUNT" -gt "0" ]; then
        echo "[entrypoint] CLIProxyAPI ready ($MODEL_COUNT models)"
        break
    fi
    sleep 1
done

# Set env vars for LangGraph to route through CLIProxyAPI
export OPENAI_API_KEY="quinn-internal"
export OPENAI_API_BASE="http://127.0.0.1:8317/v1"

# --- Token refresh loop ---
(
    LAST_TOKEN=""
    while true; do
        sleep 60
        if [ -f /opt/claude-creds/.credentials.json ]; then
            NEW_TOKEN=$(python3 -c "
import json
with open('/opt/claude-creds/.credentials.json') as f:
    print(json.load(f).get('claudeAiOauth',{}).get('accessToken',''))
" 2>/dev/null)
            if [ -n "$NEW_TOKEN" ] && [ "$NEW_TOKEN" != "$LAST_TOKEN" ]; then
                LAST_TOKEN="$NEW_TOKEN"
                inject_token
                kill $(pidof cli-proxy-api) 2>/dev/null
                sleep 1
                cli-proxy-api --config /opt/.cliproxy/config.yaml &
                echo "[token-refresh] CLIProxyAPI restarted with new token"
            fi
        fi
    done
) &
echo "[entrypoint] Token refresh watcher started (every 60s)"

# Start Quinn Gradio UI on port 7870
exec python /opt/quinn/server.py
