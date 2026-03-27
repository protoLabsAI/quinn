"""Discord server administration tool for Quinn.

Full Discord REST API wrapper for server management. Quinn's bot has
admin permissions — this tool exposes channel, message, webhook, role,
and moderation operations.

Uses the Discord REST API directly via httpx (no discord.py dependency).
Requires DISCORD_BOT_TOKEN env var.
"""

import json
import os
from typing import Any

import httpx

from nanobot.agent.tools.base import Tool

_DISCORD_API = "https://discord.com/api/v10"
_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")
_GUILD_ID = os.environ.get("DISCORD_GUILD_ID", "")
_TIMEOUT = 15

# Resolved at first use — queries bot's guild list if env var is empty
_resolved_guild_id: str | None = None


async def _get_guild_id() -> str:
    """Get the guild ID. Uses env var first, then queries the bot's guild list at runtime."""
    global _resolved_guild_id
    if _resolved_guild_id:
        return _resolved_guild_id
    if _GUILD_ID:
        _resolved_guild_id = _GUILD_ID
        return _GUILD_ID
    # Quinn is only in one guild — just ask Discord which one
    guilds = await _api("GET", "/users/@me/guilds")
    if isinstance(guilds, list) and guilds:
        _resolved_guild_id = guilds[0]["id"]
        return _resolved_guild_id
    return ""


async def _api(
    method: str, path: str, json_body: dict | None = None
) -> dict | list | None:
    """Make an authenticated Discord API request."""
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.request(
            method,
            f"{_DISCORD_API}{path}",
            headers={
                "Authorization": f"Bot {_BOT_TOKEN}",
                "Content-Type": "application/json",
            },
            json=json_body,
        )
        if resp.status_code in (200, 201, 204):
            return resp.json() if resp.content else None
        return {"error": f"Discord API {resp.status_code}: {resp.text[:300]}"}


