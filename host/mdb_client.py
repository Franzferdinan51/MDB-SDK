"""MDB host client — talk to a Muse Developer Bridge device over serial.

Implements the wire protocol in ../PROTOCOL.md (v0.1-draft):

  * line-based commands, ``mdb `` prefix, replies scanned out of the
    firmware's log stream by looking for lines starting with ``OK``/``ERR``
  * payload shapes: bare status, single-line JSON, counted lines,
    counted bytes (screenshot), touch event stream
  * config redaction respected host-side too: this library never asks for,
    and the protocol never returns, config *values* — only presence/length.

Only the standard library is used at import time. ``pyserial`` is imported
lazily when a real connection is opened, and ``Pillow`` lazily when a
screenshot is converted to an image — so the module (and its unit tests,
which inject a fake serial object) works with neither installed.

Example:
    >>> with MdbClient("/dev/cu.usbmodemXXXX") as watch:   # doctest: +SKIP
    ...     print(watch.health()["battery_pct"])
    ...     watch.tap(205, 251)
    ...     watch.screenshot().save_png("/tmp/watch.png")
"""

from __future__ import annotations

import base64
import io
import json
import re
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterator, List, Optional, Tuple

__all__ = [
    "MdbClient",
    "MdbError",
    "MdbTimeoutError",
    "Screenshot",
    "TouchEvent",
    "rgb565_to_image",
    "DEFAULT_BAUD",
]

DEFAULT_BAUD = 115200
MAX_COMMAND_BYTES = 1024

_KV_RE = re.compile(r"(\w+)=([^\s]+)")
_TOUCH_RE = re.compile(
    r"^TOUCH x=(-?\d+) y=(-?\d+) state=(down|move|up)(?:\s+src=(\w+))?\s*$"
)


class MdbError(Exception):
    """The device replied ``ERR <code> <message>`` (or a local check failed)."""

    def __init__(self, code: str, message: str = ""):
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}" if message else code)


class MdbTimeoutError(MdbError):
    """No reply arrived before the deadline."""

    def __init__(self, message: str = "no reply from device"):
        super().__init__("timeout", message)


def _coerce(value: str):
    """Meta values arrive as strings; turn obvious ints into ints."""
    try:
        return int(value)
    except ValueError:
        return value


def _parse_meta(text: str) -> Dict[str, object]:
    return {k: _coerce(v) for k, v in _KV_RE.findall(text)}


@dataclass
class TouchEvent:
    x: int
    y: int
    state: str  # "down" | "move" | "up"
    src: Optional[str] = None  # "mdb" for injected events, when reported

    @classmethod
    def from_line(cls, line: str) -> Optional["TouchEvent"]:
        m = _TOUCH_RE.match(line.strip())
        if not m:
            return None
        return cls(int(m.group(1)), int(m.group(2)), m.group(3), m.group(4))


@dataclass
class Screenshot:
    """A composited framebuffer dump (PROTOCOL.md §4.4)."""

    width: int
    height: int
    format: str  # "rgb565le" | "rgb565be"
    data: bytes
    meta: Dict[str, object] = field(default_factory=dict)

    def to_image(self):
        """Convert to a PIL Image (imports Pillow lazily)."""
        return rgb565_to_image(self.data, self.width, self.height, self.format)

    def to_png_bytes(self) -> bytes:
        buf = io.BytesIO()
        self.to_image().save(buf, format="PNG")
        return buf.getvalue()

    def save_png(self, path: str) -> str:
        with open(path, "wb") as fh:
            fh.write(self.to_png_bytes())
        return path


