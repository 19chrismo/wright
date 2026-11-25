"""Owner-only phone access (brief v3, owner decision 2026-11-25): the home network only, and only
devices the owner paired by scanning a one-time code on the Mac."""
import json
import os
import re
import time
import urllib.error
import urllib.request

import pytest

from agentkit import agent_config, can_launch_browser, chromium_path
from wright.app import App
from wright.control import route
from wright.devices import Devices, allowed_client, allowed_host
from wright.llm import ScriptedModel, say


def test_only_the_home_network_and_this_mac():
    for ok in ("192.168.50.10", "10.0.0.5", "172.16.3.4", "127.0.0.1", "::ffff:192.168.1.2", "fe80::1"):
        assert allowed_client(ok), ok
    for bad in ("8.8.8.8", "1.1.1.1", "2606:4700::1111", "not-an-ip"):
        assert not allowed_client(bad), bad
    for ok in ("192.168.50.20:7770", "macbook-air.local:7770", "localhost:7770", "127.0.0.1"):
        assert allowed_host(ok), ok
    for bad in ("evil.example.com", "8.8.8.8:7770", "wright.attacker.net:7770", "a.local.evil.com"):
        assert not allowed_host(bad), bad


def test_pairing_codes_are_one_time_and_expire(tmp_path, monkeypatch):
    d = Devices(tmp_path)
    code = d.new_code()
    tok = d.pair(code, "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X)")
    assert tok and d.check(tok) and d.list()[0]["name"] == "iPhone"
    assert d.pair(code) is None                                         # single use
    old = d.new_code()
    monkeypatch.setattr("wright.devices.time.time", lambda: time.monotonic() + 10 ** 10)
    assert d.pair(old) is None                                          # expired
    monkeypatch.undo()
    assert tok not in (tmp_path / "devices.json").read_text()           # only the hash is stored
    assert d.forget_all() == 1 and not d.check(tok)


@pytest.fixture
def lan_app(tmp_path):
    cfg = agent_config(tmp_path)
    cfg.data["ui"]["lan"] = True
    app = App(cfg, ScriptedModel([say("hi")]), tools="local", port=0, power_command=[]).start(window=False)
    app.exec = lambda exe, argv: None
    yield app
    app.stop()


def req(app, path, host=None, cookie=None):
    h = {"Host": host or f"192.168.50.20:{app.ui.port}"}
    if cookie:
        h["Cookie"] = cookie
    r = urllib.request.Request(f"http://127.0.0.1:{app.ui.port}{path}", headers=h)

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None
    try:
        with urllib.request.build_opener(NoRedirect).open(r, timeout=5) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def test_phone_pairs_by_scanning_and_stays_signed_in(lan_app):
    app = lan_app
    assert app.ui.httpd.server_address[0] == "0.0.0.0"                 # reachable on the home network
    code, _, body = req(app, "/")
    assert code == 401 and b"connect my phone" in body                 # unpaired device
    assert req(app, "/pair?c=guess")[0] == 403
    assert route("connect my phone")["action"] == "pair"
    app.control.handle(route("connect my phone"))
    card = app.events.of("pair_card")[-1]
    assert "<svg" in card["svg"] and re.search(r"/pair\?c=[\w-]+$", card["url"])
    pair_path = card["url"].split(str(app.ui.port), 1)[1]
    code, headers, _ = req(app, pair_path)
    assert code == 302 and "wright_device=" in headers["Set-Cookie"] and "HttpOnly" in headers["Set-Cookie"]
    cookie = headers["Set-Cookie"].split(";")[0]
    assert req(app, "/", cookie=cookie)[0] == 200                       # signed in from now on
    assert req(app, "/api/info", cookie=cookie)[0] == 200
    assert req(app, "/", host="evil.example.com", cookie=cookie)[0] == 403   # DNS-rebinding defence
    assert req(app, pair_path)[0] == 403                               # the code can't be reused
    app.control.handle(route("forget my phones"))
    assert req(app, "/", cookie=cookie)[0] == 401                       # revoked
    assert app.config.data["ui"]["lan"] is False


def test_turning_phone_access_on_restarts_once_and_then_shows_the_code(tmp_path):
    cfg = agent_config(tmp_path)
    app = App(cfg, ScriptedModel([say("hi")]), tools="local", port=0, power_command=[]).start(window=False)
    execed = []
    app.exec = lambda exe, argv: execed.append(argv)
    try:
        app.control.handle(route("connect my phone"))
        for _ in range(100):
            if execed:
                break
            time.sleep(0.05)
        assert execed and cfg.data["ui"]["lan"] is True and (cfg.paths.home / "pair-pending").exists()
    finally:
        os.environ.pop("WRIGHT_TOKEN", None); os.environ.pop("WRIGHT_PORT", None)
    after = App(agent_config(tmp_path), ScriptedModel([say("hi")]), tools="local", port=0, power_command=[])
    after.start(window=False)
    try:
        assert after.ui.lan and after.events.of("pair_card")                # the code appears by itself
        assert not (cfg.paths.home / "pair-pending").exists()
    finally:
        after.stop()


def test_unpaired_access_from_localhost_still_needs_the_launch_token(lan_app):
    assert req(lan_app, "/", host=f"127.0.0.1:{lan_app.ui.port}")[0] == 401
    assert req(lan_app, "/", host=f"127.0.0.1:{lan_app.ui.port}", cookie=f"wright={lan_app.ui.token}")[0] == 200


@pytest.mark.skipif(not can_launch_browser(), reason="no browser available")
def test_on_an_iphone_screen(lan_app):
    from playwright.sync_api import sync_playwright
    app = lan_app
    app.control.handle(route("connect my phone"))
    pair_path = app.events.of("pair_card")[-1]["url"].split(str(app.ui.port), 1)[1]
    code, headers, _ = req(app, pair_path)
    token = headers["Set-Cookie"].split(";")[0].split("=", 1)[1]
    with sync_playwright() as p:
        kw = {"executable_path": chromium_path(), "args": ["--no-sandbox"]} if chromium_path() else {}
        b = p.chromium.launch(**kw)
        ctx = b.new_context(**p.devices["iPhone 13"])
        ctx.add_cookies([{"name": "wright_device", "value": token, "domain": "127.0.0.1", "path": "/"}])
        page = ctx.new_page()
        page.goto(f"http://127.0.0.1:{app.ui.port}/")
        page.wait_for_selector("[data-testid=composer]")
        page.fill("[data-testid=composer]", "check setup")
        page.keyboard.press("Enter")
        page.wait_for_selector("[data-testid=setup]")
        width = page.evaluate("document.documentElement.scrollWidth")
        assert width <= 390                                                    # no sideways scrolling
        page.screenshot(path=os.path.join(os.environ.get("WRIGHT_SHOTS", "/tmp"), "wright-iphone.png"))
        b.close()
