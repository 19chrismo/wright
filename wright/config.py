"""Paths and configuration. Everything persistent lives under WRIGHT_HOME (default ~/.wright)."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PACKAGE_DIR = Path(__file__).resolve().parent


EXTRA_BIN_DIRS = ["/usr/local/bin", "/opt/homebrew/bin", "/opt/homebrew/sbin", "~/.local/bin",
                  "/Applications/Ollama.app/Contents/Resources", "~/Applications/Ollama.app/Contents/Resources"]


def ensure_path() -> list[str]:
    """Apps opened from Finder or the Dock get a minimal PATH (/usr/bin:/bin:/usr/sbin:/sbin), which
    hides Homebrew, ~/.local/bin and the ollama command. Add the usual places that exist."""
    have = os.environ.get("PATH", "").split(os.pathsep)
    added = []
    for d in EXTRA_BIN_DIRS:
        d = os.path.expanduser(d)
        if os.path.isdir(d) and d not in have:
            added.append(d)
    if added:
        os.environ["PATH"] = os.pathsep.join(have + added)
    return added


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
        """Paths no model-driven tool may read (secrets and the hidden eval, including its seed copy
        inside the installed package)."""
        return [self.secrets, self.hidden_eval, PACKAGE_DIR / "seed" / "hidden_eval"]

    def ensure(self) -> "Paths":
        for p in (self.home, self.memory, self.episodic, self.semantic, self.procedural,
                  self.tasks, self.logs):
            p.mkdir(parents=True, exist_ok=True)
        self.secrets.mkdir(parents=True, exist_ok=True)
        os.chmod(self.secrets, 0o700)
        return self


DEFAULT_CONFIG: dict[str, Any] = {
    "ui": {"host": "127.0.0.1", "port": 7770, "lan": False},   # lan: phone access (owner's paired devices)
    "pool": {
        "host": "127.0.0.1",
        "port": 8765,
        "max_wait_seconds": 120,
        "routes": {
            # Request model -> ordered candidates. The dispatcher picks one route per request:
            #   auto  — builds and research: the fast online model first, the big one second, this Mac last
            #   fast  — quick questions: the fast online model, then this Mac
            #   deep  — "think hard" requests: the big online model first
            #   local — private requests: this Mac only
            # normalize_routes() keeps them consistent with "auto" (see below).
            "auto": ["gemini-2.5-flash", "gemini-2.5-pro", "ollama:qwen2.5:7b"],
            "fast": ["gemini-2.5-flash", "ollama:qwen2.5:7b"],
            "deep": ["gemini-2.5-pro", "gemini-2.5-flash", "ollama:qwen2.5:7b"],
            "local": ["ollama:qwen2.5:7b"],
        },
        # Before handing a cloud-first request to this Mac's own (slow) model, wait this long for a
        # resting online key to come back. 0 = use the local model immediately.
        "prefer_cloud_wait_seconds": 75,
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
    "local_model": "auto",        # "auto": use a suitable installed model, else the default for this machine
    "auto_update": True,          # fetch, test in staging, switch only when green; daily and at launch
}


def _is_big(model: str) -> bool:
    """The slower, larger online models (Gemini "pro"). Everything else online counts as fast."""
    name = model.split("/")[-1].lower()
    return "-pro" in name or name.endswith("pro")


def normalize_routes(routes: dict[str, list[str]]) -> dict[str, list[str]]:
    """Derive every route from "auto" so they never disagree, with the fast online model first.

    Older versions (and the live-model-list fixer) put the big "pro" model first. On the free tier
    that model has a tiny per-minute allowance, so every step hit its limit, waited, fell through
    to flash and often ended on this Mac's own model — the main reason Wright felt slow."""
    auto = list(routes.get("auto") or [])
    local = [m for m in auto if m.startswith("ollama:")]
    if not local:
        local = [m for r in routes.values() for m in r if m.startswith("ollama:")][:1]
    cloud = [m for m in auto if not m.startswith("ollama:")]
    fast = [m for m in cloud if not _is_big(m)]
    big = [m for m in cloud if _is_big(m)]
    out = dict(routes)
    out["auto"] = fast + big + local
    out["fast"] = (fast or big) + local
    out["deep"] = big + fast + local
    out["local"] = list(local)
    return out


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
        if data.get("local_model", "auto") == "auto":
            # The stock default is sized for Apple-silicon Macs; swap it for what suits this machine.
            from .localmodel import machine_default
            stock, mine = "ollama:qwen2.5:7b", f"ollama:{machine_default()}"
            for name, route in data["pool"]["routes"].items():
                data["pool"]["routes"][name] = [mine if m == stock else m for m in route]
        data["pool"]["routes"] = normalize_routes(data["pool"]["routes"])
        return cls(paths=paths, data=data)

    def save(self) -> None:
        self.paths.config_file.write_text(json.dumps(self.data, indent=2))

    def __getitem__(self, key: str) -> Any:
        return self.data[key]
