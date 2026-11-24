"""Procedural lock: workflow state lives OUTSIDE the model, in a versioned plan graph executed as a
finite-state machine. The model proposes; the lock decides. Enforced at three points:
  1. before every tool call   — a tool not allowed in the current state is blocked (and hidden);
  2. at every transition      — only edges in the graph, and only when the exit guard holds;
  3. at the end of every turn — a turn that ends outside a terminal state is answered with the
                                 state, its exit condition and the legal next states.
Manage-Execute-Audit: the lock (manager) owns the shared state; each subtask runs in a fresh,
bounded executor context built from that state; a deterministic audit runs every N tool steps.
"""
from __future__ import annotations

import fnmatch
import json
import time
from pathlib import Path
from typing import Any, Callable

from .baseline import Baseline, Dimension
from .checks import run_checks, validate_check
from .config import PACKAGE_DIR
from .events import EventBus
from .harness import ALLOW, Task, Verdict
from .llm import Reply, ToolCall
from .tools import Tool, ToolRegistry, ToolResult, obj

GUARD_TEXT = {
    "intent_recorded": "intent__set has recorded an intent paragraph and at least one machine-checkable criterion",
    "grounded": "the knowledge pack holds at least one tagged decision",
    "baseline_frozen": "baseline__define has frozen the known-good state",
    "plan_done": "a plan exists and every subtask is marked done with plan__step_done",
    "hooks_green": "the latest verify__run was green and nothing changed after it",
    "hooks_green_nothing_to_promote": "verify__run is green and there is nothing to promote",
    "promoted": "promote__run promoted the change and post-promotion probes passed",
    "promotion_rolled_back": "promotion was rolled back (fix and verify again)",
    "completed": "task__complete accepted the final report",
}


class PlanGraph:
    def __init__(self, data: dict):
        self.data = data
        err = self.validate()
        if err:
            raise ValueError(f"invalid plan graph: {err}")

    @classmethod
    def load(cls, path: Path | None = None) -> "PlanGraph":
        return cls(json.loads((path or PACKAGE_DIR / "plans" / "default.json").read_text()))

    @property
    def start(self) -> str:
        return self.data["start"]

    @property
    def states(self) -> dict[str, dict]:
        return self.data["states"]

    def validate(self) -> str | None:
        st = self.data.get("states", {})
        if self.data.get("start") not in st:
            return "start state missing"
        if not any(s.get("terminal") for s in st.values()):
            return "no terminal state"
        for name, s in st.items():
            for n in s.get("next", []):
                if n not in st:
                    return f"{name} → unknown state {n}"
            g = s.get("exit_guard")
            guards = g.values() if isinstance(g, dict) else [g]
            for x in guards:
                if x is not None and x not in GUARD_TEXT:
                    return f"{name}: unknown guard {x}"
            if s.get("terminal") and s.get("next"):
                return f"terminal state {name} has outgoing edges"
        return None

    def guard_for(self, frm: str, to: str) -> str | None:
        g = self.states[frm].get("exit_guard")
        return g.get(to) if isinstance(g, dict) else g


Verifier = Callable[[Task], tuple[bool, list[dict]]]
Promoter = Callable[[Task], dict]


