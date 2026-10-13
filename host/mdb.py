#!/usr/bin/env python3
"""MDB command-line tool — the ``adb``-style front end for a Muse watch
speaking the Muse Developer Bridge protocol (see ../PROTOCOL.md).

One-shot use:
    mdb.py --port /dev/cu.usbmodemXXXX health
    mdb.py --port /dev/cu.usbmodemXXXX screenshot --out watch.png
    mdb.py --port /dev/cu.usbmodemXXXX tap 205 251
    mdb.py --port /dev/cu.usbmodemXXXX goto settings
    mdb.py --port /dev/cu.usbmodemXXXX logs

Interactive use:
    mdb.py --port /dev/cu.usbmodemXXXX shell
    (verbs may be typed with or without the ``mdb`` prefix; ``quit`` exits;
     ``screenshot <file>`` inside the shell saves a PNG.)

The port may also come from the MDB_PORT environment variable.
Exit status is 0 on success, 1 on a device ERR or local failure, 2 on
usage errors.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mdb_client import MdbClient, MdbError  # noqa: E402


def _print_json(obj) -> None:
    print(json.dumps(obj, indent=2, sort_keys=True))


def _one_shot(client: MdbClient, args) -> int:
    cmd = args.command
    if cmd == "health":
        _print_json(client.health())
    elif cmd == "panic":
        _print_json(client.panic_info())
    elif cmd == "version":
        _print_json(client.version())
    elif cmd == "logs":
        for line in client.logs():
            print(line)
    elif cmd == "screenshot":
        shot = client.screenshot()
        out = args.out or "screenshot.png"
        shot.save_png(out)
        print(f"saved {out} ({shot.width}x{shot.height}, {shot.format})")
    elif cmd == "touch":
        print("streaming touch events — Ctrl-C to stop")
        try:
            for ev in client.touch_events():
                print(f"x={ev.x} y={ev.y} state={ev.state}" + (f" src={ev.src}" if ev.src else ""))
        except KeyboardInterrupt:
            print("\nstopped")
    elif cmd == "tap":
        client.tap(args.x, args.y)
    elif cmd == "swipe":
        client.swipe(args.x1, args.y1, args.x2, args.y2, args.ms)
    elif cmd == "press":
        client.press(args.button)
    elif cmd == "goto":
        _print_json(client.goto(args.screen))
    elif cmd == "open":
        _print_json(client.open_app(args.app))
    elif cmd == "wake":
        _print_json(client.wake())
    elif cmd == "sleep":
        _print_json(client.sleep())
    elif cmd == "reboot":
        _print_json(client.reboot())
        print("device is rebooting; the serial port will drop and reappear")
    elif cmd == "config-get":
        _print_json(client.config_get())
    elif cmd == "config-set":
        print(
            "warning: the setup string may contain secrets. It is sent to the "
            "device only — never logged by mdb — but it will appear in your "
            "shell history.",
            file=sys.stderr,
        )
        applied = client.config_set(args.setup_string)
        print(f"applied {applied} field(s)")
    else:  # pragma: no cover - argparse prevents this
        raise AssertionError(cmd)
    return 0


_SHELL_HELP = """\
commands (the ``mdb`` prefix is optional):
  version | health | panic | logs
  screenshot <file.png>
  touch                      stream touch events (Ctrl-C to stop)
  tap <x> <y> | swipe <x1> <y1> <x2> <y2> [ms] | press <boot|ptt>
  goto <screen> | open <app> | wake | sleep | reboot
  config-get | config-set <setup-string>
  quit | exit | help
