"""Project notebooks. Jobs that last weeks (the snow bot, the P10 map, an accounting helper for work)
keep one notebook each: the goal, phases, parts list, decisions and why, what was printed and tested,
what's next, and every job that worked on it.

"Work on the snow bot" opens the notebook with a Continue button. Any request that names a project
("snow bot: design the motor mount") becomes one of its jobs: the notebook goes into the model's
context, a build works in the project's own folder, and the job is written into the notebook when it
ends. A project whose name or goal looks private (accounting for work, passwords …) keeps every job
on this Mac.

Notebooks live in ~/.wright/projects/<id>/notebook.json: Wright's own memory, not the project folder,
so a promotion can never overwrite one. Only Wright's code writes them; the model changes a notebook
through notebook__update, which checks and caps every field."""
from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

from .tools import Tool, ToolResult

PHASE_STATUS = ("todo", "doing", "done")
PART_STATUS = ("need", "ordered", "have", "not needed")
LEAD = re.compile(r"^(the|my|our|a|an)\s+", re.I)
TAIL = re.compile(r"\s+(project|notebook)$", re.I)
NEW = re.compile(r"^\s*(?:/project\s+new\s+|(?:start|create|make|begin|open|set ?up)\s+(?:a\s+|an\s+|my\s+)?"
                 r"(?:new\s+)?project(?:\s*:|\s+called|\s+named|(?=\s+(?:for|to|about)\b)|\s*$)\s*|"
                 r"new\s+project\b\s*:?\s*)(?P<rest>.*)$", re.I | re.S)


def _slug(text: str, n: int = 40) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:n].strip("-") or "project"


def norm(text: str) -> str:
    """Lowercase words only, without a leading "the/my/a" or a trailing "project": how names compare."""
    s = re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()
    s = LEAD.sub("", s)
    return TAIL.sub("", s).strip()


def _cap(v: Any, n: int) -> str:
    return re.sub(r"\s+", " ", str(v if v is not None else "")).strip()[:n]


def _day(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts))


def parse_new(text: str) -> tuple[str, str] | None:
    """"New project: snow bot — clears the driveway" → ("Snow bot", "clears the driveway").
    None when the text isn't a request to start a project."""
    m = NEW.match(text or "")
    if not m:
        return None
    rest = m.group("rest").strip().strip(".!")
    if not rest:
        return "", ""
    goal = ""
    for sep in (" — ", " – ", " - ", ": ", "; ", ". ", ", "):
        if sep in rest:
            rest, goal = (x.strip() for x in rest.split(sep, 1))
            break
    else:
        m2 = re.match(r"^(to|for|about)\s+(.+)$", rest, re.I)
        if m2:                                        # "start a project to build a snow bot": the goal names it
            goal = m2.group(2)
            rest = re.split(r"\s+(?:for|that|which|so|to|with)\s+|,", goal, maxsplit=1)[0]
            short = re.sub(r"^(build|make|create|design|write|set ?up|program|code)\s+(?:(?:a|an|the|my|our)\s+)?", "",
                           rest, flags=re.I)
            rest = " ".join((short if len(short.split()) >= 2 else rest).split()[:5])
    name = TAIL.sub("", LEAD.sub("", rest)).strip(" .:-")
    name = name[:1].upper() + name[1:60]
    return name, goal[:600]


