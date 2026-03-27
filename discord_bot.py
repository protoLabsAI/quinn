"""Quinn's community Discord bot -- moderation, slash commands, and agent integration.

Runs as a background task alongside the Gradio server. Provides:
- @mention responses via the LangGraph agent (/api/chat endpoint)
- Clipboard emoji trigger for QA analysis
- Community moderation (spam filtering, rate limiting)
- Slash commands (/quinn status, /quinn bugs, /quinn release)
- Daily digest posting
- New member welcome messages

Requires DISCORD_BOT_TOKEN env var. Optional: DISCORD_GUILD_ID,
WELCOME_CHANNEL_ID, MODERATION_LOG_CHANNEL_ID.
"""

import asyncio
import json as json_mod
import logging
import os
import re
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from audit import audit_logger

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("discord_bot")
log.setLevel(logging.INFO)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

TRIGGER_EMOJI = "\U0001f4cb"  # clipboard

_CHAT_URL = "http://127.0.0.1:7870/api/chat"
_DISCORD_API = "https://discord.com/api/v10"
_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")
_MAX_RESPONSE_LENGTH = 1900  # Discord limit is 2000; leave margin for safety

_GUILD_ID = os.environ.get("DISCORD_GUILD_ID", "1070606339363049492")
_WELCOME_CHANNEL_ID = os.environ.get("WELCOME_CHANNEL_ID", "")
_MOD_LOG_CHANNEL_ID = os.environ.get("MODERATION_LOG_CHANNEL_ID", "")

# Intents bitmask:
# GUILDS (1<<0) | GUILD_MEMBERS (1<<1) | GUILD_MESSAGES (1<<9)
# | GUILD_MESSAGE_REACTIONS (1<<10) | MESSAGE_CONTENT (1<<15)
_GATEWAY_INTENTS = (1 << 0) | (1 << 1) | (1 << 9) | (1 << 10) | (1 << 15)

# Daily digest scheduling -- 2 PM UTC = 7 AM PT by default
_DIGEST_HOUR = int(os.environ.get("DIGEST_HOUR_UTC", "14"))

MODERATION_CONFIG: dict[str, Any] = {
    "spam_patterns": [
        r"(?i)free\s+nitro",
        r"(?i)discord\.gift/",
        r"(?i)@everyone\s+https?://",
        r"(?i)steam\s*community\s*\.com/.+/gift",
    ],
    "rate_limit_messages": 5,
    "rate_limit_window_seconds": 10,
}


# ---------------------------------------------------------------------------
# Shared HTTP client (connection pooling across all API calls)
# ---------------------------------------------------------------------------

_http_client: httpx.AsyncClient | None = None


async def _get_client() -> httpx.AsyncClient:
    """Return a shared long-lived async HTTP client."""
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(timeout=30)
    return _http_client


# ---------------------------------------------------------------------------
# Rate limit tracking
# ---------------------------------------------------------------------------

# user_id -> list of message timestamps (epoch seconds)
_message_timestamps: dict[str, list[float]] = defaultdict(list)
# user_id -> True if already warned within the current window
_rate_limit_warned: dict[str, float] = {}


def _check_rate_limit(user_id: str) -> bool:
    """Return True if the user has exceeded the rate limit.

    Also prunes stale timestamps to keep memory bounded.
    """
    now = time.monotonic()
    window = MODERATION_CONFIG["rate_limit_window_seconds"]
    limit = MODERATION_CONFIG["rate_limit_messages"]

    timestamps = _message_timestamps[user_id]
    # Prune entries older than the window
    timestamps[:] = [ts for ts in timestamps if now - ts < window]
    timestamps.append(now)

    return len(timestamps) > limit


def _should_warn_rate_limit(user_id: str) -> bool:
    """Return True if we should send a warning (at most once per window)."""
    now = time.monotonic()
    window = MODERATION_CONFIG["rate_limit_window_seconds"]
    last_warned = _rate_limit_warned.get(user_id, 0.0)
    if now - last_warned < window:
        return False
    _rate_limit_warned[user_id] = now
    return True