def rgb565_to_image(data: bytes, width: int, height: int, fmt: str = "rgb565le"):
    """Convert raw RGB565 pixels (row-major, top-left origin) to a PIL Image.

    ``fmt`` is ``rgb565le`` (low byte first — the framebuffer as composed,
    before any panel flush byte-swap) or ``rgb565be`` (high byte first).
    5/6-bit channels are expanded to 8-bit by bit replication, the standard
    lossless-ish mapping (0x1F -> 255, 0x3F -> 255).
    """
    from PIL import Image  # lazy: tests of pure framing need no Pillow

    expected = width * height * 2
    if len(data) < expected:
        raise ValueError(f"framebuffer too short: {len(data)} < {expected} bytes")
    big_endian = fmt.endswith("be")
    pixels = []
    append = pixels.append
    for i in range(0, expected, 2):
        if big_endian:
            v = (data[i] << 8) | data[i + 1]
        else:
            v = data[i] | (data[i + 1] << 8)
        r5 = (v >> 11) & 0x1F
        g6 = (v >> 5) & 0x3F
        b5 = v & 0x1F
        append(((r5 << 3) | (r5 >> 2), (g6 << 2) | (g6 >> 4), (b5 << 3) | (b5 >> 2)))
    img = Image.new("RGB", (width, height))
    img.putdata(pixels)
    return img


