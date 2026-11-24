"""Step 8: calibration table + continuity. The downgrade rule fires when measured accuracy in a
domain is below stated confidence (and not below the minimum sample); a new session opens with the
episodic tail and calibration table loaded."""
import time

from wright.calibration import MIN_SAMPLES, CalibrationTable, calibration_hook, grade_decisions
from wright.config import Paths
from wright.contract import MutationBudget
from wright.harness import Task
from wright.knowledge import Decision, KnowledgePack
from wright.llm import ToolCall
from wright.memory import EpisodicLog
from wright.session import Continuity
from wright.tools import Tool, ToolResult, obj


def seed(log, domain, tool, task_type, grades, conf=0.9):
    for i, g in enumerate(grades):
        log.append({"type": "task", "task_id": f"seed{i}", "goal": f"seed task {i}", "domain": domain,
                    "task_type": task_type, "outcome": "done", "failures": {}, "sources": [], "actions": [],
                    "decisions": [{"id": "D1", "tag": "INFERRED", "confidence": conf, "grade": g,
                                   "tool": tool, "statement": "x"}]})


def task_with(domain="network", task_type="harden"):
    t = Task(goal="g", workdir=".")
    t.state.update(domain=domain, task_type=task_type)
    return t


def test_downgrade_fires_when_confidence_exceeds_measured_accuracy(tmp_path):
    log = EpisodicLog(tmp_path / "ep")
    seed(log, "network", "shell__run", "harden", ["held", "wrong", "wrong", "held", "wrong", "wrong"])
    table = CalibrationTable.from_episodes(log.all())
    b = table.lookup("network", "shell__run", "harden")
    assert b.graded == 6 and abs(b.accuracy - 3 / 8) < 1e-9            # (2+1)/(6+2)
    t = task_with()
    d = Decision("D1", "uci firewall zone change is safe", "INFERRED", 0.9, tool="shell__run", chain="c")
    calibration_hook(table, lambda: t)(d)
    assert d.mode == "sandbox-test-first"
    flag = t.state["flags"][0]
    assert flag["kind"] == "calibration" and flag["measured"] == 0.375 and flag["stated"] == 0.9
    # the downgrade has teeth: mutation budget drops to 1 per verification gate
    t.state["pack"] = KnowledgePack("kp", "network", "harden", decisions=[d])
    budget = MutationBudget(5)
    w = Tool("fs__write", "w", obj({}), lambda a: ToolResult("ok"), destructive=True, source="local:fs")
    assert budget(t, ToolCall("fs__write", {}), w).allow
    budget.observe(t, ToolCall("fs__write", {}), w, ToolResult("ok"))      # it ran
    assert not budget(t, ToolCall("fs__write", {}), w).allow


def test_no_downgrade_below_minimum_sample_or_within_gap(tmp_path):
    log = EpisodicLog(tmp_path / "ep")
    seed(log, "network", "shell__run", "harden", ["wrong"] * (MIN_SAMPLES - 2))
    seed(log, "code", "fs__write", "build", ["held"] * 8 + ["wrong"])
    table = CalibrationTable.from_episodes(log.all())
    t = task_with()
    d = Decision("D1", "x", "INFERRED", 0.95, tool="shell__run", chain="c")
    calibration_hook(table, lambda: t)(d)
    assert d.mode == "normal" and "graded claims" in t.state["calibration_notes"][0]["note"]
    t2 = task_with("code", "build")
    d2 = Decision("D2", "x", "SOURCED", 0.85, tool="fs__write")
    calibration_hook(table, lambda: t2)(d2)                             # accuracy 0.818: gap < 0.2
    assert d2.mode == "normal" and "flags" not in t2.state


def test_grading_uses_first_verification_after_the_decision():
    t = task_with()
    d1 = Decision("D1", "a", "SOURCED", 0.9, supports=["svc up"], made_at=time.time() - 10)
    d2 = Decision("D2", "b", "INFERRED", 0.6, supports=["cfg valid"], made_at=time.time() - 10, chain="c")
    t.state["pack"] = KnowledgePack("kp", "network", "harden", decisions=[d1, d2])
    t.state["verify_log"] = [
        {"t": time.time() - 5, "results": [{"name": "svc up", "ok": True}, {"name": "cfg valid", "ok": False}]},
        {"t": time.time(), "results": [{"name": "svc up", "ok": True}, {"name": "cfg valid", "ok": True}]}]
    grade_decisions(t)
    assert (d1.grade, d2.grade) == ("held", "wrong")                    # later fix doesn't erase the miss


def test_new_session_opens_with_episodic_tail_and_calibration(tmp_path):
    paths = Paths(tmp_path / "home")
    a = Continuity(paths)
    a.open()
    t = task_with("router", "harden")
    t.goal = "Harden the WAN interface of the router"
    t.status = "done"
    t.state["pack"] = KnowledgePack("kp", "router", "harden", decisions=[
        Decision("D1", "disable wan ssh", "SOURCED", 0.8, supports=["ssh closed"], tool="shell__run",
                 made_at=time.time() - 1)])
    t.state["verify_log"] = [{"t": time.time(), "results": [{"name": "ssh closed", "ok": True}]}]
    a.close_task(t)
    del a                                                                # session ends

    b = Continuity(paths)                                                # tomorrow: same agent
    opened = b.open()
    assert opened["chain_ok"] and opened["entries"] == 1
    prompt = b.system_prompt()
    assert t.id in prompt and "Harden the WAN interface" in prompt
    assert "router|shell__run|harden: accuracy 0.67 (held 1/1" in prompt
    assert "You are not conscious" in prompt
    assert b.calibration.lookup("router", "shell__run", "harden").held == 1
    assert paths.calibration_file.exists()
