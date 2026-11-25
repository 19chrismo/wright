"""The window is the control surface. Setup, keys (yours and household members'), the local model,
local-only mode, undo and connecting devices all happen from the chat — typed in plain words or as a
/command — and work before any AI model is configured. Keys typed into the key card go straight to
the Keychain; they never enter the transcript, the event log or a model request."""
from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

from .config import Config
from .pool import PROVIDER_DEFAULTS, Lane
from .secrets import KeyStore

PROVIDER_MODELS = {"gemini": ["gemini-*"], "openai": ["gpt-*", "o*"], "openrouter": ["*/*"]}
KEY_PAGES = {"gemini": "https://aistudio.google.com/apikey", "openai": "https://platform.openai.com/api-keys",
             "openrouter": "https://openrouter.ai/keys"}
OLLAMA_MAC_ZIP = "https://ollama.com/download/Ollama-darwin.zip"   # [INFERRED]; failure → one-line link

# ----------------------------------------------------------------------------- intent routing
NAME = r"([A-Z][a-zA-Z\-]{1,20})"
RULES: list[tuple[str, re.Pattern]] = [
    ("greet", re.compile(r"^\s*(hi|hello|hey|hiya|yo|good (morning|afternoon|evening)|thanks|thank you|"
                         r"ok|okay|you there\??|are you there\??)[\s!.?,]*(wright)?[\s!.?]*$", re.I)),
    ("status", re.compile(r"^\s*(/status|/setup|status|setup|check (the )?setup|is (it|everything) (set up|ready)\??|"
                          r"what('s| is) missing\??)\s*$", re.I)),
    ("keys", re.compile(r"^\s*(/keys|(show|list) (the |my |all )?keys|whose keys( are (in use|there))?\??)\s*$", re.I)),
    ("remove_key", re.compile(r"^\s*(/removekey\s+\S+|(remove|delete|take out|drop) " + NAME + r"(?:'s|’s|s') key)\s*$")),
    ("key_card", re.compile(r"^\s*(/key\b.*|(add|use|enter|paste|put in|set ?up|connect)\b.{0,40}"
                            r"\b(api key|gemini( api)? key|openai key|openrouter key|my key|another key|"
                            r"[A-Za-z]+(?:'s|’s|s') (gemini |openai |api )?key|key for [A-Za-z]+|a gemini key)\b.*)$", re.I)),
    ("install_local", re.compile(r"^\s*(/local install|(install|set ?up|download|get)\b.{0,30}"
                                 r"\b(local|offline|free|ollama)\b.{0,20}(model|ai)?\s*)$", re.I)),
    ("local_on", re.compile(r"^\s*(/local on|(keep|make) (it|everything|this) (private|local|offline)|"
                            r"local[- ]only( mode)?( on)?|(go|stay|work|run) (private|local|offline)( only)?)\s*\.?\s*$", re.I)),
    ("local_off", re.compile(r"^\s*(/local off|local[- ]only off|(use|allow) (the )?cloud( models?)?( again)?)\s*\.?\s*$", re.I)),
    ("update", re.compile(r"^\s*(/update|update( now| yourself| wright)?|check for (updates|improvements)|"
                          r"get the latest( version)?)\s*\.?\s*$", re.I)),
    ("update_undo", re.compile(r"^\s*(/update undo|undo the (last )?update|go back to the (old|previous) version)\s*\.?\s*$", re.I)),
    ("undo", re.compile(r"^\s*(/undo|/rollback\s+\S+|undo( that| it| the last( change| promotion)?)?|"
                        r"roll ?back( that| the last change)?|revert( that| the last change)?)\s*\.?\s*$", re.I)),
    ("connect", re.compile(r"^\s*(/connect|connect)\s+(.{1,40}?)\s+(?:at|to|on)?\s*(https?://\S+)\s*$", re.I)),
]