# ---------------------------------------------------------------------------
# Spam detection
# ---------------------------------------------------------------------------

_compiled_spam_patterns: list[re.Pattern[str]] | None = None


def _get_spam_patterns() -> list[re.Pattern[str]]:
    """Compile and cache spam regex patterns."""
    global _compiled_spam_patterns
    if _compiled_spam_patterns is None:
        _compiled_spam_patterns = [
            re.compile(p) for p in MODERATION_CONFIG["spam_patterns"]
        ]
    return _compiled_spam_patterns


def _is_spam(content: str) -> str | None:
    """Return the matched pattern string if content matches a spam rule, else None."""
    for pattern in _get_spam_patterns():
        if pattern.search(content):
            return pattern.pattern
    return None


# ---------------------------------------------------------------------------
# Discord REST API helpers
# ---------------------------------------------------------------------------

async def _api_request(
    method: str, path: str, json: dict | None = None
) -> dict | None:
    """Make an authenticated Discord API request."""
    client = await _get_client()
    resp = await client.request(
        method,
        f"{_DISCORD_API}{path}",
        headers={"Authorization": f"Bot {_BOT_TOKEN}"},
        json=json,
    )
    if resp.status_code in (200, 201, 204):
        return resp.json() if resp.content else None
    log.warning("Discord API %s %s: %s %s", method, path, resp.status_code, resp.text[:200])
    return None


async def _send_message(channel_id: str, content: str) -> dict | None:
    """Send a standalone message to a channel."""
    return await _api_request("POST", f"/channels/{channel_id}/messages", json={"content": content})


async def _reply(channel_id: str, message_id: str, content: str):
    """Reply to a message, splitting long content across multiple messages."""
    chunks = _split_message(content)
    for i, chunk in enumerate(chunks):
        payload: dict[str, Any] = {"content": chunk}
        if i == 0:
            payload["message_reference"] = {"message_id": message_id}
        await _api_request("POST", f"/channels/{channel_id}/messages", json=payload)


async def _react(channel_id: str, message_id: str, emoji: str):
    """Add a reaction to a message."""
    await _api_request(
        "PUT",
        f"/channels/{channel_id}/messages/{message_id}/reactions/{emoji}/@me",
    )


async def _delete_message(channel_id: str, message_id: str):
    """Delete a message from a channel."""
    await _api_request("DELETE", f"/channels/{channel_id}/messages/{message_id}")


async def _get_message(channel_id: str, message_id: str) -> dict | None:
    """Fetch a specific message by ID."""
    return await _api_request("GET", f"/channels/{channel_id}/messages/{message_id}")


async def _get_bot_user() -> dict | None:
    """Get the bot's own user info."""
    return await _api_request("GET", "/users/@me")


async def _send_typing(channel_id: str):
    """Show typing indicator."""
    await _api_request("POST", f"/channels/{channel_id}/typing")


async def _keep_typing(channel_id: str):
    """Send typing indicator every 8 seconds until cancelled."""
    try:
        while True:
            await _send_typing(channel_id)
            await asyncio.sleep(8)
    except asyncio.CancelledError:
        pass


def _split_message(content: str) -> list[str]:
    """Split content into chunks that fit within Discord's message limit."""
    chunks: list[str] = []
    while content:
        if len(content) <= _MAX_RESPONSE_LENGTH:
            chunks.append(content)
            break
        split_at = content[:_MAX_RESPONSE_LENGTH].rfind("\n")
        if split_at < 100:
            split_at = _MAX_RESPONSE_LENGTH
        chunks.append(content[:split_at])
        content = content[split_at:].lstrip()
    return chunks


# ---------------------------------------------------------------------------
# Slash command registration
# ---------------------------------------------------------------------------

