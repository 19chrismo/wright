"""Self-model: a calibration table keyed by (domain, tool, task_type), built from the episodic log.

A [SOURCED]/[INFERRED] decision HELD if the criteria it supports passed the first time they were
verified after it was made; it was WRONG if they failed then. Measured accuracy uses Laplace
smoothing: (held + 1) / (graded + 2). Rule: when a decision's stated confidence exceeds the measured
accuracy of its bucket by more than 0.2 — and the bucket has at least MIN_SAMPLES graded claims —
the decision is downgraded to sandbox-test-first (mutation budget 1, verify after each change) and
flagged. Below the minimum sample, risky steps in that bucket default to sandbox-test-first.
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

GAP = 0.2
MIN_SAMPLES = 5


def bucket(domain: str, tool: str, task_type: str) -> str:
    return f"{domain or 'general'}|{tool or '*'}|{task_type or 'general'}"


@dataclass
class Bucket:
    held: int = 0
    wrong: int = 0
    sourced_held: int = 0
    sourced_wrong: int = 0
    inferred_held: int = 0
    inferred_wrong: int = 0
    confidence_sum: float = 0.0

    @property
    def graded(self) -> int:
        return self.held + self.wrong

    @property
    def accuracy(self) -> float:
        return (self.held + 1) / (self.graded + 2)

    @property
    def mean_confidence(self) -> float:
        return self.confidence_sum / self.graded if self.graded else 0.0


@dataclass
class CalibrationTable:
    buckets: dict[str, Bucket] = field(default_factory=dict)
    episodes_seen: int = 0
    updated: float = 0.0

    # ------------------------------------------------------------------ persistence
    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        d = {"buckets": {k: asdict(v) for k, v in self.buckets.items()},
             "episodes_seen": self.episodes_seen, "updated": self.updated, "gap": GAP,
             "min_samples": MIN_SAMPLES}
        path.write_text(json.dumps(d, indent=1))

    @classmethod
    def load(cls, path: Path) -> "CalibrationTable":
        if not path.exists():
            return cls()
        d = json.loads(path.read_text())
        return cls({k: Bucket(**v) for k, v in d.get("buckets", {}).items()},
                   d.get("episodes_seen", 0), d.get("updated", 0.0))

    # ------------------------------------------------------------------ building
    @classmethod
    def from_episodes(cls, rows: list[dict]) -> "CalibrationTable":
        """Rebuilt deterministically from the (append-only) episodic log — never edited by hand."""
        t = cls()
        for r in rows:
            e = r.get("entry", r)
            if e.get("type") != "task":
                continue
            t.episodes_seen += 1
            for d in e.get("decisions", []):
                if d.get("grade") not in ("held", "wrong") or d.get("tag") == "UNKNOWN":
                    continue
                b = t.buckets.setdefault(bucket(e.get("domain"), d.get("tool"), e.get("task_type")), Bucket())
                held = d["grade"] == "held"
                b.held += held
                b.wrong += not held
                b.confidence_sum += float(d.get("confidence", 0))
                key = "sourced" if d["tag"] == "SOURCED" else "inferred"
                setattr(b, f"{key}_{'held' if held else 'wrong'}",
                        getattr(b, f"{key}_{'held' if held else 'wrong'}") + 1)
        t.updated = time.time()
        return t

    # ------------------------------------------------------------------ queries
    def lookup(self, domain: str, tool: str, task_type: str) -> Bucket | None:
        for k in (bucket(domain, tool, task_type), bucket(domain, "", task_type), bucket(domain, "", "")):
            if k in self.buckets:
                return self.buckets[k]
        return None

    def assess(self, domain: str, tool: str, task_type: str, confidence: float) -> dict[str, Any]:
        b = self.lookup(domain, tool, task_type)
        if b is None or b.graded < MIN_SAMPLES:
            return {"downgrade": False, "insufficient": True,
                    "graded": b.graded if b else 0, "accuracy": round(b.accuracy, 3) if b else None}
        gap = confidence - b.accuracy
        return {"downgrade": gap > GAP, "insufficient": False, "graded": b.graded,
                "accuracy": round(b.accuracy, 3), "gap": round(gap, 3)}

    def overall(self) -> dict[str, Any]:
        held = sum(b.held for b in self.buckets.values())
        graded = sum(b.graded for b in self.buckets.values())
        conf = sum(b.confidence_sum for b in self.buckets.values())
        acc = (held + 1) / (graded + 2)
        mean_conf = conf / graded if graded else 0.0
        return {"graded": graded, "accuracy": round(acc, 3), "mean_confidence": round(mean_conf, 3),
                "overconfidence": round(mean_conf - acc, 3) if graded else 0.0}

    def render(self, limit: int = 12) -> str:
        o = self.overall()
        lines = [f"CALIBRATION (from {self.episodes_seen} episodes): measured accuracy {o['accuracy']:.2f} "
                 f"vs mean stated confidence {o['mean_confidence']:.2f} over {o['graded']} graded claims"]
        rows = sorted(self.buckets.items(), key=lambda kv: -kv[1].graded)[:limit]
        for k, b in rows:
            lines.append(f"  {k}: accuracy {b.accuracy:.2f} (held {b.held}/{b.graded}; "
                         f"SOURCED wrong {b.sourced_wrong}, INFERRED wrong {b.inferred_wrong}); "
                         f"mean confidence {b.mean_confidence:.2f}")
        return "\n".join(lines)


def grade_decisions(task: Any) -> None:
    """Grade each [SOURCED]/[INFERRED] decision from the FIRST verification after it was made."""
    pack = task.state.get("pack")
    verifies = task.state.get("verify_log", [])
    if not pack:
        return
    for d in pack.decisions:
        if d.tag == "UNKNOWN" or d.grade is not None:
            continue
        after = [v for v in verifies if v["t"] >= d.made_at]
        if not after:
            continue
        first = after[0]
        relevant = [r for r in first["results"] if not d.supports or r["name"] in d.supports]
        if not relevant:
            continue
        d.grade = "held" if all(r["ok"] for r in relevant) else "wrong"


def calibration_hook(table: CalibrationTable, get_task: Any) -> Any:
    """Called by knowledge__decide before a decision is recorded: may downgrade it."""
    def hook(d: Any) -> None:
        t = get_task()
        a = table.assess(t.state.get("domain", "general"), d.tool, t.state.get("task_type", "general"),
                         d.confidence)
        if a["downgrade"]:
            d.mode = "sandbox-test-first"
            flag = {"kind": "calibration", "decision": d.statement[:120], "stated": d.confidence,
                    "measured": a["accuracy"], "graded": a["graded"],
                    "message": (f"stated confidence {d.confidence:.2f} exceeds measured accuracy "
                                f"{a['accuracy']:.2f} (n={a['graded']}) by more than {GAP}: "
                                "downgraded to sandbox-test-first")}
            t.state.setdefault("flags", []).append(flag)
        elif a["insufficient"] and d.tag != "UNKNOWN":
            t.state.setdefault("calibration_notes", []).append(
                {"decision": d.statement[:120], "note": f"bucket has {a['graded']} graded claims (<{MIN_SAMPLES})"})
    return hook
