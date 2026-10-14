#!/usr/bin/env python3
"""MDB MCP server — expose a Muse watch as tools for an AI agent.

Implementation choice (deliberate): this is a **minimal hand-rolled MCP
server** — JSON-RPC 2.0 over stdio, newline-delimited messages, just the
methods an MCP client needs (``initialize``, ``ping``, ``tools/list``,
``tools/call``). The official ``mcp`` Python package is *not* required:
the MDB SDK's host side stays dependency-light (pyserial + Pillow only,
both pulled in lazily by mdb_client), so the server runs anywhere the
client does, including minimal containers where installing the full MCP
SDK is awkward. The wire behaviour follows the MCP stdio transport as of
protocol version 2024-11-05.

Configuration (environment):
    MDB_PORT   serial port of the watch (required for tool calls)
    MDB_BAUD   baud rate (default 115200)

The serial link is a single conversation: tool calls are serialized with
a lock, the client connects lazily on the first call, and a dead link
(e.g. after watch_reboot) is re-opened on the next call.

Tools: watch_screenshot, watch_tap, watch_swipe, watch_press, watch_goto,
watch_open, watch_logs, watch_health, watch_reboot, watch_settings,
watch_sd, watch_sys, watch_nvs, watch_poweroff, watch_chirp, watch_diag,
watch_ota.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mdb_client import MdbClient, MdbError  # noqa: E402

SERVER_NAME = "mdb"
SERVER_VERSION = "0.2.0"
PROTOCOL_VERSION = "2024-11-05"

_lock = threading.Lock()
_client: MdbClient | None = None


def _get_client() -> MdbClient:
    """Return the shared client, (re)connecting if needed."""
    global _client
    port = os.environ.get("MDB_PORT")
    if not port:
        raise MdbError("bad_args", "MDB_PORT is not set — point it at the watch's serial port")
    if _client is None or not _client.connected:
        _client = MdbClient(port, baud=int(os.environ.get("MDB_BAUD", "115200")), timeout=10.0)
        _client.connect()
    return _client


def _drop_client() -> None:
    global _client
    if _client is not None:
        _client.close()
        _client = None


def _text(text: str, is_error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def _call_tool(name: str, args: dict) -> dict:
    with _lock:
        try:
            client = _get_client()
            if name == "watch_screenshot":
                shot = client.screenshot()
                png_b64 = base64.b64encode(shot.to_png_bytes()).decode("ascii")
                return {
                    "content": [
                        {"type": "image", "data": png_b64, "mimeType": "image/png"},
                        {
                            "type": "text",
                            "text": (
                                f"framebuffer {shot.width}x{shot.height} format={shot.format} "
                                "(composited framebuffer, pre-panel-swap — what the "
                                "software drew; compare against the physical glass "
                                "to isolate panel faults)"
                            ),
                        },
                    ],
                    "isError": False,
                }
            if name == "watch_tap":
                client.tap(int(args["x"]), int(args["y"]))
                return _text(f"tapped ({args['x']}, {args['y']})")
            if name == "watch_swipe":
                client.swipe(
                    int(args["x1"]),
                    int(args["y1"]),
                    int(args["x2"]),
                    int(args["y2"]),
                    int(args["duration_ms"]) if args.get("duration_ms") else None,
                )
                return _text("swiped")
            if name == "watch_press":
                client.press(str(args["button"]))
                return _text(f"pressed {args['button']}")
            if name == "watch_goto":
                return _text(json.dumps(client.goto(str(args["screen"]))))
            if name == "watch_open":
                return _text(json.dumps(client.open_app(str(args["app"]))))
            if name == "watch_logs":
                lines = client.logs()
                limit = int(args.get("max_lines", 200))
                return _text("\n".join(lines[-limit:]))
            if name == "watch_health":
                return _text(json.dumps(client.health(), indent=2, sort_keys=True))
            if name == "watch_reboot":
                try:
                    client.reboot()
                finally:
                    _drop_client()  # the link is gone; reconnect lazily next call
                return _text("reboot command accepted; the watch is restarting")
            if name == "watch_settings":
                action = str(args.get("action", "all"))
                if action == "set":
                    v = args["value"]
                    if isinstance(v, bool):
                        v = "on" if v else "off"
                    return _text(json.dumps(client.settings_set(str(args["key"]), v)))
                if action == "get":
                    return _text(json.dumps(client.settings_get(str(args["key"]))))
                return _text(json.dumps(client.settings_all(), indent=2, sort_keys=True))
            if name == "watch_sd":
                action = str(args.get("action", "status"))
                if action == "mount":
                    return _text(json.dumps(client.sd_mount()))
                if action == "ls":
                    return _text("\n".join(client.sd_ls(str(args.get("path", "/")))))
                if action == "cat":
                    return _text(client.sd_cat(str(args["path"])))
                if action == "rm":
                    return _text(json.dumps(client.sd_rm(str(args["path"]))))
                if action == "write":
                    return _text(json.dumps(client.sd_write(str(args["path"]), str(args["data"]).encode())))
                return _text(json.dumps(client.sd_status(), indent=2, sort_keys=True))
            if name == "watch_sys":
                what = str(args.get("what", "info"))
                if what == "heap":
                    return _text(json.dumps(client.sys_heap(), indent=2, sort_keys=True))
                if what == "tasks":
                    return _text("\n".join(client.sys_tasks()))
                return _text(json.dumps(client.sys_info(), indent=2, sort_keys=True))
            if name == "watch_nvs":
                action = str(args.get("action", "list"))
                if action == "erase":
                    return _text(json.dumps(client.nvs_erase(str(args["ns"]), str(args["key"]))))
                if action == "erase-ns":
                    return _text(json.dumps(client.nvs_erase_ns(str(args["ns"]))))
                ns = args.get("ns")
                return _text("\n".join(client.nvs_list(str(ns) if ns else None)))
            if name == "watch_poweroff":
                try:
                    client.poweroff()
                finally:
                    _drop_client()
                return _text("poweroff accepted; the watch is off")
            if name == "watch_chirp":
                return _text(json.dumps(client.chirp(int(args.get("volume", 35)))))
            if name == "watch_diag":
                what = str(args.get("what", "regs"))
                if what == "regs":
                    return _text(client.regs())
                if what == "i2cscan":
                    return _text(client.i2cscan())
                if what == "pump":
                    return _text(json.dumps(client.pump()))
                if what == "loopback":
                    return _text(json.dumps(client.loopback()))
                return _text(f"unknown diag: {what}", is_error=True)
            if name == "watch_ota":
                action = str(args.get("action", "status"))
                if action == "start":
                    return _text(json.dumps(client.ota_start(str(args["url"]), bool(args.get("force", False)))))
                return _text(json.dumps(client.ota_status()))
            return _text(f"unknown tool: {name}", is_error=True)
        except MdbError as exc:
            return _text(f"ERR {exc.code}: {exc.message}", is_error=True)
        except (OSError, ValueError, KeyError) as exc:
            _drop_client()
            return _text(f"host error: {exc}", is_error=True)


TOOLS = [
    {
        "name": "watch_screenshot",
        "description": (
            "Dump the watch's composited framebuffer as a PNG image. This is what "
            "the software drew (pre-panel-swap RGB565). Use it to see the UI; "
            "compare with a photo of the glass to isolate panel/flush faults."
        ),
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "watch_tap",
        "description": "Inject a tap at panel coordinates (origin top-left).",
        "inputSchema": {
            "type": "object",
            "properties": {"x": {"type": "integer"}, "y": {"type": "integer"}},
            "required": ["x", "y"],
        },
    },
    {
        "name": "watch_swipe",
        "description": "Inject a swipe from (x1, y1) to (x2, y2).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "x1": {"type": "integer"},
                "y1": {"type": "integer"},
                "x2": {"type": "integer"},
                "y2": {"type": "integer"},
                "duration_ms": {"type": "integer", "description": "50–5000, default 300"},
            },
            "required": ["x1", "y1", "x2", "y2"],
        },
    },
    {
        "name": "watch_press",
        "description": "Press a hardware button: 'boot' or 'ptt'.",
        "inputSchema": {
            "type": "object",
            "properties": {"button": {"type": "string", "enum": ["boot", "ptt"]}},
            "required": ["button"],
        },
    },
    {
        "name": "watch_goto",
        "description": "Navigate directly to a screen by id (e.g. clock, launcher, settings, chat, weather, notifications).",
        "inputSchema": {
            "type": "object",
            "properties": {"screen": {"type": "string"}},
            "required": ["screen"],
        },
    },
    {
        "name": "watch_open",
        "description": "Open an app by id (e.g. muse, hermes, weather, settings).",
        "inputSchema": {
            "type": "object",
            "properties": {"app": {"type": "string"}},
            "required": ["app"],
        },
    },
    {
        "name": "watch_logs",
        "description": "Dump the firmware log ring (oldest first).",
        "inputSchema": {
            "type": "object",
            "properties": {"max_lines": {"type": "integer", "description": "return at most this many of the most recent lines (default 200)"}},
            "required": [],
        },
    },
    {
        "name": "watch_health",
        "description": (
            "State snapshot: uptime, boot count, reset reason, heap/PSRAM free, "
            "battery, wifi, Muse/Hermes link states, config PRESENCE flags only "
            "(never values)."
        ),
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "watch_settings",
        "description": (
            "Read or change the watch's settings live: volume, speaker_on, "
            "mic_gain, brightness, sleep_s, wifi_on, ble_on, avatar (the "
            "watch face: gif, pixel, pixelgif, jolly, ink, knight, bubble, "
            "hires, studio), power_mode (Low Power policy: auto|on|off). "
            "action=all returns every setting as JSON; "
            "get/set take a key (set is range-checked by the device and "
            "persists)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["all", "get", "set"]},
                "key": {"type": "string"},
                "value": {},
            },
            "required": [],
        },
    },
    {
        "name": "watch_sd",
        "description": (
            "Control the watch's SD card. action=status|mount|ls|cat|rm|write. "
            "cat reads a text file (8 KB cap; *.key files are refused — secret "
            "contents never cross the bridge). write stores one small chunk "
            "(<=160 bytes: key/config files). rm is owner-confirmed server-side."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["status", "mount", "ls", "cat", "rm", "write"]},
                "path": {"type": "string"},
                "data": {"type": "string"},
            },
            "required": [],
        },
    },
    {
        "name": "watch_sys",
        "description": (
            "OS-level state. what=info: chip, IDF version, flash size, "
            "firmware, reset reason, boot/panic counters, full partition "
            "table. what=heap: internal/PSRAM/DMA free + largest blocks. "
            "what=tasks: FreeRTOS tasks with state, priority, stack "
            "high-water marks."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"what": {"type": "string", "enum": ["info", "heap", "tasks"]}},
            "required": [],
        },
    },
    {
        "name": "watch_nvs",
        "description": (
            "Inspect and control NVS storage. action=list [ns] shows "
            "namespace/key/type with numeric values; string and blob values "
            "report length only, never contents. erase / erase-ns delete "
            "(the client sends the confirm token; treat as destructive)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["list", "erase", "erase-ns"]},
                "ns": {"type": "string"},
                "key": {"type": "string"},
            },
            "required": [],
        },
    },
    {
        "name": "watch_poweroff",
        "description": "Power the watch fully off (PMIC cut) until the power button or USB returns.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "watch_chirp",
        "description": (
            "Play a gentle speaker test tone. Volume 1–80 (default 35) is "
            "runtime-only and capped by the device; it never changes the "
            "saved volume. The watch makes a sound — only call this when "
            "someone is with the watch and expecting it."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"volume": {"type": "integer", "description": "1–80, default 35"}},
            "required": [],
        },
    },
    {
        "name": "watch_diag",
        "description": (
            "Hardware diagnostics (later protocol-2 firmware). what=regs: "
            "audio codec register dump, one 'dev: rr=vv ...' line per "
            "codec. what=i2cscan: I2C bus scan report. what=pump: run one "
            "UI timer pass under the display lock. what=loopback: "
            "speaker→mic acoustic sweep — LOUD test tones at full volume "
            "for ~40 s, results land in the log (watch_logs); only run it "
            "with the wearer present. regs/i2cscan are board-dependent "
            "and may return ERR unsupported."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"what": {"type": "string", "enum": ["regs", "i2cscan", "pump", "loopback"]}},
            "required": ["what"],
        },
    },
    {
        "name": "watch_ota",
        "description": (
            "Over-the-air firmware update. action=status returns the OTA "
            "state snapshot {state, detail}. action=start begins an "
            "update from a firmware image URL (force re-offers a version "
            "the device already recorded)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["status", "start"]},
                "url": {"type": "string"},
                "force": {"type": "boolean"},
            },
            "required": [],
        },
    },
    {
        "name": "watch_reboot",
        "description": "Reboot the watch. The serial link drops and re-enumerates; the next tool call reconnects.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
]


def _handle(msg: dict) -> dict | None:
    """Handle one JSON-RPC message; return the response dict or None for
    notifications."""
    method = msg.get("method", "")
    msg_id = msg.get("id")
    if msg_id is None:  # notification — no response
        return None
    if method == "initialize":
        result = {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        }
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        params = msg.get("params") or {}
        result = _call_tool(params.get("name", ""), params.get("arguments") or {})
    else:
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "error": {"code": -32601, "message": f"method not found: {method}"},
        }
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def main() -> int:
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            continue  # not ours to answer
        try:
            response = _handle(msg)
        except Exception as exc:  # never let one bad call kill the server
            response = {
                "jsonrpc": "2.0",
                "id": msg.get("id"),
                "error": {"code": -32603, "message": f"internal error: {exc}"},
            }
        if response is not None:
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
