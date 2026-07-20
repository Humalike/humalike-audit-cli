# Humalike Audit CLI

**Find out how your AI agent's conversations actually landed.**

Paste a transcript. Humalike replays it, scores the agent's social conduct,
points at the exact messages that damaged the interaction, and rewrites the
worst ones.

```
========================================================================
  HUMALIKE SOCIAL AUDIT — agent: Nova
========================================================================

  Health score: 10/100

  Maya is at high risk of churning because the agent provides evasive,
  overly cheerful, and dismissive responses. The agent must stop
  ignoring direct questions and stop using enthusiastic tone-matching
  when a user is clearly upset.

------------------------------------------------------------------------
  FINDINGS (2)
------------------------------------------------------------------------

  1. [HIGH] Excessive and inappropriate emotional mirroring

     said: "I completely understand your frustration! Shipping delays
     can definitely be frustrating! Our carrier partners are working
     hard to get your package to you as quickly as possible!"

     fix: When a user expresses frustration with a service failure,
     replace 'cheerleader' language with direct, factual acknowledgment
     and concrete action steps.
```

---

## Run it from your agent

Paste this into Claude Code, Codex, Cursor, or any agent with a shell:

```
Clone https://github.com/Humalike/humalike-audit-cli, read its AGENTS.md, and
follow it to run a Humalike social audit on my chat transcript. Handle the
login for me — just tell me what to click.
```

That is the whole setup. The agent clones the repo, walks you through signing
in (which creates your Humalike account if you do not have one), asks for your
transcript, confirms which speaker is the AI agent, and prints the report.

## Install as a Claude Code plugin

```
/plugin marketplace add Humalike/humalike-audit-cli
/plugin install humalike-audit@humalike
```

Then just ask: *"audit this support transcript"*. The `audit-transcript` skill
handles the rest.

## Use the CLI directly

Python 3.9+. Nothing to install — the scripts are **standard library only**, no
`pip install`, ever.

```bash
git clone https://github.com/Humalike/humalike-audit-cli
cd humalike-audit-cli

# 1. Sign in. Prints a URL + code for you to approve in a browser.
python3 bin/humalike_login.py

# 2. Parse the transcript and see who is in it.
python3 bin/humalike_audit.py prepare --file chat.txt
#   Parsed 16 messages.
#   Speakers: Maya, Nova
#   Best guess at the agent: Nova

# 3. Confirm which speaker is the AI agent, and launch.
python3 bin/humalike_audit.py launch --run-id <id> --agent "Nova"

# 4. Wait for it, then read the report.
python3 bin/humalike_audit.py wait --run-id <id>
```

Add `--json` to any subcommand for machine-readable output.

### Why you confirm the agent by hand

An audit is scored from one participant's point of view. Choosing the wrong
speaker does not give you a slightly-off report — it gives you a report about
the wrong party. The API offers a guess; a human confirms it. Nothing runs until
someone does.

### Transcript formats

Anything. WhatsApp `_chat.txt`, a Slack export, a CSV, a raw log, any language.
An LLM normalizes it server-side, so **do not clean it up first** — you would
only lose signal.

---

## What it costs

Audits spend your account's credits.

- `prepare` runs an LLM to normalize the transcript — **billed**.
- The audit stages after `launch` — **billed**.
- `status`, `show`, and all polling — **free**, no LLM.

Out of credits shows up as a clear error; top up at
[humalike.ai](https://humalike.ai).

## Limits

Transcripts are capped at **250 messages**. Over that, `prepare` refuses and
tells you the actual count. The tool will not silently trim your transcript —
if you want the most recent 250, that is your call to make.

## Where your key lives

`~/.humalike/credentials`, mode `0600` (owner read/write only). It is never
printed, logged, or included in an error message. `HUMALIKE_API_KEY` overrides
it for a single run.

To check: `python3 bin/humalike_login.py --status`

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `HUMALIKE_API_URL` | `https://api.humalike.com` | API base URL |
| `HUMALIKE_API_KEY` | — | Use this key instead of the saved one |
| `HUMALIKE_APP_URL` | `https://humalike.ai` | Origin used for report permalinks |
| `HUMALIKE_KEYS_URL` | follows `HUMALIKE_API_URL` | **Dev only.** Points the device-auth calls at a different origin, for local stacks that run services as separate containers. |
| `HUMALIKE_CLI_GATEWAY_KEY` | — | **Dev only.** The shared key fronting the device-auth lane. Production's gateway injects this for you; a local stack has no gateway, so you supply it. |

## Repo layout

```
bin/humalike_login.py          device-auth login, saves the key
bin/humalike_audit.py          prepare / launch / status / wait / show
bin/_hcommon.py                shared HTTP, credentials, error handling
skills/audit-transcript/       the Claude Code skill
AGENTS.md                      the same workflow, for any agent
tests/                         stdlib unittest, no network, no deps
```

`AGENTS.md` and `skills/audit-transcript/SKILL.md` describe the same workflow
for different audiences and are kept in sync deliberately.

## Tests

```bash
python3 -m unittest discover -s tests -t tests
```

No dependencies and no network: the HTTP layer is injectable and the tests pass
a fake.

## License

MIT — see [LICENSE](LICENSE).
