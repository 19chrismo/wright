"""Project notebooks (roadmap step 2). Jobs that last weeks keep a notebook: goal, phases, parts list,
decisions and why, what was printed and tested, what's next. "Work on the snow bot" picks up where it
left off; any request that names a project becomes one of its jobs (notebook in context, the project's
own folder, recorded at the end); projects appear as cards on the home screen; a private project keeps
every job on this Mac."""
import json
import os
from pathlib import Path

import pytest

from agentkit import agent_config, can_launch_browser, launch
from test_step14_promotion import head
from wright.agent import Agent
from wright.contract import ContractGate, RoleContract
from wright.control import Control, route
from wright.events import EventBus
from wright.harness import Task
from wright.llm import ScriptedModel, ToolCall, call, say
from wright.projects import Projects, parse_new


@pytest.mark.parametrize("text,name,goal", [
    ("New project: snow bot — a robot that clears our driveway", "Snow bot", "a robot that clears our driveway"),
    ("start a project called the P10 map", "P10 map", ""),
    ("start a new project to build an accounting helper for work", "Accounting helper",
     "build an accounting helper for work"),
    ("Create a project: Garden sensors, watering alerts for the beds", "Garden sensors", "watering alerts for the beds"),
])
def test_starting_a_project_in_plain_words(text, name, goal):
    assert route(text)["action"] == "new_project"
    assert parse_new(text) == (name, goal)


@pytest.mark.parametrize("text", ["make a project plan for the garden", "build me a project tracker app",
                                  "what's a good first robotics project?"])
def test_requests_that_only_mention_projects_are_not_commands(text):
    r = route(text)
    assert r is None or r["action"] != "new_project"


def test_finding_a_project_the_way_people_say_it(tmp_path):
    pr = Projects(tmp_path / "nb", tmp_path / "Wright Projects")
    bot = pr.create("Snow bot", "clears the driveway")
    p10 = pr.create("P10 map")
    acc = pr.create("Accounting helper", "monthly reports for work")
    assert bot["folder"] == str(tmp_path / "Wright Projects" / "snow-bot")
    for said in ("the snow bot", "snow bot project", "Snow Bot", "snow"):
        assert pr.find(said)["id"] == bot["id"], said
    assert pr.find("my CV") is None
    assert pr.mentioned("design the motor mount for the snow bot")["id"] == bot["id"]
    assert pr.mentioned("p10 map: export the rooms")["id"] == p10["id"]
    assert pr.mentioned("how do snow plows work?") is None              # one word is not the project
    assert acc["private"] and not bot["private"]                         # work accounting stays home
    assert pr.create("snow bot")["id"] == bot["id"]                      # same name, same notebook


def test_notebook_changes_are_checked_and_read_back(tmp_path):
    pr = Projects(tmp_path / "nb", tmp_path / "projects")
    pid = pr.create("Snow bot")["id"]
    p, said = pr.update(pid, {
        "decisions": [{"what": "Use 24 V", "why": "the motors are rated 24 V"}],
        "parts": [{"item": "Wheelchair motor", "qty": "2", "status": "have"}, {"item": "24 V battery", "status": "??"}],
        "tests": [{"what": "Motor mount fit test v1", "result": "0.3 mm too tight", "passed": False}],
        "phases": [{"name": "Frame", "status": "doing"}],
        "next": ["Print mount v2 with 0.3 mm more clearance", ""]})
    assert said == "1 decision, 2 parts, 1 test, 1 phase, next steps"
    assert p["parts"][1]["status"] == "need"                             # unknown status → need
    assert p["next"] == ["Print mount v2 with 0.3 mm more clearance"]
    p, _ = pr.update(pid, {"parts": [{"item": "24 v BATTERY", "status": "ordered"}],
                           "phases": [{"name": "frame", "status": "done"}]})
    assert len(p["parts"]) == 2 and p["parts"][1]["status"] == "ordered"   # the same item is updated
    assert p["phases"] == [{"name": "Frame", "status": "done"}]
    assert pr.update(pid, {})[1] == "nothing to change"
    text = pr.render(pr.get(pid))
    assert "Use 24 V — why: the motors are rated 24 V" in text
    assert "Motor mount fit test v1 → 0.3 mm too tight (failed)" in text and "1) Print mount v2" in text
    assert len(pr.render({**pr.get(pid), "notes": [{"t": 0, "text": "x" * 800}] * 40})) <= 4000


