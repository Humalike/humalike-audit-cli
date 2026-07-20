# AGENTS.md — run a Humalike social audit

You are an AI agent with a shell and a filesystem. This file tells you how to
run a Humalike social audit on a chat transcript, end to end, for the human you
are working with.

A **social audit** takes a conversation between an AI agent and a human and
answers what the transcript cannot answer alone: *how did that land?* It scores
the agent's social conduct, points at the specific messages that damaged the
interaction, and rewrites the worst ones.

<!--
  SOURCE OF TRUTH: the playbook printed by `start` (see print_playbook() in the
  repo root's `start` script). This file and skills/humalike-audit/SKILL.md
  restate that same playbook for audiences that will not run `start` first. If
  you change the steps or the rules here, change them in `start` too — the
  script is what most agents actually read, and drift between them is a bug.
-->

## Setup — one command

```bash
bash ~/.humalike/audit-cli/start
```

Run this first, always. It updates the checkout, checks prerequisites, starts a
sign-in if there is no working key, and prints this same playbook with the paths
already resolved. **If you ran it, follow its output and ignore the rest of this
file** — it is the same procedure, generated fresh.

If the checkout is not there yet:

```bash
git clone https://github.com/Humalike/humalike-audit-cli ~/.humalike/audit-cli 2>/dev/null; bash ~/.humalike/audit-cli/start
```

Requirements: `git` and Python 3.9+. The scripts are standard library only —
there is nothing to install and no virtualenv to activate.

## Two rules that outrank everything else

1. **Never invent output.** Every score, finding, and rewrite you show the user
   must come from the API response. If a field is missing, say so. Do not
   analyse the transcript yourself and present the result as an audit.
2. **Never guess who the agent is.** The audit is scored from one participant's
   point of view. The wrong speaker does not give a slightly-off report — it
   gives a report about the wrong party. A human confirms this, always.

---

## Step 1 — Make sure you have a key

`start` handles this. If it printed a sign-in link:

- **Show the user the URL and code exactly as printed**, immediately. They open
  it in their own browser. You cannot approve it for them, and you must never
  ask for their password.
- Signing in there creates a Humalike account if they do not have one. There is
  no separate signup step.
- Then, in the SAME turn, run `bash ~/.humalike/audit-cli/start --wait-login`.
  Never ask the human to report back that they approved, and never end your
  turn waiting for them to say so: this command returns by itself the moment
  they approve, and writes the key to `~/.humalike/credentials` (mode 0600).
  **Never print or echo the key.**
- If it reports denied or expired, tell the user and offer to run it again.

To check at any time: `bash ~/.humalike/audit-cli/start --status` (exit 0 =
ready).

## Step 2 — Get the transcript

Ask for a file path or pasted text. Anything is fine: WhatsApp `_chat.txt`, a
Slack export, a CSV, a raw log, any language. The backend normalizes it with an
LLM.

**Do not reformat, translate, clean, or restructure it first.** You would only
destroy signal the audit needs. If the user pastes text, write it verbatim to a
file and pass `--file`.

## Step 3 — Prepare

```bash
python3 ~/.humalike/audit-cli/bin/humalike_audit.py --json prepare --file <path>
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

## Step 4 — Ask which speaker is the agent

Do not skip this. Show the participants and ask, offering the guess as a
default:

> I found 2 speakers: **Maya** and **Nova**. Which one is the AI agent?
> (Humalike's guess: **Nova**.)

Wait for a real answer before continuing.

## Step 5 — Launch

```bash
python3 ~/.humalike/audit-cli/bin/humalike_audit.py --json launch --run-id <id> --agent "<confirmed speaker>"
```

Returns immediately with `status: queued`. The stages run server-side. Retrying
is safe. If the server says the name is not a participant, relay that verbatim
and re-ask.

## Step 6 — Wait

```bash
python3 ~/.humalike/audit-cli/bin/humalike_audit.py wait --run-id <id>
```

Polls a free endpoint every ~5s and prints the rendered report when the run
settles (usually well under two minutes). Add `--json` for the raw payload if
you would rather format it yourself.

If it times out, the run may still be finishing. Give the user the permalink and
offer `show --run-id <id>` in a moment.

## Step 7 — Present the results

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

Every script lives in `~/.humalike/audit-cli/bin/`.

| Command | Cost | Purpose |
|---|---|---|
| `start --status` | free | Is everything ready? |
| `start --wait-login` | free | Block until a pending sign-in is approved |
| `humalike_login.py --status` | free | Is a working key saved? |
| `humalike_login.py --begin` | free | Create a sign-in, print the link, exit |
| `humalike_login.py --resume` | free | Wait for that sign-in, save the key |
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
| 401 / 403 | Key is dead — re-run `start`. |
| Over the 250-message cap | Relay verbatim, then offer the most recent 250. |
| Timeout on `wait` | Share the permalink, offer `show` shortly after. |

Always relay the server's own wording. It knows the exact message count and the
valid speaker names; paraphrasing only loses that.

## Cost

`prepare` and the audit stages spend the account's credits. `status`, `show`,
and polling are free. Say so before the first paid call if the user has not
already agreed to spend.

## Environment

| Variable | Default | Purpose |
|---|---|---|
| `HUMALIKE_API_URL` | `https://api.humalike.com` | API base URL |
| `HUMALIKE_API_KEY` | — | Overrides the saved key for one run |
| `HUMALIKE_APP_URL` | `https://humalike.ai` | Origin used for permalinks |
| `HUMALIKE_CONFIG_DIR` | `~/.humalike` | Where the credentials file lives |
| `HUMALIKE_KEYS_URL` | follows `HUMALIKE_API_URL` | **Dev only.** Splits the device-auth calls onto a separate origin, for local stacks that run the services as separate containers. |
| `HUMALIKE_CLI_GATEWAY_KEY` | — | **Dev only.** Shared key fronting the device-auth lane; production's gateway injects it. |

Do not set the dev-only variables when talking to the hosted API.
