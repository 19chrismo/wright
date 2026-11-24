"""wright — command line entry point."""
from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="wright", description="Local-first agent that builds the thing.")
    sub = ap.add_subparsers(dest="cmd")
    d = sub.add_parser("demo", help="run an offline demonstration of a build step")
    d.add_argument("name")
    args = ap.parse_args(argv)
    if args.cmd == "demo":
        from .demos import DEMOS
        if args.name not in DEMOS:
            print(f"unknown demo; choose from {sorted(DEMOS)}", file=sys.stderr)
            return 2
        t = DEMOS[args.name]()
        return 0 if t.status == "done" else 1
    ap.print_help()
    return 0
