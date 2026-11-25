"""The window is the single control surface: setup, keys (yours and household members'), the local
model, privacy, undo and connections work from the chat, in plain words, before any model exists.
Keys typed into the key card never reach the transcript, the event log or a model."""
import json
import os
import time
from pathlib import Path

import pytest

from agentkit import agent_config
from wright.app import App
from wright.control import Control, route
from wright.events import EventBus
from wright.llm import ScriptedModel, say
from wright.mockup import MockUpstream
from wright.pool import Pool

CHROMIUM = "/opt/pw-browsers/chromium-1243/chrome-linux64/chrome"


@pytest.mark.parametrize("text,action,args", [
    ("add Sam's gemini key", "key_card", {"provider": "gemini", "owner": "Sam"}),
    ("add my api key", "key_card", {"provider": "gemini"}),
    ("use an OpenAI key for Alex", "key_card", {"provider": "openai", "owner": "Alex"}),
    ("check setup", "status", {}),
    ("what's missing?", "status", {}),
    ("show keys", "keys", {}),
    ("remove Sam's key", "remove_key", {"owner": "Sam"}),
    ("install the free local model", "install_local", {}),
    ("keep it private", "local_on", {}),
    ("use the cloud again", "local_off", {}),
    ("undo that", "undo", {}),
    ("/rollback 20261124-0101-abc", "undo", {"snapshot": "20261124-0101-abc"}),
    ("connect home assistant at http://homeassistant.local:8123/api/mcp", "connect",
     {"name": "home-assistant", "url": "http://homeassistant.local:8123/api/mcp"}),
])
def test_plain_words_reach_the_right_control(text, action, args):
    r = route(text)
    assert r and r["action"] == action
    for k, v in args.items():
        assert r["args"][k] == v


@pytest.mark.parametrize("text", [
    "add a key feature to my app", "build me a Mac app that tracks my runs",
    "harden my router and find my Wi-Fi dead spots", "make Home Assistant fully local",
    "undo the bad merge in my repo and add tests for the parser please"])
def test_real_goals_are_not_mistaken_for_controls(text):
    assert route(text) is None


@pytest.fixture
def ctl(tmp_path):
    cfg = agent_config(tmp_path)
    ev = EventBus()
    up = MockUpstream(ScriptedModel([say("ok")], loop_last=True),
                      keys={"AIzaSAM-valid-key-0001": {"ok_for": None}, "AIzaALEX-valid-key-002": {"ok_for": None}},
                      models=["models/gemini-3-pro", "models/gemini-3-flash", "models/gemini-2.5-flash"])
    cfg.data["pool"]["provider_urls"] = {"gemini": up.url}
    pool = Pool([], dict(cfg.data["pool"]["routes"]), key_lookup=lambda r: None)
    c = Control(cfg, ev, pool)
    c.keys.keychain = False
    yield c, ev, up, pool
    up.stop()


def test_household_key_added_from_the_card_without_ever_appearing(ctl):
    c, ev, up, pool = ctl
    bad = c.add_key("gemini", "AIzaWRONG-key-000000", owner="Sam")
    assert not bad["ok"] and "rejected" in bad["message"] and c.cfg.data["pool"]["lanes"][0]["id"] == "ollama"
    r = c.add_key("gemini", "AIzaSAM-valid-key-0001", owner="Sam", daily_cap=150)
    assert r["ok"] and r["lane"] == "gemini-sam"
    lane = [l for l in c.cfg.data["pool"]["lanes"] if l["id"] == "gemini-sam"][0]
    assert lane["owner"] == "Sam" and lane["daily_cap"] == 150 and c.keys.get("gemini_sam") == "AIzaSAM-valid-key-0001"
    assert [l.id for l in pool.lanes] == ["gemini-sam"]                          # live, no restart
    assert c.cfg.data["pool"]["routes"]["auto"][:2] == ["gemini-3-pro", "gemini-3-flash"]   # from live model list
    assert "AIzaSAM" not in json.dumps(ev.history)                               # never in the transcript
    assert "AIzaSAM" not in c.cfg.paths.config_file.read_text()                  # never in config.json
    c.add_key("gemini", "AIzaALEX-valid-key-002", owner="Alex")
    listed = {k["id"]: k for k in c.list_keys()}
    assert listed["gemini-alex"]["owner"] == "Alex" and listed["gemini-sam"]["daily_cap"] == 150
    c.remove_key("Sam")
    assert c.keys.get("gemini_sam") is None and [l.id for l in pool.lanes] == ["gemini-alex"]