class Projects:
    def __init__(self, root: Path, folders: Path):
        self.root = Path(root)
        self.folders = Path(folders).expanduser()
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ storage
    def _file(self, pid: str) -> Path:
        return self.root / pid / "notebook.json"

    def get(self, pid: str | None) -> dict | None:
        if not pid or not re.fullmatch(r"[a-z0-9-]{1,48}", pid):
            return None
        try:
            return json.loads(self._file(pid).read_text())
        except (OSError, ValueError):
            return None

    def all(self) -> list[dict]:
        out = []
        for f in self.root.glob("*/notebook.json"):
            try:
                out.append(json.loads(f.read_text()))
            except (OSError, ValueError):
                continue
        return sorted(out, key=lambda p: p.get("updated", 0), reverse=True)

    def save(self, p: dict) -> dict:
        p["updated"] = time.time()
        f = self._file(p["id"])
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps(p, indent=1))
        os.replace(tmp, f)                            # atomic: a crash never leaves half a notebook
        return p

    def create(self, name: str, goal: str = "", private: bool | None = None) -> dict:
        from .dispatch import PRIVATE
        name = _cap(name, 60) or "New project"
        with self._lock:
            existing = self.find(name)
            if existing and norm(existing["name"]) == norm(name):
                return existing
            base = _slug(norm(name) or name, 40)
            pid, n = base, 2
            while self._file(pid).exists():
                pid, n = f"{base}-{n}", n + 1
            now = time.time()
            p = {"id": pid, "name": name, "goal": _cap(goal, 600),
                 "private": bool(PRIVATE.search(f"{name} {goal}")) if private is None else bool(private),
                 "folder": str(self.folders / pid), "status": "active", "created": now, "updated": now,
                 "phases": [], "parts": [], "decisions": [], "tests": [], "next": [], "notes": [], "jobs": []}
            return self.save(p)

    # ------------------------------------------------------------------ finding a project by name
    def mentioned(self, text: str) -> dict | None:
        """The project whose whole name appears in the text (longest name wins)."""
        hay = f" {re.sub(r'[^a-z0-9]+', ' ', (text or '').lower())} "
        best, size = None, 0
        for p in self.all():
            key = norm(p["name"])
            if key and (f" {key} " in hay or f" {key}s " in hay) and len(key) > size:
                best, size = p, len(key)
        return best

    def find(self, query: str) -> dict | None:
        """For "work on the snow bot": the exact name, a name in the text, or one unique project whose
        name contains every word asked for ("snow" → "Snow bot")."""
        q = norm(query)
        if not q:
            return None
        projects = self.all()
        for p in projects:
            if norm(p["name"]) == q:
                return p
        hit = self.mentioned(query)
        if hit:
            return hit
        words = [w for w in q.split() if len(w) >= 3]
        if not words:
            return None
        cands = [p for p in projects if all(w in norm(p["name"]).split() for w in words)]
        return cands[0] if len(cands) == 1 else None

    # ------------------------------------------------------------------ changes
    def update(self, pid: str, ch: dict) -> tuple[dict | None, str]:
        """Apply checked changes; returns (notebook, what changed in plain words)."""
        with self._lock:
            p = self.get(pid)
            if p is None:
                return None, "no such project"
            now, said = time.time(), []
            if ch.get("goal"):
                p["goal"] = _cap(ch["goal"], 600)
                said.append("goal")
            n = 0
            for d in (ch.get("decisions") or [])[:10]:
                what, why = _cap((d or {}).get("what"), 300), _cap((d or {}).get("why"), 500)
                if what:
                    p["decisions"].append({"t": now, "what": what, "why": why or "(no reason given)"})
                    n += 1
            if n:
                said.append(f"{n} decision{'s' * (n > 1)}")
            n = 0
            for x in (ch.get("parts") or [])[:30]:
                item = _cap((x or {}).get("item"), 120)
                if not item:
                    continue
                status = (x.get("status") or "need").lower()
                row = {"item": item, "qty": _cap(x.get("qty"), 20), "status": status if status in PART_STATUS else "need",
                       "note": _cap(x.get("note"), 200)}
                old = next((r for r in p["parts"] if r["item"].lower() == item.lower()), None)
                if old:
                    old.update({k: v for k, v in row.items() if v or k == "status"})
                else:
                    p["parts"].append(row)
                n += 1
            if n:
                said.append(f"{n} part{'s' * (n > 1)}")
            n = 0
            for x in (ch.get("tests") or [])[:10]:
                what = _cap((x or {}).get("what"), 300)
                if what:
                    ok = x.get("passed")
                    p["tests"].append({"t": now, "what": what, "result": _cap(x.get("result"), 400),
                                       "passed": ok if isinstance(ok, bool) else None})
                    n += 1
            if n:
                said.append(f"{n} test{'s' * (n > 1)}")
            n = 0
            for x in (ch.get("phases") or [])[:20]:
                name = _cap((x or {}).get("name"), 120)
                if not name:
                    continue
                status = (x.get("status") or "todo").lower()
                status = status if status in PHASE_STATUS else "todo"
                old = next((r for r in p["phases"] if r["name"].lower() == name.lower()), None)
                if old:
                    old["status"] = status
                else:
                    p["phases"].append({"name": name, "status": status})
                n += 1
            if n:
                said.append(f"{n} phase{'s' * (n > 1)}")
            if isinstance(ch.get("next"), list):
                p["next"] = [s for s in (_cap(x, 200) for x in ch["next"][:8]) if s]
                said.append("next steps")
            if ch.get("note"):
                p["notes"].append({"t": now, "text": _cap(ch["note"], 800)})
                said.append("a note")
            if ch.get("status") in ("active", "paused", "finished"):
                p["status"] = ch["status"]
                said.append(f"status {ch['status']}")
            if not said:
                return p, "nothing to change"
            self.save(p)
            return p, ", ".join(said)

    def record_job(self, pid: str, job: dict) -> None:
        with self._lock:
            p = self.get(pid)
            if p is None:
                return
            p["jobs"].append({"t": time.time(), **{k: job.get(k) for k in
                              ("task_id", "goal", "outcome", "mode", "seconds", "summary", "where")}})
            p["jobs"] = p["jobs"][-200:]
            self.save(p)

    # ------------------------------------------------------------------ what people and the model see
    def card(self, p: dict) -> dict:
        done = sum(1 for x in p["phases"] if x["status"] == "done")
        return {"id": p["id"], "name": p["name"], "goal": p["goal"][:160], "private": p.get("private", False),
                "next": (p["next"] or [""])[0], "phases": len(p["phases"]), "phases_done": done,
                "jobs": len(p["jobs"]), "updated": p.get("updated"), "status": p.get("status", "active")}

    def view(self, p: dict) -> dict:
        """The notebook for the window: everything current, the latest of the long histories."""
        home = str(Path.home())
        folder = "~" + p["folder"][len(home):] if p["folder"].startswith(home + "/") else p["folder"]
        return {**p, "decisions": p["decisions"][-20:], "tests": p["tests"][-12:], "notes": p["notes"][-6:],
                "jobs": p["jobs"][-8:], "card": self.card(p), "folder": folder}

    def cards(self, n: int = 8) -> list[dict]:
        return [self.card(p) for p in self.all() if p.get("status") != "finished"][:n]

    def render(self, p: dict, limit: int = 4000) -> str:
        """The notebook as the model reads it, newest history last, bounded."""
        out = [f"PROJECT NOTEBOOK — {p['name']} (this job is part of it; read it before you plan)",
               f"Goal: {p['goal'] or '(not written yet — ask or infer it, then save it with notebook__update)'}",
               f"Project folder: {p['folder']}" + (" · PRIVATE: stays on this Mac" if p.get("private") else "")]
        if p["phases"]:
            out.append("Phases: " + "; ".join(f"[{x['status']}] {x['name']}" for x in p["phases"]))
        if p["parts"]:
            out.append("Parts: " + "; ".join(
                f"{(x['qty'] + '× ') if x['qty'] else ''}{x['item']} ({x['status']}"
                f"{', ' + x['note'] if x['note'] else ''})" for x in p["parts"][:30]))
        if p["decisions"]:
            out.append("Decisions and why (latest last):")
            out += [f"  - {_day(d['t'])}: {d['what']} — why: {d['why']}" for d in p["decisions"][-12:]]
        if p["tests"]:
            out.append("Printed and tested (latest last):")
            out += [f"  - {_day(x['t'])}: {x['what']} → {x['result'] or '?'}"
                    + ("" if x["passed"] is None else " (passed)" if x["passed"] else " (failed)")
                    for x in p["tests"][-8:]]
        if p["notes"]:
            out.append("Notes: " + " | ".join(x["text"] for x in p["notes"][-4:]))
        if p["jobs"]:
            out.append("Earlier jobs: " + "; ".join(f"{_day(j['t'])} [{j.get('outcome')}] {(j.get('goal') or '')[:90]}"
                                                    for j in p["jobs"][-6:]))
        out.append("Next: " + ("; ".join(f"{i + 1}) {s}" for i, s in enumerate(p["next"])) if p["next"]
                               else "(nothing written yet)"))
        out.append("Keep this notebook current with notebook__update: every decision with its why, parts "
                   "needed or bought, what was printed or tested and how it went, phase changes. Before you "
                   "finish, set `next` to what should happen next time.")
        text = "\n".join(out)
        return text if len(text) <= limit else text[:limit - 1] + "…"


