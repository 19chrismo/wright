"""`wright doctor`: checks everything a run depends on and says exactly what to fix."""
from __future__ import annotations

import importlib.util
import json
import os
import platform
import shutil
import sys
import urllib.error
import urllib.request
from typing import Any

from .config import Config
from .memory import EpisodicLog
from .pool import PROVIDER_DEFAULTS, lanes_from_config
from .sandboxing import available as sandbox_available
from .secrets import KeyStore


def _get(url: str, headers: dict[str, str] | None = None, timeout: float = 10) -> tuple[int, Any]:
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception as e:
        return 0, str(e)


def _post(url: str, body: dict, headers: dict[str, str], timeout: float = 30) -> tuple[int, Any]:
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")[:300]
    except Exception as e:
        return 0, str(e)


def run(config: Config | None = None, fix_routes: bool = True, out: Any = print) -> int:
    cfg = config or Config.load()
    keys = KeyStore(cfg.paths.secrets)
    problems = 0

    def ok(msg: str) -> None:
        out(f"  ✓ {msg}")

    def bad(msg: str, fix: str) -> None:
        nonlocal problems
        problems += 1
        out(f"  ✗ {msg}\n      fix: {fix}")

    out(f"Wright doctor — {platform.platform()} · Python {sys.version.split()[0]} · home {cfg.paths.home}")
    facts = machine_facts()
    out("This machine")
    for k, label in (("model", "model"), ("arch", "architecture"), ("macos", "macOS"), ("opencore", "OpenCore"),
                     ("oclp", "OCLP"), ("memory_gb", "memory (GB)"), ("free_memory_gb", "free memory (GB)"),
                     ("ac_power", "on AC power"), ("sleep_assertion", "can keep the Mac awake")):
        if k in facts:
            out(f"  · {label}: {facts[k]}")
    out("Core")
    if sys.version_info < (3, 11):
        bad("Python is older than 3.11", "install Python 3.12 (brew install python@3.12) and re-run install.sh")
    else:
        ok("Python version")
    v = EpisodicLog(cfg.paths.episodic).verify()
    ok(f"episodic log chain intact ({v.count} entries)") if v.ok else bad(
        "episodic log failed verification: " + "; ".join(v.problems), "start Wright once; it repairs from the mirror")
    sb = sandbox_available()
    ok(f"OS write protection for shell commands: {sb}") if sb else out(
        "  · OS write protection unavailable here (string scan + tamper detection still active)")
    if sys.platform == "darwin":
        ok("caffeinate available (keeps the Mac awake during tasks)") if shutil.which("caffeinate") else bad(
            "caffeinate missing", "it ships with macOS; check /usr/bin")
    out("Model lanes")
    lanes = lanes_from_config(cfg.data["pool"]["lanes"])
    if not any(not l.local for l in lanes):
        out("  · no cloud lanes; add one with: wright keys add gemini")
    for lane in lanes:
        if lane.provider == "ollama":
            base = lane.base_url.rsplit("/v1", 1)[0]
            code, data = _get(base + "/api/tags", timeout=3)
            if code != 200:
                bad(f"{lane.id}: Ollama not reachable at {base}",
                    "install from https://ollama.com, open it, then: ollama pull qwen2.5:7b")
                continue
            have = {m["name"] for m in data.get("models", [])}
            want = [m.split(":", 1)[1] for ms in cfg.data["pool"]["routes"].values() for m in ms if m.startswith("ollama:")]
            missing = [w for w in sorted(set(want)) if w not in have and f"{w}:latest" not in have]
            ok(f"{lane.id}: Ollama running with {len(have)} model(s)")
            route_model = next((w for w in want if w in have or f"{w}:latest" in have), None)
            if route_model:
                tps = local_tokens_per_second(lane.base_url, route_model)
                out(f"  · {lane.id}: {route_model} generates {tps} tokens/s" if tps else
                    f"  · {lane.id}: couldn't measure {route_model}'s speed")
            for m in missing:
                bad(f"{lane.id}: route model {m} is not pulled", f"ollama pull {m}   (or change pool.routes in config.json)")
            continue
        key = keys.get(lane.key_ref) if lane.key_ref else None
        if lane.key_ref and not key:
            bad(f"{lane.id}: no key stored under {lane.key_ref}", f"wright keys add {lane.provider}")
            continue
        hdr = {"Authorization": f"Bearer {key}"} if key else {}
        code, data = _get(lane.base_url + "/models", hdr)
        if code == 200 and isinstance(data, dict):
            ids = sorted(m.get("id", "").split("/")[-1] for m in data.get("data", []))
            ok(f"{lane.id}: key accepted; {len(ids)} models available")
            if fix_routes and lane.provider == "gemini":
                _fix_gemini_route(cfg, ids, out)
        elif code in (401, 403):
            bad(f"{lane.id}: key rejected ({code})", f"wright keys add {lane.provider}  (replace the key)")
        elif code == 429:
            ok(f"{lane.id}: key valid but currently rate-limited (the pool will rotate)")
        else:
            bad(f"{lane.id}: endpoint {lane.base_url} answered {code}: {str(data)[:120]}",
                f"check network access, or set the lane base_url (default {PROVIDER_DEFAULTS.get(lane.provider)})")
    out("Tools")
    if importlib.util.find_spec("mcp") is None:
        bad("MCP SDK missing", "re-run install.sh")
    else:
        ok("MCP SDK installed")
    if importlib.util.find_spec("playwright") is None:
        out("  · browser tool disabled (optional): pip install playwright && python -m playwright install chromium")
    else:
        ok("browser tool available (Playwright)")
    for name, spec in cfg.data.get("mcp_servers", {}).items():
        target = spec.get("url") or spec.get("command")
        if spec.get("url"):
            ok(f"attached MCP server {name}: {target}")
        elif shutil.which(spec.get("command", "")) or str(spec.get("command", "")).startswith("/"):
            ok(f"attached MCP server {name}: {target}")
        else:
            bad(f"MCP server {name}: command {target!r} not found", "install it or remove it: wright mcp remove " + name)
    if sys.platform == "darwin":
        out("macOS one-time permissions (grant when macOS asks, or in System Settings → Privacy & Security):")
        out("  · Local Network — to reach your router / Home Assistant")
        out("  · Accessibility and Screen Recording — only if you attach desktop-automation MCP servers")
    out(f"{'All checks passed.' if not problems else f'{problems} problem(s) to fix.'}")
    cfg.save()
    return 0 if not problems else 1


