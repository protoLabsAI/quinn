# Quinn

Autonomous QA Engineer and Release Manager for protoLabs Studio.

Quinn monitors the board across all apps in your portfolio, triages bugs, verifies PRs, generates release notes, and keeps the community informed. Built on LangGraph with three specialized subagents.

## What Quinn Does

- **Board Monitoring** — Scans blocked features, stale PRs, review queue saturation across all configured apps
- **Bug Triage** — Classifies Discord/GitHub bug reports, files them on the Ava board via `file_bug`
- **PR Verification** — Checks CI status, CodeRabbit threads, auto-merge readiness
- **Release QA** — Runs verification playbooks: typecheck, wiring, endpoint contracts, visual QA
- **Release Notes** — Generates changelogs from git history, merged PRs, and board state
- **Community Updates** — Posts release announcements and QA reports to Discord

## Architecture

```
Discord / GitHub / A2A
        |
        v
Quinn (LangGraph Agent)  ← model: protolabs/quinn (Opus via LiteLLM gateway)
  |-- Auditor subagent  (board_monitor, pr_inspector, github_issues, github_actions)
  |-- Verifier subagent (qa_memory, browser)
  |-- Reporter subagent (qa_memory, discord_feed, release_notes, file_bug)
        |
        v
LiteLLM Gateway (http://gateway:4000)
protoLabs Studio API (Ava board)
GitHub CLI
Discord Webhooks
```

**LLM**: `protolabs/quinn` alias in LiteLLM gateway → `claude-opus-4-6` by default. Swap the model by updating the alias in `stacks/ai/config/litellm/config.yaml` — no Quinn changes needed.

**UI**: Gradio chat interface + A2A endpoint  
**Knowledge**: SQLite + sqlite-vec (QA reports, bug patterns, release history)  
**Observability**: Langfuse tracing, JSONL audit logs, Prometheus metrics

## Quick Start

### Prerequisites

