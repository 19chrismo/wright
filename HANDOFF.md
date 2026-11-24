# Handoff: Wright build status

This file is for any Claude session that picks up this branch on another
machine (for example, the user's MacBook Air). Read `BRIEF.md` first. It is
the spec.

## Who is building

A cloud Claude Code session is building this branch,
`claude/local-ai-app-harness-ogb2iw`, one build step at a time. It commits
and pushes after each step. A session on the Mac should **not** commit or
push to this branch while the cloud session is building. To get the latest
work, run `git pull`.

## Status

| Step | State |
|---|---|
| Brief v2 (`BRIEF.md`) | done |
| 1. Harness loop with a shell tool | done: `wright demo step1`, `tests/test_step01_loop.py` |
| 2–17 | in progress, in order |

## Set up on a Mac

1. Python 3.11 or newer is required, because the MCP SDK needs at least 3.10.
   macOS's built-in `python3` is often 3.9. If it is, install a newer one:
   `brew install python@3.12`, or use `uv`.
2. From the repository root:
   ```
   python3.12 -m venv ~/.wright/venv
   ~/.wright/venv/bin/pip install -e "./wright[test]"
   ~/.wright/venv/bin/python -m pytest wright/tests
   ~/.wright/venv/bin/wright demo step1
   ```
3. Expected result: all tests pass, and the demo ends with
   `result: DONE after 2 tool steps`.

## Layout

```
wright/
  BRIEF.md            the spec (v2)
  HANDOFF.md          this file
  pyproject.toml      package; dependency: mcp>=2.3,<3 (official SDK)
  wright/
    config.py         paths under WRIGHT_HOME (default ~/.wright), defaults
    events.py         event bus (UI, transcript, tests)
    llm.py            OpenAI-compatible client and ScriptedModel (deterministic test model)
    checks.py         deterministic checks (command, file, http, tcp, metric, ledger_ran)
    tools.py          dynamic tool registry
    harness.py        observe → plan → act → verify loop; gates, stop hooks, budgets
    secrets.py        Keychain-backed key store and redaction
    demos.py, cli.py  `wright demo <step>`
  tests/              one test file per build step
```

## Design decisions so far

- One OpenAI-compatible endpoint, the local pool proxy, sits between the
  harness and every model. Provider details live in the pool.
- Everything that decides whether an action is allowed is a gate. Everything
  that decides whether the task is done is a stop hook. Both run outside the
  model.
- Tool output is data with provenance `tool:<name>`. It is never sent with the
  user role as an instruction.
- With no model keys in the build container, demos and tests use
  `ScriptedModel`. Live model runs need the user's keys, which are entered on
  the Mac, never committed.
