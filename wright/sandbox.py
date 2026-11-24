"""Sandbox, snapshots and rollback for directory targets (code, staged configs, exported router or
Home Assistant config). All mutating work happens in a disposable copy; the real target is touched
only by promote(), after a snapshot that has been verified by restoring it."""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import shutil
import time
from pathlib import Path

DEFAULT_EXCLUDES = [".git", "node_modules", "__pycache__", ".venv", "venv", ".DS_Store", "*.pyc",
                    ".wright-sandbox"]


def _ignored(rel: str, excludes: list[str]) -> bool:
    parts = rel.split(os.sep)
    return any(fnmatch.fnmatch(p, pat) for p in parts for pat in excludes)


def manifest(root: Path, excludes: list[str] | None = None) -> dict[str, str]:
    """relative path -> sha256 for every regular file (symlinks recorded by target)."""
    excludes = DEFAULT_EXCLUDES if excludes is None else excludes
    out: dict[str, str] = {}
    root = Path(root)
    if not root.exists():
        return out
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root)
        dirnames[:] = [d for d in dirnames if not _ignored(os.path.normpath(os.path.join(rel_dir, d)), excludes)]
        for f in filenames:
            rel = os.path.normpath(os.path.join(rel_dir, f))
            if _ignored(rel, excludes):
                continue
            p = Path(dirpath, f)
            if p.is_symlink():
                out[rel] = "link:" + os.readlink(p)
            else:
                out[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def _copy(src: Path, dst: Path, excludes: list[str]) -> None:
    def ignore(d: str, names: list[str]) -> list[str]:
        rel = os.path.relpath(d, src)
        return [n for n in names if _ignored(os.path.normpath(os.path.join(rel, n)), excludes)]
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst, symlinks=True, ignore=ignore)


def _sync(src: Path, dst: Path, excludes: list[str]) -> dict:
    """Make dst's tracked files identical to src's (excluded paths untouched). Returns the diff."""
    a, b = manifest(src, excludes), manifest(dst, excludes)
    changed = [p for p in a if p in b and a[p] != b[p]]
    added = [p for p in a if p not in b]
    deleted = [p for p in b if p not in a]
    for rel in changed + added:
        s, d = Path(src, rel), Path(dst, rel)
        d.parent.mkdir(parents=True, exist_ok=True)
        if d.is_symlink() or d.exists():
            d.unlink()
        if s.is_symlink():
            os.symlink(os.readlink(s), d)
        else:
            shutil.copy2(s, d)
    for rel in deleted:
        Path(dst, rel).unlink()
    return {"changed": sorted(changed), "added": sorted(added), "deleted": sorted(deleted)}


class SnapshotStore:
    """Directory snapshots with a hash manifest; restore is verified, rehearsable, one call."""

    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)

    def take(self, target: Path, label: str, excludes: list[str] | None = None) -> str:
        excludes = DEFAULT_EXCLUDES if excludes is None else excludes
        sid = time.strftime("%Y%m%d-%H%M%S-") + hashlib.sha1(f"{target}{time.time()}".encode()).hexdigest()[:6]
        d = self.root / sid
        _copy(target, d / "files", excludes)
        meta = {"id": sid, "target": str(target), "label": label, "t": time.time(),
                "manifest": manifest(d / "files", excludes), "excludes": excludes}
        (d / "meta.json").write_text(json.dumps(meta, indent=1))
        return sid

    def meta(self, sid: str) -> dict:
        return json.loads((self.root / sid / "meta.json").read_text())

    def verify(self, sid: str, scratch: Path | None = None) -> tuple[bool, str]:
        """A backup you have not restored is a hope: restore into scratch and compare hashes."""
        m = self.meta(sid)
        scratch = scratch or (self.root / f".verify-{sid}")
        _copy(self.root / sid / "files", scratch, [])
        ok = manifest(scratch, m["excludes"]) == m["manifest"]
        shutil.rmtree(scratch, ignore_errors=True)
        return ok, "restore verified" if ok else "restored copy does not match the snapshot manifest"

    def restore(self, sid: str, target: Path | None = None) -> dict:
        m = self.meta(sid)
        target = Path(target or m["target"])
        target.mkdir(parents=True, exist_ok=True)
        diff = _sync(self.root / sid / "files", target, m["excludes"])
        if manifest(target, m["excludes"]) != m["manifest"]:
            raise RuntimeError(f"rollback of {target} to {sid} did not reproduce the snapshot")
        return diff

    def list(self) -> list[dict]:
        out = []
        for d in sorted(self.root.iterdir()):
            if (d / "meta.json").exists():
                m = self.meta(d.name)
                out.append({k: m[k] for k in ("id", "target", "label", "t")})
        return out


class DirSandbox:
    def __init__(self, real: Path, task_dir: Path, excludes: list[str] | None = None):
        self.real = Path(real).resolve()
        self.task_dir = Path(task_dir)
        self.root = self.task_dir / "sandbox"
        self.excludes = DEFAULT_EXCLUDES if excludes is None else excludes
        self.checkpoints = SnapshotStore(self.task_dir / "checkpoints")
        self.snapshots = SnapshotStore(self.task_dir / "snapshots")
        self.last_green: str | None = None

    def create(self) -> Path:
        self.real.mkdir(parents=True, exist_ok=True)
        _copy(self.real, self.root, self.excludes)
        self.base_manifest = manifest(self.real, self.excludes)
        self.last_green = self.checkpoints.take(self.root, "initial", self.excludes)
        return self.root

    def checkpoint(self, label: str) -> str:
        sid = self.checkpoints.take(self.root, label, self.excludes)
        return sid

    def mark_green(self) -> str:
        self.last_green = self.checkpoint("green")
        return self.last_green

    def reset_to_green(self) -> dict:
        assert self.last_green, "no green checkpoint"
        return self.checkpoints.restore(self.last_green, self.root)

    def diff(self) -> dict:
        a, b = manifest(self.root, self.excludes), manifest(self.real, self.excludes)
        return {"changed": sorted(p for p in a if p in b and a[p] != b[p]),
                "added": sorted(p for p in a if p not in b),
                "deleted": sorted(p for p in b if p not in a)}

    def rehearse_rollback(self) -> tuple[bool, str]:
        """Prove rollback works before it is needed: break a scratch copy, restore it, compare."""
        rehearsal = self.task_dir / "rehearsal"
        _copy(self.root, rehearsal, self.excludes)
        store = SnapshotStore(self.task_dir / "rehearsal-snaps")
        sid = store.take(rehearsal, "rehearsal", self.excludes)
        canary = rehearsal / ".wright-rehearsal-canary"
        canary.write_text("break")
        for f in list(manifest(rehearsal, self.excludes))[:1]:
            Path(rehearsal, f).write_text("deliberately broken by rollback rehearsal\n")
        store.restore(sid, rehearsal)
        ok = manifest(rehearsal, self.excludes) == store.meta(sid)["manifest"]
        shutil.rmtree(rehearsal, ignore_errors=True)
        shutil.rmtree(self.task_dir / "rehearsal-snaps", ignore_errors=True)
        return ok, "rollback rehearsal restored the broken copy exactly" if ok else "rollback rehearsal FAILED"

    def snapshot_real(self) -> tuple[str, bool, str]:
        sid = self.snapshots.take(self.real, "pre-promotion", self.excludes)
        ok, msg = self.snapshots.verify(sid)
        return sid, ok, msg

    def promote(self) -> dict:
        return _sync(self.root, self.real, self.excludes)

    def rollback(self, sid: str) -> dict:
        return self.snapshots.restore(sid, self.real)
