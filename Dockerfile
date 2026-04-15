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

# Install agent-browser CLI globally (binary available to all users via npm global bin)
RUN npm install -g agent-browser@0.24.1

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

# Install Chrome system library dependencies (what `agent-browser install --with-deps` would install
# via sudo, but here we do it as root directly since we're in a Docker build context).
RUN apt-get update && apt-get install -y --no-install-recommends \
    libxcb-shm0 libx11-xcb1 libx11-6 libxcb1 libxext6 libxrandr2 libxcomposite1 \
    libxcursor1 libxdamage1 libxfixes3 libxi6 libgtk-3-0 libpangocairo-1.0-0 \
    libpango-1.0-0 libatk1.0-0 libcairo-gobject2 libcairo2 libgdk-pixbuf-2.0-0 \
    libxrender1 libasound2 libfreetype6 libfontconfig1 libdbus-1-3 libnss3 libnspr4 \
    libatk-bridge2.0-0 libdrm2 libxkbcommon0 libatspi2.0-0 libcups2 libxshmfence1 libgbm1 \
    fonts-noto-color-emoji fonts-noto-cjk fonts-freefont-ttf \
    && rm -rf /var/lib/apt/lists/*

# Install Chrome for Testing to /opt/browser-cache, outside /home/sandbox.
# /home/sandbox is mounted as tmpfs at runtime (read_only container), which shadows the image layer —
# anything written there during build is invisible at runtime.
# HOME=/opt/browser-cache directs Chrome into /opt/browser-cache/.agent-browser/browsers/
# which is a stable image-layer path not covered by any tmpfs mount.
# chown ensures the sandbox user can execute the Chrome binary at runtime.
RUN HOME=/opt/browser-cache agent-browser install \
    && chown -R sandbox:sandbox /opt/browser-cache

USER sandbox
WORKDIR /sandbox

# At runtime, agent-browser resolves the Chrome binary via AGENT_BROWSER_EXECUTABLE_PATH.
# Chrome was installed to /opt/browser-cache/.agent-browser/browsers/ (agent-browser's data dir,
# resolved via HOME during the build step). HOME stays /home/sandbox (tmpfs) at runtime
# for all other sandbox operations — we pin the exact chrome binary path instead.
# NOTE: the chrome-147.0.7727.50 path is in lockstep with agent-browser@0.24.1 (line 28).
# If you bump agent-browser, re-run `agent-browser install` and update this path to match.
ENV AGENT_BROWSER_EXECUTABLE_PATH=/opt/browser-cache/.agent-browser/browsers/chrome-147.0.7727.50/chrome

EXPOSE 7870
CMD ["/opt/quinn/entrypoint.sh"]
