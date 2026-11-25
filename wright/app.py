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
                on_event=lambda e: events.emit(e.pop("kind"), **e), local_only=config.data.get("local_only", False),
                usage_file=config.paths.home / "usage.json")
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
                 port: int | None = None, power_command: list[str] | None = None, token: str | None = None):
        self.config = config or Config.load()
        self.events = EventBus()
        self.pool_server = None
        injected = model is not None          # tests/demos pass a model; the real app builds the pool
        if model is None:
            self.pool_server, model = build_pool(self.config, self.events)
        self.confirm = Confirmations(self.events)
        self.agent = Agent(self.config, model, confirm=self.confirm, tools=tools, events=self.events,
                           power_command=power_command)
        from .control import Control
        self.control = Control(self.config, self.events, self.pool_server.pool if self.pool_server else None,
                               self.agent)
        ui = self.config.data["ui"]
        self.ui = UIServer({"agent": self.agent}, self.events, self.confirm, ui.get("host", "127.0.0.1"),
                           port if port is not None else free_port(ui.get("port", 7770)), info=self.info,
                           control=self.control, force_ready=injected, token=token)
        self.control.on_ready = self.ui.resume_pending
        self.control.on_restart = self.restart
        self.control.on_quit = self.quit
        self.control.on_resume = lambda task_id: self.ui.start_task({"resume": task_id})
        self.exec = os.execv                                   # replaced in tests
        if self.pool_server:                                   # remember which request each key served
            pool = self.pool_server.pool
            self.events.subscribe(lambda e: pool.annotate(e["lane"], self.agent.current.goal)
                                  if e["kind"] == "lane" and self.agent.current else None)
        from .updater import Updater
        self.updater = Updater(self.config.paths.home, events=self.events)
        self.control.updater = self.updater
        self._auto_update = (not injected) and bool(self.config.data.get("auto_update", True))

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
        if not self.ui.force_ready:
            self.control.show_setup()          # the window opens on what (if anything) still needs doing
        if self._auto_update and self.updater.available():
            import threading

            def loop() -> None:
                while True:
                    try:
                        self.updater.apply()
                    except Exception as e:
                        self.events.emit("notice", text=f"Couldn't check for improvements: {e}")
                    time.sleep(24 * 3600)
            threading.Thread(target=loop, daemon=True, name="wright-update").start()
        if window:
            open_window(self.ui.url, self.config.paths.home / "window-profile")
        return self

    def restart(self) -> None:
        """Start the newest installed code in place, keeping the same window (same port and token)."""
        import threading

        def go() -> None:
            busy = self.ui.busy
            while not busy.acquire(timeout=5):          # never cut a running task short
                pass
            self.events.emit("notice", text="Restarting Wright…")
            time.sleep(0.5)
            os.environ["WRIGHT_TOKEN"] = self.ui.token
            os.environ["WRIGHT_PORT"] = str(self.ui.port)
            try:
                self.stop()
            finally:
                self.exec(sys.executable, [sys.executable, "-m", "wright", "run", "--no-window"])
        threading.Thread(target=go, daemon=True).start()

    def quit(self) -> None:
        import threading

        def go() -> None:
            self.events.emit("notice", text="Wright is closing. Open it again from the Wright app any time.")
            time.sleep(0.5)
            (self.config.paths.home / "app.json").unlink(missing_ok=True)
            self.stop()
            os._exit(0)
        threading.Thread(target=go, daemon=True).start()

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


def acquire_instance_lock(home: Path) -> Any:
    """Atomic single-instance guard: an exclusive flock held for the life of the process (released by
    the OS if it dies, and across exec because Python opens files close-on-exec). Returns the open
    lock file, or None if another Wright holds it."""
    import fcntl
    home.mkdir(parents=True, exist_ok=True)
    f = open(home / "app.lock", "a+")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    return f


def run_app(window: bool = True) -> int:
    import json
    cfg = Config.load()
    lock = cfg.paths.home / "app.json"
    holder = acquire_instance_lock(cfg.paths.home)
    if holder is None:                                     # another Wright is running or starting
        url = None
        for _ in range(60):                                # give a starting instance time to publish its URL
            url = _running_instance(lock)
            if url:
                break
            time.sleep(0.5)
        if url and window:
            open_window(url, cfg.paths.home / "window-profile")
        print(f"Wright is already running{': ' + url if url else ''}")
        return 0
    token = os.environ.pop("WRIGHT_TOKEN", None)            # set when restarting in place
    port = os.environ.pop("WRIGHT_PORT", None)
    app = App(cfg, token=token, port=int(port) if port else None).start(window)
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
