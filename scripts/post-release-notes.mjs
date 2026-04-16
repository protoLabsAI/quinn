#!/usr/bin/env node
/**
 * post-release-notes.mjs — Rewrite commits with Claude and post to Discord.
 *
 * Usage:
 *   node scripts/post-release-notes.mjs [--from <ref>] [--to <ref>] [--title <string>]
 *
 * Env vars:
 *   ANTHROPIC_API_KEY        — required for Claude Haiku rewrite; without it,
 *                              commits post as raw bullets
 *   DISCORD_WEBHOOK_URL      — Discord channel webhook URL; without it, notes
 *                              print to stdout for preview
 */

import { execSync } from "node:child_process";
import { parseArgs } from "node:util";

const { values } = parseArgs({
  options: {
    from:  { type: "string" },
    to:    { type: "string", default: "HEAD" },
    title: { type: "string" },
  },
});

const to = values.to || "HEAD";

// ── Ref sanitization (shell injection guard) ──────────────────────────────────

const SAFE_REF = /^[a-zA-Z0-9._\-/]+$/;

function safeRef(ref, name) {
  if (!SAFE_REF.test(ref)) throw new Error(`Unsafe ${name} ref: ${JSON.stringify(ref)}`);
  return ref;
}

// ── Commit range ──────────────────────────────────────────────────────────────

let from = values.from;
if (!from) {
  try {
    from = execSync("git describe --tags --abbrev=0 HEAD^", { encoding: "utf8" }).trim();
    console.log(`Auto-detected range: ${from}..${to}`);
  } catch {
    from = execSync("git rev-list --max-count=30 HEAD | tail -1", { encoding: "utf8" }).trim();
    console.log("No previous tag found — using last 30 commits");
  }
}

const rawLog = execSync(
  `git log ${safeRef(from, "from")}..${safeRef(to, "to")} --pretty=format:"%s" --no-merges`,
  { encoding: "utf8" },
).trim();

const NOISE = /^(chore: release|Merge |promote:|docs: session handoff|Co-Authored)/i;

const commits = rawLog
  .split("\n")
  .map(l => l.trim())
  .filter(l => l.length > 0 && !NOISE.test(l));

if (commits.length === 0) {
  console.log("No notable commits in range — nothing to post.");
  process.exit(0);
}

console.log(`${commits.length} commits to summarise.`);

// ── Version / title ───────────────────────────────────────────────────────────

let version = values.title;
if (!version) {
  try {
    version = execSync("git describe --tags", { encoding: "utf8" }).trim();
  } catch {
    version = execSync("git rev-parse --short HEAD", { encoding: "utf8" }).trim();
  }
}

// ── Claude rewrite ────────────────────────────────────────────────────────────

const apiKey = process.env.ANTHROPIC_API_KEY;

let notes;
if (!apiKey) {
  console.warn("ANTHROPIC_API_KEY not set — posting raw commits without rewrite.");
  notes = commits.map(c => `• ${c}`).join("\n");
} else {
  const SYSTEM_PROMPT = `\
You are writing release notes for Quinn — the protoLabs QA engineer + release manager agent. \
She audits boards, reviews PRs, triages bugs from Discord and GitHub, and exposes an A2A \
(Agent-to-Agent) API so other agents can dispatch her.

Given raw git commit subjects, rewrite them as polished release notes.

Rules:
- Group into 2–4 themed sections relevant to: A2A / Agent Protocol, QA & Review Tooling, \
  Observability & Tracing, Bug Fixes
- Each item is one sentence, present tense, outcome-focused (what it enables, not what changed)
- Skip purely internal housekeeping (fixture edits, comment typos, test data only)
- Use • for bullets. Use **Section Title** for headers. No emojis.
- Max 280 words. Plain markdown only — no code blocks, no headers with ##.`;

  const resp = await fetch("https://api.anthropic.com/v1/messages", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "x-api-key": apiKey,
      "anthropic-version": "2023-06-01",
    },
    body: JSON.stringify({
      model: "claude-haiku-4-5-20251001",
      max_tokens: 700,
      system: SYSTEM_PROMPT,
      messages: [{ role: "user", content: commits.join("\n") }],
    }),
  });

  if (!resp.ok) {
    console.error(`Claude API error: ${resp.status}`, await resp.text());
    process.exit(1);
  }

  const data = await resp.json();
  notes = data.content?.[0]?.text ?? commits.map(c => `• ${c}`).join("\n");
}

if (notes.length > 3900) notes = notes.slice(0, 3897) + "…";

// ── Discord post ──────────────────────────────────────────────────────────────

const webhookUrl = process.env.DISCORD_WEBHOOK_URL;
if (!webhookUrl) {
  console.log("DISCORD_WEBHOOK_URL not set — release notes preview:\n\n" + notes);
  process.exit(0);
}

const embed = {
  title: `Quinn ${version}`,
  description: notes,
  color: 0x7c3aed,  // protoLabs purple — QA vibes
  timestamp: new Date().toISOString(),
  footer: { text: "Quinn — protoLabs QA agent" },
};

const discordResp = await fetch(webhookUrl, {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ embeds: [embed] }),
});

if (!discordResp.ok) {
  console.error(`Discord post failed (${discordResp.status}): ${await discordResp.text()}`);
  process.exit(1);
}

console.log(`Posted release notes for ${version} to Discord.`);
