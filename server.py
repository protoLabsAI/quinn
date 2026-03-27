"""
Quinn -- QA agent for the protoLabs portfolio.

Monitors apps, triages issues, generates release notes, and runs QA playbooks.
Uses LangGraph agent backend.

Usage:
    python server.py                          # default port 7870
    python server.py --config path/to/config  # custom config
"""

import argparse
import asyncio
import json
import os
import re
import time
from pathlib import Path
from typing import Any

from chat_ui import create_chat_app

# ---------------------------------------------------------------------------
# Agent setup
# ---------------------------------------------------------------------------

_graph = None       # LangGraph compiled graph
_graph_config = None  # LangGraphConfig
_checkpointer = None  # LangGraph MemorySaver for session persistence


def _init_langgraph_agent():
    """Initialize the LangGraph agent backend."""
    global _graph, _graph_config, _checkpointer

    from graph.agent import create_quinn_graph
    from graph.config import LangGraphConfig
    from langgraph.checkpoint.memory import MemorySaver

    config_path = Path(__file__).parent / "config" / "langgraph-config.yaml"
    _graph_config = LangGraphConfig.from_yaml(config_path)

    store = _get_store()
    _checkpointer = MemorySaver()

    _graph = create_quinn_graph(
        config=_graph_config,
        knowledge_store=store,
        include_subagents=True,
    )

    print(f"[quinn] LangGraph agent initialized (model: {_graph_config.model_name})")


def _detect_vllm_model(api_base: str) -> str | None:
    """Query vLLM /v1/models to get the currently loaded model."""
    import httpx
    try:
        resp = httpx.get(f"{api_base}/models", timeout=5)
        data = resp.json().get("data", [])
        if data:
            return data[0]["id"]
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Session commands
# ---------------------------------------------------------------------------

_HELP_TEXT = """\
**Quinn QA commands:**
| Command | Description |
|---------|-------------|
| `/new` | Clear chat history + session |
| `/clear` | Clear chat display (session preserved) |
| `/model` | Show current model |
| `/tools` | List registered tools |
| `/audit` | Run full board audit across all configured apps |
| `/qa [version]` | Run QA playbook for a version |
| `/triage` | Triage open GitHub issues |
| `/release [version]` | Generate release notes |
| `/bugs` | Show active bug patterns across apps |
| `/status` | Quick health check across apps |
| `/report` | Generate daily QA digest and publish to Discord |
| `/help` | Show this help |
"""


def _msg(content: str) -> list[dict[str, Any]]:
    return [{"role": "assistant", "content": content}]


async def _handle_command(
    cmd: str, args: str, session_id: str
) -> list[dict[str, Any]] | None:
    if cmd == "help":
        return _msg(_HELP_TEXT)

    if cmd == "clear":
        return [{"role": "assistant", "content": "", "metadata": {"_clear": True}}]

    if cmd == "new":
        return [{"role": "assistant", "content": "", "metadata": {"_new": True}}]

    if cmd == "model":
        if _graph_config is not None:
            return _msg(f"**Model:** `{_graph_config.model_name}`")
        return _msg("**Model:** unknown")

    if cmd == "tools":
        if _graph is not None:
            from tools.lg_tools import get_all_tools
            tools = get_all_tools(_get_store())
            names = sorted(t.name for t in tools)
        else:
            names = []
        listing = "\n".join(f"- `{n}`" for n in names)
        return _msg(f"**Registered tools ({len(names)}):**\n{listing}")

    # QA commands -- delegate to agent as prompts
    if cmd == "audit":
        return await _dispatch_to_agent(
            "Run a full QA audit across all configured apps. "
            "Check board health, CI status, open PRs, and deployment state. "
            "Report any issues found.",
            session_id,
        )

    if cmd == "qa":
        version = args.strip() or "latest"
        return await _dispatch_to_agent(
            f"Run the QA playbook for version {version}. "
            f"Verify endpoints, run regression checks, validate deployment, "
            f"and generate a QA report with PASS/WARN/FAIL verdict.",
            session_id,
        )

    if cmd == "triage":
        return await _dispatch_to_agent(
            "Triage all open GitHub issues across configured apps. "
            "Classify each as: already_fixed, actionable, stale, or duplicate. "
            "Log triage decisions and recommend actions.",
            session_id,
        )

    if cmd == "release":
        version = args.strip() or "latest"
        return await _dispatch_to_agent(
            f"Generate release notes for version {version}. "
            f"Gather merged PRs, categorize changes (features, fixes, breaking), "
            f"and produce a formatted changelog.",
            session_id,
        )

    if cmd == "bugs":
        return await _handle_bugs_command()

    if cmd == "status":
        return await _handle_status_command()

    if cmd == "report":
        return await _handle_report_command(session_id)

    return None


