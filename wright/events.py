"""Event bus: every observable thing the harness does becomes an event (UI, transcript, tests)."""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Callable

Redactor = Callable[[str], str]


class EventBus:
    def __init__(self, transcript: Path | None = None, redact: Redactor | None = None):
        self._subs: list[Callable[[dict], None]] = []
        self._lock = threading.Lock()
        self.transcript = transcript
        self.redact = redact or (lambda s: s)
        self.history: list[dict] = []

    def subscribe(self, fn: Callable[[dict], None]) -> Callable[[], None]:
        with self._lock:
            self._subs.append(fn)
        return lambda: self._subs.remove(fn) if fn in self._subs else None

    def emit(self, kind: str, **data: Any) -> dict:
        ev = {"t": round(time.time(), 3), "kind": kind, **data}
        # Redact before the event leaves the process boundary (disk, UI, tests all see the same thing).
        ev = json.loads(self.redact(json.dumps(ev, default=str)))
        with self._lock:
            self.history.append(ev)
            subs = list(self._subs)
            if self.transcript:
                self.transcript.parent.mkdir(parents=True, exist_ok=True)
                with self.transcript.open("a") as f:
                    f.write(json.dumps(ev) + "\n")
        for fn in subs:
            try:
                fn(ev)
            except Exception:  # a broken subscriber must never stop the agent
                pass
        return ev

    def of(self, kind: str) -> list[dict]:
        return [e for e in self.history if e["kind"] == kind]


def console_printer(ev: dict) -> None:
    k = ev["kind"]
    if k == "tool_call":
        print(f"  ▸ act     {ev['tool']}({json.dumps(ev['args'])[:160]})")
    elif k == "tool_result":
        out = (ev.get("text") or "").strip().replace("\n", " ⏎ ")
        print(f"  ◂ observe {'ERROR ' if ev.get('is_error') else ''}{out[:160]}")
    elif k == "assistant":
        if ev.get("text"):
            print(f"  ✎ model   {ev['text'][:200]}")
    elif k == "verify":
        mark = "GREEN" if ev["ok"] else "RED"
        print(f"  ✔ verify  {mark}: " + "; ".join(f"{r['name']}={'pass' if r['ok'] else 'FAIL'}" for r in ev["results"]))
    elif k == "blocked":
        print(f"  ✖ blocked {ev['tool']}: {ev['reason']}")
    elif k == "state":
        print(f"  ⇢ state   {ev.get('from')} → {ev['to']}")
    elif k in ("task_start", "task_end", "flag", "lane", "audit", "promotion", "rollback", "danger",
               "lane_cooldown", "lane_disabled", "lane_error", "pool_wait", "setup_defect"):
        rest = {a: b for a, b in ev.items() if a not in ("t", "kind")}
        print(f"  • {k:8s}{json.dumps(rest, default=str)[:220]}")
