# Handoff: Wright build status

This file is for any Claude session that picks up this repository on another
machine, for example the user's MacBook Air. `BRIEF.md` is the spec, and
`README.md` explains how to use the app.

## Status

| Step | State |
|---|---|
| 1–16 (harness, pool, knowledge, memory, baseline, contract, lock, calibration, self-mod gate, MCP tools, sandbox, stop hooks, danger, promotion, UI, packaging) | done; each has its own test file in `tests/` |
| Window as the single control surface (setup, keys, privacy, undo, connect) | done (`tests/test_window_control.py`) |
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
4. **Open the window.** Run `wright`. The window opens on a setup card.
   Everything from here happens in that window; no more Terminal.
5. **Give it a model, from the window.**
   - Click **Install it for me** for the free local model. Wright installs
     Ollama and downloads a model sized for this Mac.
   - And/or type "add my Gemini key". **The user pastes the key into the key
     box themselves.** Other household members do the same with "add Sam's
     Gemini key". Never put a key in the chat text or a file.
6. **Live goal, typed in the window.** Type: "Create a folder
   ~/Wright Projects/hello containing index.html that shows the heading Hello
   from Wright, and verify the file contains that heading". It's done when the
   report card says DONE, with green verification and a promotion with an undo
   command. Then send the same goal again with a screenshot attached.
7. **Undo.** Type "undo that" and confirm the folder's file went back. Type
   "undo" again to restore it.

## Rules for a session on the Mac

- Do not commit or push while the cloud session is building. Use `git pull`
  to get updates.
- Use real keys only through `wright keys add`. Never echo or log them.
- Report results in past tense, with the exact output of any failure.