_SLASH_COMMANDS = [
    {
        "name": "quinn",
        "description": "Quinn QA assistant",
        "options": [
            {
                "name": "status",
                "description": "Quick health check across configured apps",
                "type": 1,  # SUB_COMMAND
            },
            {
                "name": "bugs",
                "description": "Show active bugs across apps",
                "type": 1,
            },
            {
                "name": "release",
                "description": "Generate release notes for a version",
                "type": 1,
                "options": [
                    {
                        "name": "version",
                        "description": "Version tag (e.g. v0.89.3). Omit for latest.",
                        "type": 3,  # STRING
                        "required": False,
                    }
                ],
            },
        ],
    }
]


async def _register_slash_commands(application_id: str):
    """Register slash commands with the Discord API for the configured guild."""
    if not _GUILD_ID:
        log.warning("DISCORD_GUILD_ID not set -- skipping slash command registration")
        return

    for cmd in _SLASH_COMMANDS:
        result = await _api_request(
            "POST",
            f"/applications/{application_id}/guilds/{_GUILD_ID}/commands",
            json=cmd,
        )
        if result:
            log.info("Registered slash command: /%s", cmd["name"])
        else:
            log.warning("Failed to register slash command: /%s", cmd["name"])


# ---------------------------------------------------------------------------
# Agent interaction (calls the Gradio /api/chat endpoint)
# ---------------------------------------------------------------------------

async def _ask_agent(prompt: str, session_id: str) -> str:
    """Send a prompt to Quinn's LangGraph agent and return the response text."""
    client = await _get_client()
    try:
        resp = await client.post(
            _CHAT_URL,
            json={"message": prompt, "session_id": session_id},
            timeout=600,
        )
        resp.raise_for_status()
        return resp.json().get("response", "")
    except httpx.HTTPStatusError as exc:
        log.error("Agent HTTP error: %s", exc.response.status_code)
        return f"Agent returned an error (HTTP {exc.response.status_code})."
    except httpx.RequestError as exc:
        log.error("Agent request failed: %s", exc)
        return "Could not reach the agent. The server may still be starting up."


# ---------------------------------------------------------------------------
# Moderation actions
# ---------------------------------------------------------------------------

async def _log_moderation_action(
    action: str,
    user_id: str,
    username: str,
    channel_id: str,
    detail: str,
):
    """Log a moderation action to both the audit system and the mod log channel."""
    audit_logger.log(
        session_id=f"moderation-{user_id}",
        tool="discord_moderation",
        args={"action": action, "user": username, "channel": channel_id},
        result_summary=detail,
        duration_ms=0,
        success=True,
    )

    if _MOD_LOG_CHANNEL_ID:
        log_msg = f"**[{action.upper()}]** {username} (`{user_id}`) in <#{channel_id}>: {detail}"
        await _send_message(_MOD_LOG_CHANNEL_ID, log_msg)


async def _handle_moderation(data: dict, bot_id: str) -> bool:
    """Run moderation checks on an incoming message.

    Returns True if the message was handled (deleted/warned) and normal
    processing should be skipped.
    """
    author = data.get("author", {})
    user_id = author.get("id", "")
    username = author.get("username", "unknown")
    channel_id = data.get("channel_id", "")
    message_id = data.get("id", "")
    content = data.get("content", "")

    # Skip bot messages entirely
    if author.get("bot") or user_id == bot_id:
        return False

    # --- Spam filter ---
    matched_pattern = _is_spam(content)
    if matched_pattern:
        await _delete_message(channel_id, message_id)
        await _log_moderation_action(
            "spam_delete",
            user_id,
            username,
            channel_id,
            f"Matched spam pattern: {matched_pattern}",
        )
        log.info("Deleted spam from %s in #%s", username, channel_id)
        return True

    # --- Rate limiting ---
    if _check_rate_limit(user_id):
        if _should_warn_rate_limit(user_id):
            await _reply(
                channel_id,
                message_id,
                f"{username}, you're sending messages too quickly. Please slow down.",
            )
            await _log_moderation_action(
                "rate_limit_warn",
                user_id,
                username,
                channel_id,
                f"Exceeded {MODERATION_CONFIG['rate_limit_messages']} messages "
                f"in {MODERATION_CONFIG['rate_limit_window_seconds']}s",
            )
        return False  # Don't suppress the message, just warn

    return False