class Lock:
    label = "procedural lock"

    def __init__(self, registry: ToolRegistry, graph: PlanGraph | None = None, *,
                 verifier: Verifier | None = None, final_check: Verifier | None = None,
                 promoter: Promoter | None = None, freeze: Callable[[Task, Baseline], dict] | None = None,
                 events: EventBus | None = None, system_prompt: str = "",
                 digest_extra: Callable[[Task], str] | None = None, audit_every: int = 5,
                 idle_turns: int = 3, fix_attempts: int = 3,
                 on_checkpoint: Callable[[Task], None] | None = None):
        self.registry = registry
        self.graph = graph or PlanGraph.load()
        self.verifier = verifier or (lambda t: (True, []))
        self.final_check = final_check or self.verifier
        self.promoter = promoter or (lambda t: {"status": "skipped", "reason": "no promoter"})
        self.freeze = freeze or (lambda t, b: b.freeze(Path(t.workdir)))
        self.events = events or EventBus()
        self.system_prompt = system_prompt
        self.digest_extra = digest_extra
        self.audit_every = audit_every
        self.idle_turns = idle_turns
        self.fix_attempts = fix_attempts
        self.on_checkpoint = on_checkpoint or (lambda t: None)

    # ================================================================== state helpers
    def _ls(self, task: Task) -> dict:
        ls = task.state.setdefault("lock", {"state": self.graph.start, "graph": self.graph.data["id"],
                                            "graph_version": self.graph.data["version"], "history": [],
                                            "idle": 0})
        return ls

    def state(self, task: Task) -> str:
        return self._ls(task)["state"]

    def visible_tools(self, task: Task) -> set[str]:
        spec = self.graph.states[self.state(task)].get("tools", [])
        out: set[str] = set()
        for t in self.registry.all():
            core = t.source == "core" or t.source.startswith("core:") and not t.source.startswith("core:skill")
            for e in spec:
                if e == "<read>" and t.read_only and not core:
                    out.add(t.name)
                elif e == "<all>" and not core:
                    out.add(t.name)
                elif e == t.name or ("*" in e and fnmatch.fnmatchcase(t.name, e)):
                    out.add(t.name)
        return out

    # ================================================================== enforcement point 1
    def __call__(self, task: Task, call: ToolCall, tool: Tool | None) -> Verdict:
        st = self.state(task)
        if self.graph.states[st].get("terminal"):
            return Verdict(False, f"state {st} is terminal; no further actions")
        if call.name not in self.visible_tools(task):
            nxt = self.graph.states[st].get("next", [])
            return Verdict(False, f"{call.name} is not allowed in state '{st}' "
                                  f"({self.graph.states[st].get('purpose', '')}). Next legal state(s): {nxt}; "
                                  f"leave '{st}' when {self._guard_text(st, nxt)}.")
        return ALLOW

    def _guard_text(self, st: str, nxt: list[str]) -> str:
        parts = []
        for n in nxt:
            g = self.graph.guard_for(st, n)
            parts.append(f"→{n}: {GUARD_TEXT.get(g, 'always')}" if g else f"→{n}: always")
        return "; ".join(parts)

    # ================================================================== guards
    def check_guard(self, task: Task, guard: str | None) -> tuple[bool, str]:
        if guard is None:
            return True, ""
        s = task.state
        if guard == "intent_recorded":
            ok = bool(s.get("intent")) and bool(task.acceptance)
        elif guard == "grounded":
            pack = s.get("pack")
            ok = bool(pack and pack.decisions)
        elif guard == "baseline_frozen":
            b = s.get("baseline")
            ok = b is not None and (bool(b.frozen) or b.dimensions == [])
        elif guard == "plan_done":
            plan = s.get("plan") or {}
            ok = bool(plan.get("subtasks")) and all(x["status"] == "done" for x in plan["subtasks"])
        elif guard in ("hooks_green", "hooks_green_nothing_to_promote"):
            lv = s.get("last_verify")
            ok = bool(lv and lv["green"] and not self._mutated_since(task, lv["ledger_len"]))
            if ok and guard == "hooks_green_nothing_to_promote":
                ok = not s.get("mutates", True) or not s.get("has_changes", True)
        elif guard == "promoted":
            ok = (s.get("promotion") or {}).get("status") == "promoted"
        elif guard == "promotion_rolled_back":
            ok = (s.get("promotion") or {}).get("status") in ("rolled_back", "failed")
        elif guard == "completed":
            ok = bool(s.get("completed"))
        else:
            return False, f"unknown guard {guard}"
        return ok, "" if ok else f"exit condition not met: {GUARD_TEXT[guard]}"

    def _mutated_since(self, task: Task, idx: int) -> bool:
        for row in task.ledger[idx:]:
            t = self.registry.get(row["tool"])
            if t and t.is_mutating and t.source != "core" and not row.get("blocked") and not row.get("is_error"):
                return True
        return False

    # ================================================================== enforcement point 2
    def transition(self, task: Task, to: str) -> tuple[bool, str]:
        frm = self.state(task)
        if to not in self.graph.states[frm].get("next", []):
            return False, f"illegal transition {frm} → {to}; legal: {self.graph.states[frm].get('next')}"
        ok, why = self.check_guard(task, self.graph.guard_for(frm, to))
        if not ok:
            return False, f"cannot leave {frm} for {to}: {why}"
        ls = self._ls(task)
        ls["state"] = to
        ls["history"].append({"from": frm, "to": to, "t": time.time(), "step": task.steps})
        if to == "execute" and frm == "verify":
            task.state["new_context"] = True
        self.events.emit("state", task=task.id, **{"from": frm, "to": to})
        self.on_checkpoint(task)
        return True, f"{frm} → {to}"

    # ================================================================== enforcement point 3
    def end_of_turn(self, task: Task, reply: Reply) -> tuple[bool, str | None]:
        st = self.state(task)
        if self.graph.states[st].get("terminal"):
            return True, None
        ls = self._ls(task)
        ls["idle"] = ls.get("idle", 0) + 1
        if ls["idle"] > self.idle_turns:
            task.status = "failed"
            task.result = (f"Stopped: the agent ended {ls['idle']} turns in a row in state '{st}' "
                           f"without meeting its exit condition ({self._guard_text(st, self.graph.states[st]['next'])}).")
            return True, None
        nxt = self.graph.states[st].get("next", [])
        return False, (f"PROCEDURAL LOCK: you are in state '{st}' — {self.graph.states[st].get('purpose', '')} "
                       f"You are not done. Legal next state(s): {nxt}; {self._guard_text(st, nxt)}. "
                       "Use plan__advance(to=...) once the condition holds; the task only ends when "
                       "task__complete succeeds in 'report'.")

    def reset_idle(self, task: Task, call: ToolCall, tool: Tool | None, res: ToolResult) -> None:
        self._ls(task)["idle"] = 0

    # ================================================================== Manage-Execute-Audit
    def digest(self, task: Task) -> str:
        s = task.state
        st = self.state(task)
        lines = [f"STATE DIGEST (shared externalized state; you are one agent continuing one plan)",
                 f"goal: {task.goal[:600]}", f"lock state: {st} — {self.graph.states[st].get('purpose', '')}",
                 f"intent: {s.get('intent', '')[:600]}",
                 "acceptance criteria: " + json.dumps([c.get("name") for c in task.acceptance]),
                 f"scope: {json.dumps(s.get('scope', {}))[:400]}"]
        pack = s.get("pack")
        if pack:
            lines.append(pack.summary()[:2500])
        plan = s.get("plan")
        if plan:
            lines.append(f"plan v{plan['version']}: " + "; ".join(
                f"[{x['status']}] {x['id']} {x['title']}" for x in plan["subtasks"]))
        lv = s.get("last_verify")
        if lv:
            lines.append("last verify: " + ("GREEN" if lv["green"] else "RED") + " " + json.dumps(
                [{"name": r["name"], "ok": r["ok"]} for r in lv["results"]])[:600])
        lines.append(f"steps used: {task.steps}; mutations since last gate: {s.get('mutations_since_gate', 0)}")
        if self.digest_extra:
            lines.append(self.digest_extra(task))
        cur = self.current_subtask(task)
        if cur:
            lines.append(f"CURRENT SUBTASK: {cur['id']} — {cur['title']}")
        return "\n".join(lines)

    def current_subtask(self, task: Task) -> dict | None:
        plan = task.state.get("plan") or {}
        return next((x for x in plan.get("subtasks", []) if x["status"] != "done"), None)

    def context_builder(self, task: Task) -> list[dict] | None:
        if not task.state.pop("new_context", False):
            return None
        task.subtask_steps = 0
        user: Any = task.goal
        if task.images:
            user = [{"type": "text", "text": task.goal}] + [
                {"type": "image_url", "image_url": {"url": f"data:{i['mime']};base64,{i['data']}"}}
                for i in task.images]
        self.events.emit("context", task=task.id, subtask=(self.current_subtask(task) or {}).get("id"))
        return [{"role": "system", "content": self.system_prompt},
                {"role": "user", "content": user},
                {"role": "user", "provenance": "harness", "content": self.digest(task)}]

    def audit(self, task: Task, call: ToolCall, tool: Tool | None, res: ToolResult) -> None:
        if task.steps == 0 or task.steps % self.audit_every:
            return
        problems = []
        for row in task.ledger:                     # invariant: mutations happen only in execute
            t = self.registry.get(row["tool"])
            if t and t.is_mutating and t.source != "core" and not row.get("blocked") \
                    and row.get("lock_state") not in (None, "execute"):
                problems.append(f"mutating {row['tool']} ran in state {row.get('lock_state')}")
        crit = []
        if task.acceptance:
            crit = [{"name": r.name, "ok": r.ok} for r in run_checks(task.acceptance, task.workdir, task.ledger)]
        report = {"step": task.steps, "state": self.state(task), "invariants_ok": not problems,
                  "problems": problems, "criteria": crit}
        task.state.setdefault("audits", []).append(report)
        self.events.emit("audit", task=task.id, **report)
        res.text += ("\n\n[AUDIT step %d] state=%s invariants=%s criteria=%s" %
                     (task.steps, report["state"], "ok" if not problems else problems,
                      ", ".join(f"{c['name']}={'pass' if c['ok'] else 'fail'}" for c in crit) or "n/a"))
        if problems:
            task.status, task.result = "failed", "Audit found a lock invariant violation: " + "; ".join(problems)

    def stamp(self, task: Task, call: ToolCall, tool: Tool | None, res: ToolResult) -> None:
        if task.ledger:
            task.ledger[-1]["lock_state"] = self.state(task)

    # ================================================================== core tools
    def tools(self, get_task: Callable[[], Task]) -> list[Tool]:
        s = {"type": "string"}

        def advance(a: dict) -> ToolResult:
            ok, msg = self.transition(get_task(), a["to"])
            return ToolResult(msg if ok else f"REFUSED: {msg}", is_error=not ok)

        def status(a: dict) -> ToolResult:
            return ToolResult(self.digest(get_task()))

        def intent_set(a: dict) -> ToolResult:
            t = get_task()
            intent = (a.get("intent") or "").strip()
            if len(intent) < 20:
                return ToolResult("REJECTED: write one paragraph saying what done means", is_error=True)
            crit = a.get("criteria") or []
            if not crit:
                return ToolResult("REJECTED: at least one machine-checkable criterion is required", is_error=True)
            names = set()
            for i, c in enumerate(crit):
                c.setdefault("name", f"criterion_{i + 1}")
                err = validate_check(c)
                if err:
                    return ToolResult(f"REJECTED: criterion {c['name']!r} is not machine-checkable: {err}. "
                                      "Prose is not a criterion.", is_error=True)
                if c["name"] in names:
                    return ToolResult(f"REJECTED: duplicate criterion name {c['name']}", is_error=True)
                names.add(c["name"])
            mutates = bool(a.get("mutates", True))
            scope = a.get("scope") or {}
            if mutates and not scope.get("paths"):
                return ToolResult("REJECTED: a mutating task must declare scope.paths (globs relative to the "
                                  "target) so out-of-scope changes can be detected", is_error=True)
            t.acceptance = crit
            t.state.update(intent=intent, scope=scope, mutates=mutates,
                           domain=a.get("domain", "general"), task_type=a.get("task_type", "general"),
                           assumptions=a.get("assumptions", []))
            self.on_checkpoint(t)
            return ToolResult(json.dumps({"recorded": True, "criteria": sorted(names), "mutates": mutates}))

        def plan_set(a: dict) -> ToolResult:
            t = get_task()
            subs = a.get("subtasks") or []
            if not subs or not all(isinstance(x, str) and x.strip() for x in subs):
                return ToolResult("REJECTED: subtasks must be a non-empty list of strings", is_error=True)
            old = t.state.get("plan")
            done = {x["title"] for x in (old or {}).get("subtasks", []) if x["status"] == "done"}
            plan = {"version": (old or {}).get("version", 0) + 1, "set_at": time.time(),
                    "subtasks": [{"id": f"S{i + 1}", "title": x.strip(),
                                  "status": "done" if x.strip() in done else "todo"} for i, x in enumerate(subs)]}
            t.state.setdefault("plan_history", []).append(plan)
            t.state["plan"] = plan
            t.state["new_context"] = True
            self.on_checkpoint(t)
            return ToolResult(json.dumps(plan))

        def step_done(a: dict) -> ToolResult:
            t = get_task()
            cur = self.current_subtask(t)
            if not cur:
                return ToolResult("REJECTED: no open subtask", is_error=True)
            if a.get("id") and a["id"] != cur["id"]:
                return ToolResult(f"REJECTED: subtasks complete in order; the open one is {cur['id']}",
                                  is_error=True)
            lv = t.state.get("last_verify")
            if t.state.get("mutates", True):
                if self._mutated_since(t, (lv or {}).get("ledger_len", 0)):
                    return ToolResult("REJECTED: changes since the last verification gate; run verify__run first",
                                      is_error=True)
                if lv is not None and not lv["green"]:
                    return ToolResult("REJECTED: the last verify__run was RED; a subtask is done only when the "
                                      "hooks are green", is_error=True)
            cur["status"] = "done"
            cur["evidence"] = a.get("evidence", "")
            t.state["new_context"] = True                 # next subtask starts in a fresh context
            self.on_checkpoint(t)
            nxt = self.current_subtask(t)
            return ToolResult(f"{cur['id']} done. " + (f"Next: {nxt['id']} {nxt['title']}" if nxt else
                                                       "All subtasks done; advance to verify."))

        def baseline_define(a: dict) -> ToolResult:
            t = get_task()
            dims = []
            try:
                for d in a.get("dimensions") or []:
                    dims.append(Dimension(name=d["name"], check=d["check"], direction=d.get("direction", "pass"),
                                          tolerance=float(d.get("tolerance", 0)),
                                          abs_tolerance=float(d.get("abs_tolerance", 0))))
            except (KeyError, TypeError) as e:
                return ToolResult(f"REJECTED: malformed dimension: {e}", is_error=True)
            root = Path(t.state.get("sandbox_root", t.workdir))
            if not dims and any(root.iterdir()):
                return ToolResult("REJECTED: the target is not empty, so it has a known-good state to protect; "
                                  "define at least one dimension (tests, build, probes, metrics)", is_error=True)
            b = Baseline(id=f"bl-{t.id}", target=str(t.state.get("real_target", t.workdir)), dimensions=dims)
            err = b.validate()
            if err:
                return ToolResult(f"REJECTED: {err}", is_error=True)
            frozen = self.freeze(t, b) if dims else {}
            t.state["baseline"] = b
            self.on_checkpoint(t)
            return ToolResult(json.dumps({"frozen": frozen, "dimensions": len(dims)}))

        def verify(a: dict) -> ToolResult:
            t = get_task()
            green, results = self.verifier(t)
            t.state["last_verify"] = {"green": green, "results": results, "ledger_len": len(t.ledger),
                                      "t": time.time()}
            t.state.setdefault("verify_log", []).append({"t": time.time(), "green": green, "results": results})
            t.state["mutations_since_gate"] = 0
            streak = t.state.setdefault("red_streak", {})
            for r in results:
                streak[r["name"]] = 0 if r["ok"] else streak.get(r["name"], 0) + 1
                if not r["ok"]:
                    t.hook_failures[r["name"]] = t.hook_failures.get(r["name"], 0) + 1
            worst = max(streak.values(), default=0)
            if worst > self.fix_attempts:
                bad = [r for r in results if not r["ok"]]
                t.status = "failed"
                t.result = (f"Stopped after {self.fix_attempts} failed fix attempts. Still red: "
                            + "; ".join(f"{r['name']}: {str(r.get('detail', ''))[:200]}" for r in bad)
                            + ". Needed: a fix for the hook above, or a corrected acceptance criterion.")
            self.on_checkpoint(t)
            self.events.emit("verify", task=t.id, ok=green, results=results)
            head = "GREEN" if green else "RED"
            return ToolResult(f"VERIFY {head}\n" + json.dumps(results, indent=1)[:6000], is_error=not green)

        def promote(a: dict) -> ToolResult:
            t = get_task()
            ok, why = self.check_guard(t, "hooks_green")
            if not ok:
                return ToolResult(f"REFUSED: {why}", is_error=True)
            rep = self.promoter(t)
            t.state["promotion"] = rep
            self.on_checkpoint(t)
            return ToolResult(json.dumps(rep, default=str)[:6000], is_error=rep.get("status") != "promoted")

        def complete(a: dict) -> ToolResult:
            t = get_task()
            summary = (a.get("summary") or "").strip()
            if not summary:
                return ToolResult("REJECTED: write the past-tense report", is_error=True)
            green, results = self.final_check(t)
            t.state.setdefault("verify_log", []).append({"t": time.time(), "green": green, "results": results})
            self.events.emit("verify", task=t.id, ok=green, results=results, final=True)
            if not green:
                for r in results:
                    if not r["ok"]:
                        t.hook_failures[r["name"]] = t.hook_failures.get(r["name"], 0) + 1
                return ToolResult("REFUSED: stop hooks are red; you cannot declare done.\n"
                                  + json.dumps([r for r in results if not r["ok"]], indent=1), is_error=True)
            t.state["completed"] = True
            self.transition(t, "done")
            t.status, t.result = "done", summary
            return ToolResult("Task complete; hooks green.")

        arr = {"type": "array", "items": {"type": "object"}}
        return [
            Tool("plan__advance", "Move the procedural lock to the next state (only legal edges, only when the "
                 "exit condition holds).", obj({"to": s}), advance, read_only=True, mutating=False),
            Tool("plan__status", "Show the lock state, plan, criteria and budgets.", obj({}, []), status,
                 read_only=True, mutating=False),
            Tool("intent__set", "Record what done means: intent paragraph, machine-checkable criteria "
                 "(each {name, kind: command|file_exists|file_contains|http|tcp|metric|ledger_ran, ...}), "
                 "scope {paths: [globs], hosts: []}, domain, task_type, mutates, assumptions.",
                 obj({"intent": s, "criteria": arr, "scope": {"type": "object"}, "domain": s, "task_type": s,
                      "mutates": {"type": "boolean"}, "assumptions": {"type": "array", "items": s}},
                     ["intent", "criteria"]), intent_set, read_only=True, mutating=False),
            Tool("plan__set", "Set (or revise) the ordered list of subtasks. Versioned.",
                 obj({"subtasks": {"type": "array", "items": s}}), plan_set, read_only=True, mutating=False),
            Tool("plan__step_done", "Mark the open subtask done (in order; requires verification after changes).",
                 obj({"id": s, "evidence": s}, ["evidence"]), step_done, read_only=True, mutating=False),
            Tool("baseline__define", "Define and freeze the regression baseline: dimensions "
                 "[{name, check, direction: pass|higher|lower, tolerance}].",
                 obj({"dimensions": arr, "reason": s}, ["dimensions"]), baseline_define,
                 read_only=True, mutating=False),
            Tool("verify__run", "Run every stop hook now (acceptance criteria, regression baseline, scope, "
                 "ledger). Resets the mutation budget.", obj({}, []), verify, read_only=True, mutating=False),
            Tool("promote__run", "Promote the verified sandbox to the real target (snapshot, apply, probe, "
                 "automatic rollback on red).", obj({}, []), promote, read_only=True, mutating=False),
            Tool("task__complete", "Finish: give the past-tense report. Refused while any hook is red.",
                 obj({"summary": s}), complete, read_only=True, mutating=False),
        ]
