# Wright

A local, single-window AI app for your Mac. You describe a goal (typed, pasted
screenshot, dropped config file or error log). Wright does the work, checks it,
and reports what it did and how it kept things safe. It doesn't hand you a
plan.

Every change happens first in a disposable copy. Your real files or devices
are touched only after deterministic checks pass, and every promotion can be
undone with one command.

## Install (one command)

```sh
git clone https://github.com/19chrismo/wright ~/wright-app && ~/wright-app/install.sh
```

The installer:

- finds or installs Python 3.12 (Homebrew or uv);
- creates `~/.wright/venv`;
- installs Wright and its tool servers;
- links `wright` into `~/.local/bin`;
- adds **Wright.app** to `~/Applications`, which you can drag to the Dock;
- runs a self-test.

## Run (one command)

```sh
wright
```

This opens the Wright window. Running it again, or clicking Wright.app, brings
back the same window; it never starts a second copy.

## Everything else happens in the window

After install you never need Terminal or a settings screen. The window opens
on a setup card that shows what's ready and what's missing, with one button
for each missing piece. You can also type these in plain words; they work even
before any AI model is set up:

| Say | What happens |
|---|---|
| "check setup" | Shows what's ready and what's missing. |
| "add my Gemini key" · "add Sam's Gemini key" | A key box appears, with name and optional daily share. Paste the key into it. |
| "show keys" · "remove Sam's key" | Shows whose keys are in use and today's use against each daily share · removes one. |
| "install the free local model" | Wright installs Ollama itself and downloads a model sized for this Mac. |
| "keep it private" · "use the cloud again" | Local-only mode on · off. |
| "undo that" · "undo" again | Rolls back the last change Wright made · puts it back. |
| "connect home assistant at http://…/api/mcp" | Attaches a device or service; a box asks for its access token. |

- **Keys:** go straight from the key box to the macOS Keychain. They never
  appear in the chat, the logs or anything sent to an AI model.
- **A request sent before setup:** waits, and starts by itself once a model is
  ready.
- **Anything else you type:** a goal, which Wright carries out.

### Models

- **How requests are routed:** Wright talks to one local endpoint, a pooling
  proxy on `127.0.0.1`. It sends each request to whichever key is healthy.
- **When a key hits its limit:** that key rests and the next one takes over.
- **When every online key is used up:** the free local model takes over. You
  never see a usage-limit error.
- **Gemini limits:** these are per Google Cloud project. Don't create extra
  projects just to multiply free quota (see BRIEF.md, quota note).
- **Household keys:** each person makes a key on their own Google account and
  pastes it into the key box under their name, with a daily share if they
  want one. Wright never uses more than a person's share, and each report says
  whose key did the work. Whether Google accepts pooled household keys is
  Google's call, and any consequence falls on the account that owns each key,
  so each person agrees for their own key.

## What happens when you send a goal

The procedural lock walks every task through the same states:

```
intent → knowledge → baseline → execute ⇄ verify → promote → report → done
```

1. **intent:** what "done" means, as machine-checkable criteria, plus the files
   and hosts in scope.
2. **knowledge:** sources captured with a hash and a verbatim quote, versions
   read from the system itself, and every decision tagged **[SOURCED]**,
   **[INFERRED]** or **[UNKNOWN]**.
3. **baseline:** the known-good state is frozen. Any change that makes any
   dimension worse is rejected, even if the average improves.
4. **execute:** small steps in the sandbox, at most 5 changes between
   verification gates. Before a risky step, a pre-mortem whose recovery
   Wright actually rehearses.
5. **verify:** stop hooks (criteria, baseline, scope, target validator). "Done"
   is refused while any hook is red.
6. **promote:** rehearse the rollback, snapshot the real target and verify the
   snapshot by restoring it, apply, probe. Any red result rolls back
   automatically. Live devices use commit-confirmed promotion with an
   auto-revert timer.
7. **report:** in past tense. It covers grounding, dangers and their
   mitigations, regression status, out-of-scope changes (target 0),
   calibration, integrity flags, and the undo command.

The only prompts you ever see are these four, because they can't be undone:

- spending money;
- irreversible hardware writes (firmware, disk erase);
- destructive actions outside the sandbox with no tested restore;
- sending or publishing to other people.

## Pre-attached tools (MCP servers)

| Server | Tools |
|---|---|
| `shell` | `run`. Commands run in the sandbox under OS write protection (macOS `sandbox-exec`). |
| `fs` | `read`, `write`, `edit`, `list`, `search` |
| `web` | `fetch` (text, title and sha256 for provenance), `search` |
| `code` | `run` (Python or Node snippets in the sandbox) |
| `image` | `view` (hands an image to the model), `info` |
| `browser` | `open`, `text`, `click`, `fill`, `screenshot` (Playwright Chromium) |

