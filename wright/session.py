"""Continuity: one agent over time. Each session opens by verifying the episodic chain and loading
its tail and the calibration table into the system prompt; each task closes by grading decisions,
appending its episode and rebuilding calibration from the (append-only) log."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .calibration import CalibrationTable, grade_decisions
from .config import PACKAGE_DIR, Paths
from .memory import EpisodicLog, episode_from_task
from .secrets import KeyStore, Redactor

TAIL = 8


@dataclass
class Continuity:
    paths: Paths
    log: EpisodicLog = field(init=False)
    calibration: CalibrationTable = field(init=False)
    redactor: Redactor = field(init=False)
    opened: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.paths.ensure()
        self.redactor = Redactor(KeyStore(self.paths.secrets))
        self.log = EpisodicLog(self.paths.episodic, redact=self.redactor)

    def open(self) -> dict:
        v = self.log.verify()
        repaired = False
        if not v.ok:
            v = self.log.repair()
            repaired = True
        self.calibration = CalibrationTable.from_episodes(self.log.all())
        self.calibration.save(self.paths.calibration_file)
        self.opened = {"chain_ok": v.ok, "entries": v.count, "repaired": repaired,
                       "problems": v.problems, "tail": [r["entry"] for r in self.log.tail(TAIL)],
                       "calibration": self.calibration.overall()}
        return self.opened

    def tail_text(self) -> str:
        rows = self.log.tail(TAIL)
        if not rows:
            return "EPISODIC TAIL: no previous tasks."
        out = ["EPISODIC TAIL (most recent last; read-only history of this same agent):"]
        for r in rows:
            e = r["entry"]
            if e.get("type") == "task":
                fails = ", ".join(e.get("failures", {})) or "none"
                out.append(f"  #{r['seq']} {e['task_id']} [{e['outcome']}] {e['goal'][:120]} "
                           f"(domain {e.get('domain')}; hook failures: {fails})")
            else:
                out.append(f"  #{r['seq']} {e.get('type')}: {json.dumps(e)[:160]}")
        return "\n".join(out)

    def system_prompt(self, base: str | None = None) -> str:
        base = base if base is not None else (PACKAGE_DIR / "prompts" / "system.md").read_text()
        return f"{base}\n\n{self.tail_text()}\n\n{self.calibration.render()}\n"

    def close_task(self, task: Any) -> dict:
        grade_decisions(task)
        row = self.log.append(episode_from_task(task))
        self.calibration = CalibrationTable.from_episodes(self.log.all())
        self.calibration.save(self.paths.calibration_file)
        return row
