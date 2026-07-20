<p align="center">
  <a href="https://humalike.ai/"><img src="assets/full_logo_huma_w_bbg.jpg" alt="Humalike" width="50%"></a>
</p>

<p align="center">
  <a href="https://github.com/Humalike/humalike-audit-cli/blob/main/LICENSE"><img src="https://img.shields.io/github/license/Humalike/humalike-audit-cli" alt="License"></a>
  <a href="https://github.com/Humalike/humalike-audit-cli/stargazers"><img src="https://img.shields.io/github/stars/Humalike/humalike-audit-cli" alt="Stars"></a>
  <a href="https://github.com/Humalike/humalike-audit-cli/issues"><img src="https://img.shields.io/github/issues/Humalike/humalike-audit-cli" alt="Issues"></a>
  <a href="https://img.shields.io/badge/python-3.9%2B-blue"><img src="https://img.shields.io/badge/python-3.9%2B-blue" alt="Python 3.9+"></a>
  <a href="https://humalike.ai/"><img src="https://img.shields.io/badge/website-humalike.ai-1f6feb" alt="Website"></a>
</p>

# Humalike Audit CLI

**Find out how your AI agent's conversations actually landed.**

Run a [Humalike](https://humalike.ai) social audit on any chat transcript,
straight from your terminal agent — a health score, the exact messages that
damaged the conversation, and rewrites that would have landed better.

## Quickstart

Paste this into Claude Code, Codex, Cursor, or any agent with a shell:

```bash
git clone https://github.com/Humalike/humalike-audit-cli ~/.humalike/audit-cli 2>/dev/null; bash ~/.humalike/audit-cli/start
```

That is the whole setup. What happens:

1. Your agent runs it, and the output tells it everything it needs — no prompt
   to write, no docs for it to read.
2. It prints a sign-in link. You click it; approving also creates your
   Humalike account if you do not have one.
3. Your agent asks for your transcript — a file path or pasted text, in any
   format or language.
4. It asks which speaker is the AI agent, and confirms with you before
   spending anything.
5. It runs the audit and shows you the report.

Re-running the same command updates the checkout and skips the login you
already did. Requirements: `git` and **Python 3.9+** — nothing else, ever.

## What you get back

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

Plus rewrites of the worst messages, and a shareable permalink at
`https://humalike.ai/audit?run=<id>`.

## Install as a Claude Code plugin

If you would rather have a native skill than the paste above:

```
/plugin marketplace add Humalike/humalike-audit-cli
/plugin install humalike-audit@humalike
```

Then just ask: *"audit this support transcript"*. Same workflow and the same
scripts — the skill runs them from the installed plugin directory.

## Use the CLI directly

Everything the agent does, you can do by hand.

```bash
# 1. Sign in. Prints a URL + code for you to approve in a browser.
python3 ~/.humalike/audit-cli/bin/humalike_login.py

# 2. Parse the transcript and see who is in it.
python3 ~/.humalike/audit-cli/bin/humalike_audit.py prepare --file chat.txt
#   Parsed 16 messages.
#   Speakers: Maya, Nova
#   Best guess at the agent: Nova

# 3. Confirm which speaker is the AI agent, and launch.
python3 ~/.humalike/audit-cli/bin/humalike_audit.py launch --run-id <id> --agent "Nova"

# 4. Wait for it, then read the report.
python3 ~/.humalike/audit-cli/bin/humalike_audit.py wait --run-id <id>
```

| Subcommand | What it does |
|---|---|
| `prepare --file <path>` / `--text <raw>` | Normalize the transcript, store a parked run, list the speakers |
| `launch --run-id <id> --agent <name>` | Confirm the agent and start the audit (server drives it) |
| `status --run-id <id>` | One-shot progress check |
| `wait --run-id <id>` | Block until the run finishes, then print the report |
| `show --run-id <id>` | Print the results of a finished run |

Add `--json` to any subcommand for machine-readable output.

`humalike_login.py` also takes `--begin` (create the sign-in, print the link,
exit) and `--resume` (wait for that same sign-in to be approved). That split is
what lets an agent hand you the link immediately instead of blocking on it.

### `start` options

| Command | Effect |
|---|---|
| `bash ~/.humalike/audit-cli/start` | Update, sign in if needed, print the playbook |
| `bash ~/.humalike/audit-cli/start --status` | Print readiness; exit 0 if ready, 1 if not |
| `bash ~/.humalike/audit-cli/start --wait-login` | Block until a pending sign-in is approved |
| `bash ~/.humalike/audit-cli/start --help` | Usage |

## Transcripts

Give it whatever you have: a WhatsApp `_chat.txt`, a Slack or Discord export, a
CSV, a raw log, any language. The backend normalizes it with an LLM.

**Do not clean it up first.** Reformatting, translating, or trimming a
transcript only destroys the signal the audit reads.

Transcripts are capped at **250 messages** per run. Over the cap the CLI relays
the server's error and offers the most recent 250 — it never trims silently. If
you want a different 250, that is your call to make.

## Your API key

Saved to `~/.humalike/credentials`, mode 0600, and never printed, logged, or
included in an error message. `HUMALIKE_API_KEY` overrides it for a single run.

To check: `bash ~/.humalike/audit-cli/start --status`

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `HUMALIKE_API_URL` | `https://api.humalike.com` | API base URL |
| `HUMALIKE_API_KEY` | — | Overrides the saved key for one run |
| `HUMALIKE_APP_URL` | `https://humalike.ai` | Origin used for permalinks |
| `HUMALIKE_CONFIG_DIR` | `~/.humalike` | Where the credentials file lives |
| `HUMALIKE_KEYS_URL` | follows `HUMALIKE_API_URL` | **Dev only.** Splits the device-auth calls onto a separate origin, for local stacks that run the services as separate containers. |
| `HUMALIKE_CLI_GATEWAY_KEY` | — | **Dev only.** Shared key fronting the device-auth lane; production's gateway injects it. |

Do not set the dev-only variables when talking to the hosted API.

## Repo layout

```
start                          the one entry point; prints the agent playbook
bin/humalike_login.py          device-auth login, saves the key
bin/humalike_audit.py          prepare / launch / status / wait / show
bin/_hcommon.py                shared HTTP, credentials, error handling
skills/humalike-audit/         the Claude Code skill
AGENTS.md                      the same playbook, for agents without skills
tests/                         stdlib unittest, no network, no deps
```

The playbook `start` prints is the **source of truth** for the agent workflow.
`AGENTS.md` and the skill restate it for their own audiences and are kept in
sync with it deliberately.

## Tests

```bash
python3 -m unittest discover -s tests -t tests
```

No dependencies and no network: the HTTP layer is injectable and the tests pass
a fake.

## License

MIT. See [LICENSE](LICENSE).
