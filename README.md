# MDB — Muse Developer Bridge

**MDB is `adb` for the Muse watch.** It is an investigation + control bridge
baked into the watch firmware, so an AI agent (or a developer) can *see* the
device, *drive* the device, and *fix* the device — over a cable, without
guessing what the glass is doing.

The reference firmware is [AIWatchOS](https://github.com/Franzferdinan51/AIWatchOS)
for the Waveshare ESP32-S3-Touch-AMOLED-2.06 (Muse) watch. MDB ships in
AIWatchOS's hardware-bringup build line, behind a debug flag. This repo is the
standalone SDK: the normative protocol spec, a Python host client, a CLI, an
MCP server, and the firmware implementer's guide — everything needed to put
MDB on another firmware or to control a watch that already speaks it.

## Why MDB exists

Host-side tests can prove logic. They cannot prove a panel, a touch
controller, or a font pipeline. MDB closes that loop: the agent pulls the
actual framebuffer, reads the actual logs, injects actual touches, and
compares what the software composed against what the panel shows. No more
"the UI looks broken" debugging by description.

## The ADB analogy

| adb | MDB | what it does |
|---|---|---|
| `adb shell screencap` | `mdb screenshot` | dump the composited framebuffer (RGB565) as structured data / PNG |
| `adb shell input tap x y` | `mdb tap <x> <y>` | inject a touch tap at panel coordinates |
| `adb shell input swipe …` | `mdb swipe <x1> <y1> <x2> <y2> [ms]` | inject a swipe |
| `adb shell input keyevent` | `mdb press <boot\|ptt>` | press a hardware button |
| `adb shell am start` | `mdb open <app>` / `mdb goto <screen>` | navigate directly, no fake touches |
| `adb logcat` | `mdb logs` | dump the firmware log ring |
| `adb shell dumpsys` | `mdb health` / `mdb panic` | state snapshot / crash retention info |
| `adb shell settings` | `mdb config get/set` | redacted config read; provisioning write |
| `adb reboot` | `mdb reboot` | reboot the watch |

## Architecture

```
┌──────────────────────────── watch firmware ────────────────────────────┐
│  UI compositor ──► PSRAM framebuffer ──► panel flush (CO5300, QSPI)    │
│        ▲                    │                                          │
│        │ input queue        │ read (pre-swap RGB565)                   │
│  touch/GPIO drivers ──► input events ──► MDB dispatcher                │
│                                     │   ▲ synthetic events injected    │
│  config store (NVS) ──presence/len──┘   │ into the same queue          │
│  log ring (PSRAM) ──────────────────────┘                              │
└──────────────────────────────┬─────────────────────────────────────────┘
                        USB serial (v1) — line-based, `mdb …` commands
                               │   later: same protocol over gadget links
┌──────────────────────────────▼─────────────── host ────────────────────┐
│  host/mdb_client.py   Python client (framing, screenshot→PNG, parsers) │
│  host/mdb.py          CLI (interactive shell + one-shot commands)      │
│  host/mcp_server.py   MCP server → watch_* tools for AI agents         │
└─────────────────────────────────────────────────────────────────────────┘
```

Protocol rules that matter (full spec: [PROTOCOL.md](PROTOCOL.md)):

- **Line-based.** One command per line, prefixed `mdb`. Replies start with
  `OK` or `ERR <code> <message>`, so they can be picked out of the firmware's
  normal log stream on the same console.
- **Secrets never cross the wire.** `mdb config get` reports *presence and
  length only* — never values. The only direction a secret may travel is
  host → device, via `mdb config set` (the provisioning route).
- **The screenshot is the composited framebuffer**, pre-panel-swap RGB565 —
  i.e. what the software intended to draw. Comparing it against a photo of
  the glass isolates panel/flush faults from composition faults.

## Quickstart

```sh
pip install -r host/requirements.txt   # pyserial + Pillow

# one-shot commands
python3 host/mdb.py --port /dev/cu.usbmodem1101 health
python3 host/mdb.py --port /dev/cu.usbmodem1101 screenshot --out watch.png
python3 host/mdb.py --port /dev/cu.usbmodem1101 tap 205 460
python3 host/mdb.py --port /dev/cu.usbmodem1101 logs

# interactive shell
python3 host/mdb.py --port /dev/cu.usbmodem1101 shell

# end-to-end smoke test against a real watch
python3 examples/smoke_test.py --port /dev/cu.usbmodem1101 --out /tmp/mdb-smoke
```

### As MCP tools (for agents)

`host/mcp_server.py` is a dependency-light MCP server (stdio, hand-rolled
JSON-RPC — no third-party MCP package required). It exposes:

`watch_screenshot`, `watch_tap`, `watch_swipe`, `watch_press`, `watch_goto`,
`watch_open`, `watch_logs`, `watch_health`, `watch_reboot`

Point your MCP client at it with the serial port in the environment:

```json
{
  "mcpServers": {
    "mdb": {
      "command": "python3",
      "args": ["host/mcp_server.py"],
      "env": { "MDB_PORT": "/dev/cu.usbmodem1101" }
    }
  }
}
```

From Python:

```python
from host.mdb_client import MdbClient

with MdbClient("/dev/cu.usbmodem1101") as watch:
    print(watch.health())                 # state snapshot dict
    watch.screenshot().save_png("now.png")
    watch.tap(205, 251)                   # centre of the 410×502 panel
    print("\n".join(watch.logs()))
```

## Repo layout

```
README.md               this file
PROTOCOL.md             normative wire protocol (v0.1-draft)
host/
  mdb_client.py         Python client library
  mdb.py                CLI + interactive shell
  mcp_server.py         MCP server exposing watch_* tools (stdio)
  test_client.py        fake-serial unit tests for the client
  requirements.txt      pyserial, Pillow
firmware/
  INTEGRATION.md        device-side implementer's guide
examples/
  smoke_test.py         connect → health → screenshot → settings → screenshot
```

## Status

Protocol **v0.1-draft**, authored to match the MDB implementation landing
concurrently in AIWatchOS's bringup build (the firmware-side protocol doc,
`docs/mdb.md` in that repo, is written by the same effort; if the two ever
diverge, the firmware doc describes the shipping bytes and this spec gets a
correcting revision). Transport v1 is USB serial; the grammar is deliberately
transport-shaped so the same commands can ride the Muse/Hermes gadget links
later.

MDB is debug tooling. In the reference firmware it is compiled in only under
a debug Kconfig flag and is expected to ship **off** in release builds.