# ---------------------------------------------------------------------------
# Event handlers
# ---------------------------------------------------------------------------

async def _handle_qa_trigger(channel_id: str, message_id: str, content: str):
    """Handle clipboard emoji trigger -- run QA analysis on the message content."""
    await _react(channel_id, message_id, "\U0001f440")  # eyes

    prompt = (
        "Analyze the following message from Discord for QA relevance. "
        "If it describes a bug, regression, or quality concern, "
        "summarize the issue, assess severity, and suggest next steps.\n\n"
        "IMPORTANT FORMATTING RULES (this will be posted to Discord):\n"
        "- Do NOT use markdown tables (Discord doesn't render them)\n"
        "- Use bullet lists instead of tables\n"
        "- Bold with **text** is fine\n"
        "- Use > for blockquotes\n"
        "- Keep sections short\n\n"
        f"{content}"
    )

    session_id = f"discord-qa-{channel_id}-{message_id}"
    typing_task = asyncio.create_task(_keep_typing(channel_id))
    try:
        result = await _ask_agent(prompt, session_id)
    finally:
        typing_task.cancel()

    if not result:
        await _reply(channel_id, message_id, "Could not produce a QA analysis. Try @mentioning me with more detail.")
        return

    await _react(channel_id, message_id, TRIGGER_EMOJI)
    await _reply(channel_id, message_id, f"**QA Analysis**\n\n{result}")


async def _handle_reaction(data: dict, bot_id: str):
    """Handle reaction events -- trigger QA analysis on clipboard emoji."""
    emoji_name = data.get("emoji", {}).get("name", "")
    if emoji_name != TRIGGER_EMOJI:
        return

    user_id = data.get("user_id", "")
    if user_id == bot_id:
        return

    channel_id = data.get("channel_id", "")
    message_id = data.get("message_id", "")
    log.info("QA trigger: channel=%s message=%s", channel_id, message_id)

    msg = await _get_message(channel_id, message_id)
    if not msg:
        return

    content = msg.get("content", "")
    for embed in msg.get("embeds", []):
        if embed.get("url"):
            content += f"\n{embed['url']}"
        if embed.get("title"):
            content += f"\n{embed['title']}"
        if embed.get("description"):
            content += f"\n{embed['description'][:500]}"

    if not content.strip():
        await _reply(channel_id, message_id, "This message doesn't have content I can analyze. Try @mentioning me with a question instead.")
        return

    asyncio.create_task(_handle_qa_trigger(channel_id, message_id, content))


async def _handle_mention(data: dict, bot_id: str):
    """Handle @mention -- send content to the agent and reply."""
    author = data.get("author", {})
    if author.get("id") == bot_id or author.get("bot"):
        return

    content = data.get("content", "")
    channel_id = data.get("channel_id", "")
    message_id = data.get("id", "")
    username = author.get("username", "someone")

    mentions = data.get("mentions", [])
    if not any(m.get("id") == bot_id for m in mentions):
        return

    clean_content = re.sub(r"<@!?" + bot_id + r">", "", content).strip()
    log.info("Mention from %s in channel=%s", username, channel_id)

    # If this is a reply, pull in the referenced message for context
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
        await _reply(
            channel_id,
            message_id,
            "What can I help with? You can:\n"
            "- Ask me a QA or release question\n"
            "- React with the clipboard emoji to any message for QA analysis\n"
            "- Use `/quinn status`, `/quinn bugs`, or `/quinn release`",
        )
        return

    # Build agent prompt with Discord formatting instructions
    prompt_parts = []
    if context_content:
        prompt_parts.append(f"Context from a referenced Discord message:\n{context_content}")
    if clean_content:
        prompt_parts.append(f"User ({username}) asks:\n{clean_content}")

    prompt_parts.append(
        "\nIMPORTANT FORMATTING RULES (this will be posted to Discord):\n"
        "- Do NOT use markdown tables (Discord doesn't render them)\n"
        "- Use bullet lists instead of tables\n"
        "- Bold with **text** is fine\n"
        "- Use > for blockquotes\n"
        "- Keep sections short -- Discord truncates long messages"
    )

    prompt = "\n\n".join(prompt_parts)
    session_id = f"discord-mention-{channel_id}-{message_id}"

    typing_task = asyncio.create_task(_keep_typing(channel_id))
    try:
        result = await _ask_agent(prompt, session_id)
    finally:
        typing_task.cancel()

    if not result:
        await _reply(channel_id, message_id, "I couldn't generate a response. Please try again.")
        return

    await _reply(channel_id, message_id, result)


