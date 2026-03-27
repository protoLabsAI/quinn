"""
protoResearcher — AI research agent powered by local LLMs.

Monitors Discord feeds, HuggingFace, GitHub for the latest in AI/ML research.
Supports two agent backends: nanobot (legacy) and LangGraph (new).

Usage:
    python server.py                          # default port 7870
    AGENT_BACKEND=langgraph python server.py  # use LangGraph backend
    python server.py --config path/to/config  # custom config
"""

import argparse
import asyncio
import contextvars
import json
import os
import re
import time
from pathlib import Path
from typing import Any

from chat_ui import create_chat_app

# Agent backend selection
_BACKEND = os.environ.get("AGENT_BACKEND", "nanobot")

# ---------------------------------------------------------------------------
# Agent setup
# ---------------------------------------------------------------------------

_agent = None       # nanobot AgentLoop (when AGENT_BACKEND=nanobot)
_graph = None       # LangGraph compiled graph (when AGENT_BACKEND=langgraph)
_graph_config = None  # LangGraphConfig
_checkpointer = None  # LangGraph MemorySaver for session persistence
_config = None


def _patch_identity():
    """Replace nanobot's default identity header with protoResearcher branding."""
    from nanobot.agent.context import ContextBuilder

    _original_get_identity = ContextBuilder._get_identity

    def _patched_get_identity(self):
        original = _original_get_identity(self)
        # Replace the nanobot header
        original = original.replace("# nanobot 🐈", "# protoResearcher 🔬")
        original = original.replace(
            "You are nanobot, a helpful AI assistant.",
            "You are protoResearcher, an autonomous AI research assistant built by protoLabs.",
        )
        original = original.replace("## nanobot Guidelines", "## Guidelines")
        return original

    ContextBuilder._get_identity = _patched_get_identity


def _init_agent(config_path: str | None = None):
    """Initialize nanobot agent loop."""
    global _agent, _config

    from nanobot.agent.loop import AgentLoop
    from nanobot.bus.queue import MessageBus
    from nanobot.config.loader import load_config, set_config_path
    from nanobot.config.paths import get_cron_dir
    from nanobot.cron.service import CronService
    from nanobot.utils.helpers import sync_workspace_templates

    if config_path:
        p = Path(config_path).expanduser().resolve()
        set_config_path(p)

    _config = load_config(Path(config_path) if config_path else None)
    sync_workspace_templates(_config.workspace_path)

    bus = MessageBus()
    provider = _make_provider(_config)

    cron = CronService(get_cron_dir() / "jobs.json")

    _agent = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=_config.workspace_path,
        model=None,
        max_iterations=_config.agents.defaults.max_tool_iterations,
        context_window_tokens=_config.agents.defaults.context_window_tokens,
        web_search_config=_config.tools.web.search,
        web_proxy=_config.tools.web.proxy or None,
        exec_config=_config.tools.exec,
        cron_service=cron,
        restrict_to_workspace=_config.tools.restrict_to_workspace,
        mcp_servers=_config.tools.mcp_servers,
        channels_config=_config.channels,
    )

    # Override nanobot's default identity with protoResearcher branding
    _patch_identity()


def _init_langgraph_agent():
    """Initialize the LangGraph agent backend."""
    global _graph, _graph_config, _checkpointer

    from graph.agent import create_researcher_graph
    from graph.config import LangGraphConfig
    from langgraph.checkpoint.memory import MemorySaver

    config_path = Path(__file__).parent / "config" / "langgraph-config.yaml"
    _graph_config = LangGraphConfig.from_yaml(config_path)

    store = _get_store()
    _checkpointer = MemorySaver()

    _graph = create_researcher_graph(
        config=_graph_config,
        knowledge_store=store,
        include_subagents=True,
    )

    print(f"[researcher] LangGraph agent initialized (model: {_graph_config.model_name})")


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


