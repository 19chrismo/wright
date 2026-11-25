"""Step 15: UI. One window, one chat. Grounding tags, danger report, regression status, calibration
number and integrity flags are visible in the transcript; a pasted/attached image reaches the model;
the UI refuses anything but localhost and requires the launch token."""
import json
import os
import struct
import urllib.error
import urllib.request
import zlib
from pathlib import Path

import pytest

from agentkit import agent_config, can_launch_browser, launch
from wright.app import App
from wright.llm import ScriptedModel, call, say

CHROMIUM = "/opt/pw-browsers/chromium-1243/chrome-linux64/chrome"
SHOTS = Path(os.environ.get("WRIGHT_SHOTS", "/tmp"))


def png(w=8, h=8):
    raw = b"".join(b"\x00" + b"\x20\x60\xd0" * w for _ in range(h))
    ch = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))
    return (b"\x89PNG\r\n\x1a\n" + ch(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + ch(b"IDAT", zlib.compress(raw)) + ch(b"IEND", b""))


PM = dict(applies_to="systemctl restart nginx",
          failure_modes=[{"mode": "bad config stops nginx"}, {"mode": "ssh session dropped"}],
          mitigations=[{"mode": "bad config stops nginx", "action": "nginx -t first"},
                       {"mode": "ssh session dropped", "action": "never touch sshd"}],
          recovery={"procedure": "restore backup", "rollback_command": "cp nginx.conf.bak nginx.conf",
                    "rehearse_command": "cp nginx.conf nginx.conf.bak && cmp nginx.conf nginx.conf.bak"})

SCRIPT = [
    call("fs__write", path="nginx.conf", content="x"),                       # blocked: integrity flag
    call("intent__set", intent="nginx.conf turns gzip on (as the screenshot asks) and nginx restarts",
         criteria=[{"name": "gzip on", "kind": "file_contains", "path": "nginx.conf", "text": "gzip on;"}],
         scope={"paths": ["nginx.conf", "nginx.conf.bak"]}, domain="web", task_type="config"),
    call("plan__advance", to="knowledge"),
    call("knowledge__capture", locator="nginx.conf", tier=3, excerpt="events {}"),
    call("knowledge__decide", statement="the file has no gzip directive yet", tag="INFERRED", confidence=0.7,
         sources=["__SID__"], chain="the captured file only contains an events block"),
    call("knowledge__decide", statement="gzip on; is accepted at top level here", tag="UNKNOWN",
         confidence=0.5, mitigation="sandbox_test"),
    call("plan__advance", to="baseline"),
    call("baseline__define", dimensions=[{"name": "conf exists", "check": {"kind": "file_exists", "path": "nginx.conf"}}]),
    call("plan__advance", to="execute"),
    call("plan__set", subtasks=["enable gzip and restart"]),
    call("fs__write", path="nginx.conf", content="events {}\ngzip on;\n"),
    call("verify__run"),
    call("danger__premortem", **PM),
    call("shell__run", command="echo would systemctl restart nginx"),
    call("verify__run"),
    call("plan__step_done", evidence="green"),
    call("plan__advance", to="verify"), call("verify__run"),
    call("plan__advance", to="promote"), call("promote__run"),
    call("plan__advance", to="report"),
    call("task__complete", summary="Turned gzip on in nginx.conf and restarted nginx after a rehearsed recovery."),
]


def fill_sid(steps):
    out = []
    for s in steps:
        if s.tool_calls and s.tool_calls[0].args.get("sources") == ["__SID__"]:
            args = dict(s.tool_calls[0].args)

            def f(m, a=args):
                from agentkit import last_json
                return call("knowledge__decide", **{**a, "sources": [last_json(m, "source_id")]})
            out.append(f)
        else:
            out.append(s)
    return out


@pytest.mark.skipif(not can_launch_browser(), reason="no browser available")
def test_one_window_chat_shows_integrity_information(tmp_path):
    from playwright.sync_api import sync_playwright
    real = tmp_path / "site"
    real.mkdir()
    (real / "nginx.conf").write_text("events {}\n")
    model = ScriptedModel(fill_sid(SCRIPT))
    app = App(agent_config(tmp_path), model, tools="local", port=0, power_command=[]).start(window=False)
    shot = tmp_path / "screenshot.png"
    shot.write_bytes(png())
    try:
        with sync_playwright() as p:
            b = launch(p)
            page = b.new_page(viewport={"width": 1100, "height": 1400}, device_scale_factor=2)
            page.goto(app.ui.url)
            page.set_input_files("#file", str(shot))                          # attach a screenshot
            page.wait_for_selector("#thumbs img")
            page.fill("#target", str(real))
            page.fill("[data-testid=composer]", "Turn on gzip like the screenshot shows, then restart nginx.")
            page.keyboard.press("Enter")
            page.wait_for_selector("[data-testid=report]", timeout=60000)
            assert page.locator("[data-testid=blocked]").count() >= 1
            assert page.locator(".tag.INFERRED").first.is_visible()
            assert page.locator(".tag.UNKNOWN").first.is_visible()
            assert "rehearsed" in page.locator("[data-testid=danger]").first.inner_text().lower()
            assert "systemctl restart nginx" in page.locator("[data-testid=danger-report]").inner_text()
            assert "green" in page.locator("[data-testid=regression]").inner_text()
            assert "measured" in page.locator("[data-testid=calibration]").inner_text()
            assert int(page.locator("[data-testid=flags] b").inner_text()) >= 1
            assert "done" in page.locator("[data-testid=lock-state]").inner_text()
            assert page.locator(".msg.user img").count() == 1
            page.locator("[data-testid=report]").scroll_into_view_if_needed()
            page.screenshot(path=str(SHOTS / "wright-ui.png"), full_page=False)
            b.close()
    finally:
        app.stop()
    first_user = [m for m in model.seen[0] if m["role"] == "user"][0]["content"]
    assert any(part.get("type") == "image_url" and part["image_url"]["url"].startswith("data:image/png;base64,")
               for part in first_user)                                          # the image reached the model
    assert (real / "nginx.conf").read_text() == "events {}\ngzip on;\n"
    rep = app.agent.continuity.log.all()[-1]["entry"]
    assert rep["outcome"] == "done"


def test_ui_refuses_foreign_hosts_and_missing_token(tmp_path):
    app = App(agent_config(tmp_path), ScriptedModel([say("hi")]), tools="local", port=0,
              power_command=[]).start(window=False)
    base = f"http://127.0.0.1:{app.ui.port}"
    try:
        def get(path, headers=None):
            req = urllib.request.Request(base + path, headers=headers or {})
            try:
                with urllib.request.urlopen(req, timeout=5) as r:
                    return r.status
            except urllib.error.HTTPError as e:
                return e.code
        assert get("/") == 401
        assert get("/api/info") == 401
        assert get("/", {"Host": "evil.example"}) == 403                       # DNS-rebinding defence
        assert get("/api/info", {"Cookie": f"wright={app.ui.token}"}) == 200
        assert get("/api/info", {"Cookie": f"wright={app.ui.token}", "Origin": "http://evil.example"}) == 403
        with pytest.raises(ValueError):
            from wright.ui.server import UIServer
            UIServer({}, None, None, host="0.0.0.0", port=0)
    finally:
        app.stop()


def test_window_uses_the_theme_wright_maintains(tmp_path):
    app = App(agent_config(tmp_path), ScriptedModel([say("hi")]), tools="local", port=0,
              power_command=[]).start(window=False)
    base = f"http://127.0.0.1:{app.ui.port}"
    try:
        get = lambda: urllib.request.urlopen(urllib.request.Request(
            base + "/theme.css", headers={"Cookie": f"wright={app.ui.token}"}), timeout=5).read().decode()
        assert "#4fd8ff" in get()                                              # the household's glass style
        css = get().replace("#4fd8ff", "#ff9f40")
        assert app.agent.selfmod.propose("theme", "", css, "orange accents")["accepted"]
        assert "#ff9f40" in get()                                              # live, no restart
    finally:
        app.stop()
