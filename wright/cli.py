"""wright — command line entry point. `wright` alone opens the app window."""
from __future__ import annotations

import argparse
import getpass
import json
import sys
from pathlib import Path

PROVIDERS = {"gemini": ["gemini-*"], "openai": ["gpt-*", "o*"], "openrouter": ["*/*"]}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="wright", description="Local-first agent that builds the thing.")
    sub = ap.add_subparsers(dest="cmd")
    r = sub.add_parser("run", help="open the app window (default)")
    r.add_argument("--no-window", action="store_true", help="serve the UI without opening a window")
    t = sub.add_parser("task", help="run one goal headless and print the report")
    t.add_argument("goal")
    t.add_argument("--target", help="folder to work in (default: a new project folder)")
    k = sub.add_parser("keys", help="manage model keys (stored in the macOS Keychain)")
    k.add_argument("action", choices=["add", "list", "remove", "set"])
    k.add_argument("provider", nargs="?", help="gemini | openai | openrouter")
    k.add_argument("--model", action="append", help="model name to put in the auto route (repeatable)")
    k.add_argument("--ref", help="key reference to remove")
    k.add_argument("--owner", help="whose key this is (e.g. a household member); shown in reports")
    k.add_argument("--daily-cap", type=int, help="most requests per day this key may serve for Wright")
    m = sub.add_parser("mcp", help="attach or detach external MCP servers")
    m.add_argument("action", choices=["add", "list", "remove"])
    m.add_argument("name", nargs="?")
    m.add_argument("--url", help="streamable HTTP endpoint (e.g. Home Assistant)")
    m.add_argument("--header", action="append", default=[], help="HTTP header for --url, 'Name: value'")
    m.add_argument("command", nargs=argparse.REMAINDER, help="-- command and args for a stdio server")
    sub.add_parser("doctor", help="check setup and print exact fixes")
    sub.add_parser("audit", help="verify the episodic log and show recent history and calibration")
    s = sub.add_parser("self", help="self-modification history and rollback")
    s.add_argument("action", choices=["log", "rollback"])
    s.add_argument("rev", nargs="?")
    rb = sub.add_parser("rollback", help="restore a target from a snapshot (one command)")
    rb.add_argument("snapshot")
    sub.add_parser("snapshots", help="list restorable snapshots")
    lo = sub.add_parser("local-only", help="route every model call to the local model")
    lo.add_argument("state", choices=["on", "off"])
    d = sub.add_parser("demo", help="run an offline demonstration of a build step")
    d.add_argument("name")
    args = ap.parse_args(argv)
    cmd = args.cmd or "run"
    from .config import ensure_path
    ensure_path()                       # Wright.app starts with Finder's minimal PATH

    if cmd == "run":
        from .app import run_app
        return run_app(window=not getattr(args, "no_window", False))
    if cmd == "demo":
        from .demos import DEMOS
        if args.name not in DEMOS:
            print(f"unknown demo; choose from {sorted(DEMOS)}", file=sys.stderr)
            return 2
        res = DEMOS[args.name]()
        task = res["task"] if isinstance(res, dict) else res
        return 0 if task.status == "done" else 1

    from .config import Config
    cfg = Config.load()

    if cmd == "task":
        from .app import build_pool
        from .agent import Agent
        from .events import EventBus, console_printer
        ev = EventBus()
        ev.subscribe(console_printer)
        server, model = build_pool(cfg, ev)
        agent = Agent(cfg, model, events=ev, confirm=_terminal_confirm)
        try:
            task = agent.run(args.goal, target=args.target)
            from .report import render
            print("\n" + render(task.state["report"]))
            return 0 if task.status == "done" else 1
        finally:
            agent.close()
            server.stop()
    if cmd == "keys":
        from .secrets import KeyStore
        ks = KeyStore(cfg.paths.secrets)
        lanes = cfg.data["pool"]["lanes"]
        if args.action == "list":
            for lane in lanes:
                ref = lane.get("key_ref")
                state = "local" if lane.get("local") else ("key stored" if ref and ks.get(ref) else "NO KEY")
                extra = (f"  owner {lane['owner']}" if lane.get("owner") else "") + \
                        (f"  cap {lane['daily_cap']}/day" if lane.get("daily_cap") else "")
                print(f"{lane['id']:18s} {lane['provider']:10s} {state}{extra}")
            return 0
        if args.action == "set":
            ref = args.provider or args.ref
            if not ref:
                print("usage: wright keys set <name>   (e.g. ha_token)", file=sys.stderr)
                return 2
            secret = getpass.getpass(f"Paste the secret for {ref} (input is hidden): ").strip()
            ks.set(ref, secret)
            print(f"saved {ref}; reference it as keyref:{ref}")
            return 0
        if args.action == "remove":
            ref = args.ref or ""
            ks.delete(ref)
            cfg.data["pool"]["lanes"] = [l for l in lanes if l.get("key_ref") != ref]
            cfg.save()
            print(f"removed {ref}")
            return 0
        prov = args.provider
        if prov not in PROVIDERS:
            print(f"provider must be one of {sorted(PROVIDERS)}", file=sys.stderr)
            return 2
        key = getpass.getpass(f"Paste your {prov} API key (input is hidden): ").strip()
        if len(key) < 10:
            print("that does not look like a key; nothing saved", file=sys.stderr)
            return 1
        import re as _re
        owner = _re.sub(r"[^a-z0-9]+", "-", (args.owner or "").lower()).strip("-")
        n = 1 + sum(1 for l in lanes if l["provider"] == prov)
        lane_id = f"{prov}-{owner}" if owner else f"{prov}-{n}"
        if any(l["id"] == lane_id for l in lanes):
            print(f"a lane named {lane_id} already exists; remove it first (wright keys remove --ref "
                  f"{lane_id.replace('-', '_')})", file=sys.stderr)
            return 1
        ref = lane_id.replace("-", "_")
        ks.set(ref, key)
        lane = {"id": lane_id, "provider": prov, "models": PROVIDERS[prov], "key_ref": ref}
        if owner:
            lane["owner"] = args.owner
        if args.daily_cap:
            lane["daily_cap"] = args.daily_cap
        lanes.insert(len([l for l in lanes if not l.get("local")]), lane)
        if args.model:
            auto = cfg.data["pool"]["routes"]["auto"]
            local = [x for x in auto if x.startswith("ollama:")]
            cfg.data["pool"]["routes"]["auto"] = [x for x in auto if not x.startswith("ollama:") and x not in args.model] \
                + args.model + local
        cfg.save()
        print(f"saved as {ref} ({'macOS Keychain' if ks.keychain else 'protected key file'}); lane {lane_id} added"
              + (f" for {args.owner}" if owner else "")
              + (f", at most {args.daily_cap} requests a day" if args.daily_cap else "")
              + ". Run `wright doctor` to test it.")
        return 0
    if cmd == "mcp":
        servers = cfg.data.setdefault("mcp_servers", {})
        if args.action == "list":
            for n, spec in servers.items():
                print(f"{n:16s} {spec.get('url') or ' '.join([spec.get('command', '')] + spec.get('args', []))}")
            return 0
        if args.action == "remove":
            servers.pop(args.name, None)
            cfg.save()
            print(f"removed {args.name}")
            return 0
        if args.url:
            spec = {"url": args.url}
            if args.header:
                spec["headers"] = dict(h.split(":", 1) for h in args.header)
        else:
            command = [c for c in args.command if c != "--"]
            if not command:
                print("give --url URL or -- command args...", file=sys.stderr)
                return 2
            spec = {"command": command[0], "args": command[1:]}
        servers[args.name] = spec
        cfg.save()
        print(f"attached {args.name}; it loads the next time Wright starts")
        return 0
    if cmd == "doctor":
        from .doctor import run as doctor
        return doctor(cfg)
    if cmd == "audit":
        from .session import Continuity
        c = Continuity(cfg.paths)
        opened = c.open()
        print(f"episodic log: {'intact' if opened['chain_ok'] else 'BROKEN'} · {opened['entries']} entries"
              + (" · repaired from mirror" if opened["repaired"] else ""))
        print(c.tail_text())
        print(c.calibration.render())
        return 0 if opened["chain_ok"] else 1
    if cmd == "self":
        from .selfmod import SelfStore
        st = SelfStore(cfg.paths)
        st.init()
        if args.action == "log":
            print("\n".join(st.log()))
            return 0
        from .selfmod import SelfModGate
        from .skills import SkillLibrary
        from .agent import Agent
        gate = SelfModGate(cfg.paths, SkillLibrary(cfg.paths.procedural / "skills"), lambda *a: (False, ""))
        print(f"self state rolled back to {args.rev}; now at {gate.rollback(args.rev)}")
        return 0
    if cmd == "local-only":
        cfg.data["local_only"] = args.state == "on"
        cfg.save()
        print(f"local-only mode {args.state}: " + ("every model call stays on this Mac" if args.state == "on"
                                                   else "cloud lanes allowed"))
        return 0
    if cmd in ("rollback", "snapshots"):
        from .sandbox import SnapshotStore
        stores = sorted(cfg.paths.tasks.glob("*/snapshots")) if cfg.paths.tasks.exists() else []
        if cmd == "snapshots":
            for st in stores:
                for m in SnapshotStore(st).list():
                    print(f"{m['id']}  {m['label']:14s} {m['target']}")
            return 0
        for st in stores:
            if (st / args.snapshot / "meta.json").exists():
                store = SnapshotStore(st)
                diff = store.restore(args.snapshot)
                print(f"restored {store.meta(args.snapshot)['target']} to {args.snapshot}: "
                      f"{len(diff['changed'])} changed, {len(diff['added'])} re-created, "
                      f"{len(diff['deleted'])} removed; verified against the snapshot manifest")
                return 0
        print(f"no snapshot {args.snapshot}", file=sys.stderr)
        return 1
    ap.print_help()
    return 0


def _terminal_confirm(category: str, description: str, details: dict) -> bool:
    if not sys.stdin.isatty():
        return False
    ans = input(f"\nConfirmation needed ({category}): {description}\nApprove? [y/N] ").strip().lower()
    return ans in ("y", "yes")