def route(text: str) -> dict | None:
    """Recognise setup/control requests deterministically (no model needed). None → it is a task."""
    t = text.strip()
    if not t or len(t.split()) > 16:
        return None
    for action, rx in RULES:
        m = rx.match(t)
        if not m:
            continue
        args: dict[str, Any] = {}
        if action == "key_card":
            low = t.lower()
            args["provider"] = "openai" if ("openai" in low or "chatgpt" in low) else \
                "openrouter" if "openrouter" in low else "gemini"
            who = re.search(r"\b" + NAME + r"(?:'s|’s|s') ", t) or re.search(r"\bfor " + NAME + r"\b", t)
            if who and who.group(1).lower() not in ("my", "the", "a", "an", "our", "gemini", "api"):
                args["owner"] = who.group(1)
        elif action == "remove_key":
            name = m.group(3) if m.group(3) else t.split(None, 1)[1]
            args["owner"] = name.strip()
        elif action == "undo":
            parts = t.split()
            if parts[0].lower() == "/rollback" and len(parts) > 1:
                args["snapshot"] = parts[1]
        elif action == "connect":
            args["name"] = re.sub(r"[^a-z0-9]+", "-", m.group(2).lower()).strip("-")[:30]
            args["url"] = m.group(3)
        return {"action": action, "args": args}
    return None


def _http(url: str, headers: dict[str, str] | None = None, timeout: float = 10) -> tuple[int, Any]:
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception as e:
        return 0, str(e)


def local_model_for_this_mac() -> str:
    from .localmodel import machine_default
    return machine_default()


