"""Discord bot for protoResearcher — watches for 🔬 reactions and @mentions.

Runs as a background task alongside the Gradio server. When a user reacts
with 🔬 to a message or @mentions the bot in a reply, it queues the message
for research and posts results back as a Discord reply.

Requires DISCORD_BOT_TOKEN env var.
"""

import asyncio
import logging
import os
import re
import threading
from typing import Any

import httpx

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("discord_bot")
log.setLevel(logging.INFO)

def _log(msg):
    """Print + log for visibility in Docker logs."""
    print(f"[discord-bot] {msg}", flush=True)

TRIGGER_EMOJI = "🔬"
_CHAT_URL = "http://127.0.0.1:7870/api/chat"
_DISCORD_API = "https://discord.com/api/v10"
_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")
_MAX_RESPONSE_LENGTH = 1900  # Discord message limit is 2000


# ---------------------------------------------------------------------------
# Discord Gateway (WebSocket) — minimal implementation
# We only need: reaction_add, message_create events
# Using raw WebSocket instead of discord.py to avoid heavy dependency
# ---------------------------------------------------------------------------

async def _api_request(method: str, path: str, json: dict | None = None) -> dict | None:
    """Make an authenticated Discord API request."""
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.request(
            method,
            f"{_DISCORD_API}{path}",
            headers={"Authorization": f"Bot {_BOT_TOKEN}"},
            json=json,
        )
        if resp.status_code in (200, 201, 204):
            return resp.json() if resp.content else None
        log.warning(f"Discord API {method} {path}: {resp.status_code} {resp.text[:200]}")
        return None


async def _react(channel_id: str, message_id: str, emoji: str):
    """Add a reaction to a message."""
    encoded = emoji if emoji.isascii() else emoji
    await _api_request("PUT", f"/channels/{channel_id}/messages/{message_id}/reactions/{encoded}/@me")


async def _create_thread(channel_id: str, message_id: str, name: str) -> dict | None:
    """Create a thread from a message."""
    return await _api_request("POST", f"/channels/{channel_id}/messages/{message_id}/threads", json={
        "name": name[:100],
        "auto_archive_duration": 1440,  # 24 hours
    })


async def _send_to_thread(thread_id: str, content: str):
    """Send a message to a thread, splitting if needed."""
    chunks = []
    while content:
        if len(content) <= _MAX_RESPONSE_LENGTH:
            chunks.append(content)
            break
        split_at = content[:_MAX_RESPONSE_LENGTH].rfind("\n")
        if split_at < 100:
            split_at = _MAX_RESPONSE_LENGTH
        chunks.append(content[:split_at])
        content = content[split_at:].lstrip()

    for chunk in chunks:
        await _api_request("POST", f"/channels/{thread_id}/messages", json={"content": chunk})


async def _get_message(channel_id: str, message_id: str) -> dict | None:
    """Fetch a specific message by ID."""
    return await _api_request("GET", f"/channels/{channel_id}/messages/{message_id}")


async def _get_bot_user() -> dict | None:
    """Get the bot's own user info."""
    return await _api_request("GET", "/users/@me")


async def _reply(channel_id: str, message_id: str, content: str):
    """Reply to a message in Discord."""
    # Split long messages
    chunks = []
    while content:
        if len(content) <= _MAX_RESPONSE_LENGTH:
            chunks.append(content)
            break
        # Split at last newline before limit
        split_at = content[:_MAX_RESPONSE_LENGTH].rfind("\n")
        if split_at < 100:
            split_at = _MAX_RESPONSE_LENGTH
        chunks.append(content[:split_at])
        content = content[split_at:].lstrip()

    for i, chunk in enumerate(chunks):
        payload = {"content": chunk}
        if i == 0:
            payload["message_reference"] = {"message_id": message_id}
        await _api_request("POST", f"/channels/{channel_id}/messages", json=payload)


async def _send_typing(channel_id: str):
    """Show typing indicator."""
    await _api_request("POST", f"/channels/{channel_id}/typing")


# ---------------------------------------------------------------------------
# Research handler
# ---------------------------------------------------------------------------

