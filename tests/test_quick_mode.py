"""Quick mode and the dispatcher: questions and research are answered read-only without the 8-stage
build process; anything that would change something becomes a proposed job with a 'Do it' button;
private requests stay on this Mac; estimates learn from finished jobs."""
from pathlib import Path

from agentkit import agent_config
from wright.agent import Agent
from wright.dispatch import classify, estimate, plan_request
from wright.llm import ScriptedModel, call, say


def test_requests_are_sorted_in_plain_terms():
    assert classify("how do I build a snow plow robot?").mode == "quick"
    assert classify("what's the best way to find Wi-Fi dead spots?").mode == "quick"
    assert classify("find out how to get the map out of my Roborock P10 locally").size == "large"
    d = classify("build me a small Mac app that shows my Wi-Fi speed")
    assert (d.mode, d.size, d.route) == ("build", "large", "auto")
    assert classify("can you make an STL for a 20 mm calibration cube").mode == "build"
    assert classify("think carefully and design a plow blade mount").route == "deep"
    assert classify("what's the weather like on Mars?").route == "fast"


def test_private_requests_stay_on_this_mac():
    d = classify("my wifi password is hunter2, put it in the router config")
    assert d.private and d.route == "local" and d.where == "this-mac"
    assert "stays on this Mac" in " ".join(d.reasons)
    d = classify("organise my tax return receipts", has_local=False)
    assert d.private and d.warning                      # says the local model is missing instead of sending it
    assert classify("anything", local_only=True).route == "local"


def test_estimate_learns_from_finished_jobs():
    d = classify("build me a web page that lists my 3D prints")
    first = estimate(d)
    assert "first guess" in first["basis"] and first["low"] < first["seconds"] < first["high"]
    hist = [{"mode": d.mode, "size": d.size, "where": d.where, "outcome": "done", "seconds": 100, "steps": 20}] * 3
    learned = estimate(d, hist)
    assert learned["seconds"] == 100 and "3 similar jobs" in learned["basis"]
    slow = estimate(classify("my bank password?"), gen_tps=2.0, prefill_tps=20.0)   # an old Mac, measured
    assert slow["where_text"].startswith("on this Mac") and slow["seconds"] > 60


def test_quick_mode_answers_read_only_and_proposes_the_build(tmp_path):
    cfg = agent_config(tmp_path)
    notes = tmp_path / "notes.txt"
    notes.write_text("P10 firmware 02.17\n")
    model = ScriptedModel([
        call("fs__read", path=str(notes)),                                  # reading is allowed
        call("fs__write", path=str(tmp_path / "x.txt"), content="nope"),   # writing is not
        call("mode__build", job="Set up local control for the P10 in Home Assistant"),
        say("Your P10 runs firmware 02.17. Local control needs … Sources: maker's docs."),
    ])
    agent = Agent(cfg, model, tools="local")
    d = agent.plan("find out how to control my P10 locally")
    assert d["mode"] == "quick"
    task = agent.run("find out how to control my P10 locally", dispatch=d)
    assert task.status == "done" and "02.17" in task.result
    assert not (tmp_path / "x.txt").exists()
    blocked = [r for r in task.ledger if r.get("blocked")]
    assert blocked and "quick mode" in blocked[0]["blocked"]
    rep = task.state["report"]
    assert rep["mode"] == "quick" and rep["proposed_build"].startswith("Set up local control")
    assert "lock" not in task.state                                        # no 8-stage process
    tools_offered = model.seen_tools[0]
    assert "fs__read" in tools_offered and "fs__write" not in tools_offered and "intent__set" not in tools_offered
    ep = agent.continuity.log.all()[-1]["entry"]
    assert ep["mode"] == "quick" and ep["where"] == d["where"]           # feeds the next estimate
    assert not list(Path(cfg.data["projects_dir"]).glob("*")) if Path(cfg.data["projects_dir"]).exists() else True
    agent.close()


def test_plan_request_carries_an_estimate():
    d = plan_request("design a phone stand I can 3D print")
    assert d.mode == "build" and d.estimate["text"] and d.estimate["where_text"]
