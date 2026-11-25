"""Helpers for full-agent tests: a scripted model that can read earlier tool results."""
import json

from wright.config import Config, Paths
from wright.llm import Reply, ScriptedModel, ToolCall, call, say


def last_json(messages, key):
    for m in reversed(messages):
        if m.get("role") == "tool":
            try:
                d = json.loads(m.get("content") or "")
            except json.JSONDecodeError:
                continue
            if isinstance(d, dict) and key in d:
                return d[key]
    raise AssertionError(f"no tool result with {key}")


def agent_config(tmp_path):
    cfg = Config.load(tmp_path / "home")
    cfg.data["projects_dir"] = str(tmp_path / "projects")
    return cfg


CONTAINER_CHROMIUM = "/opt/pw-browsers/chromium-1243/chrome-linux64/chrome"
_BROWSER = {}


def chromium_path():
    """The build container's Chromium if present; otherwise None = Playwright's own install (e.g. the
    one install.sh downloads on a Mac)."""
    import os
    return CONTAINER_CHROMIUM if os.path.exists(CONTAINER_CHROMIUM) else None


def can_launch_browser() -> bool:
    if "ok" not in _BROWSER:
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as p:
                kw = {"executable_path": chromium_path(), "args": ["--no-sandbox"]} if chromium_path() else {}
                p.chromium.launch(**kw).close()
            _BROWSER["ok"] = True
        except Exception:
            _BROWSER["ok"] = False
    return _BROWSER["ok"]


def launch(p):
    kw = {"executable_path": chromium_path(), "args": ["--no-sandbox"]} if chromium_path() else {}
    return p.chromium.launch(**kw)
