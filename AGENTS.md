# AGENTS.md — run a Humalike social audit

You are an AI agent with a shell and a filesystem. This file tells you how to
run a Humalike social audit on a chat transcript, end to end, for the human you
are working with.

A **social audit** takes a conversation between an AI agent and a human and
answers what the transcript cannot answer alone: *how did that land?* It scores
the agent's social conduct, points at the specific messages that damaged the
interaction, and rewrites the worst ones.

> This is the same workflow as `skills/audit-transcript/SKILL.md`, written for
> agents without a skill system. If you change one, change the other.

## Requirements

Python 3 (any 3.9+). Nothing to install — the scripts are standard library only.
Run every command from the root of this repository.

## Two rules that outrank everything else

1. **Never invent output.** Every score, finding, and rewrite you show the user
   must come from the API response. If a field is missing, say so. Do not
   analyse the transcript yourself and present the result as an audit.
2. **Never guess who the agent is.** The audit is scored from one participant's
   point of view. The wrong speaker does not give a slightly-off report — it
   gives a report about the wrong party. A human confirms this, always.

---

## Step 1 — Check for a working key

```bash
python3 bin/humalike_login.py --status --json
```

Exit 0 = a working key is saved, skip to Step 3. Non-zero = do Step 2.

## Step 2 — Log in

```bash
python3 bin/humalike_login.py
```

This prints an approval URL and a short user code, then blocks while it polls.

- **Show the user the URL and code exactly as printed.** They open it in their
  own browser. You cannot approve it for them, and you must never ask for their
  password.
- Signing in there creates a Humalike account if they do not have one. There is
  no separate signup step.
- On approval the command exits 0 and writes the key to
  `~/.humalike/credentials` (mode 0600). **Never print or echo the key.**
- If it reports denied or expired, tell the user and offer to run it again.

## Step 3 — Get the transcript

Ask for a file path or pasted text. Anything is fine: WhatsApp `_chat.txt`, a
Slack export, a CSV, a raw log, any language. The backend normalizes it with an
LLM.

**Do not reformat, translate, clean, or restructure it first.** You would only
destroy signal the audit needs. If the user pastes text, write it verbatim to a
file and pass `--file`.

## Step 4 — Prepare

```bash
python3 bin/humalike_audit.py --json prepare --file <path>
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

If it fails because the transcript is over the message cap (250), relay the
server's message verbatim, then **offer** to audit the most recent 250 messages
and let the human choose. Never trim silently.

## Step 5 — Ask which speaker is the agent

Do not skip this. Show the participants and ask, offering the guess as a
default:

> I found 2 speakers: **Maya** and **Nova**. Which one is the AI agent?
> (Humalike's guess: **Nova**.)

Wait for a real answer before continuing.

## Step 6 — Launch

```bash
python3 bin/humalike_audit.py --json launch --run-id <id> --agent "<confirmed speaker>"
```

Returns immediately with `status: queued`. The stages run server-side. Retrying
is safe. If the server says the name is not a participant, relay that verbatim
and re-ask.

## Step 7 — Wait

```bash
python3 bin/humalike_audit.py wait --run-id <id>
```

Polls a free endpoint every ~5s and prints the rendered report when the run
settles (usually well under two minutes). Add `--json` for the raw payload if
you would rather format it yourself.

If it times out, the run may still be finishing. Give the user the permalink and
offer `show --run-id <id>` in a moment.

## Step 8 — Present the results

`wait` and `show` already render the health score, the summary, the findings
(severity + the message each points at + a fix), the rewrites (the agent's real
message beside Humalike's version), and the permalink.

You may reformat for readability, but:

- **Never say a rewrite was sent.** They are suggestions. Nothing was delivered
  to anyone.
- **Never add findings the report did not contain.** If you notice something
  yourself, offer it separately and label it as your own observation.
- **Always give them the permalink** — `https://humalike.ai/audit?run=<id>` is
  the shareable artifact.

---

## Command reference

| Command | Cost | Purpose |
|---|---|---|
| `humalike_login.py --status` | free | Is a working key saved? |
| `humalike_login.py` | free | Device-auth login; saves the key |
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

| Situation | What to do |
|---|---|
| 402 / out of credits | Tell them to top up at https://humalike.ai. Do not retry. |
| 401 / 403 | Key is dead — go back to Step 2. |
| Over the 250-message cap | Relay verbatim, then offer the most recent 250. |
| Timeout on `wait` | Share the permalink, offer `show` shortly after. |

Always relay the server's own wording. It knows the exact message count and the
valid speaker names; paraphrasing only loses that.

## Environment

| Variable | Default | Purpose |
|---|---|---|
| `HUMALIKE_API_URL` | `https://api.humalike.com` | API base URL |
| `HUMALIKE_API_KEY` | — | Overrides the saved key for one run |
| `HUMALIKE_APP_URL` | `https://humalike.ai` | Origin used for permalinks |
| `HUMALIKE_KEYS_URL` | follows `HUMALIKE_API_URL` | **Dev only.** Splits the device-auth calls onto a separate origin, for local stacks that run the services as separate containers. |
| `HUMALIKE_CLI_GATEWAY_KEY` | — | **Dev only.** Shared key fronting the device-auth lane; production's gateway injects it. |

Do not set the dev-only variables when talking to the hosted API.
