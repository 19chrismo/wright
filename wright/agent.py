"""The agent: one context, one plan, one history. Wires the harness loop to every control:
role contract → procedural lock → drift signal → mutation budget → danger protocol (gates),
stop hooks (acceptance criteria + regression baseline + scope + target validator), promotion
with snapshot/rehearsed rollback, episodic memory, calibration, skills and the self-mod gate."""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from . import report as report_mod
from .calibration import calibration_hook
from .checks import run_checks
from .config import PACKAGE_DIR, Config, deep_merge
from .contract import (ContractGate, DriftMonitor, MutationBudget, RoleContract, injection_observer,
                       scope_tools)
from .danger import Confirmer, DangerGate, danger_tools
from .events import EventBus
from .harness import Harness, Task, Verdict
from .knowledge import SemanticStore, knowledge_tools
from .llm import Model, ToolCall
from .lock import Lock
from .memory import memory_tools
from .persist import load_task, save_task
from .power import keep_awake
from .selfmod import SelfModGate, selfmod_tools
from .servers import fs as fsmod
from .servers.code import run_snippet
from .servers.shell import run_command
from .session import Continuity
from .skills import SkillLibrary, skill_tools
from .targets import CommandTarget, DirTarget, out_of_scope_paths
from .tools import Tool, ToolRegistry, ToolResult, obj


def slug(text: str, n: int = 40) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:n].strip("-") or "task"