class MdbClient:
    """A single MDB conversation over one serial connection.

    The serial stream is a single conversation: commands are strictly
    serialized by this client (no internal threading). Pass ``connection``
    to inject an already-open pyserial-like object (tests do this); pass
    ``port`` to have the client open it (pyserial imported lazily).
    """

    def __init__(
        self,
        port: Optional[str] = None,
        baud: int = DEFAULT_BAUD,
        timeout: float = 5.0,
        connection=None,
        serial_factory: Optional[Callable[..., object]] = None,
    ):
        self.port = port
        self.baud = baud
        self.timeout = timeout
        self._conn = connection
        self._serial_factory = serial_factory
        self._noise: List[str] = []  # log lines skipped while awaiting replies

    # ------------------------------------------------------------------
    # connection handling
    # ------------------------------------------------------------------
    @staticmethod
    def list_ports() -> List[Dict[str, str]]:
        """Enumerate serial ports (pyserial). Returns dicts with device,
        description, hwid — handy for finding the watch's USB-CDC port."""
        from serial.tools import list_ports  # lazy import

        return [
            {
                "device": p.device,
                "description": p.description or "",
                "hwid": p.hwid or "",
            }
            for p in list_ports.comports()
        ]

    def connect(self) -> "MdbClient":
        if self._conn is not None:
            return self
        if self.port is None:
            raise MdbError("bad_args", "no port given and no connection injected")
        if self._serial_factory is not None:
            self._conn = self._serial_factory(self.port, self.baud, self.timeout)
        else:
            import serial  # lazy import (pyserial)

            self._conn = serial.Serial(self.port, self.baud, timeout=self.timeout)
            self._settle_after_open()
        return self

    def _settle_after_open(self) -> None:
        '''Opening the port resets the reference hardware (USB-serial-
        JTAG): bytes written during its boot are lost, so a command sent
        immediately after connect() falls into the void and times out.
        Drain the boot log until the firmware banner appears or the
        stream goes quiet (1.5 s), bounded at 10 s, then restore the
        caller's timeout. Best-effort: any failure just proceeds.'''
        conn = self._conn
        try:
            conn.timeout = 0.25
            deadline = time.monotonic() + 10.0
            quiet_since: Optional[float] = None
            while time.monotonic() < deadline:
                raw = conn.readline()
                now = time.monotonic()
                if raw:
                    quiet_since = None
                    line = raw.decode("ascii", errors="replace").rstrip("\r\n")
                    if line:
                        self._noise.append(line)
                    if "MDB ready" in line:
                        break
                else:
                    if quiet_since is None:
                        quiet_since = now
                    elif now - quiet_since >= 1.5:
                        break
        except Exception:
            pass
        finally:
            try:
                conn.timeout = self.timeout
            except Exception:
                pass

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None

    @property
    def connected(self) -> bool:
        return self._conn is not None

    def __enter__(self) -> "MdbClient":
        return self.connect()

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------------
    # low-level framing
    # ------------------------------------------------------------------
    def _require_conn(self):
        if self._conn is None:
            self.connect()
        return self._conn

    def _write_line(self, line: str) -> None:
        raw = line.encode("ascii", errors="strict")
        if len(raw) + 1 > MAX_COMMAND_BYTES:
            raise MdbError("bad_args", f"command exceeds {MAX_COMMAND_BYTES} bytes")
        self._require_conn().write(raw + b"\n")

    def _read_line(self, deadline: float) -> str:
        """Read one line; raise MdbTimeoutError once past ``deadline``."""
        conn = self._require_conn()
        while True:
            # Check the deadline BEFORE reading, not only on empty reads:
            # a chatty device (boot logs, heartbeats) keeps readline()
            # returning lines forever, so the old empty-only check let a
            # lost reply hang the caller indefinitely.
            if time.monotonic() > deadline:
                raise MdbTimeoutError()
            raw = conn.readline()
            if raw:
                return raw.decode("ascii", errors="replace").rstrip("\r\n")
            # pyserial returns b"" on its own short timeout; loop until ours.

    def _await_status(self, timeout: Optional[float] = None) -> Tuple[bool, Dict[str, object]]:
        """Read lines until an OK/ERR status line arrives.

        Non-status lines are firmware log noise (§3.1): they are collected
        into ``self.noise`` and skipped. Returns (ok, meta). Raises
        MdbError on an ERR reply, MdbTimeoutError on timeout.
        """
        deadline = time.monotonic() + (self.timeout if timeout is None else timeout)
        while True:
            line = self._read_line(deadline)
            if line == "OK" or line.startswith("OK "):
                return True, _parse_meta(line[2:].strip())
            if line == "ERR" or line.startswith("ERR "):
                parts = line[3:].strip().split(None, 1)
                code = parts[0] if parts else "internal"
                msg = parts[1] if len(parts) > 1 else ""
                raise MdbError(code, msg)
            self._noise.append(line)

    def _execute(self, verb: str, *args: object) -> Dict[str, object]:
        """Send a payload-less command; return its OK meta dict."""
        line = "mdb " + verb + "".join(f" {a}" for a in args)
        self._write_line(line)
        _, meta = self._await_status()
        return meta

    def _json_command(self, tail: str) -> dict:
        """Send ``mdb <tail>`` whose payload is one JSON line after a bare OK."""
        self._write_line(f"mdb {tail}")
        deadline = time.monotonic() + self.timeout
        self._await_status()
        line = self._read_line(deadline)
        try:
            return json.loads(line)
        except json.JSONDecodeError as exc:
            raise MdbError("internal", f"bad JSON payload from device: {exc}") from exc

    def drain_noise(self) -> List[str]:
        """Return (and clear) log lines skipped while awaiting replies."""
        noise, self._noise = self._noise, []
        return noise

    # ------------------------------------------------------------------
    # discovery & state
    # ------------------------------------------------------------------
    def version(self) -> Dict[str, object]:
        return self._execute("version")

    def health(self) -> dict:
        return self._json_command("health")

    def panic_info(self) -> dict:
        return self._json_command("panic")

    def logs(self) -> List[str]:
        """Dump the firmware log ring (oldest first)."""
        self._write_line("mdb logs")
        deadline = time.monotonic() + self.timeout
        _, meta = self._await_status()
        count = int(meta.get("lines", 0))
        return [self._read_line(deadline) for _ in range(count)]

    # ------------------------------------------------------------------
    # seeing
    # ------------------------------------------------------------------
    def _read_exact(self, n: int, deadline: float) -> bytes:
        conn = self._require_conn()
        buf = bytearray()
        while len(buf) < n:
            # Same deadline discipline as _read_line: interleaved log
            # noise keeps reads non-empty, so check the clock every pass.
            if time.monotonic() > deadline:
                raise MdbTimeoutError(f"screenshot payload stalled at {len(buf)}/{n} bytes")
            chunk = conn.read(n - len(buf))
            if chunk:
                buf.extend(chunk)
        return bytes(buf)

    def screenshot(self) -> Screenshot:
        """Dump the composited framebuffer. See PROTOCOL.md §4.4: the dump
        is pre-panel-swap — what the software composed, not panel bytes."""
        self._write_line("mdb screenshot")
        _, meta = self._await_status(timeout=max(self.timeout, 15.0))
        try:
            width = int(meta["width"])
            height = int(meta["height"])
            nbytes = int(meta["bytes"])
        except (KeyError, ValueError) as exc:
            raise MdbError("internal", f"malformed screenshot header: {meta}") from exc
        deadline = time.monotonic() + max(self.timeout, 15.0)
        data = self._read_exact(nbytes, deadline)
        return Screenshot(width, height, str(meta.get("format", "rgb565le")), data, meta)

    def touch_events(
        self, max_events: Optional[int] = None, timeout: Optional[float] = None
    ) -> Iterator[TouchEvent]:
        """Stream raw touch events (transformed panel coordinates).

        This is a generator: the stream starts on first iteration and the
        device keeps streaming until ``stop_touch()`` is called, the
        generator is closed, or another command is sent (which per spec
        ends the stream). Yields TouchEvent objects; silently skips
        non-TOUCH lines (log noise).
        """
        self._write_line("mdb touch")
        deadline = time.monotonic() + (self.timeout if timeout is None else timeout)
        self._await_status()
        sent = 0
        try:
            while max_events is None or sent < max_events:
                if time.monotonic() > deadline:
                    break
                try:
                    line = self._read_line(deadline)
                except MdbTimeoutError:
                    break
                ev = TouchEvent.from_line(line)
                if ev is not None:
                    sent += 1
                    yield ev
        finally:
            # Best-effort: leave the device out of streaming mode.
            try:
                self.stop_touch()
            except Exception:
                pass

    def stop_touch(self) -> Dict[str, object]:
        return self._execute("touch", "stop")

    # ------------------------------------------------------------------
    # driving
    # ------------------------------------------------------------------
    @staticmethod
    def _check_coord(name: str, value: int) -> int:
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise MdbError("bad_args", f"{name} must be a non-negative int, got {value!r}")
        return value

    def tap(self, x: int, y: int) -> Dict[str, object]:
        self._check_coord("x", x)
        self._check_coord("y", y)
        return self._execute("tap", x, y)

    def swipe(
        self, x1: int, y1: int, x2: int, y2: int, duration_ms: Optional[int] = None
    ) -> Dict[str, object]:
        for name, v in (("x1", x1), ("y1", y1), ("x2", x2), ("y2", y2)):
            self._check_coord(name, v)
        args: List[object] = [x1, y1, x2, y2]
        if duration_ms is not None:
            if not isinstance(duration_ms, int) or not (50 <= duration_ms <= 5000):
                raise MdbError("out_of_range", "duration_ms must be an int in 50..5000")
            args.append(duration_ms)
        return self._execute("swipe", *args)

    def press(self, button: str) -> Dict[str, object]:
        if button not in ("boot", "ptt"):
            raise MdbError("bad_args", f"unknown button {button!r} (expected boot|ptt)")
        return self._execute("press", button)

    def goto(self, screen: str) -> Dict[str, object]:
        return self._execute("goto", screen)

    def open_app(self, app: str) -> Dict[str, object]:
        return self._execute("open", app)

    def wake(self) -> Dict[str, object]:
        return self._execute("wake")

    def sleep(self) -> Dict[str, object]:
        return self._execute("sleep")

    def reboot(self) -> Dict[str, object]:
        """Reboot the watch. Per spec the device replies before resetting;
        the serial link then drops — callers should expect to reconnect."""
        return self._execute("reboot")

    # ------------------------------------------------------------------
    # config & provisioning (values never come back — PROTOCOL.md §6)
    # ------------------------------------------------------------------
    def config_get(self) -> dict:
        """Presence + length per config key. NEVER values."""
        return self._json_command("config get")

    def config_set(self, setup_string: str) -> int:
        """Apply a provisioning setup string atomically. Returns the
        number of fields applied. The string is sent verbatim as the
        remainder of the command line; it is never logged by this client
        and (per spec) never echoed by the device."""
        if "\n" in setup_string or "\r" in setup_string:
            raise MdbError("bad_args", "setup string must be a single line")
        self._write_line(f"mdb config set {setup_string}")
        _, meta = self._await_status()
        return int(meta.get("applied", 0))

    # ------------------------------------------------------------------
    # protocol 2: settings, SD card, OS state, NVS — full device control
    # ------------------------------------------------------------------
    def _lines_until_end(self) -> List[str]:
        """Collect payload lines up to the firmware's END terminator."""
        deadline = time.monotonic() + self.timeout
        lines: List[str] = []
        while True:
            line = self._read_line(deadline)
            if line.startswith("END "):
                return lines
            lines.append(line)

    def settings_all(self) -> dict:
        """Every watch setting as JSON (values are state, not secrets)."""
        return self._json_command("settings")

    def settings_get(self, key: str) -> Dict[str, object]:
        return self._execute("settings", "get", key)

    def settings_set(self, key: str, value: object) -> Dict[str, object]:
        """Set one setting live (volume, brightness, mic_gain, sleep_s,
        speaker_on, wifi_on, ble_on). The device range-checks."""
        return self._execute("settings", "set", key, value)

    def sd_status(self) -> dict:
        return self._json_command("sd status")

    def sd_mount(self) -> Dict[str, object]:
        return self._execute("sd", "mount")

    def sd_ls(self, path: str = "/") -> List[str]:
        """Directory listing lines: 'D name' / 'F name size'."""
        self._write_line(f"mdb sd ls {path}")
        self._await_status()
        return self._lines_until_end()

    def sd_cat(self, path: str) -> str:
        """Read a text file (device caps at 8 KB and refuses *.key)."""
        self._write_line(f"mdb sd cat {path}")
        self._await_status()
        return "\n".join(self._lines_until_end())

    def sd_write(self, path: str, data: bytes) -> Dict[str, object]:
        """Whole-file write of one small chunk (<=160 bytes decoded):
        key/config-sized files. Larger files are out of scope for MDB."""
        if len(data) > 160:
            raise MdbError("bad_args", "sd write is one chunk (<=160 bytes)")
        return self._execute("sd", "write", path,
                             base64.b64encode(data).decode("ascii"))

    def sd_rm(self, path: str) -> Dict[str, object]:
        return self._execute("sd", "rm", path, "confirm")

    def sys_info(self) -> dict:
        """Chip/IDF/flash/firmware + the full partition table."""
        info = self._json_command("sys info")
        deadline = time.monotonic() + self.timeout
        while True:
            line = self._read_line(deadline)
            if line.startswith("{"):
                info["partitions"] = json.loads(line).get("partitions", [])
                return info

    def sys_heap(self) -> dict:
        return self._json_command("sys heap")

    def sys_tasks(self) -> List[str]:
        """FreeRTOS task lines: 'name state prio= stack_hwm='."""
        self._write_line("mdb sys tasks")
        self._await_status()
        return self._lines_until_end()

    def nvs_list(self, namespace: Optional[str] = None) -> List[str]:
        """NVS entries: 'ns key type [value|len=N]'. String/blob VALUES
        never cross — length only (PROTOCOL.md redaction rules)."""
        self._write_line("mdb nvs list" + (f" {namespace}" if namespace else ""))
        self._await_status()
        return self._lines_until_end()

    def nvs_erase(self, namespace: str, key: str) -> Dict[str, object]:
        return self._execute("nvs", "erase", namespace, key, "confirm")

    def nvs_erase_ns(self, namespace: str) -> Dict[str, object]:
        return self._execute("nvs", "erase-ns", namespace, "confirm")

    def set_log_level(self, tag: str, level: str) -> Dict[str, object]:
        """Runtime log control: tag ('*' = all), level none|error|warn|
        info|debug|verbose."""
        return self._execute("loglevel", tag, level)

    def poweroff(self) -> Dict[str, object]:
        """Power the watch off (PMIC cut). It stays off until the power
        button or USB power returns."""
        return self._execute("poweroff")

    # ------------------------------------------------------------------
    # escape hatch for the interactive shell / future verbs
    # ------------------------------------------------------------------
    def raw(self, line: str) -> Dict[str, object]:
        """Send a full command line verbatim (must start with ``mdb ``);
        return the OK meta. Only for payload-less commands — payload
        commands (screenshot/logs/health/…) have dedicated methods."""
        if not line.startswith("mdb "):
            raise MdbError("bad_args", "raw lines must start with 'mdb '")
        self._write_line(line)
        _, meta = self._await_status()
        return meta