def test_a_question_about_a_project_reads_and_writes_its_notebook(tmp_path):
    cfg = agent_config(tmp_path)
    model = ScriptedModel([
        call("notebook__update", decisions=[{"what": "Use a 24 V lead-acid pack", "why": "cheap, and the motors are 24 V"}],
             next=["Price two 12 V batteries"]),
        say("A 24 V lead-acid pack suits those motors."),
    ])
    agent = Agent(cfg, model, tools="local")
    p = agent.projects.create("Snow bot", "clears the driveway")
    d = agent.plan("which battery should the snow bot use?")
    assert (d["mode"], d["size"], d["project"]["id"]) == ("quick", "small", p["id"])
    task = agent.run("which battery should the snow bot use?", dispatch=d)
    assert task.status == "done"
    assert "PROJECT NOTEBOOK — Snow bot" in json.dumps(model.seen[0], ensure_ascii=False)        # read before answering
    assert "notebook__update" in model.seen_tools[0]
    nb = agent.projects.get(p["id"])
    assert nb["decisions"][0]["why"] == "cheap, and the motors are 24 V" and nb["next"] == ["Price two 12 V batteries"]
    assert nb["jobs"][-1]["goal"] == "which battery should the snow bot use?" and nb["jobs"][-1]["outcome"] == "done"
    assert task.state["report"]["project"] == {"id": p["id"], "name": "Snow bot"}
    assert agent.continuity.log.all()[-1]["entry"]["project"] == p["id"]
    assert not Path(nb["folder"]).exists()                                   # a question changes no files
    agent.close()


def test_a_project_build_happens_in_its_folder_and_picks_up_next_time(tmp_path):
    cfg = agent_config(tmp_path)
    s = head([{"name": "mount", "kind": "file_contains", "path": "mount.scad", "text": "cube"}], ["mount.scad"],
             [{"name": "readme", "check": {"kind": "file_exists", "path": "README.md"}}])
    s += [call("fs__write", path="mount.scad", content="cube([40, 20, 5]);\n"),
          call("notebook__update", parts=[{"item": "M5 bolt", "qty": "4"}],
               tests=[{"what": "mount.scad renders", "result": "renders", "passed": True}]),
          call("verify__run"), call("plan__step_done", evidence="green"), call("plan__advance", to="verify"),
          call("verify__run"), call("plan__advance", to="promote"), call("promote__run"),
          call("plan__advance", to="report"), call("notebook__update", next=["Print the mount and test the fit"]),
          call("task__complete", summary="Designed the motor mount.")]
    model = ScriptedModel(s)
    agent = Agent(cfg, model, tools="local", power_command=[])
    p = agent.projects.create("Snow bot")
    folder = Path(p["folder"])
    folder.mkdir(parents=True)
    (folder / "README.md").write_text("snow bot\n")                          # files from earlier jobs
    goal = "snow bot: design the motor mount as an OpenSCAD file"
    d = agent.plan(goal)
    assert d["mode"] == "build" and d["size"] == "medium" and d["project"]["name"] == "Snow bot"
    task = agent.run(goal, dispatch=d)
    assert task.status == "done", task.result
    assert (folder / "mount.scad").exists() and (folder / "README.md").exists()   # the project's own folder
    assert "PROJECT NOTEBOOK — Snow bot" in json.dumps(model.seen[0], ensure_ascii=False)
    nb = agent.projects.get(p["id"])
    assert nb["parts"][0]["item"] == "M5 bolt" and nb["tests"][0]["passed"] is True
    assert nb["next"] == ["Print the mount and test the fit"] and nb["jobs"][-1]["outcome"] == "done"
    ev = EventBus()
    assert Control(cfg, ev, agent=agent).handle(route("work on the snow bot"))["ok"]   # next time
    assert ev.of("notebook")[-1]["project"]["next"] == ["Print the mount and test the fit"]
    agent.close()


def test_a_private_project_keeps_its_jobs_and_notebook_on_this_mac(tmp_path):
    cfg = agent_config(tmp_path)
    cfg.data["pool"]["lanes"].insert(0, {"id": "gemini-1", "provider": "gemini", "models": ["gemini-*"],
                                         "key_ref": "gemini_1"})
    agent = Agent(cfg, ScriptedModel([say("ok")]), tools="local")
    agent.projects.create("Accounting helper", "monthly reports for work")
    bot = agent.projects.create("Snow bot")
    d = agent.plan("accounting helper: add a sheet that adds up the month")
    assert d["where"] == "this-mac" and "private “Accounting helper” project" in " ".join(d["reasons"])
    assert agent.plan("snow bot: what motor?")["where"] == "online"
    agent.current = Task(goal="x", workdir=tmp_path, state={"dispatch": {"where": "online"}, "project": bot["id"]})
    r = agent.registry.get("notebook__read").fn({"project": "accounting helper"})
    assert r.is_error and "private" in r.text                                # an online job can't read it
    assert "PROJECT NOTEBOOK — Snow bot" in agent.registry.get("notebook__read").fn({}).text
    agent.current = None
    agent.close()


