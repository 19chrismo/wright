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

- needs no admin password and no Homebrew; on a Mac it only needs Apple's
  Command Line Tools (for `python3` and `git`);
- gets `uv` from PyPI, pinned to one release and refused unless its SHA-256
  matches, and never pipes a downloaded script into a shell;
- installs Python 3.12 and exactly the package versions in `uv.lock`, every
  one hash-checked and prebuilt for Intel Macs, Apple-silicon Macs and Linux,
  so nothing needs compiling;
- creates `~/.wright/venv`, links `wright` into `~/.local/bin`, adds
  **Wright.app** to `~/Applications`, runs a self-test, and opens Wright.

## Run

The installer opens the Wright window when it finishes. After that, open it
any of these ways:

- **Easiest:** double-click **Wright** in the Applications folder inside your
  home folder, or drag it to the Dock.
- **From Terminal:** type `wright` in a new Terminal window.

Opening it again brings back the same window; it never starts a second copy.

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
| "update yourself" · "undo the update" | Checks for improvements now · goes back to the previous version. |
| "connect my phone" · "forget my phones" | Shows a code to scan with your iPhone or iPad camera; the device then stays signed in · turns phone access off and forgets every paired device. |
| "call me Sam" | The home screen greets you by that name. |
| "new project: snow bot — a robot that clears the driveway" | Starts a project notebook. See "Projects" below. |
| "my projects" · "work on the snow bot" | Lists your projects · opens one's notebook, with a **Continue** button for the next step. |
| "stop" · "keep going" · "restart Wright" · "quit Wright" | Stops the running job after its current step · picks it back up · restarts in place · closes Wright. |

- **Keys:** go straight from the key box to the macOS Keychain. They never
  appear in the chat, the logs or anything sent to an AI model.
- **A request sent before setup:** waits, and starts by itself once a model is
  ready.
- **Anything else you type:** a question or a job. See "Before anything starts"
  below.
- **Details:** the window shows plain words by default. Tap **Details** (top
  right) to see every step, tool and check. Safety items (anything Wright
  blocked or flagged, confirmations, failed checks, rollbacks, risks) are
  always shown.

### On your iPhone and iPad

Say **connect my phone** in Wright on the Mac.

1. The first time, Wright restarts for a moment. If the Mac asks whether to
   accept incoming connections, choose **Allow**.
2. Wright shows a code. Point the iPhone **Camera** at it and tap the link.
   Wright opens on the phone, signed in.
3. In Safari, tap **Share → Add to Home Screen** for a Wright icon.

Phone access works only on your home network, only for devices you paired
this way, and each code works once, for 10 minutes. To talk instead of
typing, use the microphone key on the iPhone keyboard.

### Models

- **How requests are routed:** Wright talks to one local endpoint, a pooling
  proxy on `127.0.0.1`. It sends each request to whichever key is healthy.
- **Fast model first:** Gemini Flash answers first; the bigger, slower Gemini
  Pro is second (or first, when you ask it to "think carefully"); this Mac's own
  model is last. Older versions put Pro first, which on the free tier hit its
  small per-minute limit on almost every step. Saved settings are fixed
  automatically.
- **Waiting beats crawling:** when an online key is only resting for a minute,
  Wright waits for it (up to 75 seconds, `pool.prefer_cloud_wait_seconds`)
  instead of handing the work to this Mac's model, which is far slower on an
  older Mac. When the local model does take over, the window says so.
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

## It keeps improving: no versions to install

- **Daily check:** Wright looks for improvements when it opens and once a day.
- **Tested first:** each improvement is tested in a separate copy with
  Wright's own test suite and taken only if every test passes. If anything
  fails, Wright stays on the version that works and says so in the window.
- **Your edits are safe:** local edits are never overwritten.
- **When it takes effect:** the next time Wright opens.
- **Undo:** "undo the update" goes back one step.
- **Turning it off:** set `"auto_update": false` in `~/.wright/config.json`.

## Before anything starts: the plan card

Every request is sorted first, instantly, without asking an AI model
(`wright/dispatch.py`):

- **A question or research** ("how do I…", "find out…", "why is…") runs in
  **quick mode**: read-only, no build stages, usually seconds to a couple of
  minutes. If the answer needs something changed, the reply ends with a
  **Do it** button for that job.
- **A job** ("build…", "make…", "fix…", "clean up…") gets a **plan card**
  first: roughly how long it'll take, where it runs (online, or on this Mac),
  and why. Choose **Go**, **Keep it on this Mac**, or **Cancel**.
- **Anything that looks private** (passwords, keys, money, accounting, medical,
  camera footage, router configs) stays on this Mac. The card says so and lets
  you send it online if you decide it's fine.
- **The estimate learns:** every finished job records how long it really took,
  so the next estimate for a similar job comes from your own history and the
  measured speed of this Mac. Each report shows "took X (estimate Y)".

Set `"confirm_builds": false` in `~/.wright/config.json` to skip the card.

## Projects: work that lasts weeks

A project (the snow bot, the P10 map, an accounting helper for work) keeps a
**notebook** (`wright/projects.py`):

- the goal;
- phases (to do, doing, done);
- a parts list (need, ordered, have);
- decisions and why;
- what was printed or tested, and how it went;
- what's next;
- every job that worked on it.

How to use it:

- **Start one:** type "new project: snow bot — a robot that clears the
  driveway", or tap the **New project** tile. **Plan it with me** asks Wright
  to suggest phases, a parts list and first steps, and saves them.
- **Pick it up:** projects appear as cards on the home screen. Tap one, or
  type "work on the snow bot". The notebook opens with **Continue: <next
  step>**.
- **Jobs in a project:** any request that names a project becomes one of its
  jobs, for example "snow bot: design the motor mount". The plan card shows
  📒 Snow bot. Wright reads the notebook before it starts, builds in the
  project's own folder (`~/Wright Projects/snow-bot`), writes its decisions,
  parts and tests into the notebook as it goes, and sets the next step when it
  finishes.
- **Private projects:** a project whose name or goal looks private (accounting,
  passwords, money and so on) keeps every job and its notebook on this Mac.

## Tool catalogs: where Wright looks for tools

When a job needs a tool or a command Wright doesn't know, it searches curated
public catalogs first (`wright/catalogs.py`). The first is
[The Book of Secret Knowledge](https://github.com/trimstray/the-book-of-secret-knowledge)
(MIT licence). It lists about 800 command-line, desktop and web tools, manuals,
cheat sheets and shell one-liners.

- **Leads, not proof:** a catalog entry only says a tool exists and what it's
  for. Wright then reads that tool's own documentation or code before relying
  on it. Installing anything still needs your OK.
- **Nothing risky:** the book's hacking and penetration-testing chapters, and
  entries about attacking or getting round protections, are left out.
- **Kept at home:** Wright downloads the book once into `~/.wright/library`,
  refreshes it weekly and uses the last copy when offline.

## What happens when you send a job

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
| `library/<catalog>.json` | Local copy of each tool catalog, refreshed weekly. No file or shell tool can write here. |
| `projects/<id>/notebook.json` | One notebook per project. Only Wright's `notebook__update` tool changes it; no file or shell tool can write here. |

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
