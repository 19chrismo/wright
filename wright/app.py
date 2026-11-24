"""The app: local pool proxy + agent + one-window UI, all on 127.0.0.1."""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
from pathlib import Path
from typing import Any

from .agent import Agent
from .config import Config
from .events import EventBus
from .llm import Model, OpenAICompatModel
from .persist import unfinished
from .pool import Pool, PoolServer, lanes_from_config
from .secrets import KeyStore
from .ui.server import Confirmations, UIServer

APP_BROWSERS = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "google-chrome", "chromium", "chromium-browser", "microsoft-edge",
]


def free_port(preferred: int) -> int:
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", preferred))
            return preferred
        except OSError:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]


def build_pool(config: Config, events: EventBus) -> tuple[PoolServer, Model]:
    pc = config.data["pool"]
    keys = KeyStore(config.paths.secrets)
    pool = Pool(lanes_from_config(pc["lanes"]), pc["routes"], key_lookup=keys.get,
                quota_groups=pc.get("quota_groups"), daily_reset=pc.get("daily_reset"),
                max_wait=pc.get("max_wait_seconds", 120),
                on_event=lambda e: events.emit(e.pop("kind"), **e), local_only=config.data.get("local_only", False))
    server = PoolServer(pool, pc.get("host", "127.0.0.1"), free_port(pc.get("port", 8765))).start()
    return server, OpenAICompatModel(server.url, config.data["harness"]["model"], api_key=server.token)


def open_window(url: str, profile: Path) -> str:
    """One window: a Chromium-family browser in --app mode (no tabs, no address bar) when present."""
    for b in APP_BROWSERS:
        exe = b if os.path.isabs(b) and os.path.exists(b) else shutil.which(b) if not os.path.isabs(b) else None
        if exe:
            subprocess.Popen([exe, f"--app={url}", f"--user-data-dir={profile}", "--no-first-run",
                              "--window-size=1100,900"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return exe
    opener = "open" if sys.platform == "darwin" else "xdg-open"
    if shutil.which(opener):
        subprocess.Popen([opener, url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return opener
    return ""


class App:
    def __init__(self, config: Config | None = None, model: Model | None = None, *, tools: str = "mcp",
                 port: int | None = None, power_command: list[str] | None = None):
        self.config = config or Config.load()
        self.events = EventBus()
        self.pool_server = None
        if model is None:
            self.pool_server, model = build_pool(self.config, self.events)
        self.confirm = Confirmations(self.events)
        self.agent = Agent(self.config, model, confirm=self.confirm, tools=tools, events=self.events,
                           power_command=power_command)
        ui = self.config.data["ui"]
        self.ui = UIServer({"agent": self.agent}, self.events, self.confirm, ui.get("host", "127.0.0.1"),
                           port if port is not None else free_port(ui.get("port", 7770)), info=self.info)

    def info(self) -> dict[str, Any]:
        from .persist import load_task
        items = []
        for p in unfinished(self.config.paths.tasks):
            try:
                t = load_task(p)
                items.append({"id": t.id, "goal": t.goal[:200], "state": t.state.get("lock", {}).get("state")})
            except Exception:
                continue
        return {"unfinished": items, "calibration": self.agent.continuity.calibration.overall(),
                "tools": self.agent.registry.names(),
                "pool": self.pool_server.pool.status() if self.pool_server else None}

    def start(self, window: bool = True) -> "App":
        self.ui.start()
        if window:
            open_window(self.ui.url, self.config.paths.home / "window-profile")
        return self

    def stop(self) -> None:
        self.ui.stop()
        if self.pool_server:
            self.pool_server.stop()
        self.agent.close()


def _running_instance(lock: Path) -> str | None:
    """Single instance: if Wright is already running, return its window URL."""
    import json
    import urllib.request
    try:
        info = json.loads(lock.read_text())
        os.kill(int(info["pid"]), 0)
        urllib.request.urlopen(info["url"].split("?")[0], timeout=2)
    except urllib.error.HTTPError:
        return info["url"]            # 401 without the cookie: it is alive
    except Exception:
        return None
    return info["url"]


def run_app(window: bool = True) -> int:
    import json
    cfg = Config.load()
    lock = cfg.paths.home / "app.json"
    url = _running_instance(lock)
    if url:
        if window:
            open_window(url, cfg.paths.home / "window-profile")
        print(f"Wright is already running: {url}")
        return 0
    app = App(cfg).start(window)
    lock.write_text(json.dumps({"pid": os.getpid(), "url": app.ui.url}))
    os.chmod(lock, 0o600)
    print(f"Wright is running. Window: {app.ui.url}\nPress Ctrl+C to quit.", flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        lock.unlink(missing_ok=True)
        app.stop()
    return 0
