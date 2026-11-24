"""Procedural memory: successful execution paths promoted to reusable skills, each shipped with its
own regression test. Skills are versioned; a skill whose test fails is demoted automatically (and
its tool disappears from the agent's tool list) until a fixed version passes again."""
from __future__ import annotations

import json
import re
import shutil
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from .checks import run_checks, validate_check

NAME = re.compile(r"^[a-z][a-z0-9_]{1,40}$")


@dataclass
class Skill:
    name: str
    description: str
    steps: list[dict]                      # [{"tool": "shell__run", "args": {... "{{param}}" ...}}]
    test: dict                             # {"params": {...}, "setup": [cmd...], "checks": [check...]}
    params: dict[str, dict] = field(default_factory=dict)   # name -> {"type": "string", "default": ...}
    domain: str = "general"
    task_type: str = "general"
    version: int = 1
    status: str = "active"                 # active | demoted
    history: list[dict] = field(default_factory=list)
    origin: str = ""                       # task id it was promoted from

    def validate(self) -> str | None:
        if not NAME.match(self.name):
            return "skill name must be lowercase snake_case"
        if not self.steps or not all(isinstance(s, dict) and "tool" in s for s in self.steps):
            return "a skill needs at least one step with a tool"
        if not self.test or not self.test.get("checks"):
            return "a skill must ship with a regression test that has checks"
        for c in self.test["checks"]:
            err = validate_check(c)
            if err:
                return f"test check invalid: {err}"
        return None

    def render(self, params: dict[str, Any]) -> list[dict]:
        vals = {k: (params.get(k, v.get("default"))) for k, v in self.params.items()}
        vals.update(params)
        out = []
        for s in self.steps:
            raw = json.dumps(s["args"])
            for k, v in vals.items():
                raw = raw.replace("{{" + k + "}}", json.dumps(str(v))[1:-1])
            if "{{" in raw:
                missing = re.findall(r"\{\{(\w+)\}\}", raw)
                raise ValueError(f"missing skill parameters: {missing}")
            out.append({"tool": s["tool"], "args": json.loads(raw)})
        return out


Executor = Callable[[str, dict, Path], tuple[bool, str]]   # (tool, args, workdir) -> (ok, text)


