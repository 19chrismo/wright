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


def step2(verbose: bool = True) -> dict:
    """Quota pool: a task runs through the real HTTP path (harness → local pool proxy → upstream
    lanes). Key A hits a 429 mid-task, key B takes over; then every cloud lane is exhausted and the
    local lane serves (degraded) — the task still completes and no 429 ever reaches the harness."""
    import tempfile
    from .llm import OpenAICompatModel
    from .mockup import MockUpstream
    from .pool import Lane, Pool, PoolServer

    workdir = Path(tempfile.mkdtemp(prefix="wright-step2-"))
    brain = ScriptedModel([
        call("shell__run", command="echo one > a.txt"),
        call("shell__run", command="echo two > b.txt"),
        call("shell__run", command="echo three > c.txt"),
        call("shell__run", command="cat a.txt b.txt c.txt"),
        say("Wrote a.txt, b.txt, c.txt and printed them."),
    ])
    gemini = MockUpstream(brain, keys={
        "KEY-A-0000000000": {"ok_for": 2, "then": "429", "retry": 45},   # exhausts mid-task
        "KEY-B-1111111111": {"ok_for": 1, "then": "429-daily"},          # then B runs out for the day
    })
    ollama = MockUpstream(brain, keys={None: {"ok_for": None}})
    keys = {"gemini_a": "KEY-A-0000000000", "gemini_b": "KEY-B-1111111111"}
    ev = EventBus()
    if verbose:
        ev.subscribe(console_printer)
    pool = Pool(
        lanes=[Lane("gemini-a", "gemini", ["gemini-*"], gemini.url, "gemini_a"),
               Lane("gemini-b", "gemini", ["gemini-*"], gemini.url, "gemini_b"),
               Lane("ollama", "ollama", ["ollama:*"], ollama.url, local=True)],
        routes={"auto": ["gemini-2.5-flash", "ollama:qwen2.5vl:7b"]},
        key_lookup=keys.get, on_event=lambda e: ev.emit(e.pop("kind"), **e))
    server = PoolServer(pool, port=0).start()
    reg = ToolRegistry()
    reg.register(local_shell_tool(str(workdir)))
    model = OpenAICompatModel(server.url, "auto", api_key=server.token)
    task = Task(goal="Write a.txt, b.txt, c.txt containing one, two, three and print them.",
                workdir=workdir,
                acceptance=[{"name": "c.txt", "kind": "file_contains", "path": "c.txt", "text": "three"},
                            {"name": "printed", "kind": "ledger_ran", "pattern": r"^cat a\.txt"}])
    Harness(model, reg, ev, system_prompt="You are Wright.", stop_hooks=[acceptance_hook]).run(task)
    status = pool.status()
    server.stop(); gemini.stop(); ollama.stop()
    lanes_used = [e["lane"] for e in ev.of("lane")]
    if verbose:
        print(f"result: {task.status.upper()} — lanes per turn: {lanes_used}")
    return {"task": task, "events": ev, "lanes_used": lanes_used, "status": status,
            "upstream_log": gemini.log + ollama.log}


DEMOS["step2"] = step2


def step3(verbose: bool = True) -> dict:
    """Knowledge pack: capture sources, read the version from the system, tag decisions; a fabricated
    quote, a [SOURCED] tag with no source, and a mutation before grounding are all rejected."""
    import tempfile
    from .knowledge import grounding_gate, knowledge_tools
    from .testkit import VENDOR_DOC, StaticSite

    site = StaticSite({"/man/sshd_config.html": ("text/html", VENDOR_DOC)})
    workdir = Path(tempfile.mkdtemp(prefix="wright-step3-"))
    (workdir / "sshd_config").write_text("PasswordAuthentication yes\n")
    holder: dict = {}
    reg = ToolRegistry()
    reg.register(local_shell_tool(str(workdir)))
    for t in knowledge_tools(lambda: holder["task"]):
        reg.register(t)
    ev = EventBus()
    if verbose:
        ev.subscribe(console_printer)
    doc = site.url + "/man/sshd_config.html"

    def sid(messages: list[dict]) -> str:
        import json as _j
        for m in reversed(messages):
            if m.get("role") == "tool" and "source_id" in (m.get("content") or ""):
                return _j.loads(m["content"])["source_id"]
        return "?"

    model = ScriptedModel([
        call("shell__run", command="sed -i.bak 's/^PasswordAuthentication yes/PasswordAuthentication no/' sshd_config"),
        call("knowledge__capture", locator=doc, tier=1, excerpt="Password login is deprecated in 9.6"),
        call("knowledge__decide", statement="Set PasswordAuthentication no", tag="SOURCED", confidence=0.9),
        call("knowledge__version", component="openssh", command="echo OpenSSH_9.6p1"),
        call("knowledge__capture", locator=doc, tier=1, version_applies="9.6", component="openssh",
             excerpt="PasswordAuthentication: Specifies whether password authentication is allowed."),
        lambda m: call("knowledge__decide", statement="Set PasswordAuthentication no to disable password logins",
                       tag="SOURCED", confidence=0.9, sources=[sid(m)], supports=["password auth off"]),
        lambda m: call("knowledge__decide", statement="sshd must be reloaded for the change to apply",
                       tag="INFERRED", confidence=0.7, sources=[sid(m)],
                       chain="config options are read at startup; changing the file alone changes nothing running"),
        call("knowledge__show"),
        call("shell__run", command="sed -i.bak 's/^PasswordAuthentication yes/PasswordAuthentication no/' sshd_config"),
        say("Disabled password authentication in sshd_config (staged)."),
    ])
    task = Task(goal="Disable SSH password logins in ./sshd_config.", workdir=workdir,
                acceptance=[{"name": "password auth off", "kind": "file_contains", "path": "sshd_config",
                             "regex": r"(?m)^PasswordAuthentication no$"}])
    holder["task"] = task
    task.state.update(domain="ssh", task_type="harden")
    Harness(model, reg, ev, system_prompt="You are Wright.", gates=[grounding_gate],
            stop_hooks=[acceptance_hook]).run(task)
    site.stop()
    if verbose:
        print(task.state["pack"].summary())
        print(f"result: {task.status.upper()}")
    return {"task": task, "events": ev, "pack": task.state["pack"]}


DEMOS["step3"] = step3
