"""Targets: what a task changes in the real world, and how it is staged, snapshotted, promoted and
rolled back.

DirTarget      — a directory (code, app projects, config folders). Sandbox = disposable copy.
CommandTarget  — a live system with no faithful copy (router, Home Assistant). Sandbox = staged
                 export; promotion is commit-confirmed: apply, then an auto-revert timer fires unless
                 probes over a different path pass; rollback rehearsal = restore the current export
                 (a no-op change that exercises the real restore path).
"""
from __future__ import annotations

import fnmatch
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from .sandbox import DirSandbox, SnapshotStore, _copy, manifest


class DirTarget:
    kind = "dir"

    def __init__(self, real: Path, excludes: list[str] | None = None):
        self.real = Path(real).expanduser().resolve()
        self.excludes = excludes
        self.sb: DirSandbox | None = None

    def prepare(self, task_dir: Path) -> Path:
        self.sb = DirSandbox(self.real, task_dir, self.excludes)
        return self.sb.create()

    @property
    def sandbox_root(self) -> Path:
        return self.sb.root  # type: ignore[union-attr]

    @property
    def real_check_dir(self) -> Path:
        return self.real

    def validate(self) -> tuple[bool, str] | None:
        return None

    def diff(self) -> dict:
        return self.sb.diff()  # type: ignore[union-attr]

    def mark_green(self) -> None:
        self.sb.mark_green()  # type: ignore[union-attr]

    def reset_to_green(self) -> None:
        self.sb.reset_to_green()  # type: ignore[union-attr]

    def rehearse_rollback(self) -> tuple[bool, str]:
        return self.sb.rehearse_rollback()  # type: ignore[union-attr]

    def snapshot(self) -> tuple[str, bool, str]:
        return self.sb.snapshot_real()  # type: ignore[union-attr]

    def apply(self, watch: Any = None) -> dict:
        return self.sb.promote()  # type: ignore[union-attr]

    def confirm(self) -> None:
        pass

    def rollback(self, sid: str) -> dict:
        return self.sb.rollback(sid)  # type: ignore[union-attr]

    def describe(self) -> dict:
        return {"kind": "dir", "real": str(self.real)}


def _run(cmd: str, timeout: float = 300) -> tuple[int, str]:
    p = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
    return p.returncode, (p.stdout + p.stderr)[-2000:]


