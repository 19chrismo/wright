"""Step 10: MCP tool layer. Shell, filesystem, web, code, image and browser are real MCP servers over
stdio; external servers attach from config (stdio or streamable HTTP) without touching the core."""
import base64
import json
import os
import socket
import struct
import subprocess
import sys
import textwrap
import time
import zlib
from pathlib import Path

import pytest

from agentkit import can_launch_browser
from wright.events import EventBus
from wright.harness import Harness, Task, acceptance_hook
from wright.llm import ScriptedModel, call, say
from wright.mcp_hub import McpHub
from wright.testkit import StaticSite
from wright.tools import ToolRegistry

CHROMIUM = "/opt/pw-browsers/chromium-1243/chrome-linux64/chrome"


@pytest.fixture(scope="module")
def hub(tmp_path_factory):
    wd = tmp_path_factory.mktemp("mcpwork")
    reg = ToolRegistry()
    env = {"WRIGHT_CHROMIUM": CHROMIUM} if os.path.exists(CHROMIUM) else {}
    h = McpHub(reg, get_workdir=lambda: str(wd), env=env)
    h.attach_builtins()
    yield h, reg, wd
    h.close()


def png(w=3, h=2) -> bytes:
    raw = b"".join(b"\x00" + b"\xff\x00\x00" * w for _ in range(h))
    chunk = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def test_shell_over_mcp(hub):
    h, reg, wd = hub
    r = reg.get("shell__run").fn({"command": "echo $((6*7)) && pwd"})
    out = json.loads(r.text)
    assert out["exit_code"] == 0 and out["stdout"].split() == ["42", str(wd)]
    assert r.data["command"].startswith("echo") and r.data["exit_code"] == 0     # ledger row data
    assert reg.get("shell__run").destructive and reg.get("shell__run").is_mutating


def test_filesystem_over_mcp(hub):
    h, reg, wd = hub
    reg.get("fs__write").fn({"path": "notes/a.txt", "content": "alpha\nbeta\n"})
    reg.get("fs__edit").fn({"path": "notes/a.txt", "old": "beta", "new": "gamma"})
    assert reg.get("fs__read").fn({"path": "notes/a.txt"}).text == "alpha\ngamma\n"
    assert "a.txt" in reg.get("fs__list").fn({"path": "notes"}).text
    assert "notes/a.txt:2: gamma" in reg.get("fs__search").fn({"path": ".", "regex": "gam"}).text
    assert reg.get("fs__read").read_only and not reg.get("fs__read").is_mutating


def test_web_and_code_over_mcp(hub):
    h, reg, wd = hub
    site = StaticSite({"/doc": ("text/html", "<html><head><title>Manual</title></head><body><p>Port 22 is SSH.</p></body></html>")})
    page = json.loads(reg.get("web__fetch").fn({"url": site.url + "/doc"}).text)
    assert page["title"] == "Manual" and "Port 22 is SSH." in page["text"] and len(page["sha256"]) == 64
    site.stop()
    out = json.loads(reg.get("code__run").fn({"code": "import sys; print(sys.version_info.major)"}).text)
    assert out["stdout"].strip() == "3"


def test_image_ingestion_over_mcp(hub):
    h, reg, wd = hub
    (wd / "shot.png").write_bytes(png())
    r = reg.get("image__view").fn({"path": "shot.png"})
    assert r.images and r.images[0]["mime"] == "image/png"
    assert base64.b64decode(r.images[0]["data"]) == png()
    assert json.loads(r.text.splitlines()[0])["width"] == 3


@pytest.mark.skipif(not can_launch_browser(), reason="no browser available")
def test_browser_over_mcp(hub):
    h, reg, wd = hub
    site = StaticSite({"/": ("text/html", "<html><head><title>Home</title></head><body>"
                             "<a id=go href='/next'>next</a><input id=q></body></html>"),
                       "/next": ("text/html", "<html><head><title>Next page</title></head><body>arrived</body></html>")})
    opened = json.loads(reg.get("browser__open").fn({"url": site.url + "/"}).text)
    assert opened["status"] == 200 and opened["title"] == "Home"
    reg.get("browser__fill").fn({"selector": "#q", "text": "hello"})
    nav = json.loads(reg.get("browser__click").fn({"selector": "#go"}).text)
    assert nav["title"] == "Next page" and "arrived" in reg.get("browser__text").fn({}).text
    shot = reg.get("browser__screenshot").fn({})
    assert shot.images and shot.images[0]["mime"] == "image/png"
    site.stop()


ROUTER = textwrap.dedent('''
    from mcp.server.mcpserver import MCPServer
    s = MCPServer("router")
    CONFIG = {"wan_ssh": "on"}
    @s.tool(name="get_config", description="Read router config")
    def get_config() -> str:
        import json; return json.dumps(CONFIG)
    @s.tool(name="set", description="Set a key")
    def set_(key: str, value: str) -> str:
        CONFIG[key] = value; return "ok"
    import sys
    if len(sys.argv) > 1:
        s.run("streamable-http", host="127.0.0.1", port=int(sys.argv[1]))
    else:
        s.run("stdio")
''')


def test_external_server_attaches_from_config_without_touching_core(hub, tmp_path):
    h, reg, wd = hub
    script = tmp_path / "router_mcp.py"
    script.write_text(ROUTER)
    before = set(reg.names())
    out = h.attach_config({"router": {"command": sys.executable, "args": [str(script)]}})
    assert out["router"] == ["router__get_config", "router__set"]
    assert reg.get("router__set").fn({"key": "wan_ssh", "value": "off"}).text == "ok"
    assert json.loads(reg.get("router__get_config").fn({}).text) == {"wan_ssh": "off"}
    h.detach("router")
    assert set(reg.names()) == before


def test_streamable_http_server_attaches_by_url(hub, tmp_path):
    h, reg, wd = hub
    script = tmp_path / "router_http.py"
    script.write_text(ROUTER)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]
    p = subprocess.Popen([sys.executable, str(script), str(port)], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", port), 0.2).close(); break
            except OSError:
                time.sleep(0.1)
        tools = h.attach("ha", {"url": f"http://127.0.0.1:{port}/mcp"})
        assert "ha__get_config" in tools
        assert json.loads(reg.get("ha__get_config").fn({}).text) == {"wan_ssh": "on"}
        h.detach("ha")
    finally:
        p.terminate(); p.wait(5)


def test_agent_loop_runs_through_mcp_tools(hub):
    h, reg, wd = hub
    model = ScriptedModel([call("fs__write", path="out/hello.txt", content="made via MCP\n"),
                           call("shell__run", command="cat out/hello.txt"), say("Wrote out/hello.txt.")])
    task = Task(goal="write out/hello.txt", workdir=wd, acceptance=[
        {"name": "file", "kind": "file_contains", "path": "out/hello.txt", "text": "made via MCP"},
        {"name": "shown", "kind": "ledger_ran", "pattern": "cat out/hello.txt"}])
    Harness(model, reg, EventBus(), stop_hooks=[acceptance_hook]).run(task)
    assert task.status == "done"