"""


def _shell(client: MdbClient) -> int:
    print("mdb shell — type 'help' for commands, 'quit' to exit")
    while True:
        try:
            line = input("mdb> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not line:
            continue
        if line in ("quit", "exit"):
            return 0
        if line == "help":
            print(_SHELL_HELP)
            continue
        if line.startswith("mdb "):
            line = line[4:].lstrip()
        parts = line.split()
        verb, rest = parts[0], parts[1:]
        try:
            if verb == "screenshot":
                out = rest[0] if rest else "screenshot.png"
                shot = client.screenshot()
                shot.save_png(out)
                print(f"saved {out} ({shot.width}x{shot.height}, {shot.format})")
            elif verb == "health":
                _print_json(client.health())
            elif verb == "panic":
                _print_json(client.panic_info())
            elif verb == "version":
                _print_json(client.version())
            elif verb == "logs":
                for entry in client.logs():
                    print(entry)
            elif verb == "touch":
                print("streaming — Ctrl-C to stop")
                try:
                    for ev in client.touch_events():
                        print(f"x={ev.x} y={ev.y} state={ev.state}")
                except KeyboardInterrupt:
                    print("\nstopped")
            elif verb == "tap" and len(rest) == 2:
                client.tap(int(rest[0]), int(rest[1]))
                print("ok")
            elif verb == "swipe" and len(rest) in (4, 5):
                nums = [int(v) for v in rest]
                client.swipe(*nums)
                print("ok")
            elif verb == "press" and len(rest) == 1:
                client.press(rest[0])
                print("ok")
            elif verb == "goto" and len(rest) == 1:
                _print_json(client.goto(rest[0]))
            elif verb == "open" and len(rest) == 1:
                _print_json(client.open_app(rest[0]))
            elif verb in ("wake", "sleep", "reboot"):
                _print_json(getattr(client, verb)())
            elif verb == "config-get":
                _print_json(client.config_get())
            elif verb == "config-set" and rest:
                print("warning: setup strings may contain secrets (sent to device only, not logged)", file=sys.stderr)
                print(f"applied {client.config_set(' '.join(rest))} field(s)")
            else:
                print(f"unrecognised or incomplete command: {line!r} (try 'help')")
        except MdbError as exc:
            print(f"ERR {exc.code}: {exc.message}")
        except ValueError:
            print("numeric arguments must be integers")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="mdb", description="Muse Developer Bridge CLI")
    p.add_argument("--port", default=os.environ.get("MDB_PORT"), help="serial port (or $MDB_PORT)")
    p.add_argument("--baud", type=int, default=115200)
    p.add_argument("--timeout", type=float, default=5.0)
    sub = p.add_subparsers(dest="command")

    sub.add_parser("devices", help="list serial ports")
    sub.add_parser("health")
    sub.add_parser("panic")
    sub.add_parser("version")
    sub.add_parser("logs")
    sub.add_parser("touch", help="stream touch events")
    sub.add_parser("shell", help="interactive shell")
    sp = sub.add_parser("screenshot")
    sp.add_argument("--out", default=None, help="output PNG (default screenshot.png)")
    sp = sub.add_parser("tap")
    sp.add_argument("x", type=int)
    sp.add_argument("y", type=int)
    sp = sub.add_parser("swipe")
    sp.add_argument("x1", type=int)
    sp.add_argument("y1", type=int)
    sp.add_argument("x2", type=int)
    sp.add_argument("y2", type=int)
    sp.add_argument("ms", type=int, nargs="?", default=None)
    sp = sub.add_parser("press")
    sp.add_argument("button", choices=["boot", "ptt"])
    sp = sub.add_parser("goto")
    sp.add_argument("screen")
    sp = sub.add_parser("open")
    sp.add_argument("app")
    sub.add_parser("wake")
    sub.add_parser("sleep")
    sub.add_parser("reboot")
    sub.add_parser("config-get")
    sp = sub.add_parser("config-set")
    sp.add_argument("setup_string")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "devices":
        for port in MdbClient.list_ports():
            print(f"{port['device']}\t{port['description']}\t{port['hwid']}")
        return 0
    if not args.command:
        build_parser().print_help()
        return 2
    if not args.port:
        print("error: no serial port — pass --port or set MDB_PORT", file=sys.stderr)
        return 2
    try:
        with MdbClient(args.port, baud=args.baud, timeout=args.timeout) as client:
            if args.command == "shell":
                return _shell(client)
            return _one_shot(client, args)
    except MdbError as exc:
        print(f"ERR {exc.code}: {exc.message}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"serial error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
