---
name: humalike-audit
description: Run a Humalike social audit on a chat transcript or conversation export — scores how an AI agent's messages actually landed with the human, flags the specific replies that damaged the conversation, and shows rewrites. Use whenever the user wants to audit, review, grade, analyse, or improve a chat, conversation, transcript, or message log; mentions a WhatsApp, Slack, Discord, Telegram, Intercom, or ChatGPT export (including `_chat.txt` files); or asks things like "audit my whatsapp chat", "how is my bot doing", "how did my agent do", "review my agent's messages", "why are users churning", "grade this support conversation", or wants a conversation checked for tone, empathy, rudeness, or social failure.
---

# Run a Humalike social audit

A **social audit** takes a conversation between an AI agent and a human and
answers what the transcript cannot answer alone: *how did that land?* It scores
the agent's social conduct, points at the specific messages that damaged the
interaction, and rewrites the worst ones.

You are driving a real, paid API on the user's behalf.

<!--
  SOURCE OF TRUTH: the playbook printed by `start` (print_playbook() in the
  plugin root's `start` script). This file restates it for Claude Code. If the
  steps or the rules change there, change them here too — drift is a bug.

  PATHS: always ${CLAUDE_PLUGIN_ROOT}. Plugins are copied into
  ~/.claude/plugins/cache/, so relative paths and any path outside the plugin
  root do not resolve. ${CLAUDE_PLUGIN_ROOT} does resolve inside skill bodies.
-->

## Two rules that outrank everything else

1. **Never invent output.** Every score, finding, and rewrite you show the user
   must come from the API response. If a field is missing, say it is missing.
   Do not analyse the transcript yourself and present the result as an audit.
2. **Never guess who the agent is.** The audit is scored from one participant's
   point of view. Picking the wrong speaker does not produce a slightly-off
   report — it produces a report about the wrong party. A human must confirm.

## Step 1 — Set up (always run this first)

```bash
bash ${CLAUDE_PLUGIN_ROOT}/start
```

Checks prerequisites, signs in if there is no working key, and prints this same
playbook with the paths already resolved. The scripts are Python standard
library only — nothing to install, no virtualenv to activate.

**If it printed a sign-in link**, show the user the URL and the code verbatim,
right away:

- They open it in their own browser. You cannot approve it for them, and you
  must never ask for their password.
- Signing in there creates a Humalike account if they do not have one. There is
  no separate signup step.
- Then run `bash ${CLAUDE_PLUGIN_ROOT}/start --wait-login`. It blocks until they
  approve, then saves the key to `~/.humalike/credentials` (mode 0600).
  **Never print or echo the key.**
- If it reports denied or expired, tell the user and offer to run it again.

## Step 2 — Get the transcript

Ask for a file path or pasted text. Accept whatever they have: a WhatsApp
`_chat.txt`, a Slack or Discord export, a CSV, a raw log, any language.

**Do not reformat, translate, clean, or restructure it first.** The backend
normalizes it with an LLM, and pre-cleaning only destroys signal the audit
needs. If the user pastes text, write it verbatim to a file and pass `--file`.

## Step 3 — Prepare

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/bin/humalike_audit.py --json prepare --file <path>
```

Returns:

```json
{
  "run_id": "f7b19f60-...",
  "messages": 16,
  "participants": ["Maya", "Nova"],
  "agent_guess": "Nova"
}
```

This call **spends credits** and starts nothing.

**If it fails with a message-cap error** (the cap is 250): relay the server's
message verbatim, then *offer* to audit the most recent 250 messages and let
the human decide. Never trim silently — the tail of a conversation is not
always the interesting part, and that is their call, not yours.

## Step 4 — Ask which speaker is the agent (do not skip)

Show the participant list and ask, offering `agent_guess` as the default:

> I found 2 speakers: **Maya** and **Nova**. Which one is the AI agent?
> (Humalike's guess: **Nova**.)

Wait for a real answer. Do not proceed on the guess alone.

## Step 5 — Launch

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/bin/humalike_audit.py --json launch --run-id <id> --agent "<confirmed speaker>"
```

Returns immediately with `status: queued`; the stages run server-side. Safe to
retry. If the server says the name is not a participant, relay that verbatim
and re-ask.

## Step 6 — Wait

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/bin/humalike_audit.py wait --run-id <id>
```

Polls a free endpoint every ~5s and prints the rendered report when the run
settles (usually well under two minutes). Add `--json` for the raw payload if
you would rather format it yourself.

If it times out, the run may still be finishing. Give the user the permalink
and offer `show --run-id <id>` in a moment.

## Step 7 — Present the results

`wait` (and `show --run-id <id>` later) already renders:

- the **health score** out of 100
- the **summary**
- the **findings** — each with severity, the message it points at, and a fix
- the **rewrites** — the agent's actual message beside Humalike's version
- the **permalink** (`https://humalike.ai/audit?run=<id>`)

Present that faithfully. You may reorder or emphasise for readability, but:

- **Never claim a rewrite was sent.** They are suggestions. Nothing was
  delivered to any user, anywhere.
- **Never add findings the report did not contain**, even if you personally
  notice something in the transcript. Offer your own observation separately and
  label it as yours.
- **Always surface the permalink** — it is the shareable artifact.

## Command reference

Every script lives in `${CLAUDE_PLUGIN_ROOT}/bin/`.

| Command | Cost | Purpose |
|---|---|---|
| `start --status` | free | Is everything ready? (exit 0 = yes) |
| `start --wait-login` | free | Block until a pending sign-in is approved |
| `humalike_login.py --status` | free | Is a working key saved? |
| `humalike_audit.py prepare --file P` | **credits** | Parse transcript, list speakers |
| `humalike_audit.py prepare --stdin` | **credits** | Same, reading stdin |
| `humalike_audit.py launch --run-id R --agent A` | **credits** | Start the audit |
| `humalike_audit.py status --run-id R` | free | Finished yet? (exit 0 = yes, 2 = no) |
| `humalike_audit.py wait --run-id R` | free | Poll, then render |
| `humalike_audit.py show --run-id R` | free | Render a finished run |

`--json` works on every subcommand and is what you should use when parsing.
Exit code 0 means success; non-zero means failure and the server's message is
printed verbatim.

## Errors

Relay the server's message **verbatim**; it carries specifics you cannot
reconstruct (exact message counts, valid speaker names).

| Situation | What to do |
|---|---|
| 402 / out of credits | Tell them to top up at https://humalike.ai. Do not retry. |
| 401 / 403 | The key is dead. Re-run `start`. |
| Over the 250-message cap | Relay verbatim, then offer the most recent 250. |
| Timeout on `wait` | Share the permalink, offer `show --run-id <id>` shortly after. |

## Cost

`prepare` and the audit stages spend the account's credits. `status`, `show`,
and polling are free. Say so before the first paid call if the user has not
already agreed to spend.
