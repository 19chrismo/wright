# Wright roadmap

What Wright is for, in the owner's words: a personal "Jarvis" that grows with
them. You say "build me this" or "find out that"; it works out what it needs to
learn, learns it from good sources, builds and checks the thing, and says
roughly how long it will take before it starts. It decides by itself whether a
job needs an online model or can stay on this Mac, keeps everything private at
home, and relies less on outside companies as the hardware improves. It must be
as easy to use as an iPhone: simple, full of abilities that appear when needed,
and good-looking. No coding needed from the owner, no subscription over
CA$20 a month, nothing financial, nothing dangerous.

## Done

| What | Where |
|---|---|
| Safe build process: copy first, checks, undo, risks rehearsed, safety flags | `lock.py`, `agent.py`, `danger.py` (steps 1–16) |
| Shared household keys, local fallback, phone access | `pool.py`, `devices.py` |
| **Speed:** fast model first; settings saved pro-first are fixed on load; waits briefly for a resting key instead of the slow local model | `config.normalize_routes`, `pool._cloud_back_soon` |
| **Dispatcher:** question or job, private or not, size, where it runs | `dispatch.py` |
| **Time estimate** before starting, learned from finished jobs and measured local speed | `dispatch.estimate`, episodes record mode/size/where |
| **Quick mode:** questions and research answered read-only, without the build stages; a needed change becomes a "Do it" button | `agent._run_quick`, `prompts/quick.md` |
| **Window:** plan card (Go / keep it on this Mac / cancel), home screen, plain-language progress, Details toggle, "call me …" | `ui/index.html`, `ui/server.py` |
| **Project notebook:** goal, phases, parts, decisions and why, tests, next steps, jobs; "work on the snow bot" with a Continue button; project cards on the home screen; a job that names a project reads its notebook, builds in its folder and is recorded; private projects stay on this Mac | `projects.py`, `tests/test_projects.py` |

## Next, in order

Each step lands on a branch, passes the whole test suite, and reaches the Mac
only through the self-update (which runs the tests again first).

1. **Live run on the Mac (step 17 in HANDOFF.md).** Free Gemini key from
   aistudio.google.com, then real jobs. Measure real speeds and replace the
   estimate defaults in `dispatch.py` (`SECONDS_PER_STEP`, `DEFAULT_STEPS`)
   with measured numbers.
2. ~~Project notebook~~ (done, see the table above). Still open for it: a
   phone-sized check of the notebook card on the iPhone, and a "rename
   project" command.
3. **Learning loop and library.** Before a job, Wright lists what it doesn't
   know yet, then reads the best sources in order: maker's docs and manuals,
   standards, the actual code of well-maintained GitHub projects (cloned
   read-only into the sandbox), maintainer guides, then forums with a second
   source. What it learns goes into a library shared by every project (the
   semantic store already keeps sourced facts; it needs a search tool and a
   "what do I already know about X" step). Legal sources only.
4. **Abilities on demand.** When a job needs something Wright can't do yet,
   it finds a suitable free MCP server or tool, explains it in one line,
   installs it only after the owner taps Allow, tests it in the sandbox, and
   keeps it as a skill with its own regression test. First candidates:
   - 3D design from code: OpenSCAD or CadQuery. STL saved to the Desktop for
     OrcaSlicer, with a fit-test print first.
   - Home Assistant: its MCP server integration at 192.168.50.87.
   - Network diagnostics: read-only first (who's connected, signal per room,
     duplicate files on network shares). Router changes only through a
     command target with automatic rollback.
   - Documents and spreadsheets, for work tools.
5. **First real projects with the owner.** The owner tests by hand and reports
   back; Wright adjusts. Physical steps come as numbered checklists with
   pictures.
6. **Always-on and household.** Move the server part to an always-on machine
   (the HA MacBook can host it; it's too slow for local AI). One chat per
   person, shared house knowledge.
7. **Private voice.** Push-to-talk that stays at home needs HTTPS on the home
   network and a local speech model on stronger hardware. Until then: the
   iPhone keyboard's microphone key, or Fn twice on the Mac.

## Growing past the cloud

The dispatcher routes by measured speed, not by fixed names. When a stronger
machine arrives (a used Apple-silicon Mac mini with 32 GB or more is the usual
starting point), point the local lane at it in `~/.wright/config.json`
(`pool.lanes` → `base_url: http://<that machine>:11434/v1`) and choose a bigger
local model. Wright measures it, the estimates update, and more jobs stay home
by themselves. Nothing needs rewriting.

## Things the plan was missing

- **A Time Machine backup before the first live runs.** Wright's own undo
  covers what Wright changes; a full backup covers everything else.
- **Model names change.** Google renames and retires Gemini models. Adding a
  key already picks the newest Flash and Pro from the live list, and
  "check setup" re-checks it.
- **Free-tier limits change too.** If the free key runs out often, a paid key
  billed per use (with a low budget alert in Google Cloud) costs little for
  Flash. Wright's per-key daily share caps requests either way.
- **Region-locked devices.** Getting round a region lock on the owner's own
  device (the P10) is research first: quick mode, read-only, sources named.
  Nothing gets flashed or written to a device without the irreversible-write
  confirmation.
- **Work data.** Anything from the owner's job (accounting files) counts as
  private: it stays on this Mac unless the owner chooses otherwise on the plan
  card.
