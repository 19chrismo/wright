"""Reproducible install (brief v3, section 12.2): every package the install needs is locked with a
hash and has a prebuilt wheel for each target machine, so no install ever needs a compiler."""
import re
import sys
import tomllib
from pathlib import Path

import pytest
from packaging.markers import Marker

ROOT = Path(__file__).resolve().parents[1]
TARGETS = {
    "intel mac": {"sys_platform": "darwin", "platform_machine": "x86_64", "platform_system": "Darwin",
                  "os_name": "posix", "implementation_name": "cpython", "platform_python_implementation": "CPython",
                  "python_version": "3.12", "python_full_version": "3.12.8"},
    "apple silicon mac": {"sys_platform": "darwin", "platform_machine": "arm64", "platform_system": "Darwin",
                          "os_name": "posix", "implementation_name": "cpython",
                          "platform_python_implementation": "CPython", "python_version": "3.12",
                          "python_full_version": "3.12.8"},
    "linux": {"sys_platform": "linux", "platform_machine": "x86_64", "platform_system": "Linux", "os_name": "posix",
              "implementation_name": "cpython", "platform_python_implementation": "CPython",
              "python_version": "3.12", "python_full_version": "3.12.8"},
}


def wheel_fits(filename: str, env: dict) -> bool:
    m = re.match(r"^(?P<n>.+?)-(?P<v>[^-]+)(-\d[^-]*)?-(?P<py>[^-]+)-(?P<abi>[^-]+)-(?P<plat>[^-]+)\.whl$", filename)
    if not m:
        return False
    py, abi, plat = m["py"].split("."), m["abi"], m["plat"].split(".")
    py_ok = any(p in ("py3", "py2.py3", "cp312") or (abi == "abi3" and p.startswith("cp3") and int(p[3:] or 0) <= 12)
                for p in py) or "py2" in py and "py3" in py
    if not py_ok:
        return False
    if "any" in plat:
        return True
    if env["sys_platform"] == "darwin":
        want = ("x86_64", "universal2", "intel") if env["platform_machine"] == "x86_64" else ("arm64", "universal2")
        return any(p.startswith("macosx") and p.endswith(want) for p in plat)
    return any(p.startswith("manylinux") and p.endswith("x86_64") for p in plat)


def needed(lock: dict, env: dict) -> set[str]:
    pkgs = {p["name"]: p for p in lock["package"]}
    root = next(p for p in lock["package"] if p["name"] == "wright")
    todo = list(root.get("dependencies", []))
    for extra in ("browser", "test"):
        todo += root.get("optional-dependencies", {}).get(extra, [])
    seen: set[str] = set()
    while todo:
        d = todo.pop()
        if "marker" in d and not Marker(d["marker"]).evaluate({**env, "extra": ""}):
            continue
        if d["name"] in seen:
            continue
        seen.add(d["name"])
        p = pkgs[d["name"]]
        todo += p.get("dependencies", [])
        for e in d.get("extra", []):
            todo += p.get("optional-dependencies", {}).get(e, [])
    return seen


@pytest.fixture(scope="module")
def lock():
    return tomllib.loads((ROOT / "uv.lock").read_text())


@pytest.mark.parametrize("target", sorted(TARGETS))
def test_every_needed_package_has_a_prebuilt_hashed_wheel(lock, target):
    env = TARGETS[target]
    pkgs = {p["name"]: p for p in lock["package"]}
    missing = []
    for name in sorted(needed(lock, env)):
        wheels = pkgs[name].get("wheels", [])
        fits = [w for w in wheels if wheel_fits(w["url"].rsplit("/", 1)[-1], env)]
        if not fits:
            missing.append(name)
        assert all(w.get("hash", "").startswith("sha256:") for w in fits), f"{name}: wheel without hash"
    assert missing == [], f"no prebuilt wheel for {target}: {missing}"


def test_intel_mac_gets_a_cryptography_that_ships_for_it(lock):
    crypto = [p for p in lock["package"] if p["name"] == "cryptography"]
    env = TARGETS["intel mac"]
    assert any(any(wheel_fits(w["url"].rsplit("/", 1)[-1], env) for w in p.get("wheels", [])) for p in crypto)


def test_lockfile_matches_pyproject():
    import shutil, subprocess
    uv = shutil.which("uv")
    if not uv:
        pytest.skip("uv not installed")
    p = subprocess.run([uv, "lock", "--check", "--offline"], cwd=ROOT, capture_output=True, text=True)
    assert p.returncode == 0, p.stderr[-800:]


def test_wheel_check_rejects_what_the_intel_mac_cannot_use():
    intel = TARGETS["intel mac"]
    assert not wheel_fits("cryptography-50.0.2-cp311-abi3-macosx_11_0_arm64.whl", intel)
    assert wheel_fits("cryptography-48.0.1-cp311-abi3-macosx_10_9_universal2.whl", intel)
    assert wheel_fits("idna-3.10-py3-none-any.whl", intel)
    assert not wheel_fits("x-1.0-cp313-cp313-macosx_10_13_x86_64.whl", intel)      # wrong Python
