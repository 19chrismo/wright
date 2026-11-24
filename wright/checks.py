"""Deterministic checks. Acceptance criteria, baseline dimensions and stop hooks are all Checks.
A check is data (JSON) so it can be stored in manifests and audited; running it never involves a
model."""
from __future__ import annotations

import os
import re
import socket
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

KINDS = ("command", "file_exists", "file_contains", "http", "tcp", "metric", "ledger_ran")


@dataclass
class CheckResult:
    name: str
    ok: bool
    value: Any = None
    detail: str = ""

    def as_dict(self) -> dict:
        return {"name": self.name, "ok": self.ok, "value": self.value, "detail": self.detail[:500]}


def _sub(text: str, ctx: dict[str, str]) -> str:
    for k, v in ctx.items():
        text = text.replace("{" + k + "}", v)
    return text


# Checks never write Python bytecode caches: an edit that keeps size and mtime-second identical
# (e.g. "a - b" → "a + b") would otherwise be judged against stale bytecode.
CHECK_ENV = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}


def run_check(spec: dict, cwd: Path | str, ledger: list[dict] | None = None,
              ctx: dict[str, str] | None = None) -> CheckResult:
    """Run one check spec. `cwd` is where commands and relative paths resolve (sandbox or real)."""
    ctx = {"root": str(cwd), **(ctx or {})}
    kind = spec.get("kind")
    name = spec.get("name") or f"{kind}"
    timeout = float(spec.get("timeout", 120))
    try:
        if kind == "command":
            cmd = _sub(spec["run"], ctx)
            p = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True,
                               timeout=timeout, env=CHECK_ENV)
            want = int(spec.get("expect_exit", 0))
            ok = p.returncode == want
            out = (p.stdout + p.stderr).strip()
            if ok and spec.get("expect_regex"):
                ok = re.search(spec["expect_regex"], out) is not None
            return CheckResult(name, ok, p.returncode, out[-400:])
        if kind == "file_exists":
            p = Path(cwd, _sub(spec["path"], ctx))
            return CheckResult(name, p.exists(), p.exists(), str(p))
        if kind == "file_contains":
            p = Path(cwd, _sub(spec["path"], ctx))
            if not p.exists():
                return CheckResult(name, False, None, f"{p} missing")
            body = p.read_text(errors="replace")
            if "regex" in spec:
                ok = re.search(spec["regex"], body) is not None
            else:
                ok = spec["text"] in body
            return CheckResult(name, ok, None, body[:200])
        if kind == "http":
            url = _sub(spec["url"], ctx)
            try:
                with urllib.request.urlopen(url, timeout=timeout) as r:
                    status, body = r.status, r.read(65536).decode(errors="replace")
            except urllib.error.HTTPError as e:
                status, body = e.code, ""
            ok = status == int(spec.get("expect_status", 200))
            if ok and spec.get("expect_regex"):
                ok = re.search(spec["expect_regex"], body) is not None
            return CheckResult(name, ok, status, body[:200])
        if kind == "tcp":
            t0 = time.time()
            try:
                with socket.create_connection((spec["host"], int(spec["port"])), timeout=timeout):
                    return CheckResult(name, True, round(time.time() - t0, 4), "connected")
            except OSError as e:
                return CheckResult(name, False, None, str(e))
        if kind == "metric":
            # A numeric measurement: command prints a number; direction + tolerance live in the
            # baseline manifest, so here we only measure. ok = the command produced a number.
            cmd = _sub(spec["run"], ctx)
            p = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True,
                               timeout=timeout, env=CHECK_ENV)
            m = re.search(r"-?\d+(?:\.\d+)?", p.stdout)
            if p.returncode != 0 or not m:
                return CheckResult(name, False, None, (p.stdout + p.stderr)[-300:])
            return CheckResult(name, True, float(m.group(0)), p.stdout.strip()[-200:])
        if kind == "ledger_ran":
            # "Done is a claim": prove from the harness's own execution ledger that a matching
            # command really ran and exited as required. The model's say-so is never consulted.
            pat = re.compile(spec["pattern"])
            want = int(spec.get("expect_exit", 0))
            for row in reversed(ledger or []):
                cmd = row.get("command") or ""
                if pat.search(cmd):
                    ok = row.get("exit_code") == want
                    return CheckResult(name, ok, row.get("exit_code"), f"ledger: {cmd[:200]}")
            return CheckResult(name, False, None, "no matching command in the execution ledger")
        return CheckResult(name, False, None, f"unknown check kind {kind!r}")
    except subprocess.TimeoutExpired:
        return CheckResult(name, False, None, f"timed out after {timeout}s")
    except Exception as e:  # a check that crashes is a failing check, never a passing one
        return CheckResult(name, False, None, f"{type(e).__name__}: {e}")


def run_checks(specs: list[dict], cwd: Path | str, ledger: list[dict] | None = None,
               ctx: dict[str, str] | None = None) -> list[CheckResult]:
    return [run_check(s, cwd, ledger, ctx) for s in specs]


def validate_check(spec: Any) -> str | None:
    """Return an error string if the spec is not a machine-checkable criterion."""
    if not isinstance(spec, dict):
        return "criterion must be an object"
    k = spec.get("kind")
    if k not in KINDS:
        return f"kind must be one of {KINDS}"
    need = {"command": ["run"], "file_exists": ["path"], "file_contains": ["path"],
            "http": ["url"], "tcp": ["host", "port"], "metric": ["run"], "ledger_ran": ["pattern"]}[k]
    missing = [f for f in need if f not in spec]
    if k == "file_contains" and "text" not in spec and "regex" not in spec:
        missing.append("text|regex")
    return f"missing fields: {missing}" if missing else None


def env_with(**extra: str) -> dict[str, str]:
    e = dict(os.environ)
    e.update(extra)
    return e