def test_only_notebook_update_can_change_a_notebook(tmp_path):
    cfg = agent_config(tmp_path)
    agent = Agent(cfg, ScriptedModel([say("ok")]), tools="local")
    p = agent.projects.create("Snow bot")
    gate = ContractGate(RoleContract(), agent.deny_w, agent.deny_r)
    f = agent.paths.projects / p["id"] / "notebook.json"
    v = gate(Task(goal="x", workdir=tmp_path), ToolCall("fs__write", {"path": str(f), "content": "{}"}),
             agent.registry.get("fs__write"))
    assert not v.allow
    agent.close()


def test_work_on_opens_a_notebook_only_when_the_project_exists(tmp_path):
    from wright.app import App
    cfg = agent_config(tmp_path)
    app = App(cfg, ScriptedModel([say("ok")], loop_last=True), tools="local", port=0, power_command=[]).start(window=False)
    app.agent.run = lambda text, **kw: None
    try:
        app.agent.projects.create("Snow bot")
        assert app.ui.start_task({"text": "work on the snow bot"})[1]["routed"] == "project_open"
        assert app.events.of("notebook")[-1]["project"]["name"] == "Snow bot"
        body = app.ui.start_task({"text": "work on my CV"})[1]
        assert "routed" not in body and "needs_go" in body                   # an ordinary request
        assert app.ui.start_task({"text": "my projects"})[1]["routed"] == "projects"
        assert app.ui.start_task({"text": "start a project"})[1]["ok"] is False   # asks for a name
        assert app.info()["projects"][0]["name"] == "Snow bot"
    finally:
        app.stop()


@pytest.mark.skipif(not can_launch_browser(), reason="no browser available")
def test_projects_on_the_home_screen_and_continue_from_the_notebook(tmp_path):
    from playwright.sync_api import sync_playwright
    from wright.app import App
    cfg = agent_config(tmp_path)
    app = App(cfg, ScriptedModel([say("ok")], loop_last=True), tools="local", port=0, power_command=[]).start(window=False)
    pr = app.agent.projects
    pid = pr.create("Snow bot", "a robot that clears the driveway")["id"]
    pr.update(pid, {"phases": [{"name": "Motors", "status": "done"}, {"name": "Frame", "status": "doing"}],
                    "decisions": [{"what": "Use 24 V", "why": "the motors are rated 24 V"}],
                    "parts": [{"item": "Wheelchair motor", "qty": "2", "status": "have"}],
                    "next": ["Design the motor mount"]})
    shots = Path(os.environ.get("WRIGHT_SHOTS", "/tmp"))
    try:
        with sync_playwright() as p:
            b = launch(p)
            page = b.new_page(viewport={"width": 1000, "height": 1100}, device_scale_factor=2)
            page.goto(app.ui.url)
            item = page.wait_for_selector("[data-testid=projects] [data-testid=project]")
            assert "Next: Design the motor mount" in item.inner_text() and "1 of 2 phases done" in item.inner_text()
            assert page.locator(".tile", has_text="New project").count() == 1
            page.screenshot(path=str(shots / "wright-projects-home.png"))
            item.click()
            nb = page.wait_for_selector("[data-testid=notebook]")
            text = nb.inner_text()
            assert "Use 24 V" in text and "the motors are rated 24 V" in text and "Wheelchair motor" in text
            page.screenshot(path=str(shots / "wright-notebook.png"))
            page.click("[data-testid=notebook] [data-k=next]")
            card = page.wait_for_selector("[data-testid=plan-card]")
            assert "Snow bot" in card.inner_text() and "Design the motor mount" in card.inner_text()
            page.click("[data-testid=plan-card] [data-k=cancel]")
            page.wait_for_selector("text=Nothing was started")
            page.fill("[data-testid=composer]", "New project: P10 map — get the vacuum's map out locally")
            page.keyboard.press("Enter")
            page.wait_for_selector("text=Started the “P10 map” project")
            assert page.locator("[data-testid=notebook] [data-k=plan]").count() == 1   # "Plan it with me"
            b.close()
    finally:
        app.stop()