# ---------------------------------------------------------------------------
# QA commands
# ---------------------------------------------------------------------------

_knowledge_store = None


def _get_store():
    global _knowledge_store
    if _knowledge_store is None:
        from knowledge.store import KnowledgeStore
        _knowledge_store = KnowledgeStore()
    return _knowledge_store


async def _dispatch_to_agent(
    prompt: str, session_id: str,
) -> list[dict[str, Any]]:
    """Send a prompt to the LangGraph agent and return the response."""
    if _graph is not None:
        return await _chat_langgraph(prompt, session_id)
    return _msg("**Error:** No agent backend initialized.")


async def _handle_bugs_command() -> list[dict[str, Any]]:
    """Show active (unresolved) bug patterns across all apps."""
    store = _get_store()
    bugs = store.get_bug_patterns(unresolved_only=True, limit=30)
    if not bugs:
        return _msg("No active bug patterns recorded.")

    lines = ["**Active Bug Patterns:**"]
    for b in bugs:
        app = b.get("app_name", "?") or "global"
        sev = b.get("severity", "?")
        occ = b.get("occurrences", 1)
        lines.append(
            f"- [{sev}] **{b['title']}** ({app}) -- seen {occ}x, last: {b.get('last_seen', '')[:10]}"
        )
    return _msg("\n".join(lines))


async def _handle_status_command() -> list[dict[str, Any]]:
    """Quick health check: show stats and recent activity."""
    store = _get_store()
    stats = store.get_stats()
    apps = store.get_apps()

    lines = ["**Quinn QA Status:**", ""]
    lines.append(f"QA reports: {stats.get('qa_reports', 0)}")
    lines.append(f"Bug patterns (active): {stats.get('bug_patterns', 0)}")
    lines.append(f"Release notes: {stats.get('release_notes', 0)}")
    lines.append(f"Triage entries: {stats.get('triage_log', 0)}")
    lines.append(f"Tracked apps: {stats.get('apps', 0)}")

    if apps:
        lines.append("\n**Tracked Apps:**")
        for a in apps:
            last = a.get("last_checked_at", "never") or "never"
            lines.append(f"- **{a['name']}** -- last checked: {last[:10] if last != 'never' else 'never'}")

    # Show recent reports
    recent = store.get_reports(limit=5)
    if recent:
        lines.append("\n**Recent Reports:**")
        for r in recent:
            lines.append(
                f"- [{r['verdict']}] {r['app_name']} {r.get('version', '')} "
                f"({r.get('scope', '')}) -- {r.get('created_at', '')[:10]}"
            )

    return _msg("\n".join(lines))


