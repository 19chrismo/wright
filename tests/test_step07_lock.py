"""Step 7: procedural lock. On a long run the agent cannot skip a step or jump to done."""
import json

import pytest

from wright.checks import run_checks
from wright.contract import MutationBudget
from wright.events import EventBus
from wright.harness import Harness, Task
from wright.knowledge import knowledge_tools
from wright.llm import ScriptedModel, call, say
from wright.lock import Lock, PlanGraph
from wright.tools import ToolRegistry, local_fs_tools, local_shell_tool

CRIT = [{"name": "app works", "kind": "command", "run": "sh app.sh | grep -q 'v2'"}]


def build(tmp_path, steps, **lock_kw):
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    (work / "app.sh").write_text("echo v1\n")
    holder = {}
    reg = ToolRegistry()
    reg.register(local_shell_tool(str(work)))
    for t in local_fs_tools(str(work)) + knowledge_tools(lambda: holder["t"]):
        reg.register(t)

    def verifier(task):
        res = [r.as_dict() for r in run_checks(task.acceptance, task.workdir, task.ledger)]
        task.state["has_changes"] = True
        return all(r["ok"] for r in res), res

    ev = EventBus()
    lock = Lock(reg, verifier=verifier, promoter=lambda t: {"status": "promoted"}, events=ev,
                system_prompt="You are Wright.", **lock_kw)
    for t in lock.tools(lambda: holder["t"]):
        reg.register(t)
    task = Task(goal="Make app.sh print v2", workdir=work)
    holder["t"] = task
    h = Harness(ScriptedModel(steps), reg, ev, gates=[lock, MutationBudget(5)],
                observers=[lock.stamp, lock.reset_idle, lock.audit], visible_tools=lock.visible_tools,
                context_builder=lock.context_builder, end_of_turn=lock.end_of_turn)
    return h, task, ev, lock


LONG_RUN = [
    call("task__complete", summary="done!"),                                     # jump to done
    call("fs__write", path="app.sh", content="echo v2\n"),                       # act before intent
    call("intent__set", intent="app.sh prints v2 instead of v1 when run",
         criteria=[{"name": "looks right", "kind": "prose", "text": "it should be fine"}]),  # prose
    call("intent__set", intent="app.sh prints v2 instead of v1 when run", criteria=CRIT,
         scope={"paths": ["app.sh"]}, domain="shell", task_type="edit"),
    call("plan__advance", to="execute"),                                          # skip knowledge+baseline
    call("plan__advance", to="knowledge"),
    call("plan__advance", to="baseline"),                                         # guard: no decisions
    call("knowledge__decide", statement="editing the echo line changes the output", tag="UNKNOWN",
         confidence=0.6, mitigation="sandbox_test"),
    call("plan__advance", to="baseline"),
    call("baseline__define", dimensions=[{"name": "runs", "check": {"kind": "command", "run": "sh app.sh"}}]),
    call("plan__advance", to="execute"),
    call("plan__set", subtasks=["change output to v2", "double-check"]),
    call("fs__write", path="app.sh", content="echo v2\n"),
    call("plan__step_done", id="S1", evidence="edited"),                          # no verify after change
    say("All done, app prints v2."),                                              # bare claim of done
    call("verify__run"),
    call("plan__step_done", id="S1", evidence="verified green"),
    call("plan__step_done", id="S2", evidence="looked again"),
    call("plan__advance", to="report"),                                           # skip verify/promote
    call("plan__advance", to="verify"),
    call("verify__run"),
    call("plan__advance", to="report"),                                           # changes must be promoted
    call("plan__advance", to="promote"),
    call("promote__run"),
    call("plan__advance", to="report"),
    call("task__complete", summary="Changed app.sh to print v2; verified; promoted."),
]


def test_long_run_cannot_skip_steps_or_jump_to_done(tmp_path):
    h, task, ev, lock = build(tmp_path, LONG_RUN)
    h.run(task)
    assert task.status == "done"
    path = [x["to"] for x in task.state["lock"]["history"]]
    assert path == ["knowledge", "baseline", "execute", "verify", "promote", "report", "done"]
    blocked = [b["tool"] for b in ev.of("blocked")]
    assert blocked[:2] == ["task__complete", "fs__write"]                      # hidden + blocked in intent
    refusals = [e["text"] for e in ev.of("tool_result") if e["is_error"]]
    assert any("not machine-checkable" in r for r in refusals)
    assert any("illegal transition intent → execute" in r for r in refusals)
    assert any("exit condition not met" in r and "tagged decision" in r for r in refusals)
    assert any("run verify__run first" in r for r in refusals)
    assert any("illegal transition execute → report" in r or "verify → report" in r for r in refusals)
    assert any("there is nothing to promote" in r for r in refusals)
    lock_msgs = [m["content"] for m in task.messages if m.get("provenance") == "harness"]
    assert any("PROCEDURAL LOCK" in m for m in lock_msgs) or ev.of("context")
    assert ev.of("audit")                                                       # audits every 5 steps


def test_tools_offered_depend_on_state(tmp_path):
    h, task, ev, lock = build(tmp_path, [say("hi")])
    assert "fs__write" not in lock.visible_tools(task) and "intent__set" in lock.visible_tools(task)
    task.state["lock"]["state"] = "execute"
    assert "fs__write" in lock.visible_tools(task) and "intent__set" not in lock.visible_tools(task)


def test_idle_turns_end_the_task_with_a_precise_report(tmp_path):
    h, task, ev, lock = build(tmp_path, [say("thinking")], idle_turns=3)
    h.model.loop_last = True
    h.run(task)
    assert task.status == "failed" and "4 turns in a row in state 'intent'" in task.result


def test_fresh_bounded_executor_context_per_subtask(tmp_path):
    h, task, ev, lock = build(tmp_path, LONG_RUN)
    h.run(task)
    ctx = ev.of("context")
    assert len(ctx) >= 2
    first_after = [m for m in h.model.seen if any(x.get("content", "").startswith("STATE DIGEST")
                                                   if isinstance(x.get("content"), str) else False for x in m)]
    assert first_after and all(len(m) <= 30 for m in first_after)              # contexts were reset


def test_plan_graph_validation():
    g = PlanGraph.load().data
    bad = json.loads(json.dumps(g))
    bad["states"]["intent"]["next"] = ["nowhere"]
    with pytest.raises(ValueError):
        PlanGraph(bad)
    bad = json.loads(json.dumps(g))
    bad["states"]["done"]["next"] = ["intent"]
    with pytest.raises(ValueError):
        PlanGraph(bad)
