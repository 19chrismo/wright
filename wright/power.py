"""Keep the Mac awake while a task runs (macOS `caffeinate -i -w <pid>` sleep assertion), and release
it when the task ends. Linux uses `systemd-inhibit` when present; otherwise it is a no-op."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from contextlib import contextmanager
from typing import Iterator


def on_ac_power() -> bool:
    """`pmset -g batt` names the power source on its first line ("Now drawing from 'AC Power'")."""
    try:
        out = subprocess.run(["pmset", "-g", "batt"], capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return False
    return "AC Power" in out.splitlines()[0] if out else False


def assertion_command(pid: int | None = None) -> list[str] | None:
    pid = pid or os.getpid()
    if sys.platform == "darwin" and shutil.which("caffeinate"):
        # -i: prevent idle sleep. -s: prevent system sleep, which `man caffeinate` says "is valid only
        # when system is running on AC power" (brief v3, section 1). -w: end with Wright itself.
        flags = ["-i", "-s"] if on_ac_power() else ["-i"]
        return ["caffeinate", *flags, "-w", str(pid)]
    if sys.platform.startswith("linux") and shutil.which("systemd-inhibit"):
        return ["systemd-inhibit", "--what=idle:sleep", "--who=wright", "--why=task running",
                "sleep", "infinity"]
    return None


@contextmanager
def keep_awake(command: list[str] | None = None, events: object = None) -> Iterator[subprocess.Popen | None]:
    cmd = command if command is not None else assertion_command()
    proc = None
    if cmd:
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if events is not None:
                events.emit("power", state="held", command=cmd[0])
        except OSError:
            proc = None
    try:
        yield proc
    finally:
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(5)
            except subprocess.TimeoutExpired:
                proc.kill()
            if events is not None:
                events.emit("power", state="released", command=cmd[0] if cmd else None)
