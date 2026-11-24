"""Secrets: keys live in the macOS Keychain (service "wright"), or in a 0600 JSON file elsewhere.
Every string that leaves the process for disk, UI or a cloud model passes through redact()."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

SERVICE = "wright"
PATTERNS = [
    re.compile(r"AIza[0-9A-Za-z_\-]{35}"),                 # Google API keys
    re.compile(r"sk-(?:ant-|proj-)?[0-9A-Za-z_\-]{20,}"),   # OpenAI / Anthropic style keys
    re.compile(r"(?i)bearer\s+[0-9A-Za-z._\-]{20,}"),       # bearer tokens
    re.compile(r"gh[pousr]_[0-9A-Za-z]{30,}"),              # GitHub tokens
    re.compile(r"eyJ[0-9A-Za-z_\-]{10,}\.[0-9A-Za-z_\-]{10,}\.[0-9A-Za-z_\-]{10,}"),  # JWT (HA tokens)
]


class KeyStore:
    def __init__(self, secrets_dir: Path, use_keychain: bool | None = None):
        self.dir = secrets_dir
        self.file = secrets_dir / "keys.json"
        if use_keychain is None:
            use_keychain = sys.platform == "darwin" and shutil.which("security") is not None
        self.keychain = use_keychain

    # -- file backend ---------------------------------------------------------------------
    def _load(self) -> dict[str, str]:
        if self.file.exists():
            return json.loads(self.file.read_text())
        return {}

    def _save(self, d: dict[str, str]) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.dir, 0o700)
        tmp = self.file.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(d, f)
        os.replace(tmp, self.file)

    # -- public ---------------------------------------------------------------------------
    def set(self, ref: str, value: str) -> None:
        if self.keychain:
            subprocess.run(["security", "add-generic-password", "-U", "-s", SERVICE, "-a", ref,
                            "-w", value], check=True, capture_output=True)
            idx = self._load()
            idx[ref] = "__keychain__"
            self._save(idx)
        else:
            d = self._load()
            d[ref] = value
            self._save(d)

    def get(self, ref: str) -> str | None:
        d = self._load()
        v = d.get(ref)
        if v == "__keychain__" and self.keychain:
            p = subprocess.run(["security", "find-generic-password", "-s", SERVICE, "-a", ref, "-w"],
                               capture_output=True, text=True)
            return p.stdout.strip() if p.returncode == 0 else None
        if v is None:
            return os.environ.get(ref)  # allow WRIGHT_KEY_* env vars for CI
        return v

    def refs(self) -> list[str]:
        return sorted(self._load())

    def delete(self, ref: str) -> None:
        d = self._load()
        if d.pop(ref, None) == "__keychain__" and self.keychain:
            subprocess.run(["security", "delete-generic-password", "-s", SERVICE, "-a", ref],
                           capture_output=True)
        self._save(d)

    def values(self) -> list[str]:
        out = []
        for ref in self.refs():
            v = self.get(ref)
            if v and len(v) >= 8:
                out.append(v)
        return out


class Redactor:
    def __init__(self, keystore: KeyStore | None = None, extra: list[str] | None = None):
        self.keystore = keystore
        self.extra = [e for e in (extra or []) if e and len(e) >= 8]
        self._cache: list[str] | None = None

    def known(self) -> list[str]:
        if self._cache is None:
            self._cache = sorted(set(self.extra + (self.keystore.values() if self.keystore else [])),
                                 key=len, reverse=True)
        return self._cache

    def refresh(self) -> None:
        self._cache = None

    def __call__(self, text: str) -> str:
        for v in self.known():
            text = text.replace(v, "[REDACTED]")
        for p in PATTERNS:
            text = p.sub("[REDACTED]", text)
        return text
