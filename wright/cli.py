"""wright — command line entry point."""
from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="wright", description="Local-first agent that builds the thing.")
    sub = ap.add_subparsers(dest="cmd")
    d = sub.add_parser("demo", help="run an offline demonstration of a build step")
    d.add_argument("name")
    rb = sub.add_parser("rollback", help="restore a target from a snapshot (one command)")
    rb.add_argument("snapshot", help="snapshot id (see `wright snapshots`)")
    sub.add_parser("snapshots", help="list restorable snapshots")
    args = ap.parse_args(argv)
    if args.cmd in ("rollback", "snapshots"):
        from .config import Paths, default_home
        from .sandbox import SnapshotStore
        paths = Paths(default_home())
        stores = sorted(paths.tasks.glob("*/snapshots")) if paths.tasks.exists() else []
        if args.cmd == "snapshots":
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
    if args.cmd == "demo":
        from .demos import DEMOS
        if args.name not in DEMOS:
            print(f"unknown demo; choose from {sorted(DEMOS)}", file=sys.stderr)
            return 2
        t = DEMOS[args.name]()
        return 0 if t.status == "done" else 1
    ap.print_help()
    return 0
