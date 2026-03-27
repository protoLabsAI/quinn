FROM python:3.12-slim

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    git curl ca-certificates build-essential gettext-base \
    && rm -rf /var/lib/apt/lists/*

# Create non-root sandbox user
ARG SANDBOX_UID=1001
RUN useradd -m -s /bin/bash -u ${SANDBOX_UID} sandbox

# Node.js (for agent-browser)
RUN apt-get update && apt-get install -y --no-install-recommends \
    nodejs npm \
    && rm -rf /var/lib/apt/lists/*

# Browser tool: agent-browser + Chromium
RUN npm install -g agent-browser \
    && (agent-browser install --with-deps 2>/dev/null \
        || (apt-get update && apt-get install -y --no-install-recommends chromium && rm -rf /var/lib/apt/lists/*))

# Claude Code CLI (for Claude model access)
RUN npm install -g @anthropic-ai/claude-code

# CLIProxyAPI — OpenAI-compatible proxy that uses Claude Code OAuth
ARG CLIPROXY_VERSION=6.8.55
RUN ARCH=$(dpkg --print-architecture) \
    && curl -fsSL "https://github.com/router-for-me/CLIProxyAPI/releases/download/v${CLIPROXY_VERSION}/CLIProxyAPI_${CLIPROXY_VERSION}_linux_${ARCH}.tar.gz" \
    | tar xz -C /usr/local/bin cli-proxy-api \
    && chmod +x /usr/local/bin/cli-proxy-api

# Python deps (no nanobot — LangGraph only)
RUN pip install --no-cache-dir \
    gradio sqlite-vec httpx uvicorn langfuse prometheus-client pyyaml \
    langchain langchain-openai langgraph websockets

# Install Quinn
COPY tools/ /opt/quinn/tools/
COPY knowledge/ /opt/quinn/knowledge/
COPY graph/ /opt/quinn/graph/
COPY skills/ /opt/quinn/skills/
COPY audit.py /opt/quinn/audit.py
COPY tracing.py /opt/quinn/tracing.py
COPY metrics.py /opt/quinn/metrics.py
COPY chat_ui.py /opt/quinn/chat_ui.py
COPY server.py /opt/quinn/server.py
COPY discord_bot.py /opt/quinn/discord_bot.py
COPY guardrails.py /opt/quinn/guardrails.py
COPY entrypoint.sh /opt/quinn/entrypoint.sh
COPY config/ /opt/quinn/config/
COPY static/ /opt/quinn/static/

RUN chmod +x /opt/quinn/entrypoint.sh

# Sandbox workspace + knowledge/audit dirs
RUN mkdir -p /sandbox /tmp/sandbox /sandbox/audit /sandbox/knowledge \
    && chown -R sandbox:sandbox /sandbox /tmp/sandbox

# Persistent dirs (volumes mounted at runtime)
RUN mkdir -p /opt/.cliproxy \
    && chown -R sandbox:sandbox /opt/.cliproxy

# Drop to sandbox user
USER sandbox
WORKDIR /sandbox

EXPOSE 7870
CMD ["/opt/quinn/entrypoint.sh"]