### Add another MCP server

In the window, type "connect <name> at <url>", for example "connect home
assistant at http://homeassistant.local:8123/api/mcp". A box asks for its
access token, which goes to the Keychain, and the server's tools are available
straight away.

Servers that run as a local program are added from Terminal instead:

```sh
wright mcp add freecad -- python3 -m freecad_mcp                # stdio server
wright keys set ha_token                                        # store a token in the Keychain
wright mcp add homeassistant --url http://homeassistant.local:8123/api/mcp \
      --header "Authorization: Bearer keyref:ha_token"          # streamable HTTP server
wright mcp list
```

You can also edit `~/.wright/config.json` → `"mcp_servers"`. New servers load
the next time Wright starts, and their tools register automatically. The Home
Assistant endpoint path above is an assumption that couldn't be checked from
the build environment; confirm it in your Home Assistant MCP Server
integration.

### Live devices (routers and similar)

A task can target a live system through a command target. A command target is
defined by:

- `export`: dump the config into the sandbox;
- `validate`: dry-run the staged config;
- `apply`: push the staged config to the device;
- `revert`: restore a snapshot;
- `probes`: checks that run over a different path from the one being
  changed.

See `wright/targets.py` and `tests/test_step14_promotion.py` for a worked
example, including a rolled-back lockout.

## Terminal commands (optional)

Everything above works from the window. These commands do the same things for
scripting or remote use.

| Command | What it does |
|---|---|
| `wright` | Open the window. |
| `wright task "goal" [--target DIR]` | Run one goal headless and print the report. |
| `wright doctor` | Check Python, keys, lanes, Ollama, tools, the episodic chain and OS permissions. |
| `wright audit` | Verify the episodic log, then show recent history and the calibration table. |
| `wright rollback <snapshot>` · `wright snapshots` | Undo a promotion (one command) · list snapshots. |
| `wright self log` · `wright self rollback <rev>` | Self-modification history and one-command rollback. |
| `wright keys add\|list\|remove\|set` | Manage model keys and tokens (Keychain). |
| `wright mcp add\|list\|remove` | Attach or detach MCP servers. |
| `wright local-only on\|off` | Keep all model calls on this Mac. |
| `wright demo step1` … `step3` | Offline demonstrations of the harness. |

## Where things live (`~/.wright`)

| Path | Contents |
|---|---|
| `memory/episodic/` | Append-only, hash-chained task log, plus its mirror and anchor. The model can read it but no tool can write it. |
| `memory/semantic/` | Versioned knowledge packs, grouped by domain. |
| `memory/procedural/skills/` | Versioned skills, each with its own regression test. |
| `memory/calibration.json` | Measured accuracy per domain, tool and task type. |
| `self/` | git repository of the prompt, role contract, config and skills that the agent may change through the self-modification gate. |
| `hidden_eval/` | Hash-pinned held-out evaluation. Outside the model's data scope. |
| `tasks/<id>/` | Sandbox, checkpoints, snapshots, `state.json` (for resume), and the baseline manifest. |

## Formats you can audit

[`docs/FORMATS.md`](docs/FORMATS.md) describes each format. The JSON Schemas
are in [`schemas/`](schemas/), and every one is checked by the test suite:

- knowledge pack;
- regression baseline manifest;
- role contract;
- plan graph;
- episodic log entry;
- calibration table;
- skill definition with its regression test;
- the check format shared by criteria, dimensions and skill tests.

[`docs/BUILD-KNOWLEDGE.md`](docs/BUILD-KNOWLEDGE.md) records which sources
Wright's own design decisions rest on, and which could not be checked.

## What leaves your Mac

- **Cloud model lanes:** model requests go to the lanes you configured. The
  report lists which lanes served the task. On Google's free tier, Google may
  use what you send to improve its products, so use `local-only` for anything
  private.
- **Tools:** web fetch and search, the browser, and any MCP servers you attach
  reach the hosts they're pointed at.
- **Never sent:** keys never appear in logs, transcripts or model requests.
  They're redacted before anything is written.

## Tests

```sh
~/.wright/venv/bin/python -m pytest
```

There is one test file per build step (`tests/test_stepNN_*.py`) plus
`test_formats.py`. They prove the build-order guarantees with a deterministic
scripted model, real MCP servers, a real browser and a local stand-in for the
model providers.

## Status and limits

- **What has been tested:** everything above is built and tested on Linux in
  CI.
- **macOS-only parts:** the Keychain, `sandbox-exec` and `caffeinate` paths
  are written for macOS. Their tests run only on a Mac.
- **Live run (step 17):** a run with your real keys, on your Mac, is the
  remaining step. See HANDOFF.md.
- **Router and Home Assistant:** these work through command targets and
  attached MCP servers. Neither has been tested against real hardware yet.
