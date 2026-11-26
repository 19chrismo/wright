"""Speed: the fast online model answers first, saved pro-first settings are migrated, and a resting
online key is waited for (briefly) instead of handing the work to this Mac's slow local model."""
import json
import time

from agentkit import agent_config
from wright.config import Config, normalize_routes
from wright.doctor import _fix_gemini_route
from wright.llm import ScriptedModel, say
from wright.mockup import MockUpstream
from wright.pool import Lane, Pool


def test_routes_put_the_fast_model_first_and_stay_consistent():
    r = normalize_routes({"auto": ["gemini-2.5-pro", "gemini-2.5-flash", "ollama:qwen2.5:3b"]})
    assert r["auto"] == ["gemini-2.5-flash", "gemini-2.5-pro", "ollama:qwen2.5:3b"]
    assert r["fast"] == ["gemini-2.5-flash", "ollama:qwen2.5:3b"]
    assert r["deep"] == ["gemini-2.5-pro", "gemini-2.5-flash", "ollama:qwen2.5:3b"]
    assert r["local"] == ["ollama:qwen2.5:3b"]


def test_a_pro_only_route_gets_the_matching_flash_model():
    # seen on the owner's Mac: auto = [gemini-2.5-pro, local]; a free key may not offer pro at all
    r = normalize_routes({"auto": ["gemini-2.5-pro", "ollama:llama3.2:3b"]})
    assert r["auto"] == ["gemini-2.5-flash", "gemini-2.5-pro", "ollama:llama3.2:3b"]
    assert r["fast"][0] == "gemini-2.5-flash"


def test_a_saved_pro_first_config_is_migrated_on_load(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.json").write_text(json.dumps({"pool": {"routes": {
        "auto": ["gemini-3-pro", "gemini-3-flash", "ollama:llama3.2:3b"],
        "fast": ["gemini-3-flash", "ollama:llama3.2:3b"]}}}))
    cfg = Config.load(home)
    routes = cfg.data["pool"]["routes"]
    assert routes["auto"] == ["gemini-3-flash", "gemini-3-pro", "ollama:llama3.2:3b"]
    assert routes["local"] == ["ollama:llama3.2:3b"]


def test_live_model_list_fixer_keeps_flash_first(tmp_path):
    cfg = agent_config(tmp_path)
    _fix_gemini_route(cfg, ["gemini-3-pro", "gemini-3-flash", "gemini-3-flash-lite", "gemini-2.5-flash"],
                      lambda *_: None)
    assert cfg.data["pool"]["routes"]["auto"][:2] == ["gemini-3-flash", "gemini-3-pro"]


def _pool(up, loc, clock, sleep, wait):
    return Pool([Lane("g", "gemini", ["gemini-*"], up.url, "k"),
                 Lane("o", "ollama", ["ollama:*"], loc.url, local=True)],
                {"auto": ["gemini-2.5-flash", "ollama:m"]}, key_lookup={"k": "K"}.get,
                max_wait=120, clock=clock, sleep=sleep, prefer_cloud_wait=wait)


def test_a_short_rest_is_waited_out_instead_of_using_the_slow_local_model():
    up = MockUpstream(ScriptedModel([say("cloud")], loop_last=True), keys={"K": {"ok_for": 0, "then": "429", "retry": 20}})
    loc = MockUpstream(ScriptedModel([say("local")], loop_last=True))
    fake = {"now": time.time()}
    events = []

    def sleep(s):
        fake["now"] += s
        up.keys["K"] = {"ok_for": None}

    pool = _pool(up, loc, lambda: fake["now"], sleep, wait=75)
    pool.on_event = events.append
    status, data, hdr = pool.complete({"model": "auto", "messages": [{"role": "user", "content": "hi"}]})
    assert status == 200 and hdr["x-wright-lane"].startswith("g/")          # waited ~20 s, then Gemini
    assert not any(e["kind"] == "local_fallback" for e in events)
    up.stop(), loc.stop()


def test_a_long_rest_still_falls_back_to_the_local_model_and_says_so():
    up = MockUpstream(ScriptedModel([say("cloud")], loop_last=True), keys={"K": {"ok_for": 0, "then": "429", "retry": 600}})
    loc = MockUpstream(ScriptedModel([say("local")], loop_last=True))
    fake = {"now": time.time()}
    events = []

    def sleep(s):
        fake["now"] += s

    pool = _pool(up, loc, lambda: fake["now"], sleep, wait=75)
    pool.on_event = events.append
    status, data, hdr = pool.complete({"model": "auto", "messages": [{"role": "user", "content": "hi"}]})
    assert status == 200 and hdr["x-wright-lane"].startswith("o/")          # 10 min away: use the Mac now
    assert any(e["kind"] == "local_fallback" for e in events)
    up.stop(), loc.stop()
