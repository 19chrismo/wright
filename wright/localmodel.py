"""Which local model Wright uses. Automatic unless the person picked one:
  1. a suitable model already installed in Ollama is used as-is (no download), preferring the one
     that best fits this machine;
  2. otherwise the default for this machine: qwen2.5:3b on Intel Macs (no GPU acceleration) and on
     machines with 8 GB or less, qwen2.5:7b otherwise.
Families listed below support tool calling in Ollama [INFERRED from Ollama's model tags; `wright
doctor` and a failed first turn would show it if one does not]."""
from __future__ import annotations

import os
import platform
import subprocess
import sys

TOOL_CAPABLE = ("qwen3", "qwen2.5", "llama3.3", "llama3.2", "llama3.1", "gpt-oss", "mistral-nemo",
                "mistral-small", "command-r")


def memory_bytes() -> int:
    try:
        if sys.platform == "darwin":
            return int(subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True).stdout.strip())
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except Exception:
        return 0


def intel_mac() -> bool:
    return sys.platform == "darwin" and platform.machine() == "x86_64"


def machine_default() -> str:
    mem = memory_bytes()
    if intel_mac() or 0 < mem <= 9 * 1024 ** 3:
        return "qwen2.5:3b"
    return "qwen2.5:7b"


def choose(installed: list[dict], mem: int | None = None, default: str | None = None) -> tuple[str, bool]:
    """installed: Ollama /api/tags "models" entries ({"name", "size"}). Returns (model, needs_download)."""
    default = default or machine_default()
    names = {m.get("name", ""): m for m in installed}
    for n in (default, f"{default}:latest"):
        if n in names:
            return default, False
    mem = memory_bytes() if mem is None else mem
    budget = (mem * 0.6) if mem else float("inf")
    fits = [m for m in installed
            if m.get("name", "").split(":")[0].startswith(TOOL_CAPABLE) and (m.get("size") or 0) <= budget]
    if fits:
        # On an Intel Mac or a small machine, the smallest suitable model is the usable one;
        # otherwise the largest that fits.
        small = intel_mac() or (0 < mem <= 9 * 1024 ** 3)
        best = min(fits, key=lambda m: m.get("size") or 0) if small else max(fits, key=lambda m: m.get("size") or 0)
        return best["name"].removesuffix(":latest"), False
    return default, True
