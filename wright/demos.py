"""Offline demonstrations of each build step. They use ScriptedModel (deterministic, no keys) so the
mechanics are provable anywhere; the same harness runs against the pool with real models."""
from __future__ import annotations

import tempfile
from pathlib import Path

from .events import EventBus, console_printer
from .harness import Harness, Task, acceptance_hook
from .llm import Reply, ScriptedModel, call, say
from .tools import ToolRegistry, local_shell_tool


def step1(workdir: Path | None = None, verbose: bool = True) -> Task:
    """Trivial verifiable task through observe → plan → act → verify.
    The scripted model makes a typo on purpose so the verifier has something to catch."""
    workdir = Path(workdir or tempfile.mkdtemp(prefix="wright-step1-"))
    reg = ToolRegistry()
    reg.register(local_shell_tool(str(workdir)))
    ev = EventBus()
    if verbose:
        ev.subscribe(console_printer)

    def fix_after_failure(messages: list[dict]) -> Reply:
        last = messages[-1]["content"]
        assert "STOP HOOKS FAILED" in last, "expected verifier feedback"
        return call("shell__run", command="printf 'hello from wright\\n' > greeting.txt && cat greeting.txt")

    model = ScriptedModel([
        call("shell__run", command="printf 'helo from wright\\n' > greeting.txt"),
        say("Created greeting.txt."),                       # premature claim → verifier says RED
        fix_after_failure,                                   # reacts to the injected failure
        say("Created greeting.txt containing 'hello from wright' and printed it back."),
    ])
    task = Task(goal="Create greeting.txt containing exactly 'hello from wright', then print it.",
                workdir=workdir,
                acceptance=[
                    {"name": "file has greeting", "kind": "file_contains", "path": "greeting.txt",
                     "regex": r"^hello from wright\n$"},
                    {"name": "printed it back", "kind": "ledger_ran", "pattern": r"cat greeting\.txt"},
                ])
    h = Harness(model, reg, ev, system_prompt="You are Wright. Produce the result.",
                stop_hooks=[acceptance_hook])
    if verbose:
        print(f"goal: {task.goal}\nworkdir: {workdir}")
    h.run(task)
    if verbose:
        print(f"result: {task.status.upper()} after {task.steps} tool steps — {task.result}")
    return task


DEMOS = {"step1": step1}
