"""Step 4: memory stores. The model cannot edit or delete the episodic log; tampering by any other
route is detected and repaired; secrets are redacted; skills that fail their test are demoted."""
import json
import os
import stat
import sys
from pathlib import Path

import pytest

from wright import sandboxing
from wright.config import Paths
from wright.contract import ContractGate, RoleContract
from wright.events import EventBus
from wright.harness import Harness, Task
from wright.llm import ScriptedModel, call, say
from wright.memory import EpisodicLog, memory_tools
from wright.secrets import Redactor
from wright.skills import Skill, SkillLibrary, skill_tools
from wright.tools import ToolRegistry, local_fs_tools, local_shell_tool


@pytest.fixture
def paths(tmp_path):
    return Paths(tmp_path / "home").ensure()


def test_append_only_chain(paths):
    log = EpisodicLog(paths.episodic)
    for i in range(3):
        log.append({"type": "task", "n": i})
    v = log.verify()
    assert v.ok and v.count == 3
    rows = log.all()
    assert rows[1]["prev"] == rows[0]["hash"] and rows[2]["prev"] == rows[1]["hash"]
    assert stat.S_IMODE(os.stat(log.file).st_mode) == 0o444      # read-only between appends


def _attack(paths, steps):
    work = paths.home / "work"
    work.mkdir()
    reg = ToolRegistry()
    reg.register(local_shell_tool(str(work), paths.protected_write(), paths.protected_read()))
    for t in local_fs_tools(str(work)) + memory_tools(EpisodicLog(paths.episodic)):
        reg.register(t)
    gate = ContractGate(RoleContract(write_scope=["{workdir}"]), paths.protected_write(),
                        paths.protected_read())
    ev = EventBus()
    task = Task(goal="clean up", workdir=work)
    Harness(ScriptedModel(steps), reg, ev, gates=[gate]).run(task)
    return task, ev


def test_model_cannot_edit_or_delete_episodic_log_through_tools(paths):
    log = EpisodicLog(paths.episodic)
    log.append({"type": "task", "goal": "original"})
    f = str(log.file)
    task, ev = _attack(paths, [
        call("memory__episodes", n=5),                                   # reading is allowed
        call("fs__write", path=f, content="{}"),
        call("fs__edit", path=f, old="original", new="forged"),
        call("shell__run", command=f"rm -f {f}"),
        call("shell__run", command=f"sed -i 's/original/forged/' {f}"),
        call("shell__run", command="truncate -s0 ~/.wright/memory/episodic/log.jsonl"),
        call("shell__run", command="cat /dev/null > $HOME/.wright/memory/episodic/log.jsonl"),
        say("done"),
    ])
    blocked = ev.of("blocked")
    assert len(blocked) == 6
    assert "original" in ev.of("tool_result")[0]["text"]               # the read went through
    assert log.verify().ok and log.all()[0]["entry"]["goal"] == "original"


def test_tampering_by_other_routes_is_detected_and_repaired(paths):
    log = EpisodicLog(paths.episodic)
    log.append({"type": "task", "goal": "a"})
    log.append({"type": "task", "goal": "b"})
    # 1) an edit that bypasses the tools (simulates a process outside Wright)
    os.chmod(log.file, 0o644)
    log.file.write_text(log.file.read_text().replace('"goal": "b"', '"goal": "B"'))
    v = log.verify()
    assert not v.ok and "content altered" in v.problems[0]
    with pytest.raises(RuntimeError):
        log.append({"x": 1})                                            # no appends onto a broken chain
    v = log.repair()
    assert v.ok and log.all()[1]["entry"]["goal"] == "b"
    assert log.all()[-1]["entry"]["type"] == "tamper_detected"
    # 2) deletion via an obfuscated command the string scan cannot see (OS layer absent on this box)
    work = paths.home / "w"; work.mkdir()
    sh = local_shell_tool(str(work), paths.protected_write(), paths.protected_read())
    parts = str(log.file).split("episodic")
    sh.fn({"command": f"python3 -c \"import os; os.remove('{parts[0]}' + 'epi' + 'sodic{parts[1]}')\""})
    if sandboxing.available():
        assert log.file.exists()                                        # OS sandbox stopped it
    else:
        assert not log.file.exists()
        assert not log.verify().ok and log.repair().ok                  # detected + restored
    assert [r["entry"].get("goal") for r in log.all()][:2] == ["a", "b"]