class SkillLibrary:
    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)

    def _dir(self, name: str) -> Path:
        return self.root / name

    def save(self, skill: Skill, note: str) -> Skill:
        err = skill.validate()
        if err:
            raise ValueError(err)
        d = self._dir(skill.name)
        d.mkdir(parents=True, exist_ok=True)
        versions = sorted(int(p.stem[1:]) for p in d.glob("v*.json"))
        skill.version = (versions[-1] + 1) if versions else 1
        skill.history = skill.history + [{"t": time.time(), "version": skill.version,
                                          "status": skill.status, "note": note}]
        (d / f"v{skill.version}.json").write_text(json.dumps(asdict(skill), indent=1))
        (d / "current").write_text(str(skill.version))
        return skill

    def get(self, name: str, version: int | None = None) -> Skill | None:
        d = self._dir(name)
        if not d.exists():
            return None
        v = version or int((d / "current").read_text())
        p = d / f"v{v}.json"
        return Skill(**json.loads(p.read_text())) if p.exists() else None

    def all(self) -> list[Skill]:
        return [s for s in (self.get(p.name) for p in sorted(self.root.iterdir()) if p.is_dir()) if s]

    def active(self) -> list[Skill]:
        return [s for s in self.all() if s.status == "active"]

    def rollback(self, name: str, version: int) -> Skill:
        d = self._dir(name)
        if not (d / f"v{version}.json").exists():
            raise ValueError(f"{name} has no version {version}")
        (d / "current").write_text(str(version))
        return self.get(name)  # type: ignore[return-value]

    # ------------------------------------------------------------------ run / test
    def run(self, skill: Skill, params: dict, workdir: Path, execute: Executor) -> tuple[bool, list[str]]:
        log = []
        for step in skill.render(params):
            ok, text = execute(step["tool"], step["args"], workdir)
            log.append(f"{step['tool']}: {'ok' if ok else 'FAILED'} {text[:200]}")
            if not ok:
                return False, log
        return True, log

    def test(self, skill: Skill, execute: Executor) -> tuple[bool, list[dict]]:
        tmp = Path(tempfile.mkdtemp(prefix=f"wright-skilltest-{skill.name}-"))
        try:
            for cmd in skill.test.get("setup", []):
                ok, text = execute("shell__run", {"command": cmd}, tmp)
                if not ok:
                    return False, [{"name": "setup", "ok": False, "detail": text[:300]}]
            ok, log = self.run(skill, skill.test.get("params", {}), tmp, execute)
            if not ok:
                return False, [{"name": "run", "ok": False, "detail": "; ".join(log)[:500]}]
            res = [r.as_dict() for r in run_checks(skill.test["checks"], tmp)]
            return all(r["ok"] for r in res), res
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def check_all(self, execute: Executor) -> dict[str, str]:
        """Run every skill's regression test; demote failures, re-activate fixed ones."""
        out = {}
        for s in self.all():
            ok, res = self.test(s, execute)
            if not ok and s.status == "active":
                s.status = "demoted"
                failing = [r["name"] for r in res if not r["ok"]]
                self.save(s, f"auto-demoted: regression test failed ({', '.join(failing)})")
                out[s.name] = "demoted"
            elif ok and s.status == "demoted":
                out[s.name] = "still-demoted"   # re-activation only via a new version that passes
            else:
                out[s.name] = "ok" if ok else "demoted"
        return out

    def promote_from_task(self, task: Any, name: str, description: str, execute: Executor) -> tuple[Skill | None, str]:
        """Turn a green task's mutating execution path into a skill whose test is the task's own
        acceptance criteria, replayed in a fresh directory. Only saved if that test passes."""
        steps = [{"tool": r["tool"], "args": r["args"]} for r in task.ledger
                 if not r.get("blocked") and not r.get("is_error")
                 and r["tool"] not in ("verify__run",) and not r["tool"].startswith(
                     ("knowledge__", "memory__", "scope__", "plan__", "intent__", "task__", "danger__",
                      "baseline__", "self__", "skill__"))]
        if not steps:
            return None, "no executable steps to promote"
        skill = Skill(name=name, description=description, steps=steps,
                      test={"params": {}, "setup": task.state.get("skill_setup", []),
                            "checks": [c for c in task.acceptance if c.get("kind") != "ledger_ran"]},
                      domain=task.state.get("domain", "general"),
                      task_type=task.state.get("task_type", "general"), origin=task.id)
        err = skill.validate()
        if err:
            return None, err
        ok, res = self.test(skill, execute)
        if not ok:
            return None, "replay of the execution path failed its own test: " + json.dumps(res)[:300]
        return self.save(skill, f"promoted from task {task.id}"), "promoted"


def skill_tools(lib: SkillLibrary, execute: Executor, get_task: Callable[[], Any]) -> list:
    """Each active skill becomes a tool; its test runs before each use and demotes it on failure."""
    from .tools import Tool, ToolResult, obj

    tools = []
    for s in lib.active():
        def run(a: dict, _name: str = s.name) -> ToolResult:
            sk = lib.get(_name)
            if sk is None or sk.status != "active":
                return ToolResult(f"skill {_name} is demoted", is_error=True)
            ok, res = lib.test(sk, execute)
            if not ok:
                sk.status = "demoted"
                lib.save(sk, "auto-demoted: regression test failed before use")
                return ToolResult(f"skill {_name} failed its regression test and was demoted: "
                                  + json.dumps(res)[:400], is_error=True)
            ok, log = lib.run(sk, a, Path(get_task().workdir), execute)
            return ToolResult("\n".join(log), is_error=not ok)

        props = {k: {"type": v.get("type", "string")} for k, v in s.params.items()}
        tools.append(Tool(f"skill__{s.name}", f"[skill v{s.version}] {s.description}",
                          obj(props, []), run, source="core:skill", destructive=True))
    return tools