async def _handle_message(data: dict, bot_id: str):
    """Dispatch MESSAGE_CREATE events through moderation and then to handlers."""
    # Moderation runs first on every non-bot message
    suppressed = await _handle_moderation(data, bot_id)
    if suppressed:
        return

    # Mention handling
    await _handle_mention(data, bot_id)


# ---------------------------------------------------------------------------
# Slash command handler
# ---------------------------------------------------------------------------

async def _handle_interaction(data: dict):
    """Handle an INTERACTION_CREATE event (slash commands)."""
    interaction_id = data.get("id", "")
    interaction_token = data.get("token", "")
    interaction_data = data.get("data", {})
    command_name = interaction_data.get("name", "")

    if command_name != "quinn":
        return

    # Identify the subcommand
    options = interaction_data.get("options", [])
    if not options:
        await _interaction_respond(interaction_id, interaction_token, "Use a subcommand: `status`, `bugs`, or `release`.")
        return

    subcommand = options[0].get("name", "")
    sub_options = options[0].get("options", [])

    # Acknowledge immediately (the agent call may take a while)
    await _interaction_defer(interaction_id, interaction_token)

    session_id = f"discord-slash-{interaction_id}"

    if subcommand == "status":
        prompt = "/status"
        result = await _ask_agent(prompt, session_id)
        await _interaction_followup(interaction_token, result or "Health check returned no data.")

    elif subcommand == "bugs":
        prompt = "/bugs"
        result = await _ask_agent(prompt, session_id)
        await _interaction_followup(interaction_token, result or "No active bugs found.")

    elif subcommand == "release":
        version = ""
        for opt in sub_options:
            if opt.get("name") == "version":
                version = opt.get("value", "")
        prompt = f"/release {version}".strip()
        result = await _ask_agent(prompt, session_id)
        await _interaction_followup(interaction_token, result or "Could not generate release notes.")

    else:
        await _interaction_followup(interaction_token, f"Unknown subcommand: `{subcommand}`")


async def _interaction_defer(interaction_id: str, interaction_token: str):
    """Acknowledge a slash command and tell Discord we'll follow up later."""
    await _api_request(
        "POST",
        f"/interactions/{interaction_id}/{interaction_token}/callback",
        json={"type": 5},  # DEFERRED_CHANNEL_MESSAGE_WITH_SOURCE
    )


async def _interaction_respond(interaction_id: str, interaction_token: str, content: str):
    """Send an immediate response to a slash command interaction."""
    await _api_request(
        "POST",
        f"/interactions/{interaction_id}/{interaction_token}/callback",
        json={"type": 4, "data": {"content": content[:2000]}},
    )


async def _interaction_followup(interaction_token: str, content: str):
    """Send a follow-up message after a deferred interaction response."""
    client = await _get_client()
    app_id = _application_id
    if not app_id:
        log.warning("No application ID available for interaction followup")
        return

    chunks = _split_message(content)
    for chunk in chunks:
        await client.post(
            f"{_DISCORD_API}/webhooks/{app_id}/{interaction_token}",
            headers={"Authorization": f"Bot {_BOT_TOKEN}"},
            json={"content": chunk},
        )