async def _handle_report_command(session_id: str) -> list[dict[str, Any]]:
    """Generate a daily QA digest and optionally publish to Discord."""
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "")

    store = _get_store()
    stats = store.get_stats()
    bugs = store.get_bug_patterns(unresolved_only=True, limit=10)
    reports = store.get_reports(limit=10)
    apps = store.get_apps()

    from datetime import datetime, timezone
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    lines = [f"**Quinn QA Daily Digest -- {date_str}**\n"]

    if stats:
        lines.append(
            f"**Knowledge Base:** {stats.get('qa_reports', 0)} reports, "
            f"{stats.get('bug_patterns', 0)} bug patterns, "
            f"{stats.get('triage_log', 0)} triage entries\n"
        )

    if reports:
        lines.append("**Recent QA Reports:**")
        for r in reports[:5]:
            lines.append(
                f"- [{r['verdict']}] {r['app_name']} {r.get('version', '')} "
                f"({r.get('checks_passed', 0)}/{r.get('checks_total', 0)} checks)"
            )
        lines.append("")

    if bugs:
        lines.append("**Active Bug Patterns:**")
        for b in bugs[:5]:
            sev = b.get("severity", "?")
            lines.append(f"- [{sev}] {b['title']} ({b.get('app_name', 'global')})")
        lines.append("")

    if apps:
        lines.append("**Tracked Apps:** " + ", ".join(a["name"] for a in apps))

    lines.append(f"\n_Generated by Quinn QA -- protoLabs.studio_")

    digest_content = "\n".join(lines)

    # Store as a report
    store.add_report(
        app_name="all",
        content=digest_content,
        verdict="INFO",
        scope="digest",
    )

    if not webhook_url:
        return _msg(f"{digest_content}\n\n_DISCORD_WEBHOOK_URL not set -- not published._")

    # Publish via webhook
    import httpx
    payload = {
        "username": "Quinn QA",
        "embeds": [{
            "title": f"QA Daily Digest -- {date_str}",
            "description": digest_content[:4096],
            "color": 0x14b8a6,
        }],
    }

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(webhook_url, json=payload)
            if resp.status_code in (200, 204):
                return _msg(f"**Published to Discord.**\n\n{digest_content}")
            return _msg(f"**Error:** Discord returned {resp.status_code}: {resp.text[:200]}")
    except Exception as e:
        return _msg(f"**Error publishing:** {e}")


# ---------------------------------------------------------------------------
# Chat function
# ---------------------------------------------------------------------------


def _strip_think(text: str) -> str:
    text = re.sub(r"<think>[\s\S]*?</think>", "", text)
    text = re.sub(r"</think>\s*", "", text)
    return text.strip()


import queue as _queue_mod


async def chat(message: str, session_id: str) -> list[dict[str, Any]]:
    """Route to the LangGraph backend."""
    # Slash commands
    stripped = message.strip()
    if stripped.startswith("/"):
        parts = stripped.split(None, 1)
        cmd = parts[0][1:].lower()
        args = parts[1] if len(parts) > 1 else ""
        result = await _handle_command(cmd, args, session_id)
        if result is not None:
            return result

    # Route to LangGraph
    if _graph is not None:
        return await _chat_langgraph(message, session_id)
    return _msg("**Error:** No agent backend initialized.")


async def _chat_langgraph(message: str, session_id: str) -> list[dict[str, Any]]:
    """Process via LangGraph agent backend."""
    import tracing
    from langchain_core.messages import HumanMessage, AIMessage

    tracing.start_trace(session_id=session_id, name="quinn-chat-lg", metadata={"message_preview": message[:100]})
    try:
        # Invoke the graph with session-scoped checkpointing
        config = {"configurable": {"thread_id": f"gradio:{session_id}"}}
        if _checkpointer:
            config["checkpointer"] = _checkpointer

        result = await _graph.ainvoke(
            {"messages": [HumanMessage(content=message)], "session_id": session_id},
            config=config,
        )

        # Extract the last AI message
        messages = result.get("messages", [])
        response = ""
        for msg in reversed(messages):
            if isinstance(msg, AIMessage) and msg.content:
                response = msg.content if isinstance(msg.content, str) else str(msg.content)
                break

        response = _strip_think(response)
        return [{"role": "assistant", "content": response}]
    except Exception as e:
        return [{"role": "assistant", "content": f"**Error:** {e}"}]
    finally:
        tracing.end_trace()