def test_privacy_toggle_is_live(ctl):
    c, ev, up, pool = ctl
    c.handle(route("keep it private"))
    assert pool.local_only and c.cfg.data["local_only"]
    c.handle(route("use the cloud again"))
    assert not pool.local_only


def test_local_model_install_uses_what_the_mac_has(ctl, monkeypatch):
    c, ev, up, pool = ctl
    ran = []
    monkeypatch.setattr("wright.control.shutil.which", lambda n: None)
    monkeypatch.setattr("wright.control.sys.platform", "linux")
    r = c.install_local(wait=lambda s: None)
    assert not r["ok"] and "ollama.com/download" in r["message"]                 # one clear next step
    from wright.testkit import StaticSite
    tags = StaticSite({"/api/tags": ("application/json", json.dumps({"models": []}))})
    c.cfg.data["pool"]["lanes"][0]["base_url"] = tags.url + "/v1"
    monkeypatch.setattr("wright.control.shutil.which", lambda n: "/usr/local/bin/ollama" if n == "ollama" else None)
    c.runner = lambda argv, **kw: ran.append(argv) or type("P", (), {"returncode": 0, "stderr": ""})()
    r = c.install_local(wait=lambda s: None)
    assert r["ok"] and ran[-1][:2] == ["/usr/local/bin/ollama", "pull"]
    assert c.cfg.data["pool"]["routes"]["auto"][-1] == f"ollama:{ran[-1][2]}"
    tags.stop()


def test_undo_from_the_chat_restores_the_last_promotion_and_can_be_undone(tmp_path):
    from test_step14_promotion import head, tail
    from wright.agent import Agent
    from wright.llm import call
    real = tmp_path / "site"; real.mkdir()
    (real / "index.html").write_text("<h1>old</h1>\n")
    s = head([{"name": "new", "kind": "file_contains", "path": "index.html", "text": "new"}], ["index.html"],
             [{"name": "page", "check": {"kind": "file_exists", "path": "index.html"}}])
    s += [call("fs__write", path="index.html", content="<h1>new</h1>\n"), call("verify__run")] + tail("retitled")
    cfg = agent_config(tmp_path)
    agent = Agent(cfg, ScriptedModel(s), tools="local", power_command=[])
    assert agent.run("retitle", target=real).status == "done"
    c = Control(cfg, EventBus())
    assert c.handle(route("undo that"))["ok"]
    assert (real / "index.html").read_text() == "<h1>old</h1>\n"
    assert c.handle(route("undo"))["ok"]                                         # undo the undo
    assert (real / "index.html").read_text() == "<h1>new</h1>\n"


def test_goal_sent_before_setup_waits_then_starts_by_itself(tmp_path):
    cfg = agent_config(tmp_path)
    up = MockUpstream(ScriptedModel([say("ok")], loop_last=True), keys={"AIzaSAM-valid-key-0001": {"ok_for": None}})
    cfg.data["pool"]["provider_urls"] = {"gemini": up.url}
    cfg.data["pool"]["lanes"][0]["base_url"] = "http://127.0.0.1:9/v1"           # no Ollama here
    app = App(cfg, None, tools="local", port=0, power_command=[]).start(window=False)
    app.control.keys.keychain = False
    started = []
    app.agent.run = lambda text, **kw: started.append(text)
    try:
        code, body = app.ui.start_task({"text": "build me a tiny web page"})
        assert body == {"pending": True} and started == []
        assert app.ui.events.of("setup")[-1]["ready"] is False
        app.control.add_key("gemini", "AIzaSAM-valid-key-0001")
        app.ui.resume_pending()
        for _ in range(50):
            if started:
                break
            time.sleep(0.05)
        assert started == ["build me a tiny web page"]
    finally:
        app.stop(); up.stop()


