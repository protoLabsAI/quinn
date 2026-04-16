# Quinn

Autonomous QA Engineer and Release Manager for protoLabs Studio.

Quinn monitors the board across all apps in your portfolio, triages bugs, verifies PRs, generates release notes, and keeps the community informed. Built on LangGraph with three specialized subagents.

## What Quinn Does

- **Board Monitoring** — Scans blocked features, stale PRs, review queue saturation across all configured apps
- **Bug Triage** — Classifies Discord/GitHub bug reports, files them on the protoMaker team board via `file_bug`
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
protoLabs Studio API (protoMaker team board)
GitHub CLI
Discord Webhooks
```

**LLM**: `protolabs/quinn` alias in LiteLLM gateway → `claude-opus-4-6` by default. Swap the model by updating the alias in `stacks/ai/config/litellm/config.yaml` — no Quinn changes needed.

**UI**: Gradio chat interface + A2A endpoint  
**Knowledge**: SQLite + sqlite-vec (QA reports, bug patterns, release history)  
**Observability**: Langfuse tracing, JSONL audit logs, Prometheus metrics

## Deployment

Quinn is deployed as a service in the [`homelab-iac`](https://github.com/protoLabsAI/homelab-iac) AI stack. Two complementary GitHub Actions workflows publish the image:

| Workflow | Trigger | Tags published |
|---|---|---|
| `docker-publish.yml` | every push to `main` | `:latest`, `:sha-<shortsha>` |
| `release.yml` | push of a `v*.*.*` tag (cut by `prepare-release.yml`) | `:v<semver>`, `:<major>.<minor>` |

```
ghcr.io/protolabsai/quinn:latest      # Watchtower polls this every 60s
ghcr.io/protolabsai/quinn:sha-<short> # rollback target for any main commit
ghcr.io/protolabsai/quinn:v<semver>   # immutable, signed semver pin
```

Watchtower auto-pulls `:latest` on the homelab host within ~60s of every main merge, so the deploy is hands-off. Manual restart:

```bash
cd ~/dev/homelab-iac/stacks/ai
infisical run --domain https://secrets.proto-labs.ai/api --env=prod -- docker compose pull quinn
infisical run --domain https://secrets.proto-labs.ai/api --env=prod -- docker compose up -d quinn
```

Quinn's UI is reachable at **http://ava:7873** over the Tailnet (host port) or at `http://quinn:7870` from other services on `ai_default`.

## Releases

Versioning lives in `pyproject.toml` under `[project].version` (single source of truth). Two workflows automate the cadence:

- **`prepare-release.yml`** runs on every non-release PR merge (default `patch` bump) — `python scripts/version.py patch` bumps the version, opens a `prepare-release/vX.Y.Z` PR, auto-merges it once CI passes, then pushes the `vX.Y.Z` tag. `workflow_dispatch` lets the operator pick `patch`/`minor`/`major` manually.
- **`release.yml`** fires on the tag push: builds + pushes the stable semver Docker tags, creates a GitHub Release with filtered commit notes, and posts Claude-rewritten release notes to Discord via `scripts/post-release-notes.mjs`.

Required repo secrets: `GH_PAT` (repo + workflow scope — the default `GITHUB_TOKEN` can't trigger downstream workflows), `ANTHROPIC_API_KEY` (Haiku rewrite of release notes), `DISCORD_RELEASE_WEBHOOK` (channel webhook). Without `ANTHROPIC_API_KEY` the script posts raw commit bullets; without `DISCORD_RELEASE_WEBHOOK` it prints to stdout.

To bump manually: GitHub Actions → "Prepare Release" → Run workflow → choose `bump` level.

## Local development

Edit code in this repo, run tests with `pytest`, and iterate without building the full image. To test the image locally:

```bash
docker build -t quinn:local .
```

The Dockerfile + `seccomp-profile.json` are the canonical build inputs; all runtime wiring (env vars, volumes, networks, tmpfs, security hardening) lives in `homelab-iac/stacks/ai/docker-compose.yml`.

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

### Streaming + push notifications

Quinn advertises `capabilities.streaming: true` and `pushNotifications: true` and serves the full A2A spec surface (`message/send`, `message/stream`, `tasks/get`, `tasks/cancel`, `tasks/resubscribe`, `tasks/pushNotificationConfig/{set,get,list,delete}`). Every SSE event carries a `kind` discriminator (`task` / `status-update` / `artifact-update`) with camelCase wire fields per the spec — required for `@a2a-js/sdk` to route events.

Push-notification callback URLs are SSRF-validated. Trusted internal docker-network agents can be allowlisted via `PUSH_NOTIFICATION_ALLOWED_HOSTS` / `PUSH_NOTIFICATION_ALLOWED_CIDRS` env vars (default-deny otherwise).

### A2A extensions

Quinn declares and emits these extensions on the agent card:

| Extension | What Quinn provides | How Workstacean consumes it |
|---|---|---|
| [`effect-domain-v1`](https://github.com/protoLabsAI/protoWorkstacean/blob/main/docs/extensions/effect-domain-v1.md) | Card declaration: `bug_triage` increments `protomaker_board.data.backlog_count` by +1 (confidence 0.9) | L1 planner ranks Quinn against goals that target world-state selectors |
| [`worldstate-delta-v1`](https://github.com/protoLabsAI/protoWorkstacean/blob/main/docs/extensions/worldstate-delta-v1.md) | Runtime DataPart on the terminal artifact when `file_bug` succeeds — `{op: "inc", path: "data.backlog_count", value: 1}` | Effect-domain interceptor republishes as `world.state.delta` bus events so the GOAP planner's cached snapshot updates without polling |
| [`cost-v1`](https://github.com/protoLabsAI/protoWorkstacean/blob/main/docs/extensions/cost-v1.md) | Runtime DataPart on every terminal task that ran an LLM — `{usage: {input_tokens, output_tokens, total_tokens}, durationMs}` (`costUsd` pending — see #27) | Cost interceptor records per-skill samples and publishes `autonomous.cost.quinn.<skill>` events for `agent_fleet_health` |
| `a2a.trace` propagation | Reads caller's Langfuse trace context from `params.metadata["a2a.trace"]`; stamps `caller_trace_id` + `caller_span_id` into Quinn's own trace metadata | Operators can filter Langfuse by `metadata.caller_trace_id` to find every agent trace spawned from a single Workstacean dispatch |

Pending: `confidence-v1`, `blast-v1`, `hitl-mode-v1` — tracked in [#27](https://github.com/protoLabsAI/quinn/issues/27).

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
