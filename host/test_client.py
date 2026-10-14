"""Unit tests for mdb_client, driven by a scripted fake serial port.

No hardware, no pyserial needed: the client takes an injected connection
object. Run from this directory:

    python3 -m unittest test_client -v
    # or simply:  python3 test_client.py
"""

import io
import json
import sys
import unittest

sys.path.insert(0, __file__.rsplit("/", 1)[0] or ".")

from mdb_client import AVATAR_NAMES, MdbClient, MdbError, Screenshot, TouchEvent  # noqa: E402


class FakeSerial:
    """A pyserial-shaped stub.

    ``script`` maps an exact outgoing command line (without the trailing
    newline) to the raw bytes the "device" emits in response. Anything
    unscripted gets silence (so timeouts can be exercised with tiny
    client timeouts).
    """

    def __init__(self, script=None):
        self.script = dict(script or {})
        self._rx = bytearray()
        self.written = []  # list of decoded command lines, for assertions
        self.closed = False

    # pyserial-shaped API used by MdbClient
    def write(self, data: bytes) -> int:
        line = data.decode("ascii").rstrip("\r\n")
        self.written.append(line)
        response = self.script.get(line)
        if response:
            self._rx.extend(response)
        return len(data)

    def readline(self) -> bytes:
        idx = self._rx.find(b"\n")
        if idx == -1:
            if not self._rx:
                return b""
            out = bytes(self._rx)
            self._rx.clear()
            return out
        out = bytes(self._rx[: idx + 1])
        del self._rx[: idx + 1]
        return out

    def read(self, n: int) -> bytes:
        out = bytes(self._rx[:n])
        del self._rx[: len(out)]
        return out

    def close(self):
        self.closed = True


def make_client(script, timeout=1.0):
    fake = FakeSerial(script)
    return MdbClient(connection=fake, timeout=timeout), fake


HEALTH = {
    "uptime_ms": 61234,
    "boot_count": 3,
    "reset_reason": "poweron",
    "heap_free": 81000,
    "heap_min_free": 77000,
    "psram_free": 6100000,
    "battery_pct": 87,
    "charging": False,
    "panel_on": True,
    "wifi": "connected",
    "wifi_rssi": -55,
    "ip": "192.0.2.44",
    "muse_link": "up",
    "hermes_link": "up",
    "hermes_via": "lan",
    "config": {"wifi": True, "hatch_token": False},
    "last_weather_age_s": 240,
}


