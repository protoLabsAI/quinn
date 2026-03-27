"""Browser automation tool for protoResearcher using agent-browser CLI.

Provides web browsing capabilities inside the sandbox container.
Chrome runs with --no-sandbox since the container IS the sandbox.
"""

import asyncio
import json
import os
from typing import Any

from nanobot.agent.tools.base import Tool


class BrowserTool(Tool):
    """Web browser automation via agent-browser CLI."""

    _ACTIONS = {
        "open": "Navigate to a URL",
        "snapshot": "Get accessibility tree of current page",
        "screenshot": "Take a screenshot (base64 PNG)",
        "click": "Click an element by accessibility label or selector",
        "fill": "Fill an input field with text",
        "find": "Find elements matching a query",
        "type": "Type text (keyboard input)",
        "wait": "Wait for a selector to appear",
    }

    _TIMEOUT = 30  # seconds per action

    @property
    def name(self) -> str:
        return "browser"

    @property
    def description(self) -> str:
        actions = ", ".join(self._ACTIONS.keys())
        return (
            f"Automate a web browser. Actions: {actions}. "
            "Returns accessibility tree snapshots by default (token-efficient). "
            "Use 'open' first, then 'snapshot' to read page content."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": list(self._ACTIONS.keys()),
                    "description": "Browser action to perform.",
                },
                "url": {
                    "type": "string",
                    "description": "URL to navigate to (required for 'open').",
                },
                "selector": {
                    "type": "string",
                    "description": "CSS selector or accessibility label for click/fill/wait.",
                },
                "text": {
                    "type": "string",
                    "description": "Text to fill or type.",
                },
                "query": {
                    "type": "string",
                    "description": "Search query for 'find' action.",
                },
            },
            "required": ["action"],
        }

    async def execute(self, **kwargs: Any) -> str:
        action = kwargs["action"]
        cmd = ["agent-browser", action]

        if action == "open":
            url = kwargs.get("url")
            if not url:
                return "Error: 'url' is required for the 'open' action."
            cmd.append(url)
        elif action in ("click", "wait"):
            selector = kwargs.get("selector")
            if not selector:
                return f"Error: 'selector' is required for the '{action}' action."
            cmd.extend(["--selector", selector])
        elif action == "fill":
            selector = kwargs.get("selector")
            text = kwargs.get("text", "")
            if not selector:
                return "Error: 'selector' is required for the 'fill' action."
            cmd.extend(["--selector", selector, "--text", text])
        elif action == "type":
            text = kwargs.get("text", "")
            if not text:
                return "Error: 'text' is required for the 'type' action."
            cmd.extend(["--text", text])
        elif action == "find":
            query = kwargs.get("query", "")
            if not query:
                return "Error: 'query' is required for the 'find' action."
            cmd.append(query)

        # Use /tmp for Chrome profile/cache (512MB tmpfs) instead of /sandbox (256MB)
        env = {**os.environ, "HOME": "/tmp", "TMPDIR": "/tmp"}

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=self._TIMEOUT
            )
        except asyncio.TimeoutError:
            proc.kill()
            return f"Error: browser action '{action}' timed out after {self._TIMEOUT}s."
        except FileNotFoundError:
            return "Error: agent-browser is not installed. Browser tool unavailable."

        if proc.returncode != 0:
            err = stderr.decode(errors="replace").strip()
            return f"Error: agent-browser exited {proc.returncode}: {err[:500]}"

        output = stdout.decode(errors="replace").strip()
        # Truncate very long outputs (e.g. full DOM snapshots)
        if len(output) > 8000:
            output = output[:8000] + "\n\n[... truncated, use 'find' to locate specific elements]"
        return output or "(no output)"
