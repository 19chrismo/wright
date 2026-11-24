"""Step 14: promotion gate. A change reaches the real system only after green; red leaves it
untouched; a failing post-promotion probe rolls it back automatically (directory targets and
commit-confirmed live targets)."""
import time
from pathlib import Path

from agentkit import agent_config
from wright.agent import Agent
from wright.events import EventBus
from wright.llm import ScriptedModel, call, say
from wright.sandbox import manifest
from wright.targets import CommandTarget


def head(criteria, scope, dims):
    return [
        call("intent__set", intent="make the requested change and keep everything else working",
             criteria=criteria, scope={"paths": scope}, domain="ops", task_type="change"),
        call("plan__advance", to="knowledge"),
        call("knowledge__decide", statement="the change is a plain file edit", tag="UNKNOWN", confidence=0.5,
             mitigation="sandbox_test"),
        call("plan__advance", to="baseline"),
        call("baseline__define", dimensions=dims),
        call("plan__advance", to="execute"),
        call("plan__set", subtasks=["make the change"])]


def tail(summary):
    return [call("plan__step_done", evidence="green"), call("plan__advance", to="verify"), call("verify__run"),
            call("plan__advance", to="promote"), call("promote__run"), call("plan__advance", to="report"),
            call("task__complete", summary=summary)]


def test_red_never_reaches_the_real_system(tmp_path):
    real = tmp_path / "app"; real.mkdir()
    (real / "VERSION").write_text("1\n")
    s = head([{"name": "v2", "kind": "file_contains", "path": "VERSION", "text": "2"}], ["VERSION"],
             [{"name": "exists", "check": {"kind": "file_exists", "path": "VERSION"}}])
    s += [call("fs__write", path="VERSION", content="3\n"), call("verify__run"),
          call("plan__advance", to="verify"), call("promote__run"), call("plan__advance", to="promote")]
    ev = EventBus()
    agent = Agent(agent_config(tmp_path), ScriptedModel(s), tools="local", events=ev, power_command=[])
    agent.lock.idle_turns = 0
    task = agent.run("set VERSION to 2", target=real)
    assert (real / "VERSION").read_text() == "1\n"
    assert not ev.of("promotion") and task.state.get("promotion") is None


def test_green_promotes_then_failing_probe_rolls_back_automatically(tmp_path):
    real = tmp_path / "svc"; real.mkdir()
    (real / "app.conf").write_text("port=80\n")
    (real / "check.sh").write_text("grep -q 'port=8080' app.conf && [ ! -f node_modules/.broken ]\n")
    (real / "node_modules").mkdir()
    (real / "node_modules" / ".broken").write_text("prod-only dependency problem\n")   # not in the sandbox
    before = manifest(real)
    s = head([{"name": "serves 8080", "kind": "command", "run": "sh check.sh"}], ["app.conf"],
             [{"name": "conf present", "check": {"kind": "file_exists", "path": "app.conf"}}])
    s += [call("fs__write", path="app.conf", content="port=8080\n"), call("verify__run")] + tail("moved to 8080")
    ev = EventBus()
    agent = Agent(agent_config(tmp_path), ScriptedModel(s), tools="local", events=ev, power_command=[])
    agent.lock.idle_turns = 0
    task = agent.run("serve on port 8080", target=real)
    prom = task.state["promotion"]
    assert prom["status"] == "rolled_back" and "serves 8080" in prom["reason"]
    assert ev.of("rollback") and manifest(real) == before                   # real restored exactly
    assert prom["rollback_rehearsal"].startswith("rollback rehearsal restored")
    assert prom["snapshot_check"] == "restore verified"
    assert task.status != "done"


def test_green_promotes_and_is_undoable(tmp_path):
    real = tmp_path / "site"; real.mkdir()
    (real / "index.html").write_text("<h1>old</h1>\n")
    s = head([{"name": "new title", "kind": "file_contains", "path": "index.html", "text": "<h1>new</h1>"}],
             ["index.html"], [{"name": "page", "check": {"kind": "file_exists", "path": "index.html"}}])
    s += [call("fs__write", path="index.html", content="<h1>new</h1>\n"), call("verify__run")] + tail("retitled")
    agent = Agent(agent_config(tmp_path), ScriptedModel(s), tools="local", power_command=[])
    task = agent.run("retitle the page", target=real)
    assert task.status == "done" and (real / "index.html").read_text() == "<h1>new</h1>\n"
    sid = task.state["promotion"]["snapshot"]
    agent.targets[task.id].rollback(sid)
    assert (real / "index.html").read_text() == "<h1>old</h1>\n"


def device_spec(dev: Path, window: float = 30) -> dict:
    return {"name": "fake-router",
            "export": f"cp {dev}/config {{dest}}/config",
            "apply": f"cp {{staged}}/config {dev}/config",
            "revert": f"cp {{snapshot}}/config {dev}/config",
            "validate": "grep -Eq '^wan_ssh=(on|off)$' {staged}/config",
            "probes": [f"grep -q '^lan_ssh=on$' {dev}/config"],          # management path must stay open
            "confirm_window": window}


def test_live_target_commit_confirmed_rolls_back_a_lockout_then_promotes_the_fix(tmp_path):
    dev = tmp_path / "device"; dev.mkdir()
    (dev / "config").write_text("wan_ssh=on\nlan_ssh=on\n")
    s = head([{"name": "wan ssh off", "kind": "file_contains", "path": "config", "regex": "(?m)^wan_ssh=off$"}],
             ["config"], [{"name": "staged config valid", "check": {"kind": "command",
                                                                     "run": "grep -q wan_ssh config"}}])
    s += [call("fs__write", path="config", content="wan_ssh=off\nlan_ssh=off\n"),   # lockout mistake
          call("verify__run"), call("plan__step_done", evidence="green"), call("plan__advance", to="verify"),
          call("verify__run"), call("plan__advance", to="promote"), call("promote__run"),   # → rolled back
          call("plan__advance", to="execute"), call("plan__set", subtasks=["make the change", "keep LAN ssh"]),
          call("fs__write", path="config", content="wan_ssh=off\nlan_ssh=on\n"),
          call("verify__run")] + tail("Closed WAN ssh, kept LAN management ssh; probes green; timer cleared.")
    ev = EventBus()
    agent = Agent(agent_config(tmp_path), ScriptedModel(s), tools="local", events=ev, power_command=[])
    task = agent.run("Close SSH on the WAN side of the router.", target_spec=device_spec(dev))
    assert task.status == "done", task.result
    assert [p["status"] for p in task.state["promotions"]] == ["rolled_back", "promoted"]
    assert (dev / "config").read_text() == "wan_ssh=off\nlan_ssh=on\n"
    assert "restore path rehearsed" in task.state["promotion"]["rollback_rehearsal"]
    assert agent.targets[task.id].timer is None                              # confirmed: timer cleared


def test_auto_revert_timer_fires_when_not_confirmed(tmp_path):
    dev = tmp_path / "device"; dev.mkdir()
    (dev / "config").write_text("wan_ssh=on\nlan_ssh=on\n")
    t = CommandTarget(device_spec(dev, window=0.5))
    root = t.prepare(tmp_path / "task")
    (root / "config").write_text("wan_ssh=off\nlan_ssh=on\n")
    sid, ok, _ = t.snapshot()
    t.pending_snapshot = sid
    t.apply()
    assert (dev / "config").read_text().startswith("wan_ssh=off")
    assert t.reverted_by_timer.wait(5)                                        # nobody confirmed → reverted
    assert (dev / "config").read_text() == "wan_ssh=on\nlan_ssh=on\n"