async def _do_research(channel_id: str, message_id: str, content: str, context: str = ""):
    """React with 👀, do research via /api/chat, create thread with synthesis."""

    # Step 1: Acknowledge with 👀
    await _react(channel_id, message_id, "👀")

    # Build research prompt
    prompt_parts = []
    if context:
        prompt_parts.append(f"Context from Discord user: {context}")
    prompt_parts.append(
        f"Research the following and provide a structured synthesis. "
        f"Rate significance. Note practical implications for the protoLabs stack.\n\n"
        f"IMPORTANT FORMATTING RULES (this will be posted to Discord):\n"
        f"- Do NOT use markdown tables (Discord doesn't render them)\n"
        f"- Use bullet lists instead of tables\n"
        f"- Bold with **text** is fine\n"
        f"- Use > for blockquotes\n"
        f"- Keep sections short — Discord truncates long messages\n"
        f"- START your response with a **Reasoning Trace** section showing your research steps, like:\n"
        f"  > 🔍 Scanned 3 sources → Found 5 relevant items → Graded 3 as significant → Synthesized below\n\n"
        f"{content}"
    )
    prompt = "\n\n".join(prompt_parts)

    session_id = f"discord-{channel_id}-{message_id}"

    try:
        async with httpx.AsyncClient(timeout=600) as client:
            resp = await client.post(
                _CHAT_URL,
                json={"message": prompt, "session_id": session_id},
            )
            resp.raise_for_status()
            result = resp.json().get("response", "")

        if not result:
            await _reply(channel_id, message_id,
                         "🔬 Couldn't produce a useful summary. "
                         "Try @mentioning me with a specific question.")
            return

        # Step 2: React with 🔬 to confirm research is done
        await _react(channel_id, message_id, TRIGGER_EMOJI)

        # Step 3: Create a thread on the original message
        # Thread name from first line of content or a truncated version
        first_line = content.split("\n")[0][:80].strip() or "Research"
        thread = await _create_thread(channel_id, message_id, f"🔬 {first_line}")

        if thread:
            thread_id = thread.get("id")
            # Post the synthesis in the thread
            synthesis = f"🔬 **Research Synthesis**\n\n{result}"
            await _send_to_thread(thread_id, synthesis)
        else:
            # Fallback: reply directly if thread creation fails
            await _reply(channel_id, message_id, f"🔬 **Research Synthesis**\n\n{result}")

    except Exception as e:
        log.error(f"Research failed: {e}")
        await _reply(channel_id, message_id, f"🔬 Research failed: {str(e)[:200]}")


async def _keep_typing(channel_id: str):
    """Send typing indicator every 8 seconds until cancelled."""
    try:
        while True:
            await _send_typing(channel_id)
            await asyncio.sleep(8)
    except asyncio.CancelledError:
        pass


# ---------------------------------------------------------------------------
# Gateway WebSocket connection
# ---------------------------------------------------------------------------

async def _run_gateway():
    """Connect to Discord Gateway and listen for events."""
    import json as json_mod

    try:
        import websockets
    except ImportError:
        _log("ERROR: websockets not installed — bot disabled")
        return

    bot_user = await _get_bot_user()
    if not bot_user:
        log.error("Could not fetch bot user info")
        return
    bot_id = bot_user["id"]
    _log(f"Bot user: {bot_user.get('username')} ({bot_id})")

    # Get gateway URL
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(
            f"{_DISCORD_API}/gateway/bot",
            headers={"Authorization": f"Bot {_BOT_TOKEN}"},
        )
        gateway_url = resp.json().get("url", "wss://gateway.discord.gg")

    heartbeat_interval = None
    sequence = None

    while True:
        try:
            async with websockets.connect(f"{gateway_url}?v=10&encoding=json") as ws:
                _log("Connected to Discord Gateway")

                async for raw_msg in ws:
                    data = json_mod.loads(raw_msg)
                    op = data.get("op")
                    t = data.get("t")
                    d = data.get("d")
                    s = data.get("s")

                    if s is not None:
                        sequence = s

                    # Hello — start heartbeating
                    if op == 10:
                        heartbeat_interval = d["heartbeat_interval"] / 1000
                        # Identify
                        await ws.send(json_mod.dumps({
                            "op": 2,
                            "d": {
                                "token": _BOT_TOKEN,
                                "intents": (1 << 0) | (1 << 9) | (1 << 10) | (1 << 15),
                                # GUILDS | GUILD_MESSAGES | GUILD_MESSAGE_REACTIONS | MESSAGE_CONTENT
                                "properties": {
                                    "os": "linux",
                                    "browser": "protoResearcher",
                                    "device": "protoResearcher",
                                },
                            },
                        }))
                        # Start heartbeat loop
                        asyncio.create_task(_heartbeat_loop(ws, heartbeat_interval, lambda: sequence))

                    # Heartbeat ACK
                    elif op == 11:
                        pass

                    # Dispatch events
                    elif op == 0:
                        if t == "READY":
                            _log(f"Gateway READY — {len(d.get('guilds', []))} guilds")

                        elif t == "MESSAGE_REACTION_ADD":
                            await _handle_reaction(d, bot_id)

                        elif t == "MESSAGE_CREATE":
                            await _handle_message(d, bot_id)

                    # Reconnect requested
                    elif op == 7:
                        log.info("Gateway requested reconnect")
                        break

                    # Invalid session
                    elif op == 9:
                        log.warning("Invalid session — reconnecting")
                        break

        except Exception as e:
            log.error(f"Gateway error: {e}")
            await asyncio.sleep(5)
            log.info("Reconnecting to Gateway...")