# ---------------------------------------------------------------------------
# Welcome handler
# ---------------------------------------------------------------------------

async def _handle_guild_member_add(data: dict):
    """Welcome new members to the guild."""
    if not _WELCOME_CHANNEL_ID:
        return

    user = data.get("user", {})
    username = user.get("username", "new member")
    user_id = user.get("id", "")

    welcome_msg = (
        f"Welcome to protoLabs, <@{user_id}>! "
        f"I'm Quinn, the QA engineer. If you need help or have questions, "
        f"feel free to mention me or use `/quinn status` to see how things are running. "
        f"Happy to have you here."
    )

    await _send_message(_WELCOME_CHANNEL_ID, welcome_msg)
    log.info("Welcomed new member: %s (%s)", username, user_id)


# ---------------------------------------------------------------------------
# Daily digest
# ---------------------------------------------------------------------------

# Default channel for daily digest (protoLabs #dev)
_DIGEST_CHANNEL_ID = os.environ.get("DIGEST_CHANNEL_ID", "1469080556720623699")


async def post_daily_digest(channel_id: str | None = None):
    """Generate and post a daily QA status digest.

    Can be called externally (e.g. from a cron job) or from within the bot.
    Posts to the configured channel (default: #dev).
    """
    target_channel = channel_id or _DIGEST_CHANNEL_ID
    if not target_channel:
        log.warning("No channel configured for daily digest")
        return

    session_id = f"discord-digest-{int(time.time())}"
    prompt = (
        "Generate a concise daily QA status digest for the protoLabs Discord community. "
        "Include:\n"
        "- Overall health status of configured apps\n"
        "- Number of features in each status (backlog, in_progress, review, blocked, done)\n"
        "- Any active bugs or blocked features that need attention\n"
        "- PRs awaiting review\n"
        "- Brief summary of what shipped recently\n\n"
        "IMPORTANT FORMATTING RULES (this will be posted to Discord):\n"
        "- Do NOT use markdown tables (Discord doesn't render them)\n"
        "- Use bullet lists instead of tables\n"
        "- Bold with **text** is fine\n"
        "- Use > for blockquotes\n"
        "- Keep it under 1500 characters\n"
        "- Start with a one-line verdict (e.g. 'All systems healthy' or 'Attention needed: ...')"
    )

    result = await _ask_agent(prompt, session_id)
    if not result:
        log.warning("Daily digest: agent returned empty response")
        return

    header = "**Daily QA Digest**\n\n"
    await _send_message(target_channel, header + result)
    log.info("Posted daily digest to channel %s", target_channel)

    audit_logger.log(
        session_id=session_id,
        tool="daily_digest",
        args={"channel": target_channel},
        result_summary=result[:200],
        duration_ms=0,
        success=True,
    )


# ---------------------------------------------------------------------------
# Daily digest scheduler
# ---------------------------------------------------------------------------