class Agent:
    def __init__(self, config: Config | None = None, model: Model | None = None, *,
                 confirm: Confirmer | None = None, tools: str = "mcp", events: EventBus | None = None,
                 power_command: list[str] | None = None):
        self.config = config or Config.load()
        self.paths = self.config.paths
        self.continuity = Continuity(self.paths)
        self.opened = self.continuity.open()
        self.events = events or EventBus(redact=self.continuity.redactor)
        if events is not None:
            self.events.redact = self.continuity.redactor
        self.model = model
        self.power_command = power_command
        self.current: Task | None = None
        self.targets: dict[str, Any] = {}
        self.registry = ToolRegistry()
        get = lambda: self.current  # noqa: E731
        self.deny_w, self.deny_r = self.paths.protected_write(), self.paths.protected_read()

        # ---- tools: MCP servers (default) or in-process equivalents (tests / fallback)
        self.hub = None
        if tools == "mcp":
            from .mcp_hub import BUILTIN_SERVERS, McpHub
            env = {"WRIGHT_DENY_WRITE": json.dumps([str(p) for p in self.deny_w]),
                   "WRIGHT_DENY_READ": json.dumps([str(p) for p in self.deny_r]),
                   "WRIGHT_SEARCH": json.dumps(self.config.data.get("search"))}
            if self.config.data.get("chromium"):
                env["WRIGHT_CHROMIUM"] = self.config.data["chromium"]
            from .secrets import KeyStore
            self.hub = McpHub(self.registry, get_workdir=self._workdir, env=env,
                              on_event=lambda e: self.events.emit(e.pop("kind"), **e),
                              key_lookup=KeyStore(self.paths.secrets).get)
            enabled = self.config.data.get("builtin_servers") or list(BUILTIN_SERVERS)
            self.hub.attach_builtins(enabled)
            self.hub.attach_config(self.config.data.get("mcp_servers", {}))
        else:
            for t in self._local_tools():
                self.registry.register(t)

        # ---- memory, self store, contract
        self.semantic = SemanticStore(self.paths.semantic)
        self.skills = SkillLibrary(self.paths.procedural / "skills")
        self.selfmod = SelfModGate(self.paths, self.skills, self._skill_exec)
        self.contract = RoleContract.from_dict(json.loads(self.selfmod.store.read("contract") or "{}"))
        hcfg = deep_merge(self.config.data["harness"],
                          json.loads(self.selfmod.store.read("config") or "{}").get("harness", {}))
        self.skill_check = self.skills.check_all(self._skill_exec)

        # ---- procedural lock + core tools
        self.lock = Lock(self.registry, verifier=self.verify, final_check=self.final_check,
                         promoter=self.promote, freeze=self.freeze, events=self.events,
                         system_prompt=self.continuity.system_prompt(self.selfmod.store.read("prompt")),
                         digest_extra=self._digest_extra, audit_every=hcfg["audit_every"],
                         fix_attempts=hcfg["fix_attempts"], on_checkpoint=self.checkpoint)
        core = (knowledge_tools(get, cache_dir=self.paths.semantic / "_cache")
                + memory_tools(self.continuity.log) + scope_tools(get) + self.lock.tools(get)
                + danger_tools(get, self._run_in_sandbox, self.events) + selfmod_tools(self.selfmod)
                + skill_tools(self.skills, self._skill_exec, get, self._skill_run_in_task))
        for t in core:
            self.registry.register(t)

        # ---- the loop with its gates
        self.danger = DangerGate(confirm, self.events)
        self.budget = MutationBudget(hcfg["mutations_per_gate"])
        self.harness = Harness(
            model, self.registry, self.events, system_prompt=self.lock.system_prompt,
            gates=[ContractGate(self.contract, self.deny_w, self.deny_r), self.lock,
                   DriftMonitor(self.contract), self.budget, self.danger],
            observers=[self.lock.stamp, self.lock.reset_idle, self.budget.observe, injection_observer, self.lock.audit,
                       lambda t, c, tool, r: self.checkpoint(t)],
            visible_tools=self.lock.visible_tools, context_builder=self.lock.context_builder,
            end_of_turn=self.lock.end_of_turn, max_steps=hcfg["max_steps_per_task"],
            max_subtask_steps=hcfg["max_steps_per_subtask"], fix_attempts=hcfg["fix_attempts"],
            wall_clock=hcfg["wall_clock_seconds"], max_tokens=hcfg["max_tokens_per_task"],
            redact=self.continuity.redactor)

        # ---- quick mode: questions and research, read-only, no 8-stage lock (see dispatch.py)
        self.registry.register(Tool(
            "mode__build", "Quick mode is read-only. When doing what the person wants needs changes "
            "(build, install, fix, write files, touch a device), call this with a one-sentence job "
            "description; they get a 'Do it' button with a time estimate.",
            obj({"job": {"type": "string"}, "why": {"type": "string"}}, ["job"]), self._propose_build,
            source="core:quick", read_only=True, mutating=False))
        quick_prompt = (PACKAGE_DIR / "prompts" / "quick.md").read_text()
        self.quick_harness = Harness(
            model, self.registry, self.events,
            system_prompt=self.continuity.system_prompt(quick_prompt),
            gates=[self._quick_gate, ContractGate(self.contract, self.deny_w, self.deny_r), self.danger],
            observers=[injection_observer, lambda t, c, tool, r: self.checkpoint(t)],
            visible_tools=self._quick_visible, max_steps=hcfg.get("quick_max_steps", 40),
            max_subtask_steps=hcfg.get("quick_max_steps", 40), fix_attempts=hcfg["fix_attempts"],
            wall_clock=hcfg["wall_clock_seconds"], max_tokens=hcfg["max_tokens_per_task"],
            redact=self.continuity.redactor)
        self.events.subscribe(self._track_lanes)
        self.events.emit("session_open", chain_ok=self.opened["chain_ok"], entries=self.opened["entries"],
                         repaired=self.opened["repaired"], calibration=self.opened["calibration"],
                         skills=self.skill_check, tools=self.registry.names())

    # ================================================================== quick mode
    QUICK_CORE = ("knowledge__capture", "knowledge__version", "knowledge__decide", "knowledge__show",
                  "memory__episodes", "mode__build")

    def _quick_visible(self, task: Task) -> set[str]:
        """Read-only tools only: nothing on this Mac or the network can change in quick mode."""
        out = set(self.QUICK_CORE) & set(self.registry.names())
        for t in self.registry.all():
            core = t.source == "core" or t.source.startswith("core:")
            if t.read_only and not core:
                out.add(t.name)
        return out

    def _quick_gate(self, task: Task, call: ToolCall, tool: Any) -> Verdict:
        if call.name in self._quick_visible(task):
            return Verdict(True)
        return Verdict(False, f"{call.name} isn't available in quick mode (read-only). If changes are "
                              "needed, call mode__build with the job.")

    def _propose_build(self, a: dict) -> ToolResult:
        task = self.current
        job = (a.get("job") or "").strip()
        if task is None or not job:
            return ToolResult("REJECTED: describe the job in one sentence", is_error=True)
        task.state["proposed_build"] = job[:400]
        task.state["proposed_why"] = (a.get("why") or "")[:400]
        return ToolResult("Noted. Finish your answer: explain what you found and what the job would do. "
                          "The person decides whether to start it.")

    def _run_quick(self, task: Task) -> Task:
        self.current = task
        task.state["on_decision"] = calibration_hook(self.continuity.calibration, lambda: self.current)
        old = self._route_model(task)
        try:
            with keep_awake(self.power_command, self.events):
                self.quick_harness.run(task)
        except (KeyboardInterrupt, SystemExit):
            self.checkpoint(task)
            self.events.emit("interrupted", task=task.id, steps=task.steps)
            self.current = None
            raise
        finally:
            if old is not None:
                self.model.model = old
        self._finish(task)
        self.current = None
        return task

    # ================================================================== helpers
    def _workdir(self) -> str:
        return str(self.current.workdir) if self.current else str(Path.home())

    def _track_lanes(self, ev: dict) -> None:
        if ev["kind"] == "assistant" and self.current and ev.get("lane"):
            self.current.state.setdefault("lanes_used", []).append(ev["lane"].split("/")[0])

    def _local_tools(self) -> list[Tool]:
        s = {"type": "string"}

        def wd(p: str | None) -> str:
            base = self._workdir()
            if not p:
                return base
            p = str(Path(p).expanduser())
            return p if p.startswith("/") else str(Path(base, p))

        def shell(a: dict) -> ToolResult:
            out = run_command(a["command"], wd(a.get("cwd")), float(a.get("timeout", 120)), self.deny_w, self.deny_r)
            return ToolResult(json.dumps(out), is_error=out.get("exit_code") != 0,
                              data={"command": a["command"], "cwd": wd(a.get("cwd")), "exit_code": out.get("exit_code")})

        def code(a: dict) -> ToolResult:
            out = run_snippet(a.get("language", "python"), a["code"], wd(a.get("cwd")), float(a.get("timeout", 60)))
            return ToolResult(json.dumps(out), is_error=out.get("exit_code") != 0,
                              data={"command": a["code"][:500], "exit_code": out.get("exit_code")})

        def safe(fn):
            def run(a: dict) -> ToolResult:
                try:
                    return ToolResult(fn(a), data={"path": a.get("path")})
                except Exception as e:
                    return ToolResult(f"{type(e).__name__}: {e}", is_error=True)
            return run

        return [
            Tool("shell__run", "Run a shell command (/bin/sh) in the sandbox.", obj({"command": s, "cwd": s,
                 "timeout": {"type": "number"}}, ["command"]), shell, source="local:shell", destructive=True,
                 open_world=True),
            Tool("code__run", "Run a python or node snippet in the sandbox.", obj({"code": s, "language": s},
                 ["code"]), code, source="local:code", destructive=True),
            Tool("fs__read", "Read a text file.", obj({"path": s}), safe(lambda a: fsmod.fs_read(wd(a["path"]))),
                 source="local:fs", read_only=True),
            Tool("fs__write", "Create or overwrite a file.", obj({"path": s, "content": s}),
                 safe(lambda a: fsmod.fs_write(wd(a["path"]), a["content"])), source="local:fs", destructive=True),
            Tool("fs__edit", "Replace an exact string in a file.", obj({"path": s, "old": s, "new": s}),
                 safe(lambda a: fsmod.fs_edit(wd(a["path"]), a["old"], a["new"])), source="local:fs",
                 destructive=True),
            Tool("fs__list", "List files.", obj({"path": s}, []), safe(lambda a: fsmod.fs_list(wd(a.get("path")))),
                 source="local:fs", read_only=True),
            Tool("fs__search", "Search file contents with a regex.", obj({"path": s, "regex": s}),
                 safe(lambda a: fsmod.fs_search(wd(a.get("path")), a["regex"])), source="local:fs", read_only=True),
        ]

    def _run_in_sandbox(self, command: str) -> tuple[int, str]:
        out = run_command(command, self._workdir(), 300, self.deny_w, self.deny_r)
        return (out.get("exit_code") if out.get("exit_code") is not None else 1,
                (out.get("stdout", "") + out.get("stderr", "") + out.get("error", ""))[-2000:])

    def _skill_exec(self, tool: str, args: dict, workdir: Path) -> tuple[bool, str]:
        """Execute a skill step in a scratch directory (regression tests)."""
        if tool == "shell__run":
            out = run_command(args["command"], str(workdir), float(args.get("timeout", 120)), self.deny_w, self.deny_r)
            return out.get("exit_code") == 0, json.dumps(out)
        if tool == "code__run":
            out = run_snippet(args.get("language", "python"), args["code"], str(workdir))
            return out.get("exit_code") == 0, json.dumps(out)
        if tool in ("fs__write", "fs__edit"):
            p = args["path"] if args["path"].startswith("/") else str(Path(workdir, args["path"]))
            try:
                if tool == "fs__write":
                    return True, fsmod.fs_write(p, args["content"])
                return True, fsmod.fs_edit(p, args["old"], args["new"])
            except Exception as e:
                return False, str(e)
        t = self.registry.get(tool)
        if not t:
            return False, f"unknown tool {tool}"
        r = t.fn(args)
        return not r.is_error, r.text

    def _skill_run_in_task(self, tool: str, args: dict, workdir: Path) -> tuple[bool, str]:
        """Skill use inside a task goes through every gate, like any other call."""
        r = self.harness.execute_call(self.current, ToolCall(tool, args))  # type: ignore[arg-type]
        return not r.is_error, r.text

    def _digest_extra(self, task: Task) -> str:
        o = self.continuity.calibration.overall()
        flags = task.state.get("flags", [])
        return (f"calibration: accuracy {o['accuracy']} vs stated {o['mean_confidence']} over {o['graded']} claims; "
                f"integrity flags this task: {len(flags)}")

    def checkpoint(self, task: Task) -> None:
        if task.state.get("task_dir"):
            save_task(task, Path(task.state["task_dir"]) / "state.json")

    def default_project_dir(self, goal: str, task_id: str) -> Path:
        base = Path(self.config.data.get("projects_dir", "~/Wright Projects")).expanduser()
        return base / f"{slug(goal)}-{task_id[-6:]}"

    # ================================================================== stop hooks
    def freeze(self, task: Task, baseline: Any) -> dict:
        frozen = baseline.freeze(Path(task.state["sandbox_root"]))
        baseline.save(Path(task.state["task_dir"]) / "baseline.json")
        return frozen

    def verify(self, task: Task) -> tuple[bool, list[dict]]:
        tgt = self.targets[task.id]
        root = Path(task.state["sandbox_root"])
        results = [r.as_dict() for r in run_checks(task.acceptance, root, task.ledger)]
        b = task.state.get("baseline")
        if b is not None and b.dimensions:
            comp = b.compare(root)
            for row in comp["rows"]:
                results.append({"name": f"baseline:{row['name']}", "ok": row["status"] != "worse",
                                "value": row["now"], "detail": f"{row['status']} (frozen {row['before']} → now {row['now']})"})
            task.state["regression"] = {k: comp[k] for k in ("accept", "regressions", "improvements", "flakes")}
            if not comp["accept"]:
                tgt.reset_to_green()
                results.append({"name": "regression_gate", "ok": False, "value": comp["regressions"],
                                "detail": "change REJECTED: baseline dimension(s) got worse "
                                          f"{comp['regressions']} (even if others improved). The sandbox was "
                                          "returned to its last green checkpoint."})
        v = tgt.validate()
        if v is not None:
            results.append({"name": "target_validate", "ok": v[0], "value": None, "detail": v[1][-300:]})
        diff = tgt.diff()
        if task.state.get("mutates", True):
            scope = (task.state.get("scope") or {}).get("paths") or []
            bad = out_of_scope_paths(diff, scope, task.state.get("justified", set()), root)
            results.append({"name": "scope", "ok": not bad, "value": bad,
                            "detail": ("changed outside the intent's scope (revert them or scope__justify): "
                                       f"{bad}") if bad else "all changes are inside the declared scope"})
            if bad:
                task.state["out_of_scope_unjustified"] = bad
        green = all(r["ok"] for r in results)
        if green:
            tgt.mark_green()
            task.state["has_changes"] = any(diff.values())
        return green, results

    def _probe_real(self, task: Task, tgt: Any) -> list[dict]:
        cwd = tgt.real_check_dir
        res = [r.as_dict() for r in run_checks(task.acceptance, cwd, task.ledger)]
        b = task.state.get("baseline")
        if b is not None and b.dimensions:
            comp = b.compare(cwd)
            res += [{"name": f"baseline:{r['name']}", "ok": r["status"] != "worse", "value": r["now"],
                     "detail": r["status"]} for r in comp["rows"]]
        if isinstance(tgt, CommandTarget):
            res += tgt.probes()
        return res

    def final_check(self, task: Task) -> tuple[bool, list[dict]]:
        tgt = self.targets[task.id]
        prom = task.state.get("promotion") or {}
        lv = task.state.get("last_verify") or {}
        if self.lock._mutated_since(task, lv.get("ledger_len", 0)):
            return False, [{"name": "unverified_changes", "ok": False, "value": None,
                            "detail": "changes were made after the last verify__run"}]
        if prom.get("status") == "promoted":
            res = self._probe_real(task, tgt)
            return all(r["ok"] for r in res), res
        if task.state.get("mutates", True) and task.state.get("has_changes"):
            return False, [{"name": "promotion", "ok": False, "value": None,
                            "detail": "changes exist in the sandbox but were never promoted"}]
        return self.verify(task)

    # ================================================================== promotion gate
    def promote(self, task: Task) -> dict:
        tgt = self.targets[task.id]
        rep: dict[str, Any] = {"t": time.time(), "target": tgt.describe()}
        ok, msg = tgt.rehearse_rollback()
        rep["rollback_rehearsal"] = msg
        if not ok:
            rep.update(status="failed", reason="rollback rehearsal failed; nothing was changed")
            self.events.emit("promotion", task=task.id, **rep)
            return rep
        sid, verified, vmsg = tgt.snapshot()
        rep.update(snapshot=sid, snapshot_check=vmsg)
        if not verified:
            rep.update(status="failed", reason="the snapshot could not be verified by restoring it; nothing was changed")
            self.events.emit("promotion", task=task.id, **rep)
            return rep
        if isinstance(tgt, CommandTarget):
            tgt.pending_snapshot = sid
        rep["applied"] = tgt.apply()
        probes = self._probe_real(task, tgt)
        rep["probes"] = probes
        if all(p["ok"] for p in probes):
            tgt.confirm()
            rep.update(status="promoted", rollback_command=f"wright rollback {sid}")
            self.events.emit("promotion", task=task.id, status="promoted", snapshot=sid,
                             rollback_command=rep["rollback_command"])
        else:
            tgt.rollback(sid)
            rep.update(status="rolled_back", reason="post-promotion probes failed: " +
                       ", ".join(p["name"] for p in probes if not p["ok"]))
            self.events.emit("rollback", task=task.id, snapshot=sid, reason=rep["reason"])
        return rep

    # ================================================================== running tasks
    def plan(self, text: str, images: int = 0, caps: dict | None = None) -> dict:
        """The dispatcher's decision and time estimate for a request (see dispatch.py). `caps` says
        what is set up: has_cloud, has_local, gen_tps, prefill_tps (the app passes live values)."""
        from .dispatch import plan_request
        lanes = self.config.data["pool"]["lanes"]
        caps = {"has_cloud": any(not l.get("local") for l in lanes),
                "has_local": any(l.get("local") for l in lanes), **(caps or {})}
        hist = []
        try:
            hist = [r["entry"] for r in self.continuity.log.all()[-400:] if r["entry"].get("type") == "task"]
        except Exception:
            pass
        d = plan_request(text, images=images, has_cloud=caps["has_cloud"], has_local=caps["has_local"],
                         local_only=bool(self.config.data.get("local_only")), history=hist,
                         gen_tps=float(caps.get("gen_tps") or 0), prefill_tps=float(caps.get("prefill_tps") or 0))
        return d.as_dict()

    def _route_model(self, task: Task) -> Any:
        """Point the model client at this task's route (fast/auto/deep/local); returns the old one."""
        route = (task.state.get("dispatch") or {}).get("route")
        if route and isinstance(getattr(self.model, "model", None), str):
            old, self.model.model = self.model.model, route
            return old
        return None

    def run(self, goal: str, target: str | Path | None = None, images: list[dict] | None = None,
            target_spec: dict | None = None, dispatch: dict | None = None) -> Task:
        task = Task(goal=goal, workdir=Path("."), images=images or [])
        tdir = self.paths.tasks / task.id
        if dispatch:
            task.state["dispatch"] = dispatch
            self.events.emit("dispatch", task=task.id, mode=dispatch.get("mode"), size=dispatch.get("size"),
                             where=dispatch.get("where"), estimate=dispatch.get("estimate"),
                             private=dispatch.get("private"))
        if dispatch and dispatch.get("mode") == "quick" and not target_spec:
            work = tdir / "work"                    # quick mode reads; nothing is promoted anywhere
            work.mkdir(parents=True, exist_ok=True)
            task.workdir = work
            task.state.update(task_dir=str(tdir), sandbox_root=str(work), target_was_empty=False,
                              mutates=False)
            return self._run_quick(task)
        if target_spec:
            tgt: Any = CommandTarget(target_spec)
        else:
            tgt = DirTarget(Path(target) if target else self.default_project_dir(goal, task.id))
        root = tgt.prepare(tdir)
        task.workdir = root
        if target_spec:
            task.state["target_spec"] = target_spec
        task.state.update(sandbox_root=str(root), task_dir=str(tdir), target=tgt.describe(),
                          real_target=str(getattr(tgt, "real", tgt.describe().get("name"))),
                          target_was_empty=not any(Path(root).iterdir()), new_context=True)
        self.targets[task.id] = tgt
        return self._drive(task)

    def resume(self, task_id: str) -> Task:
        task = load_task(self.paths.tasks / task_id / "state.json")
        if (task.state.get("dispatch") or {}).get("mode") == "quick":
            task.state.pop("cancel", None)
            task.status = "running"
            self.events.emit("resumed", task=task.id, state="quick", steps=task.steps)
            return self._run_quick(task)
        spec = task.state.get("target", {})
        if spec.get("kind") == "dir":
            tgt = DirTarget(Path(spec["real"]))
            from .sandbox import DirSandbox
            tgt.sb = DirSandbox(tgt.real, Path(task.state["task_dir"]))
            tgt.sb.base_manifest = {}
            cps = tgt.sb.checkpoints.list()
            tgt.sb.last_green = cps[-1]["id"] if cps else tgt.sb.checkpoints.take(tgt.sb.root, "resume")
            self.targets[task.id] = tgt
        task.state["new_context"] = True
        task.state.pop("cancel", None)
        task.status = "running"
        self.events.emit("resumed", task=task.id, state=task.state.get("lock", {}).get("state"),
                         steps=task.steps)
        return self._drive(task)

    def _drive(self, task: Task) -> Task:
        self.current = task
        task.state["on_decision"] = calibration_hook(self.continuity.calibration, lambda: self.current)
        old = self._route_model(task)
        try:
            with keep_awake(self.power_command, self.events):
                self.harness.run(task)
        except (KeyboardInterrupt, SystemExit):
            # Quit mid-task: leave it resumable (status stays "running") instead of failing it.
            self.checkpoint(task)
            self.events.emit("interrupted", task=task.id, steps=task.steps)
            self.current = None
            raise
        finally:
            if old is not None:
                self.model.model = old
        self._finish(task)
        self.current = None
        return task

    def _finish(self, task: Task) -> None:
        if task.status == "stopped":                     # resumable later with "resume"
            task.state.pop("cancel", None)
            self.checkpoint(task)
            return
        if task.status == "running":
            task.status = "failed"
            task.result = task.result or "Stopped unexpectedly."
        self.continuity.close_task(task)
        pack = task.state.get("pack")
        if pack:
            self.semantic.save(pack)
        if task.status == "done" and task.state.get("target_was_empty"):
            name = slug(f"{task.state.get('domain', 'general')} {task.state.get('task_type', 'task')}", 30)
            name = ("s_" + name.replace("-", "_") + "_" + task.id[-4:]).lower()
            sk, msg = self.skills.promote_from_task(task, name, task.goal[:200], self._skill_exec)
            task.state["skill_promotion"] = msg
        rep = report_mod.build(task, self.continuity.calibration.overall())
        d = task.state.get("dispatch") or {}
        rep["mode"] = d.get("mode", "build")
        rep["where"] = d.get("where")
        rep["estimate"] = (d.get("estimate") or {}).get("text")
        rep["seconds"] = round(time.time() - task.started)
        if task.state.get("proposed_build"):
            rep["proposed_build"] = task.state["proposed_build"]
        task.state["report"] = rep
        self.events.emit("report", task=task.id, report=rep, text=report_mod.render(rep))
        self.checkpoint(task)

    def cancel(self) -> bool:
        """Stop the running task after its current step (the task stays resumable)."""
        if self.current is None:
            return False
        self.current.state["cancel"] = True
        self.events.emit("notice", text="Stopping after the current step…")
        return True

    def close(self) -> None:
        if self.hub:
            self.hub.close()
