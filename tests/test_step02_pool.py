"""Step 2: quota pool. Exhaust one key mid-task; the task completes. No 429 ever reaches the caller."""
import json
import time
import urllib.error
import urllib.request

import pytest

from wright.demos import step2
from wright.llm import ScriptedModel, say
from wright.mockup import MockUpstream
from wright.pool import Lane, Pool, PoolServer


def test_exhausted_key_mid_task_task_completes():
    r = step2(verbose=False)
    assert r["task"].status == "done"
    assert r["lanes_used"][:3] == ["gemini-a", "gemini-b", "gemini-a"]       # LRU rotation
    assert r["lanes_used"][3:] == ["ollama", "ollama"]                     # degraded to local
    kinds = [e["kind"] for e in r["events"].history]
    assert "lane_cooldown" in kinds
    # the harness never saw a usage-limit error
    assert not r["events"].of("setup_defect")
    assert all("429" not in (e.get("text") or "") for e in r["events"].of("tool_result"))


def test_keys_never_leave_except_to_their_own_lane():
    r = step2(verbose=False)
    transcript = json.dumps(r["events"].history)
    assert "KEY-A-0000000000" not in transcript and "KEY-B-1111111111" not in transcript
    local_calls = [x for x in r["upstream_log"] if x["key"] is None]
    assert all(x["auth_header"] == "" for x in local_calls)                 # no key sent to ollama


def _pool(upstream, keys, lanes, **kw):
    return Pool(lanes=lanes, routes={"auto": ["gemini-2.5-flash", "ollama:m"]},
                key_lookup=keys.get, **kw)


def _body():
    return {"model": "auto", "messages": [{"role": "user", "content": "hi"}]}


def test_daily_quota_cools_until_next_reset():
    up = MockUpstream(ScriptedModel([say("x")], loop_last=True),
                      keys={"K": {"ok_for": 0, "then": "429-daily"}})
    pool = _pool(up, {"k": "K"}, [Lane("g", "gemini", ["gemini-*"], up.url, "k")], max_wait=0)
    status, data, _ = pool.complete(_body())
    until = pool.cooldown[("g", "gemini-2.5-flash")]
    assert abs(until - pool.next_reset()) < 2              # cooled until the configured reset
    assert status == 503 and data["error"]["type"] == "wright_setup_defect"
    assert "install the free local model" in data["error"]["message"]   # one setup defect with the fix
    up.stop()


def test_short_cooldown_waits_instead_of_erroring():
    up = MockUpstream(ScriptedModel([say("after wait")], loop_last=True),
                      keys={"K": {"ok_for": 0, "then": "429", "retry": 5}})
    fake = {"now": time.time()}
    slept = []

    def sleep(s):
        slept.append(s)
        fake["now"] += s
        up.keys["K"] = {"ok_for": None}                    # quota refilled after the wait

    pool = _pool(up, {"k": "K"}, [Lane("g", "gemini", ["gemini-*"], up.url, "k")],
                 max_wait=60, clock=lambda: fake["now"], sleep=sleep)
    status, data, hdr = pool.complete(_body())
    assert status == 200 and data["choices"][0]["message"]["content"] == "after wait"
    assert slept and 4.9 < slept[0] < 5.2                  # honoured RetryInfo retryDelay "5s"
    up.stop()


def test_bad_key_disables_lane_and_rotates():
    up = MockUpstream(ScriptedModel([say("ok")], loop_last=True), keys={"GOOD": {"ok_for": None}})
    pool = _pool(up, {"bad": "WRONG", "good": "GOOD"},
                 [Lane("bad", "gemini", ["gemini-*"], up.url, "bad"),
                  Lane("good", "gemini", ["gemini-*"], up.url, "good")])
    status, _, hdr = pool.complete(_body())
    assert status == 200 and hdr["x-wright-lane"].startswith("good/")
    assert "wright keys add bad" in pool.stats["bad"].disabled
    up.stop()


def test_local_only_mode_never_calls_cloud():
    up = MockUpstream(ScriptedModel([say("cloud")], loop_last=True), keys={"K": {"ok_for": None}})
    loc = MockUpstream(ScriptedModel([say("local")], loop_last=True))
    pool = _pool(up, {"k": "K"}, [Lane("g", "gemini", ["gemini-*"], up.url, "k"),
                                  Lane("o", "ollama", ["ollama:*"], loc.url, local=True)],
                 local_only=True)
    status, data, _ = pool.complete(_body())
    assert data["choices"][0]["message"]["content"] == "local" and up.log == []
    up.stop(); loc.stop()


def test_proxy_requires_token_and_loopback():
    up = MockUpstream(ScriptedModel([say("ok")], loop_last=True))
    pool = _pool(up, {}, [Lane("o", "ollama", ["ollama:*"], up.url, local=True)])
    srv = PoolServer(pool, port=0).start()
    req = urllib.request.Request(srv.url + "/chat/completions", data=json.dumps(_body()).encode(),
                                 headers={"Content-Type": "application/json"})
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req, timeout=5)
    assert e.value.code == 401
    req.add_header("Authorization", f"Bearer {srv.token}")
    with urllib.request.urlopen(req, timeout=5) as r:
        assert r.status == 200 and r.headers["x-wright-lane"].startswith("o/")
    with pytest.raises(ValueError):
        PoolServer(pool, host="0.0.0.0", port=0)
    srv.stop(); up.stop()


def test_household_keys_each_keep_their_owner_and_daily_share():
    up = MockUpstream(ScriptedModel([say("ok")], loop_last=True),
                      keys={"KEY-ALEX-000000": {"ok_for": None}, "KEY-SAM-0000000": {"ok_for": None}})
    pool = _pool(up, {"alex": "KEY-ALEX-000000", "sam": "KEY-SAM-0000000"},
                 [Lane("gemini-alex", "gemini", ["gemini-*"], up.url, "alex", daily_cap=2, owner="Alex"),
                  Lane("gemini-sam", "gemini", ["gemini-*"], up.url, "sam", daily_cap=1, owner="Sam")],
                 max_wait=0)
    served = [pool.complete(_body())[2].get("x-wright-lane", "").split("/")[0] for _ in range(4)]
    assert served[:3].count("gemini-alex") == 2 and served[:3].count("gemini-sam") == 1
    assert served[3] == ""                                    # both shares used up: no lane may serve today
    st = {l["id"]: l for l in pool.status()["lanes"]}
    assert st["gemini-alex"]["owner"] == "Alex" and st["gemini-alex"]["used_today"] == 2
    assert st["gemini-sam"]["used_today"] == 1
    up.stop()
