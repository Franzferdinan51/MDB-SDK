#!/usr/bin/env python3
"""MDB smoke test — the five-minute proof that a watch speaks MDB.

    python3 smoke_test.py --port /dev/cu.usbmodemXXXX --out /tmp/mdb-smoke

Connects, prints version + health, screenshots the current screen,
navigates to Settings, screenshots again, and prints the last few log
lines. Everything it asserts is soft (it prints what it finds rather than
failing on content) except the protocol itself: any ERR or timeout stops
the run with a nonzero exit, because a watch that can't do *this* can't
do anything else over MDB either.
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "host"))

from mdb_client import MdbClient, MdbError  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="MDB smoke test")
    ap.add_argument("--port", default=os.environ.get("MDB_PORT"), help="serial port (or $MDB_PORT)")
    ap.add_argument("--out", default="/tmp/mdb-smoke", help="directory for screenshots")
    args = ap.parse_args()
    if not args.port:
        print("error: pass --port or set MDB_PORT", file=sys.stderr)
        return 2
    os.makedirs(args.out, exist_ok=True)

    try:
        with MdbClient(args.port, timeout=10.0) as watch:
            version = watch.version()
            print(f"version: {version}")

            health = watch.health()
            print(
                "health: battery={battery_pct}% wifi={wifi} muse={muse_link} "
                "hermes={hermes_link} reset={reset_reason} boot#{boot_count}".format(**health)
            )

            before = watch.screenshot()
            path = before.save_png(os.path.join(args.out, "01-before.png"))
            print(f"screenshot: {path} ({before.width}x{before.height} {before.format})")

            # Navigate to Settings through the shell's own navigation —
            # deterministic, unlike tapping tile coordinates.
            watch.goto("settings")
            after = watch.screenshot()
            path = after.save_png(os.path.join(args.out, "02-settings.png"))
            print(f"screenshot: {path}")

            logs = watch.logs()
            print(f"logs: {len(logs)} lines; last 3:")
            for line in logs[-3:]:
                print(f"  {line}")

            config = watch.config_get()
            summary = ", ".join(
                f"{k}={'set' if v.get('present') else 'missing'}" for k, v in sorted(config.items())
            )
            print(f"config presence: {summary}")
    except MdbError as exc:
        print(f"SMOKE FAIL — device said ERR {exc.code}: {exc.message}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"SMOKE FAIL — serial error: {exc}", file=sys.stderr)
        return 1

    print("SMOKE OK — compare 01-before.png against the glass, and 02-settings.png against Settings.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