def _fix_gemini_route(cfg: Config, ids: list[str], out: Any) -> None:
    """Make wrong default model names cheap: pick the newest available pro and flash models."""
    import re

    def best(kind: str) -> str | None:
        cands = [i for i in ids if i.startswith("gemini-") and kind in i
                 and not re.search(r"(exp|preview|tts|image|embedding|live|audio|vision|thinking)", i)]
        def ver(i: str) -> tuple:
            m = re.match(r"gemini-(\d+)(?:\.(\d+))?", i)
            return (int(m.group(1)), int(m.group(2) or 0)) if m else (0, 0)
        return max(cands, key=lambda i: (ver(i), -len(i)), default=None)

    from .config import normalize_routes
    routes = cfg.data["pool"]["routes"]
    pro, flash = best("pro"), best("flash")
    local = [m for m in routes.get("auto", []) if m.startswith("ollama:")]
    new_auto = [m for m in (flash, pro) if m] + local          # fast model first (see normalize_routes)
    if new_auto and new_auto != routes.get("auto"):
        routes.update(normalize_routes({**routes, "auto": new_auto}))
        out(f"  ✓ model route set from the live model list: {routes['auto']}")


OPENCORE_GUID = "4D1FDA02-38C7-4A6A-9CC6-4BCCA8B30102"   # OpenCore's NVRAM vendor GUID [INFERRED]


def machine_facts(run: Any = None) -> dict[str, Any]:
    """Read the platform facts the brief's section 1 table depends on, from the system itself."""
    import platform as _pf
    import re as _re
    import subprocess as _sp

    def sh(*argv: str) -> str:
        try:
            p = (run or _sp.run)(list(argv), capture_output=True, text=True, timeout=10)
            return (p.stdout or "").strip() if p.returncode == 0 else ""
        except Exception:
            return ""

    facts: dict[str, Any] = {"arch": _pf.machine(), "system": sys.platform}
    if sys.platform == "darwin":
        facts["model"] = sh("sysctl", "-n", "hw.model")
        facts["macos"] = f"{sh('sw_vers', '-productVersion')} ({sh('sw_vers', '-buildVersion')})"
        oc = sh("nvram", f"{OPENCORE_GUID}:opencore-version")
        facts["opencore"] = oc.split("\t", 1)[-1] if oc else "not detected"
        oclp = sh("nvram", f"{OPENCORE_GUID}:OCLP-Version")
        facts["oclp"] = oclp.split("\t", 1)[-1] if oclp else "not detected"
        total = sh("sysctl", "-n", "hw.memsize")
        facts["memory_gb"] = round(int(total) / 1024 ** 3, 1) if total.isdigit() else None
        vm = sh("vm_stat")
        page = int(_re.search(r"page size of (\d+)", vm).group(1)) if "page size of" in vm else 4096
        free = sum(int(m.group(1)) for m in _re.finditer(r"Pages (?:free|inactive|speculative):\s+(\d+)", vm))
        facts["free_memory_gb"] = round(free * page / 1024 ** 3, 1) if vm else None
        batt = sh("pmset", "-g", "batt")
        facts["ac_power"] = bool(batt) and "AC Power" in batt.splitlines()[0]
        facts["sleep_assertion"] = bool(shutil.which("caffeinate"))
    else:
        try:
            facts["memory_gb"] = round(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 1024 ** 3, 1)
            facts["free_memory_gb"] = round(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_AVPHYS_PAGES") / 1024 ** 3, 1)
        except (ValueError, OSError):
            pass
    return facts


def local_tokens_per_second(base: str, model: str, timeout: float = 120) -> float | None:
    """Measure the local model's generation speed with a tiny prompt (Ollama /api/generate)."""
    code, data = _post(base.rsplit("/v1", 1)[0] + "/api/generate",
                       {"model": model, "prompt": "Reply with the word OK.", "stream": False,
                        "options": {"num_predict": 16}}, {}, timeout=timeout)
    if code != 200 or not isinstance(data, dict) or not data.get("eval_duration"):
        return None
    return round(data.get("eval_count", 0) / (data["eval_duration"] / 1e9), 2)