class Control:
    def __init__(self, config: Config, events: Any, pool: Any = None, agent: Any = None,
                 runner: Callable[..., Any] = subprocess.run):
        self.cfg = config
        self.events = events
        self.pool = pool
        self.agent = agent
        self.keys = KeyStore(config.paths.secrets)
        self.runner = runner
        self.lock = threading.Lock()
        self.on_ready: Callable[[], None] | None = None     # e.g. start a goal that was waiting for setup
        self.updater: Any = None                             # set by the app (continuous, test-gated updates)

    # ================================================================== setup status
    def ollama_base(self) -> str:
        for l in self.cfg.data["pool"]["lanes"]:
            if l.get("provider") == "ollama":
                return (l.get("base_url") or PROVIDER_DEFAULTS["ollama"]).rsplit("/v1", 1)[0]
        return PROVIDER_DEFAULTS["ollama"].rsplit("/v1", 1)[0]

    def _route_local(self) -> str | None:
        for m in self.cfg.data["pool"]["routes"].get("auto", []):
            if m.startswith("ollama:"):
                return m.split(":", 1)[1]
        return None

    def _set_local_route(self, model: str) -> None:
        routes = self.cfg.data["pool"]["routes"]
        for name in ("auto", "fast"):
            routes[name] = [m for m in routes.get(name, []) if not m.startswith("ollama:")] + [f"ollama:{model}"]
        self.cfg.save()
        if self.pool is not None:
            self.pool.routes = routes

    def _installed(self) -> list[dict] | None:
        code, tags = _http(self.ollama_base() + "/api/tags", timeout=2)
        return (tags or {}).get("models", []) if code == 200 else None

    def local_model(self) -> str:
        """The local model to use. Automatic: an already-installed suitable model (no download), else
        the default for this machine."""
        from .localmodel import choose, machine_default
        chosen = self.cfg.data.get("local_model", "auto")
        if chosen != "auto":
            return chosen
        installed = self._installed()
        if installed is None:
            return self._route_local() or machine_default()
        model, _ = choose(installed)
        if model != self._route_local():
            self._set_local_route(model)
        return model

    def setup_status(self) -> dict:
        lanes = self.cfg.data["pool"]["lanes"]
        cloud = [l for l in lanes if not l.get("local") and l.get("key_ref") and self.keys.get(l["key_ref"])]
        code, tags = _http(self.ollama_base() + "/api/tags", timeout=2)
        have = {m.get("name") for m in (tags or {}).get("models", [])} if code == 200 else set()
        want = self.local_model()
        local_ok = code == 200 and (want in have or f"{want}:latest" in have)
        items = [
            {"id": "cloud", "ok": bool(cloud), "title": "Online AI model",
             "detail": (", ".join(f"{l['id']}" + (f" ({l['owner']})" if l.get("owner") else "") for l in cloud)
                        if cloud else "No key yet. A free Gemini key makes Wright faster and smarter."),
             "action": "key_card", "label": "Add a key"},
            {"id": "local", "ok": local_ok, "title": "Free local model (private, works offline)",
             "detail": (f"{want} ready in Ollama" if local_ok else
                        f"Ollama is running but {want} isn't downloaded yet" if code == 200 else
                        "Not installed. It is the private fallback when online keys run out."),
             "action": "install_local", "label": "Install it for me" if code != 200 else f"Download {want}"},
            {"id": "privacy", "ok": True, "title": "Privacy",
             "detail": "Local-only mode is ON: nothing is sent to online models." if self.cfg.data.get("local_only")
             else "Online models allowed. Say “keep it private” to keep everything on this Mac.",
             "action": "local_off" if self.cfg.data.get("local_only") else "local_on",
             "label": "Allow online models" if self.cfg.data.get("local_only") else "Keep it private"},
        ]
        ready = local_ok or (bool(cloud) and not self.cfg.data.get("local_only"))
        return {"ready": ready, "items": items}

    def check_keys(self) -> list[dict]:
        """Live check of every online key, in plain words (no generation quota is used)."""
        status = {l["id"]: l for l in (self.pool.status()["lanes"] if self.pool is not None else [])}
        out = []
        for l in self.cfg.data["pool"]["lanes"]:
            if l.get("local"):
                continue
            name = l["id"] + (f" ({l['owner']})" if l.get("owner") else "")
            key = self.keys.get(l.get("key_ref", "")) if l.get("key_ref") else None
            if not key:
                out.append({"id": l["id"], "name": name, "ok": False, "detail": "no key stored"})
                continue
            base = (l.get("base_url") or self.cfg.data["pool"].get("provider_urls", {}).get(l["provider"])
                    or PROVIDER_DEFAULTS.get(l["provider"], "")).rstrip("/")
            code, _ = _http(base + "/models", {"Authorization": f"Bearer {key}"})
            last = status.get(l["id"], {}).get("last_error", "")
            cooling = status.get(l["id"], {}).get("cooling", {})
            if code in (401, 403):
                out.append({"id": l["id"], "name": name, "ok": False, "detail": "the service rejected this key"})
            elif code == 0:
                out.append({"id": l["id"], "name": name, "ok": False, "detail": "couldn't reach the service"})
            elif cooling:
                out.append({"id": l["id"], "name": name, "ok": False,
                            "detail": f"valid, but resting after a usage limit ({last or 'limit reached'})"})
            elif last:
                out.append({"id": l["id"], "name": name, "ok": code == 200,
                            "detail": f"key valid; last problem: {last}"})
            else:
                out.append({"id": l["id"], "name": name, "ok": code == 200,
                            "detail": "key valid" if code == 200 else f"unexpected answer ({code})"})
        return out

    def show_setup(self, probe: bool = False) -> dict:
        st = self.setup_status()
        if probe:
            st["keys"] = self.check_keys()
        self.events.emit("setup", **st)
        return st

    # ================================================================== keys
    def add_key(self, provider: str, key: str, owner: str = "", daily_cap: int | None = None,
                base_url: str | None = None) -> dict:
        provider = provider if provider in PROVIDER_MODELS else "gemini"
        key = (key or "").strip()
        if len(key) < 10 or any(c.isspace() for c in key):
            return {"ok": False, "message": "That doesn't look like a key. Copy the whole key and paste it again."}
        base = (base_url or self.cfg.data["pool"].get("provider_urls", {}).get(provider)
                or PROVIDER_DEFAULTS[provider]).rstrip("/")
        code, data = _http(base + "/models", {"Authorization": f"Bearer {key}"})
        if code in (400, 401, 403):
            return {"ok": False, "message": f"The {provider} service rejected this key ({code}). Nothing was saved."}
        slug = re.sub(r"[^a-z0-9]+", "-", owner.lower()).strip("-")
        lanes = self.cfg.data["pool"]["lanes"]
        with self.lock:
            n = 1 + sum(1 for l in lanes if l["provider"] == provider)
            lane_id = f"{provider}-{slug}" if slug else f"{provider}-{n}"
            while any(l["id"] == lane_id for l in lanes):
                n += 1
                lane_id = f"{provider}-{slug}-{n}" if slug else f"{provider}-{n}"
            ref = lane_id.replace("-", "_")
            self.keys.set(ref, key)
            lane = {"id": lane_id, "provider": provider, "models": PROVIDER_MODELS[provider], "key_ref": ref}
            if base_url or self.cfg.data["pool"].get("provider_urls", {}).get(provider):
                lane["base_url"] = base
            if owner:
                lane["owner"] = owner
            if daily_cap:
                lane["daily_cap"] = int(daily_cap)
            lanes.insert(len([l for l in lanes if not l.get("local")]), lane)
            if code == 200 and provider == "gemini":
                ids = [m.get("id", "").split("/")[-1] for m in (data or {}).get("data", [])]
                from .doctor import _fix_gemini_route
                _fix_gemini_route(self.cfg, ids, lambda *_: None)
            routes = self.cfg.data["pool"]["routes"]
            if not any(any(Lane("x", provider, PROVIDER_MODELS[provider]).serves(m) for m in r)
                       for r in [routes.get("auto", [])]):
                routes["auto"] = [m for m in routes.get("auto", []) if not m.startswith("ollama:")] + \
                    {"gemini": ["gemini-2.5-flash"], "openai": [], "openrouter": []}[provider] + \
                    [m for m in routes.get("auto", []) if m.startswith("ollama:")]
            self.cfg.save()
            if self.pool is not None:
                self.pool.add_lane(Lane(lane_id, provider, PROVIDER_MODELS[provider], lane.get("base_url", ""),
                                        ref, daily_cap=lane.get("daily_cap"), owner=owner))
                self.pool.routes = self.cfg.data["pool"]["routes"]
        if self.agent is not None:
            self.agent.continuity.redactor.refresh()              # the new key is redacted from now on
        verified = "accepted by the service" if code == 200 else \
            f"saved, but it couldn't be checked right now ({code or 'no connection'})"
        who = f"{owner}'s " if owner else ""
        msg = (f"Added {who}{provider.capitalize()} key as {lane_id}"
               + (f", at most {daily_cap} requests a day" if daily_cap else "") + f" — {verified}. "
               "It's stored in the Keychain and never shown in the chat.")
        self.events.emit("key_added", lane=lane_id, owner=owner, provider=provider, daily_cap=daily_cap,
                         verified=code == 200, message=msg)
        return {"ok": True, "lane": lane_id, "message": msg}

    def remove_key(self, owner_or_lane: str) -> dict:
        lanes = self.cfg.data["pool"]["lanes"]
        slug = re.sub(r"[^a-z0-9]+", "-", owner_or_lane.lower()).strip("-")
        hit = [l for l in lanes if not l.get("local") and (l["id"] == owner_or_lane or l["id"].endswith("-" + slug)
                                                           or (l.get("owner") or "").lower() == owner_or_lane.lower())]
        if not hit:
            msg = f"No key found for {owner_or_lane}."
            self.events.emit("notice", text=msg)
            return {"ok": False, "message": msg}
        for l in hit:
            self.keys.delete(l["key_ref"])
            lanes.remove(l)
            if self.pool is not None:
                self.pool.remove_lane(l["id"])
        self.cfg.save()
        msg = "Removed " + ", ".join(l["id"] for l in hit) + " and deleted the key from the Keychain."
        self.events.emit("key_removed", lanes=[l["id"] for l in hit], message=msg)
        return {"ok": True, "message": msg}

    def list_keys(self) -> list[dict]:
        status = {l["id"]: l for l in (self.pool.status()["lanes"] if self.pool is not None else [])}
        out = []
        for l in self.cfg.data["pool"]["lanes"]:
            s = status.get(l["id"], {})
            out.append({"id": l["id"], "provider": l["provider"], "owner": l.get("owner", ""),
                        "local": bool(l.get("local")), "daily_cap": l.get("daily_cap"),
                        "used_today": s.get("used_today", 0), "cooling": s.get("cooling", {}),
                        "total": s.get("total", 0), "last_used": s.get("last_used", 0),
                        "last_model": s.get("last_model", ""), "last_task": s.get("last_task", ""),
                        "has_key": bool(l.get("key_ref") and self.keys.get(l["key_ref"])) if not l.get("local") else None})
        self.events.emit("keys", keys=out)
        return out

    # ================================================================== privacy
    def set_local_only(self, on: bool) -> dict:
        self.cfg.data["local_only"] = bool(on)
        self.cfg.save()
        if self.pool is not None:
            self.pool.local_only = bool(on)
        msg = ("Local-only mode is on: every AI request stays on this Mac." if on else
               "Online models are allowed again.")
        self.events.emit("local_only", on=bool(on), message=msg)
        self.show_setup()
        return {"ok": True, "message": msg}

    # ================================================================== local model
    def install_local(self, wait: Callable[[float], None] = time.sleep) -> dict:
        model = self.local_model()
        say = lambda text: self.events.emit("setup_progress", text=text)  # noqa: E731
        base = self.ollama_base()
        code, _ = _http(base + "/api/tags", timeout=2)
        if code != 200:
            exe = shutil.which("ollama")
            if not exe and shutil.which("brew"):
                say("Installing Ollama with Homebrew…")
                self.runner(["brew", "install", "ollama"], capture_output=True, text=True)
                exe = shutil.which("ollama")
            if not exe and sys.platform == "darwin":
                say("Downloading Ollama for Mac…")
                exe = self._install_ollama_mac(say)
            if not exe:
                msg = ("I couldn't install Ollama automatically on this computer. Download it from "
                       "https://ollama.com/download, open it once, then say “install the local model” again.")
                self.events.emit("setup_progress", text=msg, done=True, ok=False)
                return {"ok": False, "message": msg}
            say("Starting Ollama…")
            subprocess.Popen([exe, "serve"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
            for _ in range(60):
                if _http(base + "/api/tags", timeout=1)[0] == 200:
                    break
                wait(0.5)
        exe = shutil.which("ollama") or "ollama"
        from .localmodel import choose
        model, need = choose(self._installed() or []) if self.cfg.data.get("local_model", "auto") == "auto" \
            else (self.cfg.data["local_model"], True)
        if need:
            say(f"Downloading the local model {model} (a few GB; this can take a while)…")
            p = self.runner([exe, "pull", model], capture_output=True, text=True)
            ok = getattr(p, "returncode", 1) == 0
        else:
            say(f"Using {model}, which is already on this Mac — nothing to download.")
            p, ok = None, True
        self._set_local_route(model)
        msg = (f"The free local model {model} is ready. Wright now works even with no online key." if ok else
               f"Downloading {model} failed: {(getattr(p, 'stderr', '') or '')[-200:]}")
        self.events.emit("setup_progress", text=msg, done=True, ok=ok)
        self.show_setup()
        return {"ok": ok, "message": msg}

    def _install_ollama_mac(self, say: Callable[[str], None]) -> str | None:
        apps = Path.home() / "Applications"
        apps.mkdir(exist_ok=True)
        z = self.cfg.paths.home / "Ollama-darwin.zip"
        try:
            urllib.request.urlretrieve(OLLAMA_MAC_ZIP, z)
            subprocess.run(["ditto", "-xk", str(z), str(apps)], check=True)
            subprocess.run(["open", "-a", str(apps / "Ollama.app")], check=False)
            z.unlink(missing_ok=True)
        except Exception:
            return None
        cli = apps / "Ollama.app" / "Contents" / "Resources" / "ollama"
        return str(cli) if cli.exists() else shutil.which("ollama")

    # ================================================================== undo
    def _promotions(self) -> list[tuple[float, Path, dict]]:
        out = []
        for p in sorted(self.cfg.paths.tasks.glob("*/state.json")) if self.cfg.paths.tasks.exists() else []:
            try:
                st = json.loads(p.read_text()).get("state", {})
            except (json.JSONDecodeError, OSError):
                continue
            for pr in st.get("promotions", []):
                if pr.get("status") == "promoted" and pr.get("snapshot"):
                    out.append((pr.get("t") or 0, p.parent, {**pr, "target": st.get("target", {}),
                                                             "target_spec": st.get("target_spec")}))
        return sorted(out, key=lambda x: x[0])

    def undo(self, snapshot: str | None = None) -> dict:
        from .sandbox import SnapshotStore
        from .targets import CommandTarget
        proms = self._promotions()
        if snapshot:
            proms = [p for p in proms if p[2]["snapshot"] == snapshot]
        if not proms:
            msg = "There is nothing to undo yet." if not snapshot else f"No promotion with snapshot {snapshot}."
            self.events.emit("notice", text=msg)
            return {"ok": False, "message": msg}
        _, task_dir, pr = proms[-1]
        store = SnapshotStore(task_dir / "snapshots")
        if pr["target"].get("kind") == "command" and pr.get("target_spec"):
            t = CommandTarget(pr["target_spec"])
            t.task_dir, t.store = task_dir, store
            t.rollback(pr["snapshot"])
            msg = f"Undid the last change to {pr['target'].get('name', 'the device')} (restored {pr['snapshot']})."
            redo = None
        else:
            target = Path(store.meta(pr["snapshot"])["target"])
            redo = store.take(target, "before-undo")             # the undo itself can be undone
            store.restore(pr["snapshot"], target)
            pr_entry = {"status": "promoted", "snapshot": redo, "t": time.time(), "rollback_command": f"/rollback {redo}"}
            state_file = task_dir / "state.json"
            data = json.loads(state_file.read_text())
            data["state"].setdefault("promotions", []).append({**pr_entry, "note": "undo point"})
            data["state"]["promotions"] = [x for x in data["state"]["promotions"] if x.get("snapshot") != pr["snapshot"]]
            state_file.write_text(json.dumps(data))
            msg = (f"Undid the last change to {target} (restored snapshot {pr['snapshot']}). "
                   "Say “undo” again to put it back.")
        self.events.emit("undo", snapshot=pr["snapshot"], redo=redo, message=msg)
        return {"ok": True, "message": msg}

    # ================================================================== connect devices / services
    def connect(self, name: str, url: str, token: str | None = None) -> dict:
        spec: dict[str, Any] = {"url": url}
        if token:
            ref = f"mcp_{name.replace('-', '_')}"
            self.keys.set(ref, token)
            spec["headers"] = {"Authorization": f"Bearer keyref:{ref}"}
        self.cfg.data.setdefault("mcp_servers", {})[name] = spec
        self.cfg.save()
        hub = getattr(self.agent, "hub", None)
        tools: list[str] | str = []
        if hub is not None:
            try:
                tools = hub.attach(name, spec)
            except Exception as e:
                tools = f"could not connect yet: {e}"
        msg = (f"Connected {name}: {len(tools)} tools available." if isinstance(tools, list) and tools else
               f"Saved {name} ({url}); " + (tools if isinstance(tools, str) else "it will connect when reachable."))
        self.events.emit("connected", name=name, tools=tools if isinstance(tools, list) else [], message=msg)
        return {"ok": True, "message": msg}

    # ================================================================== dispatcher
    def handle(self, routed: dict) -> dict:
        a, args = routed["action"], routed.get("args", {})
        if a == "greet":
            st = self.setup_status()
            if st["ready"]:
                self.events.emit("notice", text="Hi! Tell me what you'd like done — for example “make a simple "
                                                "web page for our family”, or paste a screenshot of something to fix.")
            else:
                self.events.emit("notice", text="Hi! Before I can do things, I need an AI model — see the setup card.")
                self.show_setup(probe=True)
            return {"ok": True}
        if a == "status":
            return {"ok": True, **self.show_setup(probe=True)}
        if a == "keys":
            return {"ok": True, "keys": self.list_keys()}
        if a == "key_card":
            self.events.emit("key_card", provider=args.get("provider", "gemini"), owner=args.get("owner", ""),
                             key_page=KEY_PAGES.get(args.get("provider", "gemini")), text=args.get("text", ""))
            return {"ok": True}
        if a == "remove_key":
            return self.remove_key(args["owner"])
        if a == "install_local":
            def run() -> None:
                if self.install_local()["ok"] and self.on_ready:
                    self.on_ready()
            threading.Thread(target=run, daemon=True).start()
            return {"ok": True, "message": "started"}
        if a in ("local_on", "local_off"):
            return self.set_local_only(a == "local_on")
        if a == "undo":
            return self.undo(args.get("snapshot"))
        if a in ("update", "update_undo"):
            if self.updater is None:
                self.events.emit("notice", text="Updates aren't available in this copy of Wright.")
                return {"ok": False}
            if a == "update_undo":
                return self.updater.rollback()
            threading.Thread(target=self.updater.apply, daemon=True).start()
            self.events.emit("notice", text="Checking for improvements…")
            return {"ok": True, "message": "started"}
        if a == "connect":
            self.events.emit("token_card", name=args["name"], url=args["url"])
            return {"ok": True}
        return {"ok": False, "message": f"unknown action {a}"}
