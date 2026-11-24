"""The final report, in past tense, assembled by the harness from recorded facts (never from the
model's say-so): result, verification, grounding, dangers and how each was handled, regression
status, out-of-scope touches, calibration, integrity flags, promotion and rollback command, and
what left the machine."""
from __future__ import annotations

from typing import Any


def build(task: Any, calibration: dict | None = None) -> dict:
    s = task.state
    lv = s.get("last_verify") or {}
    final = (s.get("verify_log") or [{}])[-1]
    pack = s.get("pack")
    prom = s.get("promotion") or {}
    reg = s.get("regression") or {}
    lanes = sorted({e for e in s.get("lanes_used", [])})
    out = {
        "status": task.status,
        "summary": task.result,
        "verification": {"green": bool(final.get("green", lv.get("green"))),
                         "checks": [{"name": r["name"], "ok": r["ok"]} for r in final.get("results", lv.get("results", []))]},
        "grounding": [{"tag": d.tag, "statement": d.statement, "confidence": d.confidence, "sources": d.sources,
                       "mode": d.mode, "grade": d.grade} for d in (pack.decisions if pack else [])],
        "sources": [{"id": x.id, "locator": x.locator, "tier": x.tier, "reachable": x.reachable}
                    for x in (pack.sources if pack else [])],
        "dangers": s.get("dangers", []),
        "confirmations": s.get("confirmations", []),
        "regression": {"baseline_dimensions": len(s["baseline"].dimensions) if s.get("baseline") else 0,
                       "accepted": reg.get("accept", True), "regressions": reg.get("regressions", []),
                       "improvements": reg.get("improvements", []), "flakes": reg.get("flakes", [])},
        "out_of_scope": {"justified": s.get("justifications", []),
                         "touches": s.get("out_of_scope", []),
                         "blocked": s.get("out_of_scope_blocked", []),
                         "count": len(s.get("out_of_scope", []))},
        "flags": s.get("flags", []),
        "promotion": prom,
        "calibration": calibration or {},
        "steps": task.steps,
        "data_left_machine": lanes,
    }
    return out


def render(rep: dict) -> str:
    lines = [f"**{rep['status'].upper()}** — {rep['summary']}"]
    v = rep["verification"]
    lines.append("Verification: " + ("GREEN" if v["green"] else "RED") + " — " +
                 ", ".join(f"{c['name']} {'✓' if c['ok'] else '✗'}" for c in v["checks"]))
    if rep["grounding"]:
        lines.append("Grounding: " + "; ".join(f"[{g['tag']}] {g['statement']} ({g['confidence']:.2f})"
                                               for g in rep["grounding"]))
    if rep["dangers"]:
        for d in rep["dangers"]:
            lines.append(f"Danger: {d.get('step') or d.get('call')} — "
                         + (f"failure modes {d.get('failure_modes')}; mitigations {d.get('mitigations')}; "
                            f"recovery rehearsal {d.get('rehearsal')}" if d.get("failure_modes") else d.get("status", "")))
    r = rep["regression"]
    lines.append(f"Regression baseline: {r['baseline_dimensions']} dimensions, "
                 + ("no regressions" if not r["regressions"] else f"REJECTED changes: {r['regressions']}")
                 + (f"; improved {r['improvements']}" if r["improvements"] else ""))
    o = rep["out_of_scope"]
    lines.append(f"Out-of-scope mutations: {o['count']}" + (f" (justified: {[j['target'] for j in o['justified']]})"
                                                             if o["justified"] else ""))
    p = rep["promotion"]
    if p:
        lines.append(f"Promotion: {p.get('status')}" + (f"; snapshot {p['snapshot']} — undo with "
                                                         f"`{p.get('rollback_command')}`" if p.get("snapshot") else ""))
    c = rep["calibration"]
    if c:
        lines.append(f"Calibration: measured accuracy {c.get('accuracy')} vs stated {c.get('mean_confidence')} "
                     f"({c.get('graded')} graded claims)")
    if rep["flags"]:
        lines.append("Integrity flags: " + "; ".join(f"{f.get('kind')}: {f.get('message') or f.get('text') or f.get('tool')}"
                                                     for f in rep["flags"]))
    if rep["data_left_machine"]:
        lines.append("Model lanes used (data sent): " + ", ".join(rep["data_left_machine"]))
    return "\n".join(lines)