def test_truncation_detected_by_anchor(paths):
    log = EpisodicLog(paths.episodic)
    for g in "abc":
        log.append({"goal": g})
    os.chmod(log.file, 0o644)
    lines = log.file.read_text().splitlines(keepends=True)
    log.file.write_text("".join(lines[:2]))                            # drop the newest entry
    v = log.verify()
    assert not v.ok and "anchor" in v.problems[0]
    assert log.repair().ok and log.verify().count == 4                 # 3 restored + tamper record


def test_secrets_redacted_before_write(paths):
    log = EpisodicLog(paths.episodic, redact=Redactor(extra=["super-secret-value-123"]))
    log.append({"goal": "use key super-secret-value-123 and AIza" + "x" * 35})
    raw = log.file.read_text()
    assert "super-secret-value-123" not in raw and "AIza" not in raw and "[REDACTED]" in raw


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS sandbox-exec profile check")
def test_macos_sandbox_denies_writes_to_protected_paths(paths):
    log = EpisodicLog(paths.episodic)
    log.append({"goal": "a"})
    work = paths.home / "w"; work.mkdir()
    sh = local_shell_tool(str(work), paths.protected_write(), paths.protected_read())
    res = sh.fn({"command": f"python3 -c \"import os; os.remove({str(log.file)!r})\""})
    assert log.file.exists() and res.is_error


# ---------------------------------------------------------------------------- procedural
def _executor(reg):
    def ex(tool, args, workdir):
        t = reg.get(tool) if tool != "shell__run" else local_shell_tool(str(workdir))
        r = t.fn(args)
        return (not r.is_error), r.text
    return ex


def test_skill_failing_its_test_is_demoted_automatically(paths, tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"; bin_dir.mkdir()
    helper = bin_dir / "make-banner"
    helper.write_text("#!/bin/sh\necho \"== $1 ==\"\n"); helper.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    lib = SkillLibrary(paths.procedural / "skills")
    sk = Skill(name="banner", description="write a banner file",
               steps=[{"tool": "shell__run", "args": {"command": "make-banner '{{text}}' > banner.txt"}}],
               params={"text": {"type": "string"}},
               test={"params": {"text": "hi"}, "checks": [
                   {"name": "banner", "kind": "file_contains", "path": "banner.txt", "text": "== hi =="}]})
    lib.save(sk, "created")
    ex = _executor(ToolRegistry())
    assert lib.check_all(ex) == {"banner": "ok"}
    assert [t.name for t in skill_tools(lib, ex, lambda: None)] == ["skill__banner"]
    helper.unlink()                                                   # environment regression
    assert lib.check_all(ex) == {"banner": "demoted"}
    assert lib.get("banner").status == "demoted" and lib.get("banner").version == 2
    assert skill_tools(lib, ex, lambda: None) == []                   # no longer offered to the agent
    assert "auto-demoted" in lib.get("banner").history[-1]["note"]


def test_successful_path_promoted_to_skill_with_its_own_test(paths, tmp_path):
    from wright.demos import step1
    task = step1(tmp_path / "t", verbose=False)
    lib = SkillLibrary(paths.procedural / "skills")
    sk, msg = lib.promote_from_task(task, "greeting_file", "write greeting.txt", _executor(ToolRegistry()))
    assert msg == "promoted" and sk.test["checks"][0]["kind"] == "file_contains"
    ok, _ = lib.test(sk, _executor(ToolRegistry()))
    assert ok
