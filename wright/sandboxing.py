"""OS-level write protection for shell commands, and path extraction for scope checks.

macOS: commands run under `sandbox-exec` with a profile that denies writes to protected paths
(and reads of secrets / hidden eval). Linux: `bwrap` read-only binds when bubblewrap is installed.
Elsewhere the contract's string scan and the episodic hash chain remain (detect + repair).
[INFERRED] sandbox-exec is deprecated by Apple but still shipped; the darwin-only test in
tests/test_step04_memory.py verifies it on the Mac.
"""
from __future__ import annotations

import os
import re
import shlex
import shutil
import sys
from pathlib import Path


def sbpl_profile(deny_write: list[Path], deny_read: list[Path]) -> str:
    def q(p: Path) -> str:
        return '"' + str(p).replace("\\", "\\\\").replace('"', '\\"') + '"'
    lines = ["(version 1)", "(allow default)"]
    if deny_write:
        lines.append("(deny file-write* " + " ".join(f"(subpath {q(p)})" for p in deny_write) + ")")
    if deny_read:
        lines.append("(deny file-read* " + " ".join(f"(subpath {q(p)})" for p in deny_read) + ")")
    return "\n".join(lines)


def available() -> str | None:
    if sys.platform == "darwin" and shutil.which("sandbox-exec"):
        return "sandbox-exec"
    if sys.platform.startswith("linux") and shutil.which("bwrap"):
        return "bwrap"
    return None


def wrap(command: str, deny_write: list[Path], deny_read: list[Path]) -> list[str]:
    """Return argv that runs `command` in /bin/sh with OS-enforced protection when available."""
    kind = available()
    deny_write = [Path(p) for p in deny_write if Path(p).exists()]
    deny_read = [Path(p) for p in deny_read if Path(p).exists()]
    if kind == "sandbox-exec":
        real_w = [Path(os.path.realpath(p)) for p in deny_write]
        real_r = [Path(os.path.realpath(p)) for p in deny_read]
        return ["sandbox-exec", "-p", sbpl_profile(real_w, real_r), "/bin/sh", "-c", command]
    if kind == "bwrap":
        argv = ["bwrap", "--bind", "/", "/", "--dev", "/dev", "--proc", "/proc"]
        for p in deny_write:
            argv += ["--ro-bind", str(p), str(p)]
        for p in deny_read:
            argv += ["--tmpfs", str(p)] if p.is_dir() else []
        return argv + ["/bin/sh", "-c", command]
    return ["/bin/sh", "-c", command]


_PATHISH = re.compile(r"""(?:^|[\s=:'"(<>|;&])((?:~|\$HOME|\$\{HOME\}|\.{1,2})?/[^\s'";|&<>()]*|[\w.-]+/[^\s'";|&<>()]*)""")


def path_tokens(command: str) -> list[str]:
    """Best-effort list of path-like tokens in a shell command (heuristic; fs tools are exact)."""
    toks: list[str] = []
    try:
        parts = shlex.split(command, posix=True)
    except ValueError:
        parts = command.split()
    for p in parts:
        for m in _PATHISH.finditer(" " + p):
            toks.append(m.group(1))
    return toks


def mentions(command: str, protected: list[Path], home: Path | None = None) -> str | None:
    """Return the protected path a command refers to, if any (literal, ~, $HOME, ${HOME} forms)."""
    home = home or Path.home()
    lowered = command.replace("${HOME}", "$HOME")
    for p in protected:
        p = Path(p)
        forms = {str(p), os.path.realpath(p)}
        try:
            rel = p.relative_to(home)
            forms |= {f"~/{rel}", f"$HOME/{rel}"}
        except ValueError:
            pass
        for f in forms:
            if f and f in lowered:
                return str(p)
    for marker in (".wright/memory", ".wright/secrets", ".wright/hidden_eval", ".wright/self",
                   "WRIGHT_HOME", "hidden_eval", ".anchor.json"):
        if marker in command:
            return marker
    return None
