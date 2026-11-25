"""Step 16: Mac packaging. One install command, one run command (single instance, one window), the
Mac stays awake while a task runs and the assertion is released after, and a task interrupted by
quitting (or a crash) resumes from its checkpoint."""
import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from agentkit import agent_config
from wright import power
from wright.agent import Agent
from wright.events import EventBus
from wright.llm import ScriptedModel, call, say
from wright.persist import unfinished

ROOT = Path(__file__).resolve().parents[1]


def test_macos_sleep_assertion_command(monkeypatch):
    monkeypatch.setattr(power.sys, "platform", "darwin")
    monkeypatch.setattr(power.shutil, "which", lambda n: "/usr/bin/" + n if n == "caffeinate" else None)
    assert power.assertion_command(4242) == ["caffeinate", "-i", "-w", "4242"]


def _alive(pattern):
    return subprocess.run(["pgrep", "-f", pattern], capture_output=True).returncode == 0


def test_mac_stays_awake_during_a_task_and_is_released_after(tmp_path):
    marker = "sleep 7431.25"
    seen = {}

    def probe(messages):
        seen["during"] = _alive(marker)
        return say("checking")
    ev = EventBus()
    agent = Agent(agent_config(tmp_path), ScriptedModel([probe]), tools="local", events=ev,
                  power_command=marker.split())
    agent.lock.idle_turns = 0
    agent.run("anything", target=tmp_path / "t")
    assert seen["during"] is True
    time.sleep(0.2)
    assert not _alive(marker)
    assert [e["state"] for e in ev.of("power")] == ["held", "released"]


CRASH = textwrap.dedent('''
    import os, sys
    sys.path.insert(0, {tests!r})
    from agentkit import agent_config
    from pathlib import Path
    from wright.agent import Agent
    from wright.llm import ScriptedModel, call
    def crash(m):
        os._exit(3)                       # the app is killed mid-task: no cleanup at all
    steps = [
        call("intent__set", intent="notes.txt holds three lines: one, two and three",
             criteria=[{{"name": "three lines", "kind": "command", "run": "test $(wc -l < notes.txt) -eq 3"}}],
             scope={{"paths": ["notes.txt"]}}, domain="files", task_type="write"),
        call("plan__advance", to="knowledge"),
        call("knowledge__decide", statement="appending lines with echo works", tag="UNKNOWN", confidence=0.5,
             mitigation="sandbox_test"),
        call("plan__advance", to="baseline"),
        call("baseline__define", dimensions=[]),
        call("plan__advance", to="execute"),
        call("plan__set", subtasks=["write lines"]),
        call("shell__run", command="echo one > notes.txt"),
        crash]
    a = Agent(agent_config(Path({tmp!r})), ScriptedModel(steps), tools="local", power_command=[])
    a.run("write notes.txt with one, two, three", target=Path({tmp!r}) / "proj")
''')


def test_task_interrupted_by_quitting_resumes_from_its_checkpoint(tmp_path):
    p = subprocess.run([sys.executable, "-c", CRASH.format(tests=str(ROOT / "tests"), tmp=str(tmp_path))],
                       cwd=ROOT, capture_output=True, text=True)
    assert p.returncode == 3, p.stderr
    cfg = agent_config(tmp_path)
    left = unfinished(cfg.paths.tasks)
    assert len(left) == 1
    saved = json.loads(left[0].read_text())
    assert saved["state"]["lock"]["state"] == "execute" and saved["steps"] == 8
    resumed_model = ScriptedModel([
        call("verify__run"),                      # [UNKNOWN] decision → budget 1: verify before the next change
        call("shell__run", command="echo two >> notes.txt && echo three >> notes.txt"),
        call("verify__run"), call("plan__step_done", evidence="green"),
        call("plan__advance", to="verify"), call("verify__run"),
        call("plan__advance", to="promote"), call("promote__run"),
        call("plan__advance", to="report"), call("task__complete", summary="Wrote three lines.")])
    ev = EventBus()
    agent = Agent(cfg, resumed_model, tools="local", events=ev, power_command=[])
    task = agent.resume(saved["id"])
    assert task.status == "done", task.result
    assert (tmp_path / "proj" / "notes.txt").read_text() == "one\ntwo\nthree\n"
    assert ev.of("resumed")[0]["state"] == "execute"
    digest = [m for m in resumed_model.seen[0] if m.get("role") == "user"][-1]["content"]
    assert "lock state: execute" in digest and "write lines" in digest          # continued, not restarted
    assert unfinished(cfg.paths.tasks) == []


def test_single_instance_reopens_the_existing_window(tmp_path):
    from wright.app import App, _running_instance
    app = App(agent_config(tmp_path), ScriptedModel([say("x")]), tools="local", port=0, power_command=[]).start(False)
    lock = tmp_path / "app.json"
    lock.write_text(json.dumps({"pid": os.getpid(), "url": app.ui.url}))
    try:
        assert _running_instance(lock) == app.ui.url
    finally:
        app.stop()
    assert _running_instance(lock) is None                                       # stale lock ignored


@pytest.mark.skipif(os.environ.get("WRIGHT_SKIP_INSTALL_TEST") == "1", reason="installer test disabled")
def test_one_command_install_and_run_command(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    env = {"HOME": str(home), "WRIGHT_HOME": str(home / ".wright"), "SHELL": "/bin/zsh",
           "PATH": "/usr/local/bin:/usr/bin:/bin", "PIP_DISABLE_PIP_VERSION_CHECK": "1"}
    p = subprocess.run(["sh", str(ROOT / "install.sh"), "--no-browser"], env={**env, "WRIGHT_NO_LAUNCH": "1"}, capture_output=True, text=True,
                       timeout=900)
    assert p.returncode == 0, p.stdout[-2000:] + p.stderr[-2000:]
    assert "harness self-test passed" in p.stdout
    exe = home / ".local" / "bin" / "wright"
    assert exe.exists()
    assert ".local/bin" in (home / ".zshrc").read_text()          # new Terminal windows find `wright`
    h = subprocess.run([str(exe), "--help"], env=env, capture_output=True, text=True)
    assert "open the app window (default)" in h.stdout


def test_app_opened_from_finder_still_finds_ollama_and_homebrew(tmp_path, monkeypatch):
    """Finder/Dock launches get PATH=/usr/bin:/bin:/usr/sbin:/sbin; ollama lives in /usr/local/bin
    (Intel) or /opt/homebrew/bin (Apple silicon)."""
    from wright import config
    fake = tmp_path / "usr-local-bin"
    fake.mkdir()
    (fake / "ollama").write_text("#!/bin/sh\necho ollama\n")
    (fake / "ollama").chmod(0o755)
    monkeypatch.setattr(config, "EXTRA_BIN_DIRS", [str(fake), str(tmp_path / "missing")])
    monkeypatch.setenv("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")
    assert config.ensure_path() == [str(fake)]
    import shutil
    assert shutil.which("ollama") == str(fake / "ollama")
    assert config.ensure_path() == []                                   # idempotent