class TestFraming(unittest.TestCase):
    def test_health_skips_log_noise_and_parses_json(self):
        payload = (
            b"I (4211) aiw-net: sta ip changed\r\n"  # noise before the reply
            b"OK\r\n" + json.dumps(HEALTH).encode() + b"\r\n"
        )
        client, fake = make_client({"mdb health": payload})
        health = client.health()
        self.assertEqual(health["battery_pct"], 87)
        self.assertEqual(health["ip"], "192.0.2.44")
        self.assertEqual(fake.written, ["mdb health"])
        self.assertEqual(client.drain_noise(), ["I (4211) aiw-net: sta ip changed"])

    def test_err_reply_raises_with_code(self):
        client, _ = make_client({"mdb tap 999 10": b"ERR out_of_range x=999 outside panel width 410\r\n"})
        with self.assertRaises(MdbError) as ctx:
            client.tap(999, 10)
        self.assertEqual(ctx.exception.code, "out_of_range")

    def test_simple_commands_send_exact_lines(self):
        script = {
            "mdb tap 10 20": b"OK\r\n",
            "mdb swipe 1 2 3 4": b"OK\r\n",
            "mdb swipe 1 2 3 4 500": b"OK\r\n",
            "mdb press boot": b"OK\r\n",
            "mdb goto settings": b"OK screen=settings\r\n",
            "mdb open muse": b"OK app=muse\r\n",
            "mdb wake": b"OK panel=on\r\n",
            "mdb sleep": b"OK panel=off\r\n",
            "mdb reboot": b"OK rebooting\r\n",
            "mdb version": b"OK version=0.1 protocol=1 firmware=test-fw\r\n",
        }
        client, fake = make_client(script)
        client.tap(10, 20)
        client.swipe(1, 2, 3, 4)
        client.swipe(1, 2, 3, 4, 500)
        client.press("boot")
        self.assertEqual(client.goto("settings")["screen"], "settings")
        self.assertEqual(client.open_app("muse")["app"], "muse")
        client.wake()
        client.sleep()
        client.reboot()
        self.assertEqual(client.version()["firmware"], "test-fw")
        self.assertEqual(
            fake.written,
            [
                "mdb tap 10 20",
                "mdb swipe 1 2 3 4",
                "mdb swipe 1 2 3 4 500",
                "mdb press boot",
                "mdb goto settings",
                "mdb open muse",
                "mdb wake",
                "mdb sleep",
                "mdb reboot",
                "mdb version",
            ],
        )

    def test_client_side_validation(self):
        client, fake = make_client({})
        with self.assertRaises(MdbError):
            client.tap(-1, 5)
        with self.assertRaises(MdbError):
            client.press("volume")
        with self.assertRaises(MdbError):
            client.swipe(0, 0, 1, 1, 10)  # below 50 ms floor
        self.assertEqual(fake.written, [])  # nothing reached the device

    def test_logs_counted_lines(self):
        script = {
            "mdb logs": b"OK lines=3\r\n"
            b"W (100) wifi: reconnecting\r\n"
            b"OK this line is log text, not a status\r\n"
            b"I (200) app: ready\r\n"
        }
        client, _ = make_client(script)
        lines = client.logs()
        self.assertEqual(len(lines), 3)
        self.assertEqual(lines[1], "OK this line is log text, not a status")

    def test_config_get_and_set(self):
        redacted = {
            "wifi_ssid": {"present": True, "len": 10},
            "wifi_password": {"present": True, "len": 12},
            "hatch_token": {"present": False, "len": 0},
        }
        script = {
            "mdb config get": b"OK\r\n" + json.dumps(redacted).encode() + b"\r\n",
            "mdb config set ssid=ExampleNet&pw=fake-password": b"OK applied=2\r\n",
        }
        client, _ = make_client(script)
        got = client.config_get()
        self.assertEqual(got["wifi_ssid"], {"present": True, "len": 10})
        self.assertFalse(got["hatch_token"]["present"])
        self.assertEqual(client.config_set("ssid=ExampleNet&pw=fake-password"), 2)


def _fb_2x2(byteorder: str) -> bytes:
    """2x2 framebuffer: red, green / blue, white."""
    values = [0xF800, 0x07E0, 0x001F, 0xFFFF]
    out = bytearray()
    for v in values:
        if byteorder == "le":
            out += bytes((v & 0xFF, v >> 8))
        else:
            out += bytes((v >> 8, v & 0xFF))
    return bytes(out)


class TestScreenshot(unittest.TestCase):
    def _client_for(self, fmt: str) -> MdbClient:
        payload = _fb_2x2("le" if fmt == "rgb565le" else "be")
        header = f"OK width=2 height=2 format={fmt} bytes={len(payload)}\r\n".encode()
        client, _ = make_client({"mdb screenshot": header + payload})
        return client

    def test_header_and_payload(self):
        shot = self._client_for("rgb565le").screenshot()
        self.assertIsInstance(shot, Screenshot)
        self.assertEqual((shot.width, shot.height, shot.format), (2, 2, "rgb565le"))
        self.assertEqual(shot.data, _fb_2x2("le"))

    def test_png_conversion_le_and_be(self):
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Pillow not installed")
        expected = [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 255)]
        for fmt in ("rgb565le", "rgb565be"):
            with self.subTest(fmt=fmt):
                shot = self._client_for(fmt).screenshot()
                img = Image.open(io.BytesIO(shot.to_png_bytes()))
                self.assertEqual(img.size, (2, 2))
                self.assertEqual(list(img.convert("RGB").getdata()), expected)


class TestTouchStream(unittest.TestCase):
    def test_touch_event_parsing(self):
        ev = TouchEvent.from_line("TOUCH x=100 y=200 state=down")
        self.assertEqual((ev.x, ev.y, ev.state, ev.src), (100, 200, "down", None))
        ev = TouchEvent.from_line("TOUCH x=1 y=2 state=up src=mdb")
        self.assertEqual(ev.src, "mdb")
        self.assertIsNone(TouchEvent.from_line("I (1) some log line"))

    def test_touch_stream_and_stop(self):
        script = {
            "mdb touch": b"OK streaming=touch\r\n"
            b"TOUCH x=10 y=20 state=down\r\n"
            b"TOUCH x=12 y=24 state=move\r\n"
            b"TOUCH x=12 y=24 state=up\r\n",
            "mdb touch stop": b"OK streaming=stopped\r\n",
        }
        client, fake = make_client(script)
        events = list(client.touch_events(max_events=3))
        self.assertEqual([e.state for e in events], ["down", "move", "up"])
        self.assertIn("mdb touch stop", fake.written)


