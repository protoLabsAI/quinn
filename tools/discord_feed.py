"""Discord feed tool for protoResearcher.

Reads messages from Discord channels via the REST API, extracts URLs,
and classifies them for the research pipeline (arxiv, HF, GitHub, blogs).

Requires DISCORD_BOT_TOKEN env var. Channel IDs configured in research-config.json.
"""

import os
import re
from typing import Any
from urllib.parse import urlparse

import httpx

from nanobot.agent.tools.base import Tool

_DISCORD_API = "https://discord.com/api/v10"
_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
_URL_PATTERN = re.compile(r'https?://[^\s<>\[\]()\"\']+')

# URL classification patterns
_CLASSIFIERS = [
    ("arxiv", re.compile(r'arxiv\.org/(abs|pdf)/(\d{4}\.\d{4,5})')),
    ("huggingface", re.compile(r'huggingface\.co/([^/\s]+/[^/\s]+)')),
    ("github", re.compile(r'github\.com/([^/\s]+/[^/\s]+)')),
    ("paper", re.compile(r'(arxiv|openreview|papers\.nips|aclanthology)\.')),
    ("blog", re.compile(r'(blog|medium\.com|substack\.com|\.ai/blog)')),
]


def _classify_url(url: str) -> str:
    """Classify a URL by source type."""
    for label, pattern in _CLASSIFIERS:
        if pattern.search(url):
            return label
    return "link"


def _extract_urls(text: str) -> list[dict[str, str]]:
    """Extract and classify URLs from text."""
    urls = _URL_PATTERN.findall(text)
    results = []
    seen = set()
    for url in urls:
        # Clean trailing punctuation
        url = url.rstrip('.,;:!?)>')
        if url in seen:
            continue
        seen.add(url)
        results.append({"url": url, "type": _classify_url(url)})
    return results


def _get_token() -> str | None:
    return os.environ.get("DISCORD_BOT_TOKEN")