def _make_provider(config):
    """Create provider — auto-detects vLLM model."""
    from nanobot.providers.base import GenerationSettings
    from nanobot.providers.litellm_provider import LiteLLMProvider

    model = config.agents.defaults.model
    provider_name = config.get_provider_name(model)
    p = config.get_provider(model)
    api_base = config.get_api_base(model)

    if api_base and (model == "auto" or provider_name in ("vllm", "ollama")):
        detected = _detect_vllm_model(api_base)
        if detected:
            model = detected

    # CLIProxyAPI is OpenAI-compatible — tell nanobot/litellm to use openai protocol
    effective_provider = provider_name
    api_key = p.api_key if p else None
    if provider_name == "cliproxy":
        effective_provider = "openai"
        api_key = api_key or "protoresearcher-internal"
        # litellm's openai provider needs this env var
        os.environ["OPENAI_API_KEY"] = api_key

    provider = LiteLLMProvider(
        api_key=api_key,
        api_base=api_base,
        default_model=model,
        extra_headers=p.extra_headers if p else None,
        provider_name=effective_provider,
    )

    defaults = config.agents.defaults
    provider.generation = GenerationSettings(
        temperature=defaults.temperature,
        max_tokens=defaults.max_tokens,
        reasoning_effort=defaults.reasoning_effort,
    )
    return provider


# ---------------------------------------------------------------------------
# Session commands
# ---------------------------------------------------------------------------

_HELP_TEXT = """\
**protoResearcher commands:**
| Command | Description |
|---------|-------------|
| `/new` | Clear chat history + session |
| `/clear` | Clear chat display (session preserved) |
| `/think <level>` | Set reasoning effort (low/medium/high/off) |
| `/compact` | Force memory consolidation |
| `/model` | Show current model |
| `/tools` | List registered tools |
| `/topics` | Show tracked research topics |
| `/agenda` | Show research agenda with stats |
| `/papers [query]` | Search stored papers |
| `/recent [n]` | Show recent findings |
| `/audit [n]` | Show recent audit log entries |
| `/lab on\\|off\\|status` | Toggle lab mode (GPU experiment runner) |
| `/publish` | Generate weekly digest and publish to Discord |
| `/help` | Show this help |
"""

_THINK_LEVELS = {"low", "medium", "high", "off"}


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
        session_key = f"gradio:{session_id}"
        session = _agent.sessions.get_or_create(session_key)
        session.clear()
        _agent.sessions.save(session)
        return [{"role": "assistant", "content": "", "metadata": {"_new": True}}]

    if cmd == "model":
        return _msg(f"**Model:** `{_agent.model}`")

    if cmd == "tools":
        names = _agent.tools.tool_names
        listing = "\n".join(f"- `{n}`" for n in sorted(names))
        return _msg(f"**Registered tools ({len(names)}):**\n{listing}")

    if cmd == "think":
        level = args.strip().lower()
        if level not in _THINK_LEVELS:
            return _msg(f"Invalid level. Use one of: {', '.join(sorted(_THINK_LEVELS))}")
        val = None if level == "off" else level
        _agent.provider.generation.reasoning_effort = val
        return _msg(f"Reasoning effort set to **{level}**.")

    if cmd == "compact":
        session_key = f"gradio:{session_id}"
        session = _agent.sessions.get_or_create(session_key)
        await _agent.memory_consolidator.maybe_consolidate_by_tokens(session)
        return _msg("Memory consolidation complete.")

    if cmd == "audit":
        from audit import audit_logger
        n = 20
        if args.strip().isdigit():
            n = int(args.strip())
        entries = audit_logger.get_recent(n, session_id=session_id)
        if not entries:
            return _msg("No audit entries found.")
        lines = []
        for e in entries:
            status = "ok" if e.get("success") else "FAIL"
            lines.append(
                f"- `{e['ts'][:19]}` **{e['tool']}** ({e['duration_ms']}ms) [{status}] — {e.get('result_summary', '')[:80]}"
            )
        return _msg(f"**Recent audit log ({len(entries)} entries):**\n" + "\n".join(lines))

    # Research-specific commands
    if cmd == "topics":
        return await _handle_topics_command()

    if cmd == "agenda":
        return await _handle_agenda_command()

    if cmd == "papers":
        return await _handle_papers_command(args)

    if cmd == "recent":
        return await _handle_recent_command(args)

    if cmd == "lab":
        return await _handle_lab_command(args)

    if cmd == "publish":
        return await _handle_publish_command(session_id)

    return None