class TestProtocol2Extensions(unittest.TestCase):
    """Avatar setting + the diagnostic/OTA verbs of later protocol-2
    firmware (MuseOS 1.7.23+)."""

    def test_avatar_get_and_set(self):
        script = {
            "mdb settings get avatar": b"OK avatar=jolly\r\n",
            "mdb settings set avatar studio": b"OK avatar=studio\r\n",
        }
        client, fake = make_client(script)
        self.assertEqual(client.settings_get("avatar"), {"avatar": "jolly"})
        self.assertEqual(client.set_avatar("studio"), {"avatar": "studio"})
        self.assertEqual(
            fake.written,
            ["mdb settings get avatar", "mdb settings set avatar studio"],
        )
        for name in AVATAR_NAMES:  # every advertised face is settable
            c2, f2 = make_client({f"mdb settings set avatar {name}": b"OK avatar=" + name.encode() + b"\r\n"})
            self.assertEqual(c2.set_avatar(name), {"avatar": name})

    def test_set_avatar_validates_locally(self):
        client, fake = make_client({})
        with self.assertRaises(MdbError):
            client.set_avatar("bogus")
        self.assertEqual(fake.written, [])  # nothing reached the device

    def test_chirp_default_explicit_and_range(self):
        script = {
            "mdb chirp 35": b"OK chirp vol=35\r\n",
            "mdb chirp 10": b"OK chirp vol=10\r\n",
        }
        client, fake = make_client(script)
        self.assertEqual(client.chirp(), {"vol": 35})
        self.assertEqual(client.chirp(10), {"vol": 10})
        self.assertEqual(fake.written, ["mdb chirp 35", "mdb chirp 10"])
        for bad in (0, 81):
            with self.assertRaises(MdbError):
                client.chirp(bad)
        self.assertEqual(len(fake.written), 2)  # range errors stay local

    def test_pump_i2cscan_loopback(self):
        script = {
            "mdb pump": b"OK pumped next=12 ticks=345\r\n",
            "mdb i2cscan": b"OK found 0x18 0x38 0x40\r\n",
            "mdb loopback": b"OK loopback requested, results in log\r\n",
        }
        client, fake = make_client(script)
        self.assertEqual(client.pump(), {"next": 12, "ticks": 345})
        self.assertEqual(client.i2cscan(), "found 0x18 0x38 0x40")
        self.assertEqual(client.loopback(), {})
        self.assertEqual(fake.written, ["mdb pump", "mdb i2cscan", "mdb loopback"])

    def test_regs_two_codec_lines(self):
        script = {
            "mdb regs": b"OK es8311: 00=3c 01=00 44=ff\r\n"
            b"es7210: 00=11 01=XX\r\n"
        }
        client, fake = make_client(script)
        report = client.regs()
        self.assertEqual(
            report,
            "es8311: 00=3c 01=00 44=ff\nes7210: 00=11 01=XX",
        )
        self.assertEqual(fake.written, ["mdb regs"])

    def test_ota_status_and_start(self):
        script = {
            "mdb ota status": b"OK state=idle detail=none\r\n",
            "mdb ota start https://example.invalid/fw.bin": b"OK ota=starting\r\n",
            "mdb ota start https://example.invalid/fw.bin force": b"OK ota=starting\r\n",
        }
        client, fake = make_client(script)
        self.assertEqual(client.ota_status(), {"state": "idle", "detail": "none"})
        self.assertEqual(client.ota_start("https://example.invalid/fw.bin"), {"ota": "starting"})
        self.assertEqual(
            client.ota_start("https://example.invalid/fw.bin", force=True),
            {"ota": "starting"},
        )
        with self.assertRaises(MdbError):
            client.ota_start("two words")
        self.assertEqual(
            fake.written,
            [
                "mdb ota status",
                "mdb ota start https://example.invalid/fw.bin",
                "mdb ota start https://example.invalid/fw.bin force",
            ],
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
