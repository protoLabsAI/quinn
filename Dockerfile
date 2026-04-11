FROM python:3.12-slim

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    git curl ca-certificates build-essential gettext-base gnupg \
    && rm -rf /var/lib/apt/lists/*

# GitHub CLI — required by tools/pr_inspector.py (gh pr review --approve/--request-changes)
# Installed from GitHub's official apt repo so it stays current.
RUN mkdir -p -m 755 /etc/apt/keyrings \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | tee /etc/apt/keyrings/githubcli-archive-keyring.gpg >/dev/null \
    && chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends gh \
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

# Python deps (no nanobot — LangGraph only)
# pyjwt[crypto] pulls in cryptography for RS256 signing of GitHub App JWTs.
RUN pip install --no-cache-dir \
    gradio sqlite-vec httpx uvicorn langfuse prometheus-client pyyaml \
    langchain langchain-openai langgraph websockets \
    'pyjwt[crypto]'

# Install Quinn
COPY nanobot/ /opt/quinn/nanobot/
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

# Drop to sandbox user
ENV PYTHONPATH=/opt/quinn

USER sandbox
WORKDIR /sandbox

EXPOSE 7870
CMD ["/opt/quinn/entrypoint.sh"]
