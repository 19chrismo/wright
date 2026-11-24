"""Paths and configuration. Everything persistent lives under WRIGHT_HOME (default ~/.wright)."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PACKAGE_DIR = Path(__file__).resolve().parent


def default_home() -> Path:
    return Path(os.environ.get("WRIGHT_HOME", "~/.wright")).expanduser().resolve()


@dataclass(frozen=True)
class Paths:
    home: Path

    @property
    def config_file(self) -> Path: return self.home / "config.json"
    @property
    def memory(self) -> Path: return self.home / "memory"
    @property
    def episodic(self) -> Path: return self.memory / "episodic"
    @property
    def semantic(self) -> Path: return self.memory / "semantic"
    @property
    def procedural(self) -> Path: return self.memory / "procedural"
    @property
    def calibration_file(self) -> Path: return self.memory / "calibration.json"
    @property
    def tasks(self) -> Path: return self.home / "tasks"
    @property
    def self_repo(self) -> Path: return self.home / "self"
    @property
    def hidden_eval(self) -> Path: return self.home / "hidden_eval"
    @property
    def secrets(self) -> Path: return self.home / "secrets"
    @property
    def logs(self) -> Path: return self.home / "logs"

    def protected_write(self) -> list[Path]:
        """Paths no model-driven tool may write to (the audit log, secrets, hidden eval, self store)."""
        return [self.episodic, self.secrets, self.hidden_eval, self.self_repo, self.calibration_file]

    def protected_read(self) -> list[Path]:
        """Paths no model-driven tool may read (secrets and the hidden eval)."""
        return [self.secrets, self.hidden_eval]

    def ensure(self) -> "Paths":
        for p in (self.home, self.memory, self.episodic, self.semantic, self.procedural,
                  self.tasks, self.logs):
            p.mkdir(parents=True, exist_ok=True)
        self.secrets.mkdir(parents=True, exist_ok=True)
        os.chmod(self.secrets, 0o700)
        return self


DEFAULT_CONFIG: dict[str, Any] = {
    "ui": {"host": "127.0.0.1", "port": 7770},
    "pool": {
        "host": "127.0.0.1",
        "port": 8765,
        "max_wait_seconds": 120,
        "routes": {
            # Request model -> ordered candidates. "auto" is what the harness asks for.
            "auto": ["gemini-2.5-pro", "gemini-2.5-flash", "ollama:qwen2.5vl:7b"],
            "fast": ["gemini-2.5-flash", "ollama:qwen2.5vl:7b"],
        },
        # Each lane is one credential at one endpoint. Keys are referenced, never inlined.
        "lanes": [
            {"id": "ollama", "provider": "ollama", "base_url": "http://127.0.0.1:11434/v1",
             "models": ["ollama:*"], "local": True},
        ],
        "quota_groups": {"gemini-2.5-pro": "pro", "gemini-2.5-flash": "flash"},
        "daily_reset": {"time": "00:00", "tz": "America/Los_Angeles"},
    },
    "harness": {
        "model": "auto",
        "max_steps_per_task": 200,
        "max_steps_per_subtask": 25,
        "audit_every": 5,
        "mutations_per_gate": 5,
        "fix_attempts": 3,
        "wall_clock_seconds": 3600,
        "max_tokens_per_task": 2_000_000,
    },
    "mcp_servers": {},  # user-attached servers; built-ins are added in mcp_hub.BUILTIN_SERVERS
    "local_only": False,
}


def deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


@dataclass
class Config:
    paths: Paths
    data: dict[str, Any] = field(default_factory=lambda: json.loads(json.dumps(DEFAULT_CONFIG)))

    @classmethod
    def load(cls, home: Path | None = None) -> "Config":
        paths = Paths(home or default_home()).ensure()
        data = json.loads(json.dumps(DEFAULT_CONFIG))
        if paths.config_file.exists():
            data = deep_merge(data, json.loads(paths.config_file.read_text()))
        return cls(paths=paths, data=data)

    def save(self) -> None:
        self.paths.config_file.write_text(json.dumps(self.data, indent=2))

    def __getitem__(self, key: str) -> Any:
        return self.data[key]