class DiscordAdminTool(Tool):
    """Full Discord server administration."""

    @property
    def name(self) -> str:
        return "discord_admin"

    @property
    def description(self) -> str:
        return (
            "Manage the Discord server: channels, categories, messages, webhooks, "
            "reactions, forums, and server info. Quinn has admin permissions."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "action": "server_info | list_channels | create_channel | delete_channel | "
                      "create_category | edit_category | delete_category | "
                      "send_message | read_messages | delete_message | "
                      "add_reaction | remove_reaction | "
                      "list_webhooks | create_webhook | send_webhook | delete_webhook | "
                      "list_forums | create_forum_post | reply_to_forum | "
                      "edit_channel | set_channel_topic",
            "channel_id": "Discord channel ID (required for most actions)",
            "name": "Channel/category/webhook name (for create actions)",
            "content": "Message content (for send actions)",
            "message_id": "Message ID (for reactions, delete)",
            "emoji": "Emoji for reactions (unicode or custom format)",
            "category_id": "Parent category ID (for create_channel)",
            "webhook_url": "Full webhook URL (for send_webhook)",
            "topic": "Channel topic or forum post title",
            "guild_id": "Server ID (defaults to configured guild)",
            "limit": "Number of messages to fetch (default 20)",
        }

    async def execute(self, **kwargs: Any) -> str:
        action = kwargs.get("action", "")
        guild_id = kwargs.get("guild_id") or await _get_guild_id()

        if not _BOT_TOKEN:
            return "Error: DISCORD_BOT_TOKEN not set"

        if not guild_id:
            return "Error: Could not resolve guild ID. Set DISCORD_GUILD_ID or ensure bot is in a server."

        # --- Server Info ---
        if action == "server_info":
            guild = await _api("GET", f"/guilds/{guild_id}?with_counts=true")
            channels = await _api("GET", f"/guilds/{guild_id}/channels")
            if isinstance(guild, dict) and "error" in guild:
                return guild["error"]

            categories = [c for c in channels if c["type"] == 4]
            text_channels = [c for c in channels if c["type"] == 0]
            voice_channels = [c for c in channels if c["type"] == 2]
            forum_channels = [c for c in channels if c["type"] == 15]

            lines = [
                f"**Server: {guild['name']}**",
                f"Members: ~{guild.get('approximate_member_count', '?')}",
                f"Categories: {len(categories)} | Text: {len(text_channels)} | Voice: {len(voice_channels)} | Forums: {len(forum_channels)}",
                "",
            ]

            # Group channels by category
            cat_map = {c["id"]: c["name"] for c in categories}
            cat_map[None] = "No Category"

            for cat_id, cat_name in cat_map.items():
                children = [c for c in channels if c.get("parent_id") == cat_id and c["type"] != 4]
                if children:
                    lines.append(f"**{cat_name}:**")
                    for ch in sorted(children, key=lambda c: c.get("position", 0)):
                        ch_type = {0: "text", 2: "voice", 5: "announcement", 15: "forum"}.get(ch["type"], "other")
                        lines.append(f"  #{ch['name']} ({ch_type}) — `{ch['id']}`")
                    lines.append("")

            return "\n".join(lines)

        # --- Channel Management ---
        elif action == "list_channels":
            channels = await _api("GET", f"/guilds/{guild_id}/channels")
            if isinstance(channels, dict) and "error" in channels:
                return channels["error"]
            lines = []
            for ch in sorted(channels, key=lambda c: (c.get("position", 0))):
                ch_type = {0: "text", 2: "voice", 4: "category", 5: "announcement", 15: "forum"}.get(ch["type"], "other")
                lines.append(f"#{ch['name']} ({ch_type}) — `{ch['id']}`")
            return "\n".join(lines) or "No channels found."

        elif action == "create_channel":
            name = kwargs.get("name", "")
            if not name:
                return "Error: name is required"
            body: dict[str, Any] = {"name": name, "type": 0}
            if kwargs.get("category_id"):
                body["parent_id"] = kwargs["category_id"]
            if kwargs.get("topic"):
                body["topic"] = kwargs["topic"]
            result = await _api("POST", f"/guilds/{guild_id}/channels", body)
            if isinstance(result, dict) and "error" in result:
                return result["error"]
            return f"Channel #{result['name']} created — `{result['id']}`"

        elif action == "edit_channel":
            channel_id = kwargs.get("channel_id", "")
            if not channel_id:
                return "Error: channel_id is required"
            body = {}
            if kwargs.get("name"):
                body["name"] = kwargs["name"]
            if kwargs.get("topic"):
                body["topic"] = kwargs["topic"]
            if not body:
                return "Error: provide name or topic to edit"
            result = await _api("PATCH", f"/channels/{channel_id}", body)
            if isinstance(result, dict) and "error" in result:
                return result["error"]
            return f"Channel #{result['name']} updated."

        elif action == "set_channel_topic":
            channel_id = kwargs.get("channel_id", "")
            topic = kwargs.get("topic", "")
            if not channel_id or not topic:
                return "Error: channel_id and topic are required"
            result = await _api("PATCH", f"/channels/{channel_id}", {"topic": topic})
            if isinstance(result, dict) and "error" in result:
                return result["error"]
            return f"Topic set for #{result['name']}: {topic}"

        elif action == "delete_channel":
            channel_id = kwargs.get("channel_id", "")
            if not channel_id:
                return "Error: channel_id is required"
            result = await _api("DELETE", f"/channels/{channel_id}")
            if isinstance(result, dict) and "error" in result:
                return result["error"]
            return f"Channel deleted."

        # --- Category Management ---
        elif action == "create_category":
            name = kwargs.get("name", "")
            if not name:
                return "Error: name is required"
            result = await _api("POST", f"/guilds/{guild_id}/channels", {"name": name, "type": 4})
            if isinstance(result, dict) and "error" in result:
                return result["error"]
            return f"Category '{result['name']}' created — `{result['id']}`"

        elif action == "edit_category":
            channel_id = kwargs.get("channel_id", "")
            name = kwargs.get("name", "")
            if not channel_id or not name:
                return "Error: channel_id and name are required"
            result = await _api("PATCH", f"/channels/{channel_id}", {"name": name})
            if isinstance(result, dict) and "error" in result:
                return result["error"]
            return f"Category renamed to '{result['name']}'."

        elif action == "delete_category":
            channel_id = kwargs.get("channel_id", "")
            if not channel_id:
                return "Error: channel_id is required"
            result = await _api("DELETE", f"/channels/{channel_id}")
            return "Category deleted."

        # --- Messages ---
        elif action == "send_message":
            channel_id = kwargs.get("channel_id", "")
            content = kwargs.get("content", "")
            if not channel_id or not content:
                return "Error: channel_id and content are required"
            result = await _api("POST", f"/channels/{channel_id}/messages", {"content": content})
            if isinstance(result, dict) and "error" in result:
                return result["error"]
            return f"Message sent to <#{channel_id}> — ID: `{result['id']}`"

        elif action == "read_messages":
            channel_id = kwargs.get("channel_id", "")
            limit = int(kwargs.get("limit", 20))
            if not channel_id:
                return "Error: channel_id is required"
            messages = await _api("GET", f"/channels/{channel_id}/messages?limit={limit}")
            if isinstance(messages, dict) and "error" in messages:
                return messages["error"]
            lines = []
            for msg in reversed(messages):
                author = msg["author"]["username"]
                content = msg["content"][:200] or "[embed/attachment]"
                ts = msg["timestamp"][:16]
                lines.append(f"`{ts}` **{author}**: {content}")
            return "\n".join(lines) or "No messages."

        elif action == "delete_message":
            channel_id = kwargs.get("channel_id", "")
            message_id = kwargs.get("message_id", "")
            if not channel_id or not message_id:
                return "Error: channel_id and message_id are required"
            await _api("DELETE", f"/channels/{channel_id}/messages/{message_id}")
            return "Message deleted."

        # --- Reactions ---
        elif action == "add_reaction":
            channel_id = kwargs.get("channel_id", "")
            message_id = kwargs.get("message_id", "")
            emoji = kwargs.get("emoji", "")
            if not all([channel_id, message_id, emoji]):
                return "Error: channel_id, message_id, and emoji are required"
            await _api("PUT", f"/channels/{channel_id}/messages/{message_id}/reactions/{emoji}/@me")
            return f"Reacted with {emoji}."

        elif action == "remove_reaction":
            channel_id = kwargs.get("channel_id", "")
            message_id = kwargs.get("message_id", "")
            emoji = kwargs.get("emoji", "")
            if not all([channel_id, message_id, emoji]):
                return "Error: channel_id, message_id, and emoji are required"
            await _api("DELETE", f"/channels/{channel_id}/messages/{message_id}/reactions/{emoji}/@me")
            return f"Reaction {emoji} removed."

        # --- Webhooks ---
        elif action == "list_webhooks":
            channel_id = kwargs.get("channel_id", "")
            if channel_id:
                hooks = await _api("GET", f"/channels/{channel_id}/webhooks")
            else:
                hooks = await _api("GET", f"/guilds/{guild_id}/webhooks")
            if isinstance(hooks, dict) and "error" in hooks:
                return hooks["error"]
            lines = []
            for h in hooks:
                lines.append(f"**{h['name']}** — channel: <#{h['channel_id']}> — `{h['id']}`")
            return "\n".join(lines) or "No webhooks found."

        elif action == "create_webhook":
            channel_id = kwargs.get("channel_id", "")
            name = kwargs.get("name", "Quinn")
            if not channel_id:
                return "Error: channel_id is required"
            result = await _api("POST", f"/channels/{channel_id}/webhooks", {"name": name})
            if isinstance(result, dict) and "error" in result:
                return result["error"]
            url = f"https://discord.com/api/webhooks/{result['id']}/{result['token']}"
            return f"Webhook '{result['name']}' created.\nURL: `{url}`"

        elif action == "send_webhook":
            webhook_url = kwargs.get("webhook_url", "")
            content = kwargs.get("content", "")
            if not webhook_url or not content:
                return "Error: webhook_url and content are required"
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resp = await client.post(webhook_url, json={"content": content})
                if resp.status_code in (200, 204):
                    return "Webhook message sent."
                return f"Error: {resp.status_code} {resp.text[:200]}"

        elif action == "delete_webhook":
            webhook_url = kwargs.get("webhook_url", "")
            if not webhook_url:
                return "Error: webhook_url is required"
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resp = await client.delete(webhook_url)
                return "Webhook deleted." if resp.status_code == 204 else f"Error: {resp.status_code}"

        # --- Forums ---
        elif action == "list_forums":
            channels = await _api("GET", f"/guilds/{guild_id}/channels")
            if isinstance(channels, dict) and "error" in channels:
                return channels["error"]
            forums = [c for c in channels if c["type"] == 15]
            lines = [f"#{f['name']} — `{f['id']}`" for f in forums]
            return "\n".join(lines) or "No forum channels."

        elif action == "create_forum_post":
            channel_id = kwargs.get("channel_id", "")
            name = kwargs.get("name", "")
            content = kwargs.get("content", "")
            if not all([channel_id, name, content]):
                return "Error: channel_id, name, and content are required"
            result = await _api("POST", f"/channels/{channel_id}/threads", {
                "name": name,
                "message": {"content": content},
            })
            if isinstance(result, dict) and "error" in result:
                return result["error"]
            return f"Forum post '{result['name']}' created — `{result['id']}`"

        elif action == "reply_to_forum":
            channel_id = kwargs.get("channel_id", "")
            content = kwargs.get("content", "")
            if not channel_id or not content:
                return "Error: channel_id (thread ID) and content are required"
            result = await _api("POST", f"/channels/{channel_id}/messages", {"content": content})
            if isinstance(result, dict) and "error" in result:
                return result["error"]
            return "Reply posted."

        else:
            return (
                "Unknown action. Available: server_info, list_channels, create_channel, "
                "edit_channel, set_channel_topic, delete_channel, create_category, "
                "edit_category, delete_category, send_message, read_messages, "
                "delete_message, add_reaction, remove_reaction, list_webhooks, "
                "create_webhook, send_webhook, delete_webhook, list_forums, "
                "create_forum_post, reply_to_forum"
            )
