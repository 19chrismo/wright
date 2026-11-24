"""Step 12: stop hooks. The agent cannot declare done while a hook fails, and it keeps working; a
deliberate error introduced by the user is fixed without asking."""
from pathlib import Path

from agentkit import agent_config, last_json
from wright.agent import Agent
from wright.events import EventBus
from wright.llm import ScriptedModel, call, say

BROKEN = "def add(a, b):\n    return a - b\n\n\ndef mul(a, b):\n    return a * b\n"
TEST = "import calc\nassert calc.add(2, 3) == 5, 'add is wrong'\nassert calc.mul(3, 4) == 12\nprint('ok')\n"


def project(tmp_path) -> Path:
    real = tmp_path / "calc-project"
    real.mkdir()
    (real / "calc.py").write_text(BROKEN)          # the user's deliberate error
    (real / "test_calc.py").write_text(TEST)
    (real / "README.md").write_text("calc\n")
    return real


def fix_script(first_fix: str, second_fix: str | None):
    s = [
        call("intent__set", intent="python3 test_calc.py passes: add() returns the sum and mul() keeps working",
             criteria=[{"name": "tests pass", "kind": "command", "run": "python3 test_calc.py"}],
             scope={"paths": ["calc.py"]}, domain="python", task_type="bugfix"),
        call("plan__advance", to="knowledge"),
        call("knowledge__capture", locator="calc.py", tier=3, excerpt="return a - b"),
        lambda m: call("knowledge__decide", statement="add() subtracts; it must return a + b", tag="SOURCED",
                       confidence=0.9, sources=[last_json(m, "source_id")], supports=["tests pass"], tool="fs__edit"),
        call("plan__advance", to="baseline"),
        call("baseline__define", dimensions=[{"name": "mul works", "check": {
            "kind": "command", "run": "python3 -c 'import calc; assert calc.mul(3, 4) == 12'"}}]),
        call("plan__advance", to="execute"),
        call("plan__set", subtasks=["fix add"]),
        call("fs__edit", path="calc.py", old="return a - b", new=first_fix),
        call("task__complete", summary="Fixed add."),                          # claim done early
        say("Fixed it."),                                                     # bare claim
        call("verify__run"),                                                  # RED
    ]
    if second_fix:
        s += [call("fs__edit", path="calc.py", old=first_fix, new=second_fix),
              call("verify__run"),
              call("plan__step_done", evidence="verify green"),
              call("plan__advance", to="verify"),
              call("verify__run"),
              call("plan__advance", to="promote"),
              call("promote__run"),
              call("plan__advance", to="report"),
              call("task__complete", summary="Fixed add() to return a + b; tests pass; mul unchanged.")]
    return s


def test_failing_hook_blocks_done_and_agent_keeps_working_until_fixed(tmp_path):
    real = project(tmp_path)
    ev = EventBus()
    confirms = []
    agent = Agent(agent_config(tmp_path), ScriptedModel(fix_script("return abs(a - b)", "return a + b")),
                  tools="local", events=ev, power_command=[], confirm=lambda *a: confirms.append(a) or False)
    task = agent.run("The add test is failing. Fix it.", target=real)
    assert task.status == "done", task.result
    assert (real / "calc.py").read_text() == BROKEN.replace("a - b", "a + b")   # fixed in the real project
    assert [b["tool"] for b in ev.of("blocked")] == ["task__complete"]         # early done claim blocked
    verifies = [e["ok"] for e in ev.of("verify")]
    assert verifies[0] is False and verifies[-1] is True                       # red, kept working, green
    assert any("PROCEDURAL LOCK" in str(m.get("content")) for m in task.messages) or ev.of("context")
    assert confirms == [] and not ev.of("confirm_request")                     # never asked
    rep = task.state["report"]
    assert rep["promotion"]["status"] == "promoted" and rep["promotion"]["rollback_command"].startswith("wright rollback")
    assert rep["out_of_scope"]["count"] == 0 and rep["regression"]["regressions"] == []
    assert rep["grounding"][0]["tag"] == "SOURCED" and rep["grounding"][0]["grade"] == "wrong"  # first fix failed
    log = agent.continuity.log.all()
    assert log[-1]["entry"]["outcome"] == "done" and log[-1]["entry"]["task_id"] == task.id


def test_bounded_retry_stops_with_a_precise_report(tmp_path):
    real = project(tmp_path)
    s = fix_script("return abs(a - b)", None)
    s += [call("fs__edit", path="calc.py", old="return abs(a - b)", new="return a ** b"), call("verify__run"),
          call("fs__edit", path="calc.py", old="return a ** b", new="return a // b"), call("verify__run"),
          call("fs__edit", path="calc.py", old="return a // b", new="return b - a"), call("verify__run")]
    agent = Agent(agent_config(tmp_path), ScriptedModel(s), tools="local", power_command=[])
    task = agent.run("The add test is failing. Fix it.", target=real)
    assert task.status == "failed"
    assert "Stopped after 3 failed fix attempts" in task.result and "tests pass" in task.result
    assert (real / "calc.py").read_text() == BROKEN                            # real project untouched


def test_out_of_scope_change_turns_the_scope_hook_red(tmp_path):
    real = project(tmp_path)
    s = fix_script("return a + b", None)
    s = [x for x in s if not (hasattr(x, "tool_calls") and x.tool_calls and x.tool_calls[0].name == "task__complete")
         and not (hasattr(x, "text") and x.text == "Fixed it.")]
    s += [call("fs__edit", path="README.md", old="calc", new="calc (edited)"), call("verify__run")]
    ev = EventBus()
    agent = Agent(agent_config(tmp_path), ScriptedModel(s), tools="local", events=ev, power_command=[])
    agent.lock.idle_turns = 0
    task = agent.run("The add test is failing. Fix it.", target=real)
    first, last = ev.of("verify")[0], ev.of("verify")[-1]
    assert first["ok"] is True                                                  # in-scope fix: green
    scope = [r for r in last["results"] if r["name"] == "scope"][0]
    assert last["ok"] is False and scope["value"] == ["README.md"]             # README edit: red
    assert (real / "README.md").read_text() == "calc\n"                       # never reached the real file