- Docker and Docker Compose
- [Infisical CLI](https://infisical.com/docs/cli/overview) installed and logged in to `secrets.proto-labs.ai`
- LiteLLM gateway running (`stacks/ai`) with `protolabs/quinn` alias configured
- protoLabs Studio server (Ava) running (default: `http://automaker-server:3008`)

### 1. Clone and configure

```bash
git clone https://github.com/protoLabsAI/quinn.git
cd quinn

# Log in to Infisical (one-time)
infisical login --domain https://secrets.proto-labs.ai

# Edit config/qa-config.json with your apps
```

### 2. Run

```bash
infisical run --domain https://secrets.proto-labs.ai/api --env=prod -- docker compose up -d --build
```

Quinn's UI will be available at **http://localhost:7873**.

### 3. Auto-start on boot (systemd)

```bash
sudo cp quinn.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable quinn
sudo systemctl start quinn
```

## Secrets (Infisical AI Project)

All secrets come from the **AI project** in Infisical (`secrets.proto-labs.ai`). The `.infisical.json` in this repo points to project `11e172e0-a1f6-41d5-9464-df72779a7063`.

| Infisical Secret       | Container env var     | Purpose                               |
| ---------------------- | --------------------- | ------------------------------------- |
| `LITELLM_MASTER_KEY`   | `OPENAI_API_KEY`      | LiteLLM gateway auth (required)       |
| `DISCORD_BOT_QUINN`    | `DISCORD_BOT_TOKEN`   | Discord bot for reading channels      |
| `DISCORD_WEBHOOK_ALERTS` | `DISCORD_WEBHOOK_URL` | Discord webhook for publishing      |
| `GITHUB_TOKEN`         | `GITHUB_TOKEN`        | GitHub issue/PR operations (optional) |
| `LANGFUSE_PUBLIC_KEY`  | `LANGFUSE_PUBLIC_KEY` | Tracing (optional)                    |
| `LANGFUSE_SECRET_KEY`  | `LANGFUSE_SECRET_KEY` | Tracing (optional)                    |

> **`GITHUB_TOKEN` setup**: Create a fine-grained PAT scoped to `protoLabsAI/protoMaker` with Contents (Read), Issues (R/W), Pull Requests (R/W), Actions (R/W), Metadata (Read). Add to the AI Infisical project as `GITHUB_TOKEN`.

The entrypoint automatically maps `DISCORD_BOT_QUINN` → `DISCORD_BOT_TOKEN`.

## LiteLLM Gateway Setup

Quinn routes all LLM calls through the protoLabs AI gateway. Two things must be configured there:

### 1. Model alias (`stacks/ai/config/litellm/config.yaml`)

```yaml
- model_name: protolabs/quinn
  litellm_params:
    model: anthropic/claude-opus-4-6
    api_key: os.environ/ANTHROPIC_API_KEY
```

To swap Quinn's model, update this alias and reload the gateway — no changes needed in Quinn.

### 2. Agent entry (for calling Quinn via the gateway)

```yaml
- model_name: quinn
  litellm_params:
    model: openai/quinn
    api_base: http://quinn:7870/v1
    api_key: quinn-internal
```

Quinn joins the `ai_default` and `automaker-staging_default` Docker networks so the gateway can reach it at `quinn:7870`.

## A2A Protocol

Quinn implements the [Google A2A protocol](https://github.com/google/A2A) for agent-to-agent communication.

### Agent card

```bash
curl http://localhost:7873/.well-known/agent.json
```

### Send a message (JSON-RPC 2.0)

```bash
curl http://localhost:7873/a2a \
  -H "Content-Type: application/json" \
  -d '{
    "jsonrpc": "2.0",
    "id": "1",
    "method": "message/send",
    "params": {
      "message": {
        "role": "user",
        "parts": [{"kind": "text", "text": "triage this bug and file it: button crash in Safari"}]
      }
    }
  }'
```

Quinn has 4 A2A skills: `qa_report`, `board_audit`, `bug_triage`, `pr_review`.

### Register in LiteLLM Agent Hub (Phase 3)

Once registered at `ai.proto-labs.ai/ui`, other agents can call Quinn via:

```python
response = client.chat.completions.create(
    model="a2a/quinn",
    messages=[{"role": "user", "content": "Run a QA audit."}]
)
```

## API

### Chat endpoint

```
POST http://localhost:7873/api/chat
{"message": "<command or natural language>", "session_id": "optional"}
```

### OpenAI-compatible

```bash
curl http://localhost:7873/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model": "quinn", "messages": [{"role": "user", "content": "/report"}]}'
```

## Chat Commands

| Command              | Description                                        |
| -------------------- | -------------------------------------------------- |
| `/report`            | Generate QA digest and publish to Discord          |
| `/audit`             | Full board scan across all configured apps         |
| `/qa [version]`      | Run QA playbook for a specific version             |
| `/triage`            | Triage open GitHub issues                          |
| `/release [version]` | Generate release notes                             |
| `/bugs`              | Show active bug reports across apps                |
| `/status`            | Quick health check                                 |

## E2E Smoke Test

Validates the full Discord → Quinn → Ava pipeline:

```bash
python tests/test_e2e_smoke.py
# or against specific hosts:
python tests/test_e2e_smoke.py --quinn http://ava:7873 --ava http://ava:3008
```

Tests: agent card discovery, A2A `/report`, bug triage → `file_bug` → Ava board, board verification.

## Multi-App Support

Configure in `config/qa-config.json`:

```json
{
  "apps": [
    {
      "name": "protoMaker",
      "projectPath": "/path/to/project",
      "serverUrl": "http://localhost:3008",
      "githubRepo": "org/repo"
    }
  ]
}
```

## Part of protoLabs

Quinn is part of the [protoLabs](https://protolabs.studio) autonomous development studio.

| Agent     | Role                                             |
| --------- | ------------------------------------------------ |
| **Ava**   | Chief of Staff — orchestration and strategy      |
| **Quinn** | QA Engineer — testing, triage, and release notes |
