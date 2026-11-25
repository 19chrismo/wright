"""Step 13: danger pre-mortem. A risky step is blocked until failure modes are enumerated, each has a
mitigation, and the recovery has been rehearsed by the harness. Only the four unsnapshottable
categories ever reach the user; reversible actions never prompt."""
import json

from agentkit import agent_config, last_json
from wright.agent import Agent
from wright.danger import DangerGate, classify, danger_tools
from wright.events import EventBus
from wright.harness import Harness, Task
from wright.llm import ScriptedModel, ToolCall, call, say
from wright.tools import Tool, ToolRegistry, ToolResult, local_shell_tool, obj

PM = dict(applies_to=r"systemctl restart nginx",
          failure_modes=[{"mode": "config error stops nginx", "impact": "site down", "detect": "curl fails"},
                         {"mode": "ssh session dropped", "impact": "lockout", "detect": "no prompt"}],
          mitigations=[{"mode": "config error stops nginx", "action": "nginx -t before restart"},
                       {"mode": "ssh session dropped", "action": "restart only nginx, never sshd; console path checked"}],
          recovery={"procedure": "restore nginx.conf.bak and restart", "rollback_command": "cp nginx.conf.bak nginx.conf",
                    "rehearse_command": "cp nginx.conf nginx.conf.bak && cp nginx.conf.bak nginx.conf && cmp nginx.conf nginx.conf.bak"})


def rig(tmp_path, steps, confirm=None):
    work = tmp_path / "w"
    work.mkdir(exist_ok=True)
    (work / "nginx.conf").write_text("events {}\n")
    holder = {}
    ev = EventBus()
    reg = ToolRegistry()
    # a stand-in for systemctl so the "risky" command is harmless here
    reg.register(local_shell_tool(str(work)))

    def sandbox_run(cmd):
        r = json.loads(reg.get("shell__run").fn({"command": cmd}).text)
        return r["exit_code"] or 0, r["stdout"] + r["stderr"]
    for t in danger_tools(lambda: holder["t"], sandbox_run, ev):
        reg.register(t)
    reg.register(Tool("router__flash_firmware", "flash", obj({"image": {"type": "string"}}),
                      lambda a: ToolResult("flashed"), source="mcp:router", destructive=True))
    task = Task(goal="g", workdir=work)
    task.state["sandbox_root"] = str(work)
    holder["t"] = task
    Harness(ScriptedModel(steps), reg, ev, gates=[DangerGate(confirm, ev)]).run(task)
    return task, ev


SYSTEMCTL = "systemctl() { echo \"would $*\"; }; systemctl restart nginx"


def test_risky_step_blocked_until_recovery_is_rehearsed(tmp_path):
    bad = dict(PM, recovery=dict(PM["recovery"], rehearse_command="cp missing.bak nginx.conf"))
    task, ev = rig(tmp_path, [
        call("shell__run", command=SYSTEMCTL),                        # no pre-mortem
        call("danger__premortem", **dict(PM, mitigations=PM["mitigations"][:1])),   # a mode lacks mitigation
        call("danger__premortem", **bad),                             # recovery does not work
        call("shell__run", command=SYSTEMCTL),                        # still blocked
        call("danger__premortem", **PM),                              # rehearsed for real
        call("shell__run", command=SYSTEMCTL),                        # now allowed
        say("done")])
    blocked = ev.of("blocked")
    assert len(blocked) == 2 and all("pre-mortem" in b["reason"] for b in blocked)
    results = [e["text"] for e in ev.of("tool_result")]
    assert "no mitigation for failure mode(s) ['ssh session dropped']" in results[0]
    assert results[1].startswith("REHEARSAL FAILED")
    assert "would restart nginx" in results[-1]
    d = task.state["dangers"][-1]
    assert d["rehearsal"] == "rehearsed" and "ssh session dropped" in d["failure_modes"]
    assert not ev.of("confirm_request")                               # risky ≠ ask the user


def test_only_unsnapshottable_categories_reach_the_user(tmp_path):
    asked = []

    def confirm(cat, desc, details):
        asked.append(cat)
        return cat == "disclosure"                                    # user approves the push, not the flash
    task, ev = rig(tmp_path, [
        call("shell__run", command="rm -rf build && mkdir build"),    # destructive but inside the sandbox
        call("shell__run", command="dd if=fw.bin of=/dev/disk4 bs=1m"),
        call("router__flash_firmware", image="fw.bin"),
        call("shell__run", command="git push origin main || true"),
        say("done")], confirm)
    assert asked == ["hardware", "hardware", "disclosure"]
    blocked = [b["tool"] for b in ev.of("blocked")]
    assert blocked == ["shell__run", "router__flash_firmware"]       # the two hardware writes
    assert task.state["confirmations"][0]["category"] == "disclosure"
    assert task.state["confirmations"][0]["by"] == "user"


def test_destructive_outside_sandbox_needs_confirmation_unless_restore_is_tested(tmp_path):
    outside = tmp_path / "shared"
    asked = []
    pm = dict(PM, applies_to=r"rm -rf .*shared",
              recovery={"procedure": "restore from tarball", "rollback_command": "tar xzf shared.tgz",
                        "rehearse_command": "mkdir -p r && tar czf r.tgz r && tar xzf r.tgz"})
    task, ev = rig(tmp_path, [
        call("shell__run", command=f"rm -rf {outside}/cache"),
        call("danger__premortem", **pm),
        call("shell__run", command=f"rm -rf {outside}/cache"),
        say("done")], lambda *a: asked.append(a[0]) or False)
    assert asked == ["outside_destructive"]                            # asked once, before the tested restore
    assert len(ev.of("blocked")) == 1