@pytest.mark.skipif(not os.path.exists(CHROMIUM), reason="no chromium in this environment")
def test_everything_from_the_window(tmp_path):
    from playwright.sync_api import sync_playwright
    cfg = agent_config(tmp_path)
    up = MockUpstream(ScriptedModel([say("ok")], loop_last=True), keys={"AIzaSAM-valid-key-0001": {"ok_for": None}})
    cfg.data["pool"]["provider_urls"] = {"gemini": up.url}
    cfg.data["pool"]["lanes"][0]["base_url"] = "http://127.0.0.1:9/v1"
    app = App(cfg, None, tools="local", port=0, power_command=[]).start(window=False)
    app.control.keys.keychain = False
    try:
        with sync_playwright() as p:
            b = p.chromium.launch(executable_path=CHROMIUM, args=["--no-sandbox"])
            page = b.new_page(viewport={"width": 1000, "height": 1100}, device_scale_factor=2)
            page.goto(app.ui.url)
            setup = page.wait_for_selector("[data-testid=setup]")
            assert "Add a key" in setup.inner_text() and "Install it for me" in setup.inner_text()
            page.fill("[data-testid=composer]", "add Sam's gemini key")
            page.keyboard.press("Enter")
            card = page.wait_for_selector("[data-testid=key-card]")
            assert page.input_value("[data-testid=key-card] [name=owner]") == "Sam"
            page.fill("[data-testid=key-card] [name=cap]", "120")
            page.fill("[data-testid=key-card] [name=key]", "AIzaSAM-valid-key-0001")
            page.click("[data-testid=key-card] [data-k=save]")
            page.wait_for_selector("text=Added Sam's Gemini key")
            page.wait_for_function("document.querySelectorAll('[data-testid=setup]').length === 1 && "
                                   "document.querySelector('[data-testid=setup]').innerText.includes('READY')")
            page.fill("[data-testid=composer]", "show keys")
            page.keyboard.press("Enter")
            page.wait_for_selector("[data-testid=keys]")
            assert "Sam" in page.inner_text("[data-testid=keys]") and "0 / 120" in page.inner_text("[data-testid=keys]")
            page.screenshot(path=str(Path(os.environ.get("WRIGHT_SHOTS", "/tmp")) / "wright-setup.png"))
            assert "AIzaSAM" not in page.content()
            b.close()
    finally:
        app.stop(); up.stop()
    assert "AIzaSAM" not in json.dumps(app.events.history)


def test_show_keys_says_where_each_key_was_used_and_survives_restart(tmp_path):
    from wright.pool import Lane
    up = MockUpstream(ScriptedModel([say("ok")], loop_last=True), keys={"KEY-A-1111111111": {"ok_for": None}})
    usage = tmp_path / "usage.json"
    mk = lambda: Pool([Lane("gemini-sam", "gemini", ["gemini-*"], up.url, "a", daily_cap=2, owner="Sam")],
                      {"auto": ["gemini-2.5-flash"]}, key_lookup={"a": "KEY-A-1111111111"}.get, usage_file=usage,
                      max_wait=0)
    pool = mk()
    body = {"model": "auto", "messages": [{"role": "user", "content": "hi"}]}
    assert pool.complete(body)[0] == 200
    pool.annotate("gemini-sam", "retitle the family website")
    assert pool.complete(body)[0] == 200
    restarted = mk()                                              # Wright closed and reopened
    assert restarted.complete(body)[0] == 503                     # Sam's daily share of 2 still holds
    st = {l["id"]: l for l in restarted.status()["lanes"]}["gemini-sam"]
    assert st["total"] == 2 and st["used_today"] == 2 and st["last_model"] == "gemini-2.5-flash"
    assert st["last_task"] == "retitle the family website" and st["last_used"] > 0
    up.stop()
