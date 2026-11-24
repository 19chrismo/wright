"""Regression baseline: freeze the known-good state, re-run after every change, fail closed on ANY
dimension that gets worse — even if the average improves. Flaky dimensions are re-run and decided
by majority; newly fixed cases become permanent dimensions."""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .checks import run_check, validate_check

DIRECTIONS = ("pass", "higher", "lower")


@dataclass
class Dimension:
    name: str
    check: dict
    direction: str = "pass"            # pass: must keep passing; higher/lower: numeric metric
    tolerance: float = 0.0             # relative tolerance for metrics (0.05 = 5%)
    abs_tolerance: float = 0.0
    origin: str = "baseline"           # baseline | fixed-case | acceptance


@dataclass
class Baseline:
    id: str
    target: str
    dimensions: list[Dimension] = field(default_factory=list)
    frozen: dict[str, dict] = field(default_factory=dict)      # name -> {"ok": bool, "value": ...}
    frozen_at: float = 0.0
    version: int = 1
    flaky_runs: int = 3
    flakes: list[dict] = field(default_factory=list)

    # ------------------------------------------------------------------ manifest io
    def as_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Baseline":
        d = dict(d)
        d["dimensions"] = [Dimension(**x) for x in d.get("dimensions", [])]
        return cls(**d)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.as_dict(), indent=1))

    @classmethod
    def load(cls, path: Path) -> "Baseline":
        return cls.from_dict(json.loads(path.read_text()))

    def validate(self) -> str | None:
        names = set()
        for d in self.dimensions:
            if d.name in names:
                return f"duplicate dimension {d.name}"
            names.add(d.name)
            if d.direction not in DIRECTIONS:
                return f"{d.name}: direction must be one of {DIRECTIONS}"
            err = validate_check(d.check)
            if err:
                return f"{d.name}: {err}"
            if d.direction != "pass" and d.check.get("kind") != "metric":
                return f"{d.name}: higher/lower dimensions need a metric check"
        return None

    # ------------------------------------------------------------------ measure
    def _measure(self, d: Dimension, cwd: Path) -> dict:
        r = run_check(d.check, cwd)
        return {"ok": r.ok, "value": r.value, "detail": r.detail[:300]}

    def measure(self, cwd: Path) -> dict[str, dict]:
        return {d.name: self._measure(d, cwd) for d in self.dimensions}

    def freeze(self, cwd: Path) -> dict[str, dict]:
        err = self.validate()
        if err:
            raise ValueError(err)
        self.frozen = self.measure(cwd)
        self.frozen_at = time.time()
        return self.frozen

    # ------------------------------------------------------------------ compare
    def _status(self, d: Dimension, before: dict, now: dict) -> str:
        if d.direction == "pass":
            if before["ok"] and not now["ok"]:
                return "worse"
            if not before["ok"] and now["ok"]:
                return "fixed"
            return "same"
        if not now["ok"] or now["value"] is None:
            return "worse"                      # a metric that can no longer be measured is a regression
        if not before["ok"] or before["value"] is None:
            return "fixed"
        b, n = float(before["value"]), float(now["value"])
        slack = max(abs(b) * d.tolerance, d.abs_tolerance)
        if d.direction == "lower":
            return "worse" if n > b + slack else ("better" if n < b - slack else "same")
        return "worse" if n < b - slack else ("better" if n > b + slack else "same")

    def compare(self, cwd: Path) -> dict[str, Any]:
        """Fail-closed comparison. Dimensions that look worse are re-run (majority of flaky_runs)."""
        if not self.frozen:
            raise RuntimeError("baseline not frozen")
        now = self.measure(cwd)
        rows = []
        for d in self.dimensions:
            before = self.frozen.get(d.name)
            if before is None:
                rows.append({"name": d.name, "status": "new", "before": None, "now": now[d.name]})
                continue
            st = self._status(d, before, now[d.name])
            if st == "worse" and self.flaky_runs > 1:
                votes = [st] + [self._status(d, before, self._measure(d, cwd))
                                for _ in range(self.flaky_runs - 1)]
                worse = votes.count("worse")
                if 0 < worse < len(votes):
                    self.flakes.append({"dimension": d.name, "votes": votes, "t": time.time()})
                st = "worse" if worse * 2 > len(votes) else "same"
            rows.append({"name": d.name, "status": st, "before": before.get("value"),
                         "now": now[d.name].get("value"), "detail": now[d.name].get("detail", "")})
        regressions = [r["name"] for r in rows if r["status"] == "worse"]
        improvements = [r["name"] for r in rows if r["status"] in ("better", "fixed")]
        return {"accept": not regressions, "regressions": regressions, "improvements": improvements,
                "rows": rows, "flakes": self.flakes[-5:]}

    # ------------------------------------------------------------------ evolve
    def absorb(self, cwd: Path, fixed_cases: list[dict] | None = None) -> list[str]:
        """After an accepted change: re-freeze improved dimensions at their new level and add every
        newly fixed case as a permanent dimension. Returns names of new/raised dimensions."""
        added = []
        for spec in fixed_cases or []:
            name = spec.get("name") or f"case{len(self.dimensions)}"
            if any(d.name == name for d in self.dimensions):
                continue
            if validate_check(spec):
                continue
            self.dimensions.append(Dimension(name=name, check=spec, origin="fixed-case"))
            added.append(name)
        now = self.measure(cwd)
        for d in self.dimensions:
            before = self.frozen.get(d.name)
            if before is None or self._status(d, before, now[d.name]) in ("better", "fixed"):
                if now[d.name]["ok"]:
                    self.frozen[d.name] = now[d.name]
                    if d.name not in added:
                        added.append(d.name)
        if added:
            self.version += 1
        return added