# ---------------------------------------------------------------------------
# Research commands
# ---------------------------------------------------------------------------

_knowledge_store = None


def _get_store():
    global _knowledge_store
    if _knowledge_store is None:
        from knowledge.store import KnowledgeStore
        _knowledge_store = KnowledgeStore()
    return _knowledge_store


async def _handle_topics_command() -> list[dict[str, Any]]:
    store = _get_store()
    topics = store.get_topics()
    if not topics:
        return _msg("No research topics configured. Ask me to add topics or use the research_memory tool.")

    lines = ["**Research Topics:**"]
    for t in topics:
        kw = json.loads(t.get("keywords", "[]"))
        kw_str = ", ".join(kw[:5]) if kw else ""
        scanned = t.get("last_scanned_at", "never") or "never"
        lines.append(
            f"- **{t['name']}** (P{t['priority']}) — {t.get('description', '')}\n"
            f"  Keywords: {kw_str} | Last scanned: {scanned}"
        )
    return _msg("\n".join(lines))


async def _handle_agenda_command() -> list[dict[str, Any]]:
    store = _get_store()
    stats = store.get_stats()
    topics = store.get_topics()

    lines = ["**Research Agenda:**", ""]
    lines.append(f"Papers tracked: {stats.get('papers', 0)}")
    lines.append(f"Findings stored: {stats.get('findings', 0)}")
    lines.append(f"Digests generated: {stats.get('digests', 0)}")
    lines.append(f"Model releases: {stats.get('model_releases', 0)}")
    lines.append(f"Active topics: {len(topics)}")

    if topics:
        lines.append("\n**Topics by priority:**")
        for t in topics:
            lines.append(f"- P{t['priority']}: {t['name']}")

    return _msg("\n".join(lines))


async def _handle_papers_command(args: str) -> list[dict[str, Any]]:
    store = _get_store()
    query = args.strip()

    if query:
        results = store.search(query, k=10, filter_table="papers")
        if not results:
            return _msg(f"No papers found matching '{query}'.")
        lines = [f"**Papers matching '{query}':**"]
        for i, r in enumerate(results, 1):
            lines.append(f"{i}. [{r['source_id']}] {r['preview']}")
        return _msg("\n".join(lines))
    else:
        papers = store.get_papers(limit=10)
        if not papers:
            return _msg("No papers in the knowledge base yet.")
        lines = ["**Recent papers:**"]
        for p in papers:
            sig = p.get("significance", "?")
            lines.append(f"- [{sig}] **{p['title']}** ({p['id']})")
        return _msg("\n".join(lines))


async def _handle_recent_command(args: str) -> list[dict[str, Any]]:
    store = _get_store()
    n = 10
    if args.strip().isdigit():
        n = int(args.strip())

    # Show recent papers + findings
    papers = store.get_papers(limit=n)
    lines = []

    if papers:
        lines.append("**Recent papers:**")
        for p in papers[:n]:
            sig = p.get("significance", "?")
            lines.append(f"- [{sig}] {p['title']} ({p['id']}) — {p.get('discovered_at', '')[:10]}")

    if not lines:
        return _msg("No recent research activity.")

    return _msg("\n".join(lines))