class CommandTarget:
    """spec: {"name", "export": cmd with {dest}, "apply": cmd with {staged}, "revert": cmd with {snapshot},
    "validate": optional cmd with {staged}, "probes": [cmd...], "confirm_window": seconds}"""
    kind = "command"

    def __init__(self, spec: dict):
        for k in ("export", "apply", "revert"):
            if k not in spec:
                raise ValueError(f"command target needs {k!r}")
        self.spec = spec
        self.task_dir: Path | None = None
        self.timer: threading.Timer | None = None
        self.reverted_by_timer = threading.Event()

    def fmt(self, key: str, **kw: str) -> str:
        cmd = self.spec[key]
        for k, v in kw.items():
            cmd = cmd.replace("{" + k + "}", v)
        return cmd

    def prepare(self, task_dir: Path) -> Path:
        self.task_dir = Path(task_dir)
        root = self.task_dir / "sandbox"
        root.mkdir(parents=True, exist_ok=True)
        code, out = _run(self.fmt("export", dest=str(root)))
        if code != 0:
            raise RuntimeError(f"export failed: {out}")
        self.store = SnapshotStore(self.task_dir / "snapshots")
        self.checkpoints = SnapshotStore(self.task_dir / "checkpoints")
        self.base = manifest(root, [])
        self.last_green = self.checkpoints.take(root, "initial", [])
        return root

    @property
    def sandbox_root(self) -> Path:
        return self.task_dir / "sandbox"  # type: ignore[operator]

    @property
    def real_check_dir(self) -> Path:
        return self.sandbox_root

    def validate(self) -> tuple[bool, str] | None:
        if "validate" not in self.spec:
            return None
        code, out = _run(self.fmt("validate", staged=str(self.sandbox_root)))
        return code == 0, out

    def diff(self) -> dict:
        a, b = manifest(self.sandbox_root, []), self.base
        return {"changed": sorted(p for p in a if p in b and a[p] != b[p]),
                "added": sorted(p for p in a if p not in b), "deleted": sorted(p for p in b if p not in a)}

    def mark_green(self) -> None:
        self.last_green = self.checkpoints.take(self.sandbox_root, "green", [])

    def reset_to_green(self) -> None:
        self.checkpoints.restore(self.last_green, self.sandbox_root)

    def snapshot(self) -> tuple[str, bool, str]:
        d = self.task_dir / "live-export"  # type: ignore[operator]
        if d.exists():
            shutil.rmtree(d)
        d.mkdir(parents=True)
        code, out = _run(self.fmt("export", dest=str(d)))
        if code != 0:
            return "", False, f"export of the live system failed: {out}"
        sid = self.store.take(d, "pre-promotion", [])
        ok, msg = self.store.verify(sid)
        return sid, ok, msg

    def rehearse_rollback(self) -> tuple[bool, str]:
        """Exercise the real restore path with a no-op: export → revert to that export → export again."""
        sid, ok, msg = self.snapshot()
        if not ok:
            return False, msg
        snap = self.store.root / sid / "files"
        code, out = _run(self.fmt("revert", snapshot=str(snap)))
        if code != 0:
            return False, f"revert command failed during rehearsal: {out}"
        after = self.task_dir / "rehearsal-export"  # type: ignore[operator]
        if after.exists():
            shutil.rmtree(after)
        after.mkdir()
        _run(self.fmt("export", dest=str(after)))
        same = manifest(after, []) == self.store.meta(sid)["manifest"]
        shutil.rmtree(after, ignore_errors=True)
        return same, ("restore path rehearsed: revert reproduced the live export exactly" if same
                      else "rehearsal: revert did not reproduce the export")

    def apply(self, watch: Any = None) -> dict:
        """Apply staged config and arm the auto-revert timer (commit-confirmed)."""
        sid = getattr(self, "pending_snapshot", None)
        window = float(self.spec.get("confirm_window", 60))
        if sid:
            snap = str(self.store.root / sid / "files")

            def auto_revert() -> None:
                _run(self.fmt("revert", snapshot=snap))
                self.reverted_by_timer.set()
            self.timer = threading.Timer(window, auto_revert)
            self.timer.daemon = True
            self.timer.start()
        code, out = _run(self.fmt("apply", staged=str(self.sandbox_root)))
        return {"exit_code": code, "output": out[-500:], "auto_revert_armed_seconds": window if sid else None}

    def probes(self) -> list[dict]:
        out = []
        for i, cmd in enumerate(self.spec.get("probes", [])):
            code, text = _run(cmd, timeout=60)
            out.append({"name": f"probe{i + 1}", "ok": code == 0, "value": code, "detail": text[-300:]})
        return out

    def confirm(self) -> None:
        if self.timer:
            self.timer.cancel()
            self.timer = None

    def rollback(self, sid: str) -> dict:
        self.confirm()
        code, out = _run(self.fmt("revert", snapshot=str(self.store.root / sid / "files")))
        if code != 0:
            raise RuntimeError(f"revert failed: {out}")
        return {"reverted_to": sid}

    def describe(self) -> dict:
        return {"kind": "command", "name": self.spec.get("name", "device")}


def out_of_scope_paths(diff: dict, scope_globs: list[str], justified: set[str], root: Path) -> list[str]:
    touched = diff.get("changed", []) + diff.get("added", []) + diff.get("deleted", [])
    bad = []
    for rel in touched:
        if any(fnmatch.fnmatch(rel, g) or fnmatch.fnmatch(rel, g.rstrip("/") + "/*") or
               (g.endswith("/**") and rel.startswith(g[:-3])) or g in ("**", "*") for g in scope_globs):
            continue
        if str(root / rel) in justified or rel in justified:
            continue
        bad.append(rel)
    return bad
