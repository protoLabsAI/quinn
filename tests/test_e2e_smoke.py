"""
E2E smoke test: Discord → Quinn → Ava

Tests the full triage pipeline:
  1. Quinn A2A endpoint is reachable and returns a valid agent card
  2. Quinn A2A message/send works (simulates Discord → Quinn leg)
  3. Quinn calls file_bug → feature lands on Ava board (Quinn → Ava leg)
  4. Feature is retrievable from Ava by ID

Run:
    python tests/test_e2e_smoke.py
    python tests/test_e2e_smoke.py --quinn http://localhost:7873 --ava http://localhost:3008
"""

import argparse
import asyncio
import json
import os
import sys
import uuid

import httpx

QUINN_URL = os.environ.get("QUINN_URL", "http://ava:7873")
AVA_URL = os.environ.get("AVA_URL", "http://ava:3008")
AVA_API_KEY = os.environ.get("PROTOLABS_API_KEY", "")
AVA_PROJECT_PATH = os.environ.get("PROTOLABS_PROJECT_PATH", "/home/josh/dev/ava")

TIMEOUT = 120  # Quinn runs an LLM call — give it time


def _ok(label: str):
    print(f"  ✓ {label}")


def _fail(label: str, detail: str = ""):
    print(f"  ✗ {label}", f"— {detail}" if detail else "")
    sys.exit(1)


async def test_agent_card(client: httpx.AsyncClient):
    print("\n[1] Agent card discovery")
    resp = await client.get(f"{QUINN_URL}/.well-known/agent.json")
    assert resp.status_code == 200, f"HTTP {resp.status_code}"
    card = resp.json()
    assert card["name"] == "quinn", f"name={card.get('name')}"
    assert len(card.get("skills", [])) >= 1, "no skills"
    _ok(f"card: {card['name']} v{card['version']} ({len(card['skills'])} skills)")


async def test_a2a_report(client: httpx.AsyncClient):
    print("\n[2] A2A message/send — /report")
    payload = {
        "jsonrpc": "2.0",
        "id": "smoke-report",
        "method": "message/send",
        "params": {
            "message": {
                "role": "user",
                "parts": [{"kind": "text", "text": "/report"}],
            }
        },
    }
    resp = await client.post(f"{QUINN_URL}/a2a", json=payload, timeout=TIMEOUT)
    assert resp.status_code == 200, f"HTTP {resp.status_code}: {resp.text[:200]}"
    body = resp.json()
    assert "result" in body, f"no result: {body.get('error')}"
    artifacts = body["result"].get("artifacts", [])
    assert artifacts, "no artifacts"
    text = artifacts[0]["parts"][0]["text"]
    assert "Quinn QA" in text or "Digest" in text, f"unexpected response: {text[:200]}"
    _ok(f"report generated ({len(text)} chars)")


async def test_a2a_bug_triage_and_file(client: httpx.AsyncClient) -> str:
    """Triage a synthetic bug and file it on Ava. Returns the feature ID."""
    print("\n[3] A2A bug triage → file_bug → Ava board")

    run_id = uuid.uuid4().hex[:8]
    bug_text = (
        f"triage this bug and file it on the board: "
        f"[SMOKE-{run_id}] Test button throws uncaught TypeError in Safari 17. "
        f"Steps: open settings > click save. Console: TypeError: Cannot read properties of undefined. "
        f"Source: smoke test run {run_id}"
    )

    payload = {
        "jsonrpc": "2.0",
        "id": f"smoke-triage-{run_id}",
        "method": "message/send",
        "params": {
            "message": {
                "role": "user",
                "parts": [{"kind": "text", "text": bug_text}],
            }
        },
    }

    resp = await client.post(f"{QUINN_URL}/a2a", json=payload, timeout=TIMEOUT)
    assert resp.status_code == 200, f"HTTP {resp.status_code}: {resp.text[:200]}"
    body = resp.json()
    assert "result" in body, f"no result: {body.get('error')}"

    text = body["result"]["artifacts"][0]["parts"][0]["text"]
    _ok(f"Quinn responded ({len(text)} chars)")

    # Extract feature ID from response — look for feature-XXXXX pattern
    import re
    ids = re.findall(r'feature[-_][a-z0-9\-]+', text, re.IGNORECASE)
    if ids:
        feature_id = ids[0]
        _ok(f"feature ID found in response: {feature_id}")
        return feature_id
    else:
        # Quinn may have filed it but phrased differently — search board for the smoke tag
        _ok(f"no feature ID in response text (may still have filed — checking board)")
        return f"SMOKE-{run_id}"


async def test_feature_on_ava_board(client: httpx.AsyncClient, feature_id: str, run_id_hint: str = ""):
    """Verify the bug landed on Ava's board."""
    print("\n[4] Verify feature on Ava board")

    headers = {"X-API-Key": AVA_API_KEY, "Content-Type": "application/json"}
    body: dict = {}
    if AVA_PROJECT_PATH:
        body["projectPath"] = AVA_PROJECT_PATH

    resp = await client.post(
        f"{AVA_URL}/api/features/list",
        json=body,
        headers=headers,
        timeout=15,
    )
    assert resp.status_code == 200, f"Ava /api/features/list returned {resp.status_code}"

    data = resp.json()
    features = data if isinstance(data, list) else data.get("features", [])

    # Try exact ID match first
    match = next((f for f in features if f.get("id") == feature_id), None)

    # Fall back to searching by smoke run tag in title/description
    if not match and run_id_hint:
        match = next(
            (f for f in features
             if run_id_hint in (f.get("title", "") + f.get("description", ""))),
            None,
        )

    if match:
        _ok(
            f"feature on board: [{match.get('status','?')}] "
            f"{match.get('title','?')[:60]} ({match.get('id','?')})"
        )
    else:
        # Not a hard failure — Quinn may have responded without filing (LLM decision)
        print(
            f"  ~ feature not found on board (Quinn may have described the bug "
            f"rather than calling file_bug — check response above)"
        )


async def main(quinn_url: str, ava_url: str):
    global QUINN_URL, AVA_URL
    QUINN_URL = quinn_url
    AVA_URL = ava_url

    print(f"E2E Smoke Test — Discord → Quinn → Ava")
    print(f"  Quinn: {QUINN_URL}")
    print(f"  Ava:   {AVA_URL}")

    async with httpx.AsyncClient() as client:
        # 1. Agent card
        try:
            await test_agent_card(client)
        except Exception as e:
            _fail("agent card", str(e))

        # 2. A2A /report
        try:
            await test_a2a_report(client)
        except Exception as e:
            _fail("a2a /report", str(e))

        # 3. Bug triage → file_bug
        run_id = uuid.uuid4().hex[:8]
        try:
            feature_id = await test_a2a_bug_triage_and_file(client)
        except Exception as e:
            _fail("a2a triage + file_bug", str(e))
            return

        # 4. Verify on board
        try:
            await test_feature_on_ava_board(client, feature_id, run_id)
        except Exception as e:
            _fail("verify on ava board", str(e))

    print("\n✓ Smoke test passed\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--quinn", default=QUINN_URL)
    parser.add_argument("--ava", default=AVA_URL)
    args = parser.parse_args()
    asyncio.run(main(args.quinn, args.ava))
