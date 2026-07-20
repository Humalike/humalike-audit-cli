---
name: audit-transcript
description: Run a Humalike social audit on a chat transcript — score how an AI agent's messages landed with the human, flag the specific replies that damaged the conversation, and show rewrites. Use when the user wants to audit, review, grade, or improve an agent/chatbot/support conversation, or asks "how did my agent do", "why are users churning", or wants a transcript analysed for tone, empathy, or social failure.
---

# Run a Humalike social audit

You are driving a real, paid API on the user's behalf. Two rules outrank
everything else below:

1. **Never invent output.** Every score, finding, and rewrite you show must come
   from the API response. If a field is missing, say it is missing. Do not
   summarise a transcript yourself and present it as an audit.
2. **Never guess who the agent is.** The audit is scored from one participant's
   point of view. Picking the wrong speaker does not produce a slightly-off
   report — it produces a report about the wrong party. A human must confirm.

> This workflow is kept in sync with `AGENTS.md` at the repo root, which is the
> same procedure written for agents without a skill system. If you change one,
> change the other.

## Step 1 — Check for a working key

```bash
python3 bin/humalike_login.py --status --json
```

Exit code 0 means a working key is saved; keep going. Non-zero means you need
Step 2.

## Step 2 — Log in (only if Step 1 failed)

```bash
python3 bin/humalike_login.py
```

This prints an approval URL and a short code, then blocks while polling.

- **Show the user the URL and the code verbatim.** They must open it themselves.
  You cannot approve this, and you must not ask for their password.
- Signing in on that page creates a Humalike account if they do not have one —
  there is no separate signup step to send them to.
- The command exits 0 once approved and saves the key to
  `~/.humalike/credentials` (mode 0600). Never print the key.

## Step 3 — Get the transcript

Ask for either a file path or pasted text. Accept whatever they have: WhatsApp
`_chat.txt`, a Slack export, a CSV, a log, a screenshot's text, any language.
The backend normalizes it with an LLM, so **do not reformat, translate, clean,
or restructure it first** — you would only lose signal.

If they paste text, write it to a file and use `--file`.

## Step 4 — Prepare

```bash
python3 bin/humalike_audit.py --json prepare --file <path>
```

Returns `run_id`, `messages`, `participants`, and `agent_guess`. This call
spends credits and starts nothing.

**If it fails with a message-cap error:** relay the server's message verbatim,
then *offer* to audit the most recent 250 messages and let the human decide.
Never trim silently — the tail of a conversation is not always the interesting
part, and that is their call, not yours.

## Step 5 — Ask which speaker is the agent (do not skip)

Show the participant list and ask. You may offer `agent_guess` as the default:

> I found 2 speakers: **Maya** and **Nova**. Which one is the AI agent?
> (Humalike's guess: **Nova**.)

Wait for an answer. Do not proceed on the guess alone.

## Step 6 — Launch

```bash
python3 bin/humalike_audit.py --json launch --run-id <id> --agent "<confirmed speaker>"
```

Returns immediately; the stages run server-side. Safe to retry.

## Step 7 — Wait

```bash
python3 bin/humalike_audit.py wait --run-id <id>
```

Polls the free projection endpoint every ~5s and prints the rendered report
when the run settles. Add `--json` if you want the raw payload to reformat
yourself. A full run usually takes under a minute or two.

## Step 8 — Present the results

`wait` (and `show --run-id <id>` later) already renders:

- the **health score** out of 100
- the **summary**
- the **findings** — each with severity, the message it points at, and a fix
- the **rewrites** — the agent's actual message beside what Humalike would have
  said instead
- the **permalink** (`https://humalike.ai/audit?run=<id>`)

Present that faithfully. You may reorder or emphasise for readability, but:

- **Never claim a rewrite was sent.** They are suggestions. Nothing was
  delivered to any user, anywhere.
- **Never add findings the report did not contain**, even if you personally
  notice something in the transcript. Offer your own observation separately and
  label it as yours.
- Always surface the permalink — it is the shareable artifact.

## Errors

Relay the server's message **verbatim**; it carries specifics you cannot
reconstruct (exact message counts, valid speaker names).

| Situation | What to do |
|---|---|
| 402 / out of credits | Tell them to top up at https://humalike.ai. Do not retry. |
| 401 / 403 | The key is dead. Go back to Step 2. |
| Over the message cap | Relay the message, then offer the most recent 250. |
| Timeout on `wait` | The run may still be going. Give them the permalink and offer `show --run-id <id>`. |

## Cost

`prepare` and the audit stages spend the account's credits. `status`, `show`,
and polling are free. Say so before the first paid call if the user has not
already agreed to spend.