def chat_streaming(message: str, history: list[dict], session_id: str):
    """Streaming wrapper -- yields incremental history updates as tools run."""
    import threading

    result_queue: _queue_mod.Queue = _queue_mod.Queue()

    original_chat = chat

    async def _run():
        try:
            result = await original_chat(message, session_id)
            result_queue.put(("done", result))
        except Exception as e:
            result_queue.put(("error", str(e)))

    # Run agent in a background thread
    def _thread():
        asyncio.run(_run())

    t = threading.Thread(target=_thread, daemon=True)
    t.start()

    # Poll for progress and yield updates
    placeholder_shown = False
    while t.is_alive():
        try:
            status, data = result_queue.get(timeout=0.5)
            if status == "done":
                for msg in data:
                    meta = msg.get("metadata", {})
                    if meta.get("_clear"):
                        yield [], session_id
                        return
                    if meta.get("_new"):
                        import secrets as _s
                        yield [], _s.token_hex(4)
                        return
                history.extend(data)
                yield history, session_id
                return
            elif status == "error":
                history.append({"role": "assistant", "content": f"**Error:** {data}"})
                yield history, session_id
                return
        except _queue_mod.Empty:
            # Show a working indicator if nothing yet
            if not placeholder_shown:
                history.append({
                    "role": "assistant",
                    "metadata": {"title": "Working..."},
                    "content": "",
                })
                placeholder_shown = True
                yield history, session_id

    # Thread finished, get final result
    try:
        status, data = result_queue.get_nowait()
        if placeholder_shown:
            history.pop()  # remove working indicator
        if status == "done":
            for msg in data:
                meta = msg.get("metadata", {})
                if meta.get("_clear"):
                    yield [], session_id
                    return
                if meta.get("_new"):
                    import secrets as _s
                    yield [], _s.token_hex(4)
                    return
            history.extend(data)
        elif status == "error":
            history.append({"role": "assistant", "content": f"**Error:** {data}"})
    except _queue_mod.Empty:
        if placeholder_shown:
            history.pop()
        history.append({"role": "assistant", "content": "*Task completed.*"})

    yield history, session_id


# ---------------------------------------------------------------------------
# Settings callbacks
# ---------------------------------------------------------------------------


def _build_settings_callbacks() -> dict:
    def get_tools_list() -> str:
        if _graph is not None:
            from tools.lg_tools import get_all_tools
            tools = get_all_tools(_get_store())
            names = sorted(t.name for t in tools)
        else:
            names = []
        return "\n".join(f"- `{n}`" for n in names) or "No tools registered."

    def get_model_info() -> str:
        if _graph_config is not None:
            model = _graph_config.model_name
            return f"**Model:** `{model}`\n\n**Backend:** LangGraph"
        return "**Model:** unknown"

    def get_provider_choices() -> list[str]:
        choices = []
        # Check vLLM directly
        detected = _detect_vllm_model("http://host.docker.internal:8000/v1")
        if detected:
            choices.append(f"local: {detected}")
        # Claude models via CLIProxyAPI (OAuth)
        choices.extend([
            "claude: claude-sonnet-4-6",
            "claude: claude-haiku-4-5",
            "claude: claude-opus-4-6",
        ])
        return choices

    def get_current_provider() -> str:
        if _graph_config is not None:
            model = _graph_config.model_name
        else:
            model = "unknown"
        if model.startswith("claude-"):
            current = f"claude: {model}"
        else:
            current = f"local: {model}"
        choices = get_provider_choices()
        if current not in choices and choices:
            return choices[0]
        return current

    def switch_provider(choice: str) -> str:
        global _graph, _graph_config
        if not choice:
            return "No provider selected."

        parts = choice.split(": ", 1)
        provider_type = parts[0]
        model_name = parts[1] if len(parts) > 1 else ""

        # Rebuild graph with new model
        if _graph_config is not None:
            if provider_type == "local":
                _graph_config.model_provider = "vllm"
                detected = _detect_vllm_model("http://host.docker.internal:8000/v1")
                _graph_config.model_name = detected or model_name
            elif provider_type == "claude":
                _graph_config.model_provider = "cliproxy"
                _graph_config.model_name = model_name
            else:
                return f"**Error:** Unknown provider: {provider_type}"

            from graph.agent import create_quinn_graph
            _graph = create_quinn_graph(
                config=_graph_config, knowledge_store=_get_store(),
                include_subagents=True,
            )
            return f"**Switched to:** `{_graph_config.model_name}` (graph rebuilt)"
        return "**Error:** LangGraph config not initialized."

    def get_subtitle() -> str:
        if _graph_config is not None:
            display_model = _graph_config.model_name
        else:
            display_model = "unknown"
        return f"**Quinn QA** &nbsp; `{display_model}`"

    def get_knowledge_stats() -> str:
        store = _get_store()
        stats = store.get_stats()
        if not stats:
            return "Knowledge base not initialized."
        lines = []
        for table, count in stats.items():
            lines.append(f"- {table}: {count}")
        return "\n".join(lines)

    return {
        "get_tools_list": get_tools_list,
        "get_model_info": get_model_info,
        "get_provider_choices": get_provider_choices,
        "get_current_provider": get_current_provider,
        "switch_provider": switch_provider,
        "get_subtitle": get_subtitle,
        "get_knowledge_stats": get_knowledge_stats,
    }


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def _seed_apps():
    """Seed default tracked apps from config."""
    try:
        config_path = Path(__file__).parent / "config" / "qa-config.json"
        if not config_path.exists():
            config_path = Path("/opt/quinn/config/qa-config.json")
        if not config_path.exists():
            return

        qa_config = json.loads(config_path.read_text())
        store = _get_store()
        existing = {a["name"] for a in store.get_apps()}

        for app in qa_config.get("apps", []):
            if app["name"] not in existing:
                store.add_app(
                    name=app["name"],
                    github_repo=app.get("github_repo", ""),
                    server_url=app.get("server_url", ""),
                    config=app.get("config"),
                )
        print(f"[quinn] Seeded {len(qa_config.get('apps', []))} tracked apps")
    except Exception as e:
        print(f"[quinn] App seeding failed: {e}")