async def _daily_digest_scheduler():
    """Run post_daily_digest() once per day at DIGEST_HOUR_UTC."""
    while True:
        now = datetime.now(timezone.utc)
        target = now.replace(hour=_DIGEST_HOUR, minute=0, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        wait_seconds = (target - now).total_seconds()
        log.info(
            "Next daily digest in %.1f hours (at %s)",
            wait_seconds / 3600,
            target.strftime("%H:%M UTC"),
        )
        await asyncio.sleep(wait_seconds)
        try:
            await post_daily_digest()
            log.info("Daily digest posted successfully")
        except Exception as e:
            log.error("Daily digest failed: %s", e)


# ---------------------------------------------------------------------------
# Gateway WebSocket connection
# ---------------------------------------------------------------------------

# Set after READY event
_application_id: str = ""


async def _heartbeat_loop(ws, interval: float, get_sequence):
    """Send heartbeats at the required interval."""
    try:
        while True:
            await asyncio.sleep(interval)
            await ws.send(json_mod.dumps({"op": 1, "d": get_sequence()}))
    except Exception:
        pass


async def _run_gateway():
    """Connect to Discord Gateway and listen for events."""
    global _application_id

    try:
        import websockets
    except ImportError:
        log.error("websockets not installed -- bot disabled")
        return

    bot_user = await _get_bot_user()
    if not bot_user:
        log.error("Could not fetch bot user info")
        return
    bot_id = bot_user["id"]
    _application_id = bot_user.get("id", "")
    log.info("Bot user: %s (%s)", bot_user.get("username"), bot_id)

    # Fetch the actual application ID from the bot's application info
    app_info = await _api_request("GET", "/oauth2/applications/@me")
    if app_info:
        _application_id = app_info.get("id", _application_id)

    # Register slash commands
    await _register_slash_commands(_application_id)

    # Get gateway URL
    client = await _get_client()
    resp = await client.get(
        f"{_DISCORD_API}/gateway/bot",
        headers={"Authorization": f"Bot {_BOT_TOKEN}"},
    )
    gateway_url = resp.json().get("url", "wss://gateway.discord.gg")

    sequence = None

    while True:
        try:
            async with websockets.connect(f"{gateway_url}?v=10&encoding=json") as ws:
                log.info("Connected to Discord Gateway")

                async for raw_msg in ws:
                    data = json_mod.loads(raw_msg)
                    op = data.get("op")
                    t = data.get("t")
                    d = data.get("d")
                    s = data.get("s")

                    if s is not None:
                        sequence = s

                    # Hello -- start heartbeating and identify
                    if op == 10:
                        heartbeat_interval = d["heartbeat_interval"] / 1000
                        await ws.send(json_mod.dumps({
                            "op": 2,
                            "d": {
                                "token": _BOT_TOKEN,
                                "intents": _GATEWAY_INTENTS,
                                "properties": {
                                    "os": "linux",
                                    "browser": "quinn",
                                    "device": "quinn",
                                },
                            },
                        }))
                        asyncio.create_task(
                            _heartbeat_loop(ws, heartbeat_interval, lambda: sequence)
                        )

                    # Heartbeat ACK -- no action needed
                    elif op == 11:
                        pass

                    # Dispatch events
                    elif op == 0:
                        if t == "READY":
                            guilds = d.get("guilds", [])
                            log.info("Gateway READY -- %d guild(s)", len(guilds))
                            asyncio.create_task(_daily_digest_scheduler())

                        elif t == "MESSAGE_REACTION_ADD":
                            asyncio.create_task(_safe_dispatch(_handle_reaction, d, bot_id))

                        elif t == "MESSAGE_CREATE":
                            asyncio.create_task(_safe_dispatch(_handle_message, d, bot_id))

                        elif t == "INTERACTION_CREATE":
                            asyncio.create_task(_safe_dispatch(_handle_interaction, d))

                        elif t == "GUILD_MEMBER_ADD":
                            asyncio.create_task(_safe_dispatch(_handle_guild_member_add, d))

                    # Reconnect requested
                    elif op == 7:
                        log.info("Gateway requested reconnect")
                        break

                    # Invalid session
                    elif op == 9:
                        log.warning("Invalid session -- reconnecting")
                        break

        except Exception as exc:
            log.error("Gateway error: %s", exc)
            await asyncio.sleep(5)
            log.info("Reconnecting to Gateway...")


async def _safe_dispatch(handler, *args):
    """Run an event handler with exception logging so one failure doesn't crash the gateway."""
    try:
        await handler(*args)
    except Exception as exc:
        log.error("Handler %s raised: %s", handler.__name__, exc, exc_info=True)


# ---------------------------------------------------------------------------
# Public API -- called from server.py
# ---------------------------------------------------------------------------

def start_bot():
    """Start the Discord bot in a background thread."""
    if not _BOT_TOKEN:
        log.warning("DISCORD_BOT_TOKEN not set -- bot disabled")
        return

    def _run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(_run_gateway())

    thread = threading.Thread(target=_run, daemon=True, name="discord-bot")
    thread.start()
    log.info("Discord bot started in background thread")