async def _heartbeat_loop(ws, interval: float, get_sequence):
    """Send heartbeats at the required interval."""
    import json as json_mod
    try:
        while True:
            await asyncio.sleep(interval)
            await ws.send(json_mod.dumps({"op": 1, "d": get_sequence()}))
    except Exception:
        pass


async def _handle_reaction(data: dict, bot_id: str):
    """Handle a reaction event — trigger research on 🔬."""
    emoji = data.get("emoji", {})
    emoji_name = emoji.get("name", "")

    if emoji_name != TRIGGER_EMOJI:
        return

    # Don't respond to our own reactions
    user_id = data.get("user_id", "")
    if user_id == bot_id:
        return

    channel_id = data.get("channel_id", "")
    message_id = data.get("message_id", "")

    _log(f"🔬 Reaction trigger: channel={channel_id} message={message_id}")

    # Fetch the original message
    msg = await _get_message(channel_id, message_id)
    if not msg:
        return

    content = msg.get("content", "")
    # Also include embed URLs/descriptions
    for embed in msg.get("embeds", []):
        if embed.get("url"):
            content += f"\n{embed['url']}"
        if embed.get("title"):
            content += f"\n{embed['title']}"
        if embed.get("description"):
            content += f"\n{embed['description'][:500]}"

    if not content.strip():
        await _reply(channel_id, message_id, "🔬 This message doesn't have content I can research. Try @mentioning me with a question instead.")
        return

    # Queue research in background
    asyncio.create_task(_do_research(channel_id, message_id, content))


async def _handle_message(data: dict, bot_id: str):
    """Handle a message event — respond to @mentions."""
    # Ignore bot's own messages
    author = data.get("author", {})
    if author.get("id") == bot_id:
        return
    if author.get("bot"):
        return

    content = data.get("content", "")
    channel_id = data.get("channel_id", "")
    message_id = data.get("id", "")

    # Check if bot is mentioned
    mentions = data.get("mentions", [])
    mentioned = any(m.get("id") == bot_id for m in mentions)

    if not mentioned:
        return

    # Strip the @mention from the content
    clean_content = re.sub(r'<@!?' + bot_id + r'>', '', content).strip()

    _log(f"🔬 Mention trigger: channel={channel_id} from={author.get('username')}")

    # If this is a reply, fetch the referenced message for context
    ref = data.get("message_reference")
    context_content = ""
    if ref and ref.get("message_id"):
        ref_msg = await _get_message(channel_id, ref["message_id"])
        if ref_msg:
            context_content = ref_msg.get("content", "")
            for embed in ref_msg.get("embeds", []):
                if embed.get("url"):
                    context_content += f"\n{embed['url']}"
                if embed.get("description"):
                    context_content += f"\n{embed['description'][:500]}"

    if not clean_content and not context_content:
        await _reply(channel_id, message_id,
                     "🔬 What would you like me to research? "
                     "You can:\n"
                     "- React with 🔬 to any message with links\n"
                     "- @mention me with a question\n"
                     "- Reply to a message and @mention me for context")
        return

    # Build the research input
    if context_content and clean_content:
        research_content = f"{context_content}\n\nUser's question: {clean_content}"
    elif context_content:
        research_content = context_content
    else:
        research_content = clean_content

    asyncio.create_task(_do_research(channel_id, message_id, research_content, context=clean_content))


# ---------------------------------------------------------------------------
# Public API — called from server.py
# ---------------------------------------------------------------------------

def start_bot():
    """Start the Discord bot in a background thread."""
    if not _BOT_TOKEN:
        _log("DISCORD_BOT_TOKEN not set — bot disabled")
        return

    def _run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(_run_gateway())

    thread = threading.Thread(target=_run, daemon=True, name="discord-bot")
    thread.start()
    _log("Discord bot started in background thread")