# ---------------------------------------------------------------------------------------------- tools
def notebook_tools(projects: Projects, get_task: Any) -> list[Tool]:
    s = {"type": "string"}

    def which(a: dict) -> tuple[dict | None, str]:
        task = get_task()
        pid = (task.state.get("project") if task else None)
        p = projects.find(a["project"]) if a.get("project") else projects.get(pid)
        if p is None:
            return None, ("This job isn't part of a project. Projects: "
                          + (", ".join(x["name"] for x in projects.all()) or "none yet"))
        where = ((task.state.get("dispatch") or {}).get("where") if task else None) or "this-mac"
        if p.get("private") and where != "this-mac":
            return None, f"“{p['name']}” is a private project; its notebook is only opened on this Mac."
        return p, ""

    def read(a: dict) -> ToolResult:
        p, err = which(a)
        return ToolResult(err, is_error=True) if p is None else ToolResult(projects.render(p))

    def update(a: dict) -> ToolResult:
        p, err = which(a)
        if p is None:
            return ToolResult(err, is_error=True)
        p, said = projects.update(p["id"], a)
        if said == "nothing to change":
            return ToolResult("Nothing was saved: give decisions, parts, tests, phases, next or note.", is_error=True)
        return ToolResult(f"Saved in the “{p['name']}” notebook: {said}.")

    def obj(props: dict, required: list[str]) -> dict:       # tools.obj treats [] as "all required"
        return {"type": "object", "properties": props, "required": required}

    item = lambda props, req: {"type": "array", "items": obj(props, req)}  # noqa: E731
    return [
        Tool("notebook__read", "Read a project's notebook: goal, phases, parts, decisions and why, tests, "
             "next steps, earlier jobs. Defaults to this job's project; `project` names another.",
             obj({"project": s}, []), read, source="core:notebook", read_only=True, mutating=False),
        Tool("notebook__update", "Record in this job's project notebook (it lasts across jobs and weeks). "
             "decisions: [{what, why}] — always say why; parts: [{item, qty, status: need|ordered|have|not "
             "needed, note}] (same item updates it); tests: [{what, result, passed}] for anything printed, "
             "built or tried; phases: [{name, status: todo|doing|done}]; next: the full list of next steps "
             "(replaces it); note: anything else worth keeping; goal: only if it was missing or changed.",
             obj({"decisions": item({"what": s, "why": s}, ["what", "why"]),
                  "parts": item({"item": s, "qty": s, "status": s, "note": s}, ["item"]),
                  "tests": item({"what": s, "result": s, "passed": {"type": "boolean"}}, ["what"]),
                  "phases": item({"name": s, "status": s}, ["name"]),
                  "next": {"type": "array", "items": s}, "note": s, "goal": s, "project": s}, []),
             update, source="core:notebook", mutating=False),
    ]