async def _handle_publish_command(session_id: str) -> list[dict[str, Any]]:
    """Generate a digest and publish to Discord via webhook."""
    import os
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "")
    if not webhook_url:
        return _msg("**Error:** DISCORD_WEBHOOK_URL not set.")

    # Gather research data for the digest
    store = _get_store()
    stats = store.get_stats()
    papers = store.get_papers(limit=15)
    topics = store.get_topics()

    # Build the newsletter
    from datetime import datetime, timezone
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    lines = [f"**🔬 protoResearcher Weekly Digest — {date_str}**\n"]

    if stats:
        lines.append(f"📊 **Knowledge Base:** {stats.get('papers', 0)} papers, "
                     f"{stats.get('findings', 0)} findings, {stats.get('model_releases', 0)} model releases\n")

    if papers:
        lines.append("**📄 Recent Papers:**")
        for p in papers[:10]:
            sig = p.get("significance", "?")
            lines.append(f"• [{sig}] {p['title']}")
        lines.append("")

    if topics:
        lines.append("**🎯 Active Topics:** " + ", ".join(t["name"] for t in topics))

    lines.append("\n_Generated by protoResearcher — protoLabs.studio_")

    digest_content = "\n".join(lines)

    # Publish via webhook
    import httpx
    payload = {
        "username": "protoResearcher",
        "embeds": [{
            "title": f"🔬 Weekly Research Digest — {date_str}",
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
# Lab mode — toggleable GPU experiment runner
# ---------------------------------------------------------------------------

_lab_mode = False
_lab_tool = None


def _is_lab_available() -> bool:
    """Check if GPU/lab dependencies are available."""
    import os
    return os.path.exists("/opt/llama-factory") or os.environ.get("LAB_GPU") is not None


async def _handle_lab_command(args: str) -> list[dict[str, Any]]:
    global _lab_mode, _lab_tool
    subcmd = args.strip().lower() or "status"

    if subcmd == "on":
        if _lab_mode:
            return _msg("Lab mode is already **on**.")
        if not _is_lab_available():
            return _msg(
                "**Lab mode unavailable.** Run with the lab profile:\n"
                "```\ndocker compose --profile lab up --build\n```"
            )
        from tools.lab_bench import LabBenchTool
        _lab_tool = LabBenchTool()
        _agent.tools.register(_lab_tool)
        _lab_mode = True

        import os
        gpu = os.environ.get("LAB_GPU", "1")
        return _msg(
            f"**Lab mode ON.** `lab_bench` tool registered.\n"
            f"GPU: `CUDA_VISIBLE_DEVICES={gpu}`\n"
            f"Models: Qwen3.5-0.8B, Qwen3.5-2B\n"
            f"Stack: LLaMA-Factory (LoRA DPO)\n\n"
            f"Use `lab_bench` tool to init and run experiments."
        )

    if subcmd == "off":
        if not _lab_mode:
            return _msg("Lab mode is already **off**.")
        if _lab_tool:
            _agent.tools.unregister("lab_bench")
            _lab_tool = None
        _lab_mode = False
        return _msg("**Lab mode OFF.** `lab_bench` tool unregistered.")

    if subcmd == "status":
        if not _lab_mode:
            return _msg("Lab mode is **off**. Use `/lab on` to enable.")
        if _lab_tool:
            status = _lab_tool._runner.get_status()
            return _msg(f"Lab mode is **on**.\n\n{status}")
        return _msg("Lab mode is **on** (no experiments yet).")

    return _msg("Usage: `/lab on`, `/lab off`, `/lab status`")


# ---------------------------------------------------------------------------
# Audit logging wrapper
# ---------------------------------------------------------------------------

_current_session_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "_current_session_id", default=""
)


def _install_audit_wrapper():
    from audit import audit_logger
    import tracing
    import metrics

    original_execute = _agent.tools.execute

    # Map tool names to research phase spans for Langfuse
    _TOOL_PHASE_MAP = {
        "discord_feed": "explorer",
        "huggingface": "explorer",
        "github_trending": "explorer",
        "web_search": "explorer",
        "web_fetch": "explorer",
        "browser": "explorer",
        "paper_reader": "analyst",
        "research_memory": "analyst",
        "message": "writer",
    }

    async def _audited_execute(name: str, params: dict[str, Any]) -> str:
        session_id = _current_session_id.get("")
        t0 = time.monotonic()
        phase = _TOOL_PHASE_MAP.get(name, "general")

        # Capture message tool content so it can be surfaced in the chat
        if name == "message":
            content = params.get("content", "")
            if content:
                try:
                    captured = _message_tool_content.get([])
                    captured.append(content)
                except LookupError:
                    pass

        try:
            result = await original_execute(name, params)
            duration_ms = int((time.monotonic() - t0) * 1000)
            success = not (isinstance(result, str) and result.startswith("Error"))
            result_summary = result[:200] if isinstance(result, str) else str(result)[:200]
            audit_logger.log(
                session_id=session_id, tool=name, args=params,
                result_summary=result_summary, duration_ms=duration_ms, success=success,
            )
            tracing.trace_tool_call(
                tool_name=f"{phase}:{name}", args=params, result=result_summary,
                duration_ms=duration_ms, success=success, session_id=session_id,
            )
            metrics.record_tool_call(name, success, duration_ms / 1000)
            return result
        except Exception as exc:
            duration_ms = int((time.monotonic() - t0) * 1000)
            audit_logger.log(
                session_id=session_id, tool=name, args=params,
                result_summary=str(exc)[:200], duration_ms=duration_ms, success=False,
            )
            tracing.trace_tool_call(
                tool_name=f"{phase}:{name}", args=params, result=str(exc)[:200],
                duration_ms=duration_ms, success=False, session_id=session_id,
            )
            metrics.record_tool_call(name, False, duration_ms / 1000)
            raise

    _agent.tools.execute = _audited_execute


# ---------------------------------------------------------------------------
# Chat function
# ---------------------------------------------------------------------------


def _strip_think(text: str) -> str:
    text = re.sub(r"<think>[\s\S]*?</think>", "", text)
    text = re.sub(r"</think>\s*", "", text)
    return text.strip()


# Captured message tool content — nanobot sends final responses via message() tool
_message_tool_content: contextvars.ContextVar[list[str]] = contextvars.ContextVar(
    "_message_tool_content", default=[]
)


import queue as _queue_mod


async def chat(message: str, session_id: str) -> list[dict[str, Any]]:
    """Route to the active backend."""
    # Slash commands are handled identically by both backends
    stripped = message.strip()
    if stripped.startswith("/"):
        parts = stripped.split(None, 1)
        cmd = parts[0][1:].lower()
        args = parts[1] if len(parts) > 1 else ""
        result = await _handle_command(cmd, args, session_id)
        if result is not None:
            return result

    # Route to backend
    if _BACKEND == "langgraph" and _graph is not None:
        return await _chat_langgraph(message, session_id)
    else:
        return await _chat_nanobot(message, session_id)


async def _chat_nanobot(message: str, session_id: str) -> list[dict[str, Any]]:
    """Process via nanobot's agent loop (legacy backend)."""
    import tracing
    token = _current_session_id.set(session_id)
    msg_token = _message_tool_content.set([])
    tracing.start_trace(session_id=session_id, name="researcher-chat", metadata={"message_preview": message[:100]})
    try:
        progress_messages: list[dict] = []

        async def on_progress(content: str, *, tool_hint: bool = False) -> None:
            content = _strip_think(content)
            if not content:
                return
            if tool_hint:
                progress_messages.append({
                    "role": "assistant",
                    "metadata": {"title": f"🔧 {content}"},
                    "content": "",
                })
            else:
                progress_messages.append({
                    "role": "assistant",
                    "metadata": {"title": "💭 Thinking"},
                    "content": content,
                })

        response = await _agent.process_direct(
            content=message,
            session_key=f"gradio:{session_id}",
            channel="gradio",
            chat_id=session_id,
            on_progress=on_progress,
        )

        if hasattr(response, "content"):
            response = response.content
        response = _strip_think(response or "")

        captured = _message_tool_content.get([])
        if not response and captured:
            response = "\n\n".join(captured)

        return [*progress_messages, {"role": "assistant", "content": response}]
    finally:
        tracing.end_trace()
        _current_session_id.reset(token)
        _message_tool_content.reset(msg_token)


async def _chat_langgraph(message: str, session_id: str) -> list[dict[str, Any]]:
    """Process via LangGraph agent backend."""
    import tracing
    from langchain_core.messages import HumanMessage, AIMessage

    tracing.start_trace(session_id=session_id, name="researcher-chat-lg", metadata={"message_preview": message[:100]})
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
    """Streaming wrapper — yields incremental history updates as tools run."""
    import threading

    result_queue: _queue_mod.Queue = _queue_mod.Queue()
    progress_so_far: list[dict] = []

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
                    "metadata": {"title": "🔬 Working..."},
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
        if _BACKEND == "langgraph" and _graph is not None:
            from tools.lg_tools import get_all_tools
            tools = get_all_tools(_get_store())
            names = sorted(t.name for t in tools)
        elif _agent is not None:
            names = sorted(_agent.tools.tool_names)
        else:
            names = []
        return "\n".join(f"- `{n}`" for n in names) or "No tools registered."

    def get_model_info() -> str:
        if _BACKEND == "langgraph" and _graph_config is not None:
            model = _graph_config.model_name
            return f"**Model:** `{model}`\n\n**Backend:** LangGraph"
        elif _agent is not None:
            model = _agent.model or "unknown"
            effort = getattr(_agent.provider.generation, "reasoning_effort", None) or "default"
            return f"**Model:** `{model}`\n\n**Reasoning:** {effort}"
        return "**Model:** unknown"

    def get_provider_choices() -> list[str]:
        choices = []
        if _config is not None:
            try:
                api_base = _config.get_api_base(_config.agents.defaults.model)
                if api_base:
                    detected = _detect_vllm_model(api_base)
                    label = detected or "local vLLM"
                    choices.append(f"local: {label}")
            except Exception:
                pass
        else:
            # LangGraph backend — check vLLM directly
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
        if _BACKEND == "langgraph" and _graph_config is not None:
            model = _graph_config.model_name
        elif _agent is not None:
            model = (_agent.model or "").replace("openai/", "")
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

        if _BACKEND == "langgraph":
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

                from graph.agent import create_researcher_graph
                _graph = create_researcher_graph(
                    config=_graph_config, knowledge_store=_get_store(),
                    include_subagents=True,
                )
                return f"**Switched to:** `{_graph_config.model_name}` (graph rebuilt)"
            return "**Error:** LangGraph config not initialized."

        # Nanobot backend
        from nanobot.providers.base import GenerationSettings
        from nanobot.providers.litellm_provider import LiteLLMProvider

        if provider_type == "local":
            import litellm
            api_base = _config.get_api_base(_config.agents.defaults.model) if _config else None
            detected = _detect_vllm_model(api_base) if api_base else None
            model = detected or model_name
            if api_base:
                litellm.api_base = api_base

            p = _config.get_provider(_config.agents.defaults.model) if _config else None
            provider = LiteLLMProvider(
                api_key=p.api_key if p else None,
                api_base=api_base,
                default_model=model,
                extra_headers=p.extra_headers if p else None,
                provider_name="vllm",
            )
        elif provider_type == "claude":
            provider = LiteLLMProvider(
                api_key="protoresearcher-internal",
                api_base="http://127.0.0.1:8317/v1",
                default_model=f"openai/{model_name}",
                provider_name="openai",
            )
        else:
            return f"**Error:** Unknown provider type: {provider_type}"

        old_gen = _agent.provider.generation
        provider.generation = GenerationSettings(
            temperature=old_gen.temperature,
            max_tokens=old_gen.max_tokens,
            reasoning_effort=old_gen.reasoning_effort,
        )

        _agent.provider = provider
        _agent.model = provider.default_model
        return f"**Switched to:** `{provider.default_model}`"

    def get_subtitle() -> str:
        if _BACKEND == "langgraph" and _graph_config is not None:
            display_model = _graph_config.model_name
        elif _agent is not None:
            display_model = (_agent.model or "").replace("openai/", "")
        else:
            display_model = "unknown"
        return f"**🔬 protoResearcher** &nbsp; `{display_model}`"

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


def _seed_topics():
    """Seed default research topics from config."""
    try:
        config_path = Path(__file__).parent / "config" / "research-config.json"
        if not config_path.exists():
            config_path = Path("/opt/protoresearcher/config/research-config.json")
        if not config_path.exists():
            return

        research_config = json.loads(config_path.read_text())
        store = _get_store()
        existing = {t["name"] for t in store.get_topics(active_only=False)}

        for topic in research_config.get("topics", []):
            if topic["name"] not in existing:
                store.add_topic(
                    name=topic["name"],
                    keywords=topic.get("keywords", []),
                    priority=topic.get("priority", 2),
                )
        print(f"[researcher] Seeded {len(research_config.get('topics', []))} research topics")
    except Exception as e:
        print(f"[researcher] Topic seeding failed: {e}")


def _main():
    parser = argparse.ArgumentParser(description="protoResearcher Gradio UI")
    parser.add_argument("--port", type=int, default=7870)
    parser.add_argument("--config", type=str, default=None)
    parser.add_argument("--share", action="store_true")
    args = parser.parse_args()

    # Initialize observability (shared by both backends)
    import tracing
    import metrics
    tracing.init()
    metrics.init()

    print(f"[researcher] Agent backend: {_BACKEND}")

    if _BACKEND == "langgraph":
        _init_langgraph_agent()
    else:
        # Nanobot backend (legacy)
        _init_agent(args.config)
        _install_audit_wrapper()

        from tools.paper_reader import PaperReaderTool
        from tools.huggingface import HuggingFaceTool
        from tools.github_trending import GitHubTrendingTool
        from tools.research_memory import ResearchMemoryTool
        from tools.browser import BrowserTool
        from tools.discord_feed import DiscordFeedTool

        _agent.tools.register(PaperReaderTool())
        _agent.tools.register(HuggingFaceTool())
        _agent.tools.register(GitHubTrendingTool())
        _agent.tools.register(ResearchMemoryTool(_get_store()))
        _agent.tools.register(BrowserTool())

        if os.environ.get("DISCORD_BOT_TOKEN"):
            _agent.tools.register(DiscordFeedTool())
            print("[researcher] Discord feed tool registered")
        else:
            print("[researcher] Discord feed: skipped (no DISCORD_BOT_TOKEN)")

    # Seed default research topics
    _seed_topics()

    # Start Discord bot (watches for 🔬 reactions and @mentions)
    from discord_bot import start_bot
    start_bot()

    blocks = create_chat_app(
        chat_fn=chat,
        title="🔬 protoResearcher",
        subtitle="",
        placeholder="Ask me about the latest in AI research...",
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

    fastapi_app = FastAPI(title="protoResearcher — protoLabs")

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

        fastapi_app.mount("/static", StaticFiles(directory=str(static_dir)), name="ava-static")

    app = gr.mount_gradio_app(
        fastapi_app, blocks, path="/",
        footer_links=[],
        favicon_path=str(static_dir / "favicon.svg") if (static_dir / "favicon.svg").exists() else None,
    )

    print(f"[protoResearcher] Starting on http://0.0.0.0:{args.port}")
    uvicorn.run(app, host="0.0.0.0", port=args.port)


if __name__ == "__main__":
    _main()