def _main():
    parser = argparse.ArgumentParser(description="Quinn QA Gradio UI")
    parser.add_argument("--port", type=int, default=7870)
    parser.add_argument("--config", type=str, default=None)
    parser.add_argument("--share", action="store_true")
    args = parser.parse_args()

    # Initialize observability
    import tracing
    import metrics
    tracing.init()
    metrics.init()

    _init_langgraph_agent()

    # Seed default tracked apps
    _seed_apps()

    # Start Discord bot (watches for reactions and @mentions)
    from discord_bot import start_bot
    start_bot()

    blocks = create_chat_app(
        chat_fn=chat,
        title="Quinn QA",
        subtitle="",
        placeholder="Ask me about app health, bugs, releases, or run a QA audit...",
        settings=_build_settings_callbacks(),
        pwa=True,
    )

    # ---------------------------------------------------------------------------
    # FastAPI + PWA static serving
    # ---------------------------------------------------------------------------
    import gradio as gr
    import uvicorn
    from fastapi import FastAPI
    from fastapi.responses import FileResponse
    from fastapi.staticfiles import StaticFiles

    static_dir = Path(__file__).parent / "static"

    fastapi_app = FastAPI(title="Quinn QA -- protoLabs")

    # Chat API endpoint (for evals and programmatic access)
    from pydantic import BaseModel as PydanticBaseModel

    class ChatRequest(PydanticBaseModel):
        message: str
        session_id: str = "api-default"

    @fastapi_app.post("/api/chat")
    async def _api_chat(req: ChatRequest):
        result = await chat(req.message, req.session_id)
        # Extract assistant content
        parts = [m["content"] for m in result if m.get("role") == "assistant" and m.get("content")]
        return {"response": "\n\n".join(parts), "messages": result}

    # Prometheus /metrics endpoint
    if metrics.is_enabled():
        try:
            from prometheus_client import generate_latest, CONTENT_TYPE_LATEST
            from fastapi import Response as FastAPIResponse

            @fastapi_app.get("/metrics", include_in_schema=False)
            async def _prometheus_metrics():
                return FastAPIResponse(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
        except ImportError:
            pass

    if static_dir.exists():
        manifest_path = static_dir / "manifest.json"
        if manifest_path.exists():
            @fastapi_app.get("/manifest.json", include_in_schema=False)
            async def _serve_manifest() -> FileResponse:
                return FileResponse(str(manifest_path), media_type="application/manifest+json")

        sw_path = static_dir / "sw.js"
        if sw_path.exists():
            @fastapi_app.get("/sw.js", include_in_schema=False)
            async def _serve_sw() -> FileResponse:
                return FileResponse(
                    str(sw_path), media_type="application/javascript",
                    headers={"Service-Worker-Allowed": "/"},
                )

        fastapi_app.mount("/static", StaticFiles(directory=str(static_dir)), name="quinn-static")

    app = gr.mount_gradio_app(
        fastapi_app, blocks, path="/",
        footer_links=[],
        favicon_path=str(static_dir / "favicon.svg") if (static_dir / "favicon.svg").exists() else None,
    )

    print(f"[quinn] Starting on http://0.0.0.0:{args.port}")
    uvicorn.run(app, host="0.0.0.0", port=args.port)


if __name__ == "__main__":
    _main()
