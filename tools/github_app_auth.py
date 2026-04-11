"""GitHub App installation token helper.

Mints short-lived installation tokens from QUINN_APP_ID + QUINN_APP_PRIVATE_KEY
and refreshes them continuously so Quinn's `gh` CLI always sees a fresh token.

The flow:
  1. Build a JWT signed with the App private key (RS256, 10-min expiry).
  2. Call GET /app/installations to discover the installation id.
  3. Call POST /app/installations/{id}/access_tokens → installation token
     (lives ~1 hour, scoped to the app's installed repos).
  4. Write it to a file that gh_cli.py reads before each gh invocation.
  5. Refresh every 45 min so we never serve a near-expired token.

Without this, we'd have to choose between a stale-forever PAT (wrong author)
or a stale-after-1h App token (broken after one hour). With it, Quinn posts
every review as @protoquinn[bot] and keeps doing so indefinitely.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

import httpx
import jwt as pyjwt


def _log(msg: str) -> None:
    """Plain stderr logger — avoids pulling in loguru for this small helper."""
    print(f"[github_app_auth] {msg}", file=sys.stderr, flush=True)

_TOKEN_FILE = Path.home() / ".github_token"
_REFRESH_INTERVAL_S = 45 * 60  # refresh every 45 minutes (installation tokens live ~60 min)
_GITHUB_API = "https://api.github.com"


def _build_app_jwt(app_id: str, private_key_pem: str) -> str:
    """Encode a 10-minute JWT signed with the GitHub App private key."""
    now = int(time.time())
    payload = {
        "iat": now - 60,      # GitHub allows 60s clock skew
        "exp": now + 9 * 60,  # 9 minutes — must be < 10
        "iss": app_id,
    }
    return pyjwt.encode(payload, private_key_pem, algorithm="RS256")


async def _fetch_installation_id(app_jwt: str) -> int:
    """Discover the app's installation id (assumes single-install apps)."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            f"{_GITHUB_API}/app/installations",
            headers={
                "Authorization": f"Bearer {app_jwt}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        resp.raise_for_status()
        installations = resp.json()
        if not installations:
            raise RuntimeError("GitHub App has no installations")
        # If the app is installed in multiple orgs, prefer protoLabsAI.
        for inst in installations:
            acct = inst.get("account", {}).get("login", "")
            if acct.lower() == "protolabsai":
                return inst["id"]
        return installations[0]["id"]


async def _mint_installation_token(app_id: str, private_key: str) -> tuple[str, int]:
    """Return (token, expires_at_unix). Raises on failure."""
    app_jwt = _build_app_jwt(app_id, private_key)
    installation_id = await _fetch_installation_id(app_jwt)
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            f"{_GITHUB_API}/app/installations/{installation_id}/access_tokens",
            headers={
                "Authorization": f"Bearer {app_jwt}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        resp.raise_for_status()
        data = resp.json()
        # GitHub returns expires_at as ISO-8601; treat as advisory, we refresh on a
        # fixed interval regardless.
        return data["token"], int(time.time()) + 3600


def _write_token(token: str) -> None:
    """Atomic write to ~/.github_token with mode 600."""
    tmp = _TOKEN_FILE.with_suffix(".tmp")
    tmp.write_text(token)
    try:
        tmp.chmod(0o600)
    except PermissionError:
        pass
    tmp.replace(_TOKEN_FILE)


def read_cached_token() -> str | None:
    """Synchronous read for gh_cli.py — cheap, no I/O loop needed."""
    try:
        return _TOKEN_FILE.read_text().strip() or None
    except FileNotFoundError:
        return None


async def refresh_forever(app_id: str, private_key: str) -> None:
    """Background task — mints a token and refreshes every 45 min forever."""
    backoff = 10
    while True:
        try:
            token, _ = await _mint_installation_token(app_id, private_key)
            _write_token(token)
            _log("refreshed installation token (45m cadence)")
            backoff = 10
            await asyncio.sleep(_REFRESH_INTERVAL_S)
        except Exception as e:
            _log(f"refresh failed: {e} — retry in {backoff}s")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 300)


async def mint_once_and_exit(app_id: str, private_key: str) -> None:
    """Mint a single token, write it, exit. Used by entrypoint.sh at startup
    before server.py boots, so the token file exists before any gh call."""
    token, _ = await _mint_installation_token(app_id, private_key)
    _write_token(token)
    _log(f"initial token written to {_TOKEN_FILE}")


if __name__ == "__main__":
    import sys

    app_id = os.environ.get("QUINN_APP_ID", "").strip()
    private_key = os.environ.get("QUINN_APP_PRIVATE_KEY", "").strip()
    if not app_id or not private_key:
        print("[github_app_auth] QUINN_APP_ID or QUINN_APP_PRIVATE_KEY not set — skipping", file=sys.stderr)
        sys.exit(0)

    mode = sys.argv[1] if len(sys.argv) > 1 else "once"
    if mode == "daemon":
        asyncio.run(refresh_forever(app_id, private_key))
    else:
        asyncio.run(mint_once_and_exit(app_id, private_key))
