"""Checkpoint task state to disk after every transition and tool call, so a task interrupted by
quitting (or a crash) resumes from where it was. Working memory is rebuilt from this file."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .baseline import Baseline
from .harness import Task
from .knowledge import KnowledgePack

TRANSIENT = {"on_decision", "recent_tools"}


def _state_out(state: dict) -> dict:
    out: dict[str, Any] = {}
    for k, v in state.items():
        if k in TRANSIENT or callable(v):
            continue
        if isinstance(v, KnowledgePack):
            out[k] = {"__pack__": v.as_dict()}
        elif isinstance(v, Baseline):
            out[k] = {"__baseline__": v.as_dict()}
        elif isinstance(v, set):
            out[k] = {"__set__": sorted(v)}
        else:
            out[k] = v
    return out


def _state_in(state: dict) -> dict:
    out: dict[str, Any] = {}
    for k, v in state.items():
        if isinstance(v, dict) and "__pack__" in v:
            out[k] = KnowledgePack.from_dict(v["__pack__"])
        elif isinstance(v, dict) and "__baseline__" in v:
            out[k] = Baseline.from_dict(v["__baseline__"])
        elif isinstance(v, dict) and "__set__" in v:
            out[k] = set(v["__set__"])
        else:
            out[k] = v
    return out


def save_task(task: Task, path: Path) -> None:
    data = {"id": task.id, "goal": task.goal, "workdir": str(task.workdir), "images": task.images,
            "acceptance": task.acceptance, "ledger": task.ledger, "messages": task.messages,
            "steps": task.steps, "subtask_steps": task.subtask_steps, "tokens": task.tokens,
            "started": task.started, "hook_failures": task.hook_failures, "status": task.status,
            "result": task.result, "state": _state_out(task.state)}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, default=str))
    os.replace(tmp, path)                                 # atomic: a crash never leaves half a file


def load_task(path: Path) -> Task:
    d = json.loads(path.read_text())
    t = Task(goal=d["goal"], workdir=Path(d["workdir"]), id=d["id"], images=d.get("images", []),
             acceptance=d["acceptance"], ledger=d["ledger"], messages=d["messages"], steps=d["steps"],
             subtask_steps=d.get("subtask_steps", 0), tokens=d.get("tokens", 0),
             hook_failures=d.get("hook_failures", {}), status=d["status"], result=d.get("result", ""),
             state=_state_in(d.get("state", {})))
    t.started = d.get("started", t.started)
    return t


def unfinished(tasks_dir: Path) -> list[Path]:
    out = []
    if not tasks_dir.exists():
        return out
    for p in sorted(tasks_dir.glob("*/state.json")):
        try:
            if json.loads(p.read_text()).get("status") == "running":
                out.append(p)
        except (json.JSONDecodeError, OSError):
            continue
    return out
