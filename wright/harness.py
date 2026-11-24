"""The agent loop: observe → plan → act → verify, until the goal is verified complete.

The loop itself is small on purpose. Everything that decides whether something is *allowed*
(role contract, procedural lock, drift bounds, danger protocol) is a Gate; everything that decides
whether the task is *done* is a StopHook. Both live outside the model and outside this file.
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

from .events import EventBus
from .llm import Model, ModelUnavailable, Reply, ToolCall
from .tools import Tool, ToolRegistry, ToolResult


@dataclass
class Verdict:
    allow: bool
    reason: str = ""
    flag: str | None = None          # integrity flag shown in the UI even when allowed


ALLOW = Verdict(True)


@dataclass
class Task:
    goal: str
    workdir: Path
    id: str = field(default_factory=lambda: time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6])
    images: list[dict] = field(default_factory=list)
    acceptance: list[dict] = field(default_factory=list)     # machine-checkable criteria
    ledger: list[dict] = field(default_factory=list)         # what the harness actually executed
    messages: list[dict] = field(default_factory=list)
    steps: int = 0
    subtask_steps: int = 0
    tokens: int = 0
    started: float = field(default_factory=time.time)
    hook_failures: dict[str, int] = field(default_factory=dict)
    status: str = "running"                                  # running|done|failed|blocked
    result: str = ""
    state: dict[str, Any] = field(default_factory=dict)      # shared externalized state (lock etc.)


class Gate(Protocol):
    def __call__(self, task: Task, call: ToolCall, tool: Tool | None) -> Verdict: ...


StopHook = Callable[[Task], tuple[bool, list[dict]]]        # -> (green, results)
Observer = Callable[[Task, ToolCall, Tool | None, ToolResult], None]


class Harness:
    def __init__(self, model: Model, registry: ToolRegistry, events: EventBus | None = None, *,
                 system_prompt: str = "", gates: list[Gate] | None = None,
                 stop_hooks: list[StopHook] | None = None, observers: list[Observer] | None = None,
                 visible_tools: Callable[[Task], set[str] | None] | None = None,
                 context_builder: Callable[[Task], list[dict] | None] | None = None,
                 max_steps: int = 200, max_subtask_steps: int = 25, fix_attempts: int = 3,
                 wall_clock: float = 3600, max_tokens: int = 2_000_000):
        self.model = model
        self.registry = registry
        self.events = events or EventBus()
        self.system_prompt = system_prompt
        self.gates = list(gates or [])
        self.stop_hooks = list(stop_hooks or [])
        self.observers = list(observers or [])
        self.visible_tools = visible_tools or (lambda t: None)
        self.context_builder = context_builder
        self.max_steps = max_steps
        self.max_subtask_steps = max_subtask_steps
        self.fix_attempts = fix_attempts
        self.wall_clock = wall_clock
        self.max_tokens = max_tokens

    # ------------------------------------------------------------------ helpers
    def _user_message(self, task: Task) -> dict:
        if not task.images:
            return {"role": "user", "content": task.goal}
        parts: list[dict] = [{"type": "text", "text": task.goal}]
        for im in task.images:
            parts.append({"type": "image_url",
                          "image_url": {"url": f"data:{im['mime']};base64,{im['data']}"}})
        return {"role": "user", "content": parts}

    def start_messages(self, task: Task) -> list[dict]:
        return [{"role": "system", "content": self.system_prompt}, self._user_message(task)]

    def _budget_exceeded(self, task: Task) -> str | None:
        if task.steps >= self.max_steps:
            return f"step budget reached ({self.max_steps} tool steps)"
        if time.time() - task.started > self.wall_clock:
            return f"wall-clock budget reached ({self.wall_clock:.0f}s)"
        if task.tokens > self.max_tokens:
            return f"token budget reached ({self.max_tokens})"
        return None

    def execute_call(self, task: Task, call: ToolCall) -> ToolResult:
        tool = self.registry.get(call.name)
        self.events.emit("tool_call", task=task.id, tool=call.name, args=call.args, id=call.id)
        for gate in self.gates:
            v = gate(task, call, tool)
            if v.flag:
                self.events.emit("flag", task=task.id, tool=call.name, flag=v.flag)
            if not v.allow:
                self.events.emit("blocked", task=task.id, tool=call.name, reason=v.reason)
                task.ledger.append({"step": task.steps, "tool": call.name, "args": call.args,
                                    "blocked": v.reason, "t": time.time()})
                return ToolResult(f"BLOCKED by {getattr(gate, 'label', 'gate')}: {v.reason}",
                                  is_error=True, data={"blocked": True})
        if tool is None:
            res = ToolResult(f"unknown tool {call.name!r}; available: {self.registry.names()}",
                             is_error=True)
        else:
            try:
                res = tool.fn(call.args)
            except Exception as e:
                res = ToolResult(f"tool raised {type(e).__name__}: {e}", is_error=True)
        row = {"step": task.steps, "tool": call.name, "args": call.args, "t": time.time(),
               "is_error": res.is_error, **res.data}
        task.ledger.append(row)
        self.events.emit("tool_result", task=task.id, tool=call.name, id=call.id,
                         text=res.text[:4000], is_error=res.is_error,
                         images=len(res.images))
        for ob in self.observers:
            ob(task, call, tool, res)
        return res

    def _tool_message(self, call: ToolCall, res: ToolResult) -> list[dict]:
        # Tool output is DATA (provenance: tool). It goes in a tool-role message, never user role.
        msgs: list[dict] = [{"role": "tool", "tool_call_id": call.id, "name": call.name,
                             "content": res.text}]
        if res.images:
            parts: list[dict] = [{"type": "text",
                                  "text": f"[data from tool {call.name}: image attached]"}]
            for im in res.images:
                parts.append({"type": "image_url",
                              "image_url": {"url": f"data:{im['mime']};base64,{im['data']}"}})
            msgs.append({"role": "user", "content": parts, "provenance": f"tool:{call.name}"})
        return msgs

    def run_stop_hooks(self, task: Task) -> tuple[bool, list[dict]]:
        results: list[dict] = []
        green = True
        for hook in self.stop_hooks:
            ok, res = hook(task)
            results.extend(res)
            green = green and ok
        self.events.emit("verify", task=task.id, ok=green, results=results)
        return green, results

    # ------------------------------------------------------------------ the loop
    def run(self, task: Task) -> Task:
        self.events.emit("task_start", task=task.id, goal=task.goal[:500], workdir=str(task.workdir))
        if not task.messages:
            task.messages = self.start_messages(task)
        while task.status == "running":
            why = self._budget_exceeded(task)
            if why:
                task.status, task.result = "failed", f"Stopped: {why}."
                break
            if self.context_builder:                       # fresh bounded executor context
                fresh = self.context_builder(task)
                if fresh is not None:
                    task.messages = fresh
            visible = self.visible_tools(task)
            tools = self.registry.specs(visible)
            try:
                reply: Reply = self.model.complete(_wire(task.messages), tools)   # plan
            except ModelUnavailable as e:
                task.status, task.result = "blocked", f"Model setup defect: {e}"
                self.events.emit("setup_defect", task=task.id, detail=str(e))
                break
            task.tokens += int(reply.usage.get("total_tokens", 0))
            task.messages.append(reply.as_message())
            self.events.emit("assistant", task=task.id, text=reply.text, lane=reply.lane,
                             calls=[c.name for c in reply.tool_calls])
            if reply.tool_calls:
                for call in reply.tool_calls:                 # act
                    task.steps += 1
                    task.subtask_steps += 1
                    res = self.execute_call(task, call)
                    task.messages.extend(self._tool_message(call, res))   # observe
                    if task.status != "running":
                        break
                continue
            # End of turn without tool calls: the model is claiming it is done. Verify.
            green, results = self.run_stop_hooks(task)
            if green:
                task.status, task.result = "done", reply.text
                break
            failing = [r["name"] for r in results if not r["ok"]]
            for name in failing:
                task.hook_failures[name] = task.hook_failures.get(name, 0) + 1
            worst = max((task.hook_failures[n] for n in failing), default=0)
            if worst > self.fix_attempts:
                task.status = "failed"
                task.result = (f"Stopped after {self.fix_attempts} fix attempts. Still failing: "
                               + "; ".join(f"{r['name']}: {r['detail'][:200]}" for r in results
                                           if not r["ok"]))
                break
            task.messages.append({"role": "user", "provenance": "harness", "content":
                                  "STOP HOOKS FAILED — you are not done. Fix these and continue:\n"
                                  + json.dumps([r for r in results if not r["ok"]], indent=1)})
        self.events.emit("task_end", task=task.id, status=task.status, result=task.result[:1000],
                         steps=task.steps)
        return task


def _wire(messages: list[dict]) -> list[dict]:
    """Strip harness-only keys (provenance) before sending messages to the model API."""
    return [{k: v for k, v in m.items() if k != "provenance"} for m in messages]


def acceptance_hook(task: Task) -> tuple[bool, list[dict]]:
    from .checks import run_checks
    res = [r.as_dict() for r in run_checks(task.acceptance, task.workdir, task.ledger)]
    return all(r["ok"] for r in res), res
