# Handoff: Wright build status

This file is for any Claude session that picks up this repository on another
machine, for example the user's MacBook Air. `BRIEF.md` is the spec, and
`README.md` explains how to use the app.

## Status

| Step | State |
|---|---|
| 1–16 (harness, pool, knowledge, memory, baseline, contract, lock, calibration, self-mod gate, MCP tools, sandbox, stop hooks, danger, promotion, UI, packaging) | done; each has its own test file in `tests/` |
| 17. Live run on the Mac with real keys | **to do on the Mac** (steps below) |

The cloud session built and tested everything on Linux with a deterministic
scripted model, because it had no model keys. Three parts are macOS-only and
are exercised for the first time on the Mac:

- Keychain storage;
- `sandbox-exec` write protection;
- `caffeinate`.

## Step 17 on the Mac (a Claude session there can run these)

1. **Get the code.** Run `git clone https://github.com/19chrismo/wright ~/wright-app`,
   or `git -C ~/wright-app pull` if it's already there.
2. **Install.** Run `~/wright-app/install.sh`. The run should end with
   "harness self-test passed".
3. **Run the full test suite.** Run `~/.wright/venv/bin/python -m pytest -q`
   from `~/wright-app`. The macOS-only test,
   `test_macos_sandbox_denies_writes_to_protected_paths`, runs here. Report
   any failure with its output.
4. **Give it a model.** At least one of:
   - Ollama: install from ollama.com, then `ollama pull qwen2.5:7b` (or
     `qwen2.5:3b` on an 8 GB Mac).
   - A Gemini key: `wright keys add gemini`. **The user types the key.**
     Never put it in chat or a file.
5. **Check the setup.** Run `wright doctor`; every line should be ✓.
6. **Live goal.** Run:
   `wright task "Create a folder ~/Wright Projects/hello containing index.html that shows the heading Hello from Wright, and verify the file contains that heading"`.
   It's done when the printed report says DONE and shows green verification
   and a promotion with an undo command.
7. **The window.** Run `wright`. A single window opens. Send the same goal
   with a screenshot attached.

## Rules for a session on the Mac

- Do not commit or push while the cloud session is building. Use `git pull`
  to get updates.
- Use real keys only through `wright keys add`. Never echo or log them.
- Report results in past tense, with the exact output of any failure.