def test_classifier_examples():
    sh = Tool("shell__run", "", {}, lambda a: None, source="mcp:shell", destructive=True)
    cats = lambda cmd: [r.category for r in classify(ToolCall("shell__run", {"command": cmd}), sh, "/sb")]
    assert cats("uci set firewall.@zone[1].input=REJECT && uci commit") == ["service"]
    assert cats("diskutil eraseDisk APFS X disk4") == ["hardware"]
    assert cats("gh pr create --fill") == ["disclosure"]
    assert cats("ls -la && cat README.md") == []
    assert cats("rm -rf ./build") == []                               # sandbox-relative: reversible


def test_full_agent_reports_dangers_and_mitigations(tmp_path):
    real = tmp_path / "site"
    real.mkdir()
    (real / "nginx.conf").write_text("events {}\n")
    (real / "systemctl").write_text("#!/bin/sh\necho \"would $*\"\n")
    (real / "systemctl").chmod(0o755)
    steps = [
        call("intent__set", intent="nginx.conf gains a gzip directive and the service restarts cleanly",
             criteria=[{"name": "gzip on", "kind": "file_contains", "path": "nginx.conf", "text": "gzip on;"},
                       {"name": "restarted", "kind": "ledger_ran", "pattern": "systemctl restart nginx"}],
             scope={"paths": ["nginx.conf", "nginx.conf.bak"]}, domain="web", task_type="config"),
        call("plan__advance", to="knowledge"),
        call("knowledge__decide", statement="gzip on; is valid in the http/events context of this file",
             tag="UNKNOWN", confidence=0.5, mitigation="sandbox_test"),
        call("plan__advance", to="baseline"),
        call("baseline__define", dimensions=[{"name": "conf exists", "check": {"kind": "file_exists", "path": "nginx.conf"}}]),
        call("plan__advance", to="execute"),
        call("plan__set", subtasks=["add gzip and restart"]),
        call("fs__write", path="nginx.conf", content="events {}\ngzip on;\n"),
        call("verify__run"),
        call("shell__run", command="PATH=.:$PATH systemctl restart nginx"),
        call("danger__premortem", **dict(PM, applies_to="systemctl restart nginx")),
        call("shell__run", command="PATH=.:$PATH systemctl restart nginx"),
        call("verify__run"),
        call("plan__step_done", evidence="green"),
        call("plan__advance", to="verify"), call("verify__run"),
        call("plan__advance", to="promote"), call("promote__run"),
        call("plan__advance", to="report"),
        call("task__complete", summary="Added gzip on; restarted nginx after a rehearsed recovery.")]
    ev = EventBus()
    agent = Agent(agent_config(tmp_path), ScriptedModel(steps), tools="local", events=ev, power_command=[])
    task = agent.run("Turn on gzip in nginx and restart it.", target=real)
    assert task.status == "done", task.result
    rep = task.state["report"]
    assert rep["dangers"] and rep["dangers"][0]["rehearsal"] == "rehearsed"
    assert "Danger: systemctl restart nginx" in ev.of("report")[0]["text"]
    assert not ev.of("confirm_request")


def test_brief_v3_confirm_list_c2_and_host_os():
    sh = Tool("shell__run", "", {}, lambda a: None, source="mcp:shell", destructive=True)
    cats = lambda cmd: [r.category for r in classify(ToolCall("shell__run", {"command": cmd}), sh, "/sb")]
    assert "host_os" in cats("softwareupdate -i -a")                          # would wipe OCLP root patches
    assert "host_os" in cats("sudo nvram boot-args='-v'")
    assert "host_os" in cats("softwareupdate --schedule on")                  # never changes update settings
    assert "account_security" in cats("sysadminctl -adminUser x -resetPasswordFor y")
    assert "account_security" in cats("spctl --master-disable")
    assert "credentials" in cats("security find-generic-password -s wifi -w")
    assert "credentials" in cats("sshpass -p hunter2 ssh admin@router")
    assert "permanent_deletion" in cats("rm -rf ~/.Trash/*")
    assert "permanent_deletion" in cats("tmutil delete /Volumes/Backup/old")
    assert cats("softwareupdate --list") == []                                # reading is fine
    router = Tool("router__set_admin_password", "", {}, lambda a: None, source="mcp:router", destructive=True)
    assert "credentials" in [r.category for r in classify(ToolCall("router__set_admin_password", {}), router, None)]
    ha = Tool("homeassistant__delete_automation", "", {}, lambda a: None, source="mcp:homeassistant", destructive=True)
    assert "permanent_deletion" in [r.category for r in classify(ToolCall(ha.name, {}), ha, None)]


def test_wright_never_types_your_password_even_if_confirmed(tmp_path):
    fill = Tool("browser__fill", "fill", obj({"selector": {"type": "string"}, "text": {"type": "string"}}),
                lambda a: ToolResult("filled"), source="mcp:browser", open_world=True)
    asked = []
    holder = {}
    reg = ToolRegistry()
    reg.register(fill)
    ev = EventBus()
    task = Task(goal="g", workdir=tmp_path)
    Harness(ScriptedModel([call("browser__fill", selector="input[type=password]", text="hunter2"),
                           call("browser__fill", selector="#username", text="sam"), say("ok")]),
            reg, ev, gates=[DangerGate(lambda *a: asked.append(a) or True, ev)]).run(task)
    blocked = ev.of("blocked")
    assert len(blocked) == 1 and "never types your passwords" in blocked[0]["reason"]
    assert asked == []                                                        # not even offered for approval
    assert task.state["checklist"]                                            # handed to the person instead
