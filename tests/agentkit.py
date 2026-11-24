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