class DiscordFeedTool(Tool):
    """Read Discord channels and extract research links."""

    @property
    def name(self) -> str:
        return "discord_feed"

    @property
    def description(self) -> str:
        return (
            "Read messages from Discord channels and publish research digests.\n\n"
            "READING (requires channel_id):\n"
            "- scan: Read recent messages and extract classified URLs\n"
            "- history: Get raw message history\n"
            "- channels: List channels in a server (guild_id required)\n"
            "- digest: Scan a channel and produce a structured link digest\n\n"
            "PUBLISHING (NO channel_id needed — uses pre-configured webhook):\n"
            "- publish: Post content to #protolabs-research via webhook. "
            "Just provide 'content' and optionally 'title'. The webhook is auto-configured.\n\n"
            "URLs are classified as: arxiv, huggingface, github, paper, blog, or link."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["scan", "history", "channels", "digest", "publish"],
                    "description": "Action to perform.",
                },
                "channel_id": {
                    "type": "string",
                    "description": "Discord channel ID to read from (for scan/history/digest only — NOT needed for publish).",
                },
                "guild_id": {
                    "type": "string",
                    "description": "Discord server (guild) ID for listing channels.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Number of messages to fetch (default 50, max 100).",
                },
                "after": {
                    "type": "string",
                    "description": "Only fetch messages after this message ID (for pagination).",
                },
                "content": {
                    "type": "string",
                    "description": "Content to publish via webhook (for 'publish' action only). Markdown supported. No channel_id needed.",
                },
                "title": {
                    "type": "string",
                    "description": "Embed title for publish (default: '🔬 Research Update'). Only used with publish action.",
                },
            },
            "required": ["action"],
        }

    async def execute(self, **kwargs: Any) -> str:
        action = kwargs["action"]

        # Publish doesn't need bot token, just webhook
        if action == "publish":
            return await self._publish(kwargs)

        token = _get_token()
        if not token:
            return "Error: DISCORD_BOT_TOKEN not set. Add it to your environment."

        if action == "scan":
            return await self._scan(token, kwargs)
        elif action == "history":
            return await self._history(token, kwargs)
        elif action == "channels":
            return await self._channels(token, kwargs)
        elif action == "digest":
            return await self._digest(token, kwargs)
        else:
            return f"Error: Unknown action '{action}'."

    async def _fetch_messages(
        self, token: str, channel_id: str, limit: int = 50, after: str | None = None
    ) -> list[dict] | str:
        """Fetch messages from a Discord channel."""
        params: dict[str, Any] = {"limit": min(limit, 100)}
        if after:
            params["after"] = after

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    f"{_DISCORD_API}/channels/{channel_id}/messages",
                    params=params,
                    headers={"Authorization": f"Bot {token}"},
                )
                if resp.status_code == 403:
                    return "Error: Bot lacks permission to read this channel."
                if resp.status_code == 404:
                    return "Error: Channel not found."
                resp.raise_for_status()
                return resp.json()
        except Exception as e:
            return f"Error fetching messages: {e}"

    async def _scan(self, token: str, kwargs: dict) -> str:
        """Scan a channel for research links."""
        channel_id = kwargs.get("channel_id", "")
        if not channel_id:
            return "Error: 'channel_id' is required."

        limit = kwargs.get("limit", 50)
        after = kwargs.get("after")

        messages = await self._fetch_messages(token, channel_id, limit, after)
        if isinstance(messages, str):
            return messages

        if not messages:
            return "No messages found."

        # Extract all URLs from all messages
        all_links: list[dict] = []
        for msg in messages:
            content = msg.get("content", "")
            author = msg.get("author", {}).get("username", "?")
            msg_id = msg.get("id", "")
            timestamp = msg.get("timestamp", "")[:16]

            # Also check embeds
            for embed in msg.get("embeds", []):
                if embed.get("url"):
                    content += " " + embed["url"]
                if embed.get("description"):
                    content += " " + embed["description"]

            urls = _extract_urls(content)
            for url_info in urls:
                url_info["author"] = author
                url_info["timestamp"] = timestamp
                url_info["message_id"] = msg_id
                # Grab message context (first 150 chars)
                url_info["context"] = content[:150].replace("\n", " ")
                all_links.append(url_info)

        if not all_links:
            return f"Scanned {len(messages)} messages — no URLs found."

        # Group by type
        by_type: dict[str, list[dict]] = {}
        for link in all_links:
            by_type.setdefault(link["type"], []).append(link)

        lines = [f"**Scanned {len(messages)} messages — {len(all_links)} links found:**\n"]
        for link_type, links in sorted(by_type.items()):
            lines.append(f"### {link_type} ({len(links)})")
            for l in links:
                lines.append(
                    f"- {l['url']}\n"
                    f"  _{l['author']} at {l['timestamp']}_ — {l['context'][:80]}"
                )
            lines.append("")

        return "\n".join(lines)

    async def _history(self, token: str, kwargs: dict) -> str:
        """Get raw message history."""
        channel_id = kwargs.get("channel_id", "")
        if not channel_id:
            return "Error: 'channel_id' is required."

        limit = kwargs.get("limit", 20)
        after = kwargs.get("after")

        messages = await self._fetch_messages(token, channel_id, limit, after)
        if isinstance(messages, str):
            return messages

        if not messages:
            return "No messages found."

        lines = [f"**Last {len(messages)} messages:**\n"]
        for msg in reversed(messages):  # chronological order
            author = msg.get("author", {}).get("username", "?")
            content = msg.get("content", "")[:300]
            timestamp = msg.get("timestamp", "")[:16]
            attachments = len(msg.get("attachments", []))
            embeds = len(msg.get("embeds", []))

            extras = []
            if attachments:
                extras.append(f"{attachments} attachment(s)")
            if embeds:
                extras.append(f"{embeds} embed(s)")
            extra_str = f" [{', '.join(extras)}]" if extras else ""

            lines.append(f"**{author}** _{timestamp}_{extra_str}")
            if content:
                lines.append(content)
            lines.append("")

        return "\n".join(lines)

    async def _channels(self, token: str, kwargs: dict) -> str:
        """List channels in a Discord server."""
        guild_id = kwargs.get("guild_id", "")
        if not guild_id:
            return "Error: 'guild_id' is required."

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    f"{_DISCORD_API}/guilds/{guild_id}/channels",
                    headers={"Authorization": f"Bot {token}"},
                )
                if resp.status_code == 403:
                    return "Error: Bot lacks permission to access this server."
                resp.raise_for_status()
                channels = resp.json()
        except Exception as e:
            return f"Error: {e}"

        # Filter to text channels (type 0) and sort by position
        text_channels = [c for c in channels if c.get("type") == 0]
        text_channels.sort(key=lambda c: (c.get("parent_id") or "", c.get("position", 0)))

        if not text_channels:
            return "No text channels found."

        lines = [f"**Text channels ({len(text_channels)}):**"]
        current_category = None
        for ch in text_channels:
            cat = ch.get("parent_id")
            if cat != current_category:
                current_category = cat
                # Find category name
                cat_name = next(
                    (c.get("name", "?") for c in channels if c.get("id") == cat),
                    "uncategorized"
                )
                lines.append(f"\n**{cat_name}**")
            lines.append(f"- `{ch['id']}` #{ch.get('name', '?')}")

        return "\n".join(lines)

    async def _digest(self, token: str, kwargs: dict) -> str:
        """Scan a channel and produce a structured research digest."""
        channel_id = kwargs.get("channel_id", "")
        if not channel_id:
            return "Error: 'channel_id' is required."

        limit = kwargs.get("limit", 100)

        messages = await self._fetch_messages(token, channel_id, limit)
        if isinstance(messages, str):
            return messages

        if not messages:
            return "No messages found."

        # Collect all links with classification
        all_links: list[dict] = []
        for msg in messages:
            content = msg.get("content", "")
            for embed in msg.get("embeds", []):
                if embed.get("url"):
                    content += " " + embed["url"]

            for url_info in _extract_urls(content):
                url_info["context"] = content[:200].replace("\n", " ")
                all_links.append(url_info)

        # Deduplicate by URL
        seen = set()
        unique_links = []
        for link in all_links:
            if link["url"] not in seen:
                seen.add(link["url"])
                unique_links.append(link)

        # Build structured digest
        arxiv_links = [l for l in unique_links if l["type"] == "arxiv"]
        hf_links = [l for l in unique_links if l["type"] == "huggingface"]
        gh_links = [l for l in unique_links if l["type"] == "github"]
        paper_links = [l for l in unique_links if l["type"] == "paper"]
        blog_links = [l for l in unique_links if l["type"] == "blog"]
        other_links = [l for l in unique_links if l["type"] == "link"]

        lines = [
            f"**Discord Research Digest** — {len(messages)} messages, {len(unique_links)} unique links\n"
        ]

        if arxiv_links:
            lines.append(f"**Arxiv Papers ({len(arxiv_links)}):**")
            for l in arxiv_links:
                # Extract arxiv ID
                match = re.search(r'(\d{4}\.\d{4,5})', l["url"])
                aid = match.group(1) if match else ""
                lines.append(f"- [{aid}]({l['url']})")
            lines.append(f"\n_Tip: Use `browser` to fetch these papers, or rabbit-hole MCP to ingest them._\n")

        if hf_links:
            lines.append(f"**HuggingFace ({len(hf_links)}):**")
            for l in hf_links:
                lines.append(f"- {l['url']}")
            lines.append(f"\n_Tip: Use `huggingface` tool to get model cards._\n")

        if gh_links:
            lines.append(f"**GitHub ({len(gh_links)}):**")
            for l in gh_links:
                lines.append(f"- {l['url']}")
            lines.append("")

        if paper_links:
            lines.append(f"**Other Papers ({len(paper_links)}):**")
            for l in paper_links:
                lines.append(f"- {l['url']}")
            lines.append("")

        if blog_links:
            lines.append(f"**Blog Posts ({len(blog_links)}):**")
            for l in blog_links:
                lines.append(f"- {l['url']}")
            lines.append("")

        if other_links:
            lines.append(f"**Other Links ({len(other_links)}):**")
            for l in other_links[:10]:
                lines.append(f"- {l['url']}")
            if len(other_links) > 10:
                lines.append(f"  _...and {len(other_links) - 10} more_")

        return "\n".join(lines)

    async def _publish(self, kwargs: dict) -> str:
        """Publish a message to Discord via webhook. Always uses embeds, chunks if needed."""
        webhook_url = _WEBHOOK_URL
        if not webhook_url:
            return "Error: DISCORD_WEBHOOK_URL not set. Add it to your environment."

        content = kwargs.get("content", "")
        title = kwargs.get("title", "🔬 Research Update")

        if not content:
            return "Error: 'content' is required for publish."

        # Discord embed description limit is 4096 chars
        # Split into multiple embeds if content is longer
        chunks = []
        remaining = content
        while remaining:
            chunks.append(remaining[:4096])
            remaining = remaining[4096:]

        # Build embeds — first one gets the title, rest are continuations
        embeds = []
        for i, chunk in enumerate(chunks):
            embed: dict[str, Any] = {
                "description": chunk,
                "color": 0x14b8a6,
            }
            if i == 0:
                embed["title"] = title
            embeds.append(embed)

        # Discord allows max 10 embeds per message
        sent = 0
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                # Send in batches of 10 embeds
                for batch_start in range(0, len(embeds), 10):
                    batch = embeds[batch_start:batch_start + 10]
                    payload = {
                        "username": "protoResearcher",
                        "embeds": batch,
                    }
                    resp = await client.post(webhook_url, json=payload)
                    if resp.status_code not in (200, 204):
                        return f"Error: Discord returned {resp.status_code} on chunk {batch_start // 10 + 1}"
                    sent += len(batch)
        except Exception as e:
            return f"Error publishing to Discord: {e}"

        return f"Published to Discord ({sent} embed{'s' if sent > 1 else ''})."
