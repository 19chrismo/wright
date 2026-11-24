"""Step 11: sandbox + snapshot + rollback. Break something deliberately and revert in one command;
the rollback is rehearsed before it is needed; the real target is untouched until promotion."""
import os
import subprocess
import sys

from wright.sandbox import DirSandbox, SnapshotStore, manifest


def make_real(root):
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text("print('v1')\n")
    (root / "config.yaml").write_text("mode: safe\n")
    (root / ".git").mkdir()
    (root / ".git" / "HEAD").write_text("ref: main\n")
    os.symlink("config.yaml", root / "current.yaml")
    return root


def test_sandbox_is_a_disposable_copy(tmp_path):
    real = make_real(tmp_path / "real")
    sb = DirSandbox(real, tmp_path / "task")
    root = sb.create()
    (root / "src" / "app.py").write_text("print('v2')\n")
    (root / "new.txt").write_text("n\n")
    (root / "config.yaml").unlink()
    assert (real / "src" / "app.py").read_text() == "print('v1')\n"          # real untouched
    assert sb.diff() == {"changed": ["src/app.py"], "added": ["new.txt"], "deleted": ["config.yaml"]}
    assert not (root / ".git").exists()                                     # VCS internals excluded


def test_break_deliberately_and_rollback_in_one_command(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("WRIGHT_HOME", str(home))
    real = make_real(tmp_path / "real")
    sb = DirSandbox(real, home / "tasks" / "t1")
    sb.create()
    ok, msg = sb.rehearse_rollback()                                        # rehearsed first
    assert ok, msg
    sid, verified, why = sb.snapshot_real()
    assert verified, why                                                    # backup proven by restoring it
    before = manifest(real)
    # deliberate damage of every kind
    (real / "src" / "app.py").write_text("garbage")
    (real / "config.yaml").unlink()
    (real / "stray.bin").write_bytes(b"\x00" * 10)
    os.unlink(real / "current.yaml"); os.symlink("elsewhere", real / "current.yaml")
    assert manifest(real) != before
    out = subprocess.run([sys.executable, "-m", "wright", "rollback", sid], capture_output=True, text=True,
                         env={**os.environ, "WRIGHT_HOME": str(home)})
    assert out.returncode == 0, out.stderr
    assert "verified against the snapshot manifest" in out.stdout
    assert manifest(real) == before                                         # exact restore
    assert (real / ".git" / "HEAD").read_text() == "ref: main\n"           # excluded dirs never touched
    listing = subprocess.run([sys.executable, "-m", "wright", "snapshots"], capture_output=True, text=True,
                             env={**os.environ, "WRIGHT_HOME": str(home)}).stdout
    assert sid in listing


def test_rejected_change_returns_sandbox_to_last_green(tmp_path):
    real = make_real(tmp_path / "real")
    sb = DirSandbox(real, tmp_path / "task")
    root = sb.create()
    (root / "src" / "app.py").write_text("print('v2')\n")
    sb.mark_green()
    (root / "src" / "app.py").write_text("broken(")
    (root / "junk").write_text("x")
    sb.reset_to_green()
    assert (root / "src" / "app.py").read_text() == "print('v2')\n" and not (root / "junk").exists()


def test_corrupted_snapshot_is_caught_by_verification(tmp_path):
    real = make_real(tmp_path / "real")
    store = SnapshotStore(tmp_path / "snaps")
    sid = store.take(real, "x")
    (tmp_path / "snaps" / sid / "files" / "config.yaml").write_text("tampered\n")
    ok, msg = store.verify(sid)
    assert not ok and "does not match" in msg
