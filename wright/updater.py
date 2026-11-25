"""Continuous, gated self-update: no "v2" to install and nobody locked into an old version.

Wright fetches new commits for the checkout it was installed from, tests them in a separate staging
copy with the project's own test suite, and moves forward only when every test passes — the same
fail-closed rule as any other change. Local edits are never overwritten. Every update records the
previous revision, so "undo the update" is one step. Changes take effect the next time Wright opens.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

PACKAGE_ROOT = Path(__file__).resolve().parents[1]


def _git(repo: Path, *args: str, check: bool = True) -> str:
    # Never stop to ask for a password in the background (e.g. if the repository is made private):
    # fail fast, and the window says the check couldn't run.
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "", "SSH_ASKPASS": ""}
    p = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, env=env, timeout=120)
    if check and p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {p.stderr.strip()[:300]}")
    return p.stdout.strip()


class Updater:
    def __init__(self, home: Path, repo: Path | None = None, branch: str = "main", python: str | None = None,
                 events: Any = None, test_timeout: float = 1800,
                 test_cmd: list[str] | None = None, runner: Callable[..., Any] = subprocess.run):
        self.home = Path(home)
        self.repo = Path(repo) if repo else PACKAGE_ROOT
        self.branch = branch
        self.python = python or sys.executable
        self.events = events
        self.test_timeout = test_timeout
        self.test_cmd = test_cmd or [self.python, "-m", "pytest", "-q", "-x", "-o", "addopts="]
        self.runner = runner
        self.history_file = self.home / "update-history.json"

    def _emit(self, kind: str, **kw: Any) -> None:
        if self.events is not None:
            self.events.emit(kind, **kw)

    def available(self) -> bool:
        return (self.repo / ".git").exists() and shutil.which("git") is not None

    def history(self) -> list[dict]:
        return json.loads(self.history_file.read_text()) if self.history_file.exists() else []

    def _record(self, entry: dict) -> None:
        h = self.history() + [entry]
        self.history_file.parent.mkdir(parents=True, exist_ok=True)
        self.history_file.write_text(json.dumps(h[-50:], indent=1))

    def check(self) -> dict:
        if not self.available():
            return {"available": False, "reason": "this copy of Wright was not installed from a git checkout"}
        _git(self.repo, "fetch", "-q", "origin", self.branch)
        head = _git(self.repo, "rev-parse", "HEAD")
        remote = _git(self.repo, "rev-parse", f"origin/{self.branch}")
        behind = int(_git(self.repo, "rev-list", "--count", f"HEAD..origin/{self.branch}") or 0)
        dirty = bool(_git(self.repo, "status", "--porcelain", "--untracked-files=no"))
        changes = _git(self.repo, "log", "--format=%s", f"HEAD..origin/{self.branch}").splitlines() if behind else []
        return {"available": True, "head": head, "remote": remote, "behind": behind, "dirty": dirty,
                "changes": changes}

    def apply(self) -> dict:
        """One updater at a time (an exclusive lock), so two checks never share a staging folder."""
        import fcntl
        self.home.mkdir(parents=True, exist_ok=True)
        with open(self.home / "update.lock", "a+") as lf:
            try:
                fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return {"status": "busy", "message": "An update check is already running."}
            return self._apply()

    def _apply(self) -> dict:
        try:
            info = self.check()
        except Exception as e:
            r = {"status": "unreachable", "message": f"Couldn't check for improvements right now ({e})."}
            return r
        if not info["available"]:
            return {"status": "unavailable", "message": info["reason"]}
        if info["behind"] == 0:
            return {"status": "current", "message": "Wright is already up to date."}
        if info["dirty"]:
            msg = "New improvements are available, but this copy has local edits; they were left untouched."
            self._emit("update", status="held", message=msg)
            return {"status": "held", "message": msg}
        staging = self.home / "updates" / info["remote"][:12]
        if staging.exists():
            shutil.rmtree(staging)
        staging.parent.mkdir(parents=True, exist_ok=True)
        _git(self.repo, "worktree", "prune", check=False)
        _git(self.repo, "worktree", "add", "-q", "--detach", str(staging), info["remote"])
        self._emit("update", status="testing", message=f"Testing {info['behind']} new improvement(s) before switching…")
        try:
            p = self.runner(self.test_cmd, cwd=staging, capture_output=True, text=True, timeout=self.test_timeout)
            passed = p.returncode == 0
            tail = ((p.stdout or "") + (p.stderr or ""))[-600:]
        except subprocess.TimeoutExpired:
            passed, tail = False, "tests timed out"
        finally:
            _git(self.repo, "worktree", "remove", "--force", str(staging), check=False)
        if not passed:
            msg = (f"Held back {info['behind']} new improvement(s): their tests failed, so Wright stayed on the "
                   "version that works.")
            self._record({"t": time.time(), "status": "held_back", "from": info["head"], "to": info["remote"],
                          "tests": tail})
            self._emit("update", status="held_back", message=msg)
            return {"status": "held_back", "message": msg, "tests": tail}
        def deps() -> str:
            return "".join((self.repo / f).read_text() for f in ("pyproject.toml", "uv.lock") if (self.repo / f).exists())
        before = deps()
        _git(self.repo, "merge", "-q", "--ff-only", f"origin/{self.branch}")
        if deps() != before:
            uv = self.home / "bootstrap" / "bin" / "uv"
            if uv.exists() and (self.repo / "uv.lock").exists():
                env = {**os.environ, "UV_PROJECT_ENVIRONMENT": str(Path(self.python).resolve().parents[1])}
                self.runner([str(uv), "sync", "--quiet", "--frozen", "--project", str(self.repo), "--extra", "test",
                             "--extra", "browser"], capture_output=True, text=True, env=env)
            else:
                self.runner([self.python, "-m", "pip", "install", "-q", "--prefer-binary", "-e", str(self.repo)],
                            capture_output=True, text=True)
        self._record({"t": time.time(), "status": "updated", "from": info["head"], "to": info["remote"],
                      "changes": info["changes"][:20]})
        msg = (f"Updated itself with {info['behind']} improvement(s) after all tests passed. "
               "Say “restart Wright” to switch to them now (your window stays open), or “undo the update” "
               "to go back.")
        self._emit("update", status="updated", message=msg, changes=info["changes"][:10])
        return {"status": "updated", "message": msg, "changes": info["changes"]}

    def rollback(self) -> dict:
        last = next((h for h in reversed(self.history()) if h["status"] == "updated"), None)
        if not last:
            return {"status": "nothing", "message": "There is no update to undo."}
        if _git(self.repo, "status", "--porcelain", "--untracked-files=no"):
            return {"status": "held", "message": "This copy has local edits, so the update was not undone."}
        _git(self.repo, "reset", "-q", "--hard", last["from"])
        self._record({"t": time.time(), "status": "rolled_back", "from": last["to"], "to": last["from"]})
        msg = "Went back to the version before the last update. It takes effect the next time Wright opens."
        self._emit("update", status="rolled_back", message=msg)
        return {"status": "rolled_back", "message": msg}
