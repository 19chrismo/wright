"""Phone access for one owner: the home network only, and only devices the owner paired.

- The UI binds to the LAN only after the owner turns phone access on ("connect my phone").
- A client must come from a private address (home network or this Mac); public addresses are refused.
- The Host header must name this machine by a private IP, localhost, or a .local name (DNS-rebinding
  defence).
- A device is paired by scanning a one-time code shown on the Mac (10 minutes, single use). It then
  holds a long random token; only its SHA-256 is stored, and "forget my phones" revokes all of them.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import secrets
import socket
import threading
import time
from pathlib import Path
from typing import Any

PAIR_SECONDS = 600


def allowed_client(addr: str) -> bool:
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_loopback or ip.is_private or ip.is_link_local


def allowed_host(host_header: str) -> bool:
    host = host_header.rsplit(":", 1)[0] if host_header.count(":") == 1 else host_header
    host = host.strip("[]").lower()
    if host in ("localhost", "127.0.0.1", "::1"):
        return True
    if host.endswith(".local") and all(c.isalnum() or c in "-." for c in host):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private or ip.is_link_local


def lan_address() -> str | None:
    """This machine's address on the home network (no packet is sent)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            ip = s.getsockname()[0]
        return ip if ipaddress.ip_address(ip).is_private and not ip.startswith("127.") else None
    except OSError:
        return None


def _h(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Devices:
    def __init__(self, home: Path):
        self.file = home / "devices.json"
        self.lock = threading.Lock()
        self.codes: dict[str, float] = {}          # one-time pairing codes -> expiry

    def _load(self) -> dict[str, dict]:
        try:
            return json.loads(self.file.read_text())
        except (OSError, ValueError):
            return {}

    def _save(self, d: dict[str, dict]) -> None:
        self.file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.file.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, indent=1))
        tmp.chmod(0o600)
        tmp.replace(self.file)

    def new_code(self) -> str:
        code = secrets.token_urlsafe(9)
        with self.lock:
            now = time.time()
            self.codes = {c: t for c, t in self.codes.items() if t > now}
            self.codes[code] = now + PAIR_SECONDS
        return code

    def pair(self, code: str, user_agent: str = "") -> str | None:
        with self.lock:
            exp = self.codes.pop(code, 0)
        if exp < time.time():
            return None
        token = secrets.token_urlsafe(32)
        name = ("iPhone" if "iPhone" in user_agent else "iPad" if "iPad" in user_agent
                else "Mac" if "Macintosh" in user_agent else "device")
        with self.lock:
            d = self._load()
            d[_h(token)] = {"name": name, "paired": time.time(), "last_seen": time.time()}
            self._save(d)
        return token

    def check(self, token: str) -> bool:
        if not token:
            return False
        with self.lock:
            d = self._load()
            rec = d.get(_h(token))
            if rec is None:
                return False
            if time.time() - rec.get("last_seen", 0) > 3600:
                rec["last_seen"] = time.time()
                self._save(d)
        return True

    def list(self) -> list[dict[str, Any]]:
        return list(self._load().values())

    def forget_all(self) -> int:
        with self.lock:
            n = len(self._load())
            self._save({})
            self.codes.clear()
        return n
