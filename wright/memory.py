"""Episodic memory: an append-only, hash-chained, tamper-evident audit log of every task.

Only the harness appends (EpisodicLog.append is never exposed as a tool). The model reads it through
a read-only tool. Defences, in depth:
  1. no model tool can write/edit/delete it; the role contract blocks its path for fs and shell;
  2. files are chmod 0444 between appends; on macOS the shell runs under a sandbox profile that
     denies writes to it (see sandboxing.py);
  3. every entry is hash-chained; a head anchor and a mirror copy are kept; verify() runs at every
     session start and before every append, and repair() restores from the intact copy and records
     a tamper event.
Secrets are redacted before an entry is written.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

GENESIS = "0" * 64


def _canon(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def _hash(prev: str, seq: int, ts: float, entry: dict) -> str:
    return hashlib.sha256(f"{prev}|{seq}|{ts}|{_canon(entry)}".encode()).hexdigest()


@dataclass
class Verification:
    ok: bool
    count: int
    head: str
    problems: list[str]


class EpisodicLog:
    def __init__(self, root: Path, redact: Callable[[str], str] | None = None):
        self.root = root
        self.file = root / "log.jsonl"
        self.mirror = root / ".mirror" / "log.jsonl"
        self.anchor = root / ".anchor.json"
        self.redact = redact or (lambda s: s)
        self._lock = threading.Lock()
        root.mkdir(parents=True, exist_ok=True)
        self.mirror.parent.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ low level
    @staticmethod
    def _read(path: Path) -> list[dict]:
        if not path.exists():
            return []
        rows = []
        for line in path.read_text().splitlines():
            if line.strip():
                rows.append(json.loads(line))
        return rows

    @staticmethod
    def _check_chain(rows: list[dict]) -> tuple[bool, str, list[str]]:
        prev = GENESIS
        for i, r in enumerate(rows):
            if r.get("seq") != i:
                return False, prev, [f"entry {i}: sequence {r.get('seq')} out of order"]
            if r.get("prev") != prev:
                return False, prev, [f"entry {i}: chain broken (prev hash mismatch)"]
            if _hash(prev, r["seq"], r["ts"], r["entry"]) != r.get("hash"):
                return False, prev, [f"entry {i}: content altered (hash mismatch)"]
            prev = r["hash"]
        return True, prev, []

    def _anchor(self) -> dict:
        if self.anchor.exists():
            return json.loads(self.anchor.read_text())
        return {"count": 0, "head": GENESIS}

    def _write_protected(self, path: Path, text: str) -> None:
        if path.exists():
            os.chmod(path, 0o644)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(text)
        os.replace(tmp, path)
        os.chmod(path, 0o444)

    # ------------------------------------------------------------------ public
    def verify(self) -> Verification:
        rows = self._read(self.file)
        ok, head, problems = self._check_chain(rows)
        anc = self._anchor()
        if ok and (len(rows) != anc["count"] or head != anc["head"]):
            ok = False
            problems.append(f"log has {len(rows)} entries / head {head[:12]} but anchor records "
                            f"{anc['count']} / {anc['head'][:12]} (truncated, deleted or replaced)")
        return Verification(ok, len(rows), head, problems)

    def repair(self) -> Verification:
        """Restore the primary log from the mirror when the mirror is intact and matches the anchor."""
        with self._lock:
            v = self.verify()
            if v.ok:
                return v
            mrows = self._read(self.mirror)
            mok, mhead, _ = self._check_chain(mrows)
            anc = self._anchor()
            if mok and len(mrows) == anc["count"] and mhead == anc["head"]:
                self._write_protected(self.file, "".join(json.dumps(r) + "\n" for r in mrows))
                restored = True
            else:
                restored = False
        entry = {"type": "tamper_detected", "problems": v.problems, "restored_from_mirror": restored}
        if restored:
            self.append(entry)
        return self.verify() if restored else Verification(False, v.count, v.head,
                                                            v.problems + ["mirror also invalid; manual audit required"])

    def append(self, entry: dict) -> dict:
        with self._lock:
            v = self.verify()
            if not v.ok:
                raise RuntimeError("episodic log failed verification; run repair() first: "
                                   + "; ".join(v.problems))
            entry = json.loads(self.redact(_canon(entry)))
            seq, ts = v.count, round(time.time(), 6)
            row = {"seq": seq, "ts": ts, "prev": v.head, "hash": _hash(v.head, seq, ts, entry),
                   "entry": entry}
            line = json.dumps(row) + "\n"
            for path in (self.file, self.mirror):
                existing = path.read_text() if path.exists() else ""
                self._write_protected(path, existing + line)
            self._write_protected(self.anchor, json.dumps({"count": seq + 1, "head": row["hash"]}))
            return row

    def tail(self, n: int = 10) -> list[dict]:
        return self._read(self.file)[-n:]

    def all(self) -> list[dict]:
        return self._read(self.file)

    def search(self, query: str, n: int = 10) -> list[dict]:
        q = query.lower()
        return [r for r in self._read(self.file) if q in _canon(r["entry"]).lower()][-n:]


def episode_from_task(task: Any) -> dict:
    """The record of one task: goal, sources, actions, outcome, failures."""
    pack = task.state.get("pack")
    return {
        "type": "task",
        "task_id": task.id,
        "goal": task.goal[:2000],
        "domain": task.state.get("domain", "general"),
        "task_type": task.state.get("task_type", "general"),
        "intent": task.state.get("intent", ""),
        "sources": [{"id": s.id, "locator": s.locator, "tier": s.tier, "reachable": s.reachable,
                     "sha256": s.sha256} for s in (pack.sources if pack else [])],
        "decisions": [{"id": d.id, "tag": d.tag, "confidence": d.confidence, "grade": d.grade,
                       "tool": d.tool, "mode": d.mode, "statement": d.statement[:300]}
                      for d in (pack.decisions if pack else [])],
        "actions": [{"tool": r["tool"], "args": _short(r.get("args")), "blocked": r.get("blocked"),
                     "exit_code": r.get("exit_code"), "is_error": r.get("is_error")}
                    for r in task.ledger],
        "outcome": task.status,
        "result": task.result[:2000],
        "failures": {k: v for k, v in task.hook_failures.items()},
        "out_of_scope": task.state.get("out_of_scope", []),
        "flags": task.state.get("flags", []),
        "dangers": task.state.get("dangers", []),
        "steps": task.steps,
        "seconds": round(time.time() - task.started, 1),
    }


def _short(v: Any) -> Any:
    s = json.dumps(v, default=str)
    return v if len(s) <= 400 else s[:400] + "…"


def memory_tools(log: EpisodicLog) -> list:
    from .tools import Tool, ToolResult, obj

    def read(a: dict) -> ToolResult:
        rows = log.search(a["query"], int(a.get("n", 10))) if a.get("query") else log.tail(int(a.get("n", 10)))
        return ToolResult(json.dumps([{"seq": r["seq"], **r["entry"]} for r in rows])[:20000])

    return [Tool("memory__episodes", "Read the episodic log (read-only): recent tasks, or those "
                 "matching a query. There is no tool to write, edit or delete it.",
                 obj({"query": {"type": "string"}, "n": {"type": "integer"}}, []), read,
                 read_only=True, mutating=False)]


def copy_tree(src: Path, dst: Path) -> None:
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst, symlinks=True)
