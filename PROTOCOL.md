# MDB Wire Protocol

**Version 0.1-draft** — authored to match the MDB implementation landing in
AIWatchOS (reference firmware). This document is the contract between the
firmware-side MDB dispatcher and every host tool (client library, CLI, MCP
server). If a shipping firmware's `docs/mdb.md` ever disagrees with this
draft, the firmware's bytes win and this spec takes a correcting revision.

Keywords **MUST / MUST NOT / SHOULD** are normative.

---

## 1. Transport

- **v1 transport: USB serial** (USB-CDC on the reference hardware; any
  byte-stream serial works). 115200 8N1 nominal — over USB-CDC the baud rate
  is advisory.
- The MDB command channel shares the console with the firmware's normal log
  output. Unsolicited log lines **may** interleave with MDB traffic at any
  time; hosts **MUST** tolerate them (§3.1).
- The grammar is line-based and transport-shaped: the same commands are
  intended to ride the Muse / Hermes gadget links in later versions with no
  grammar change.

## 2. Framing

- A command is a single ASCII line terminated by `\n`. Devices **MUST**
  also accept `\r\n`. A command line **MUST NOT** exceed 1024 bytes
  (provisioning strings included); longer lines are an `ERR bad_args`.
- Every command line begins with the literal prefix `mdb`, one space, then
  the verb:

  ```
  mdb <verb> [args…]
  ```

- Tokens are separated by single spaces. Arguments never contain spaces,
  except the final argument of `mdb config set`, which is the remainder of
  the line verbatim.
- Coordinates are integers in **panel pixels**, origin at the top-left of
  the panel in its natural orientation, `x ∈ [0, width)`, `y ∈ [0, height)`.
  The reference panel is **410 × 502**.

## 3. Replies

Every command produces exactly one reply. A reply begins with a status line:

```
OK [key=value …]
ERR <code> <human-readable message>
```

`OK` lines may carry space-separated `key=value` metadata pairs. Values
contain no spaces.

### 3.1 Picking replies out of the log stream

A host sends a command, then reads lines until it sees one starting with
`OK ` / `OK` EOL / `ERR `. All other lines are firmware log noise and
**MUST** be skipped (a host may retain them for display). Payload bytes, when
the command has any, follow the status line immediately and are consumed by
count (§4), never by scanning.

### 3.2 Error codes

| code | meaning |
|---|---|
| `unknown_command` | verb not recognised |
| `bad_args` | missing / malformed argument, unknown screen or app id, unknown button |
| `out_of_range` | coordinate outside the panel, duration out of bounds |
| `not_implemented` | command is in the spec but not in this firmware build |
| `busy` | device cannot comply right now (e.g. stream already active) |
| `locked` | MDB is present but disabled by policy/build flag |
| `bad_setup_string` | `config set` payload failed to parse; nothing was applied |
| `internal` | firmware-side failure; message carries detail |

An `ERR` reply has **no payload**. State-changing commands that fail **MUST
NOT** partially apply (in particular `config set` is all-or-nothing).

## 4. Payload shapes

Four payload shapes exist. The status line declares which one follows.

### 4.1 None

Most commands (`tap`, `press`, `goto`, …) end at the status line.

### 4.2 Single-line JSON

Used by `health`, `panic`, `config get`. The status line is a bare `OK`;
the next line is exactly one line of compact JSON (UTF-8, no embedded
newlines). The JSON line is payload, not log noise — hosts read it
unconditionally as the line immediately after `OK`.

### 4.3 Counted lines

Used by `logs`. Status line: `OK lines=<n>`. Exactly `n` lines follow,
verbatim log text (they may themselves start with any characters, including
`OK` — they are consumed by count).

### 4.4 Counted bytes (binary)

Used by `screenshot`. Status line:

```
OK width=<w> height=<h> format=<fmt> bytes=<n>
```

Exactly `n` raw bytes follow immediately. There is no trailing newline
guarantee; hosts **MUST** read by count.

`format` values:

| format | meaning |
|---|---|
| `rgb565le` | 16-bit RGB565 per pixel, row-major from the top-left, low byte first |
| `rgb565be` | same, high byte first |

The dump is the **composited framebuffer** — the pixel data as the UI
composed it, **before** any panel-level byte-swap applied at flush time
(the reference CO5300 panel driver applies its swap internally while
flushing). What a host reconstructs from the dump is therefore what the
software intended to draw; discrepancies against the physical glass are, by
construction, panel/flush-layer faults. Devices whose framebuffer is stored
post-swap **MUST** declare `rgb565be` honestly rather than converting.

A screenshot returns the framebuffer regardless of panel power state (the
panel may be asleep while the framebuffer is fully composed); `health`
reports panel power.

### 4.5 Touch stream

`mdb touch` replies `OK streaming=touch`, then emits one line per touch
event:

```
TOUCH x=<int> y=<int> state=<down|move|up>
```

Coordinates are the **raw controller readings after the firmware's
coordinate transform** (i.e. in panel coordinates — the same space `tap`
uses), so a host can compare a human's physical taps against the transform.
The stream ends when the host sends `mdb touch stop` (device replies
`OK streaming=stopped`) or when the host sends any other command line,
which the device then processes normally. While streaming, the device
**MUST NOT** emit `TOUCH` lines for MDB-injected events without marking
them — injected events are reported with `src=mdb` appended. (v0.1:
firmwares may omit `src` tagging only if injected events are not reported
at all during a stream.)

## 5. Commands

### 5.1 Discovery & state

#### `mdb version`
→ `OK version=<spec> protocol=<n> firmware=<string>`
Reports the protocol version implemented and a firmware build string.
Example: `OK version=0.1 protocol=1 firmware=aiwatchos-bringup3`

#### `mdb health`
→ bare `OK` + single-line JSON (§4.2). Required keys:

| key | type | notes |
|---|---|---|
| `uptime_ms` | int | since boot |
| `boot_count` | int | persistent count of boots |
| `reset_reason` | string | e.g. `poweron`, `panic`, `sw_reset`, `deep_sleep` |
| `heap_free` | int | internal SRAM free, bytes |
| `heap_min_free` | int | internal SRAM low-water mark, bytes |
| `psram_free` | int | PSRAM free, bytes |
| `battery_pct` | int \| null | 0–100, null if unreadable |
| `charging` | bool | |
| `panel_on` | bool | display power state |
| `wifi` | string | `off` \| `connecting` \| `connected` \| `ap` |
| `wifi_rssi` | int \| null | dBm when connected |
| `ip` | string \| null | dotted quad when connected |
| `muse_link` | string | link state name (`down`, `connecting`, `up`, …) |
| `hermes_link` | string | link state name |
| `hermes_via` | string \| null | `lan` \| `remote` \| null |
| `config` | object | key → `true`/`false` **presence only** (e.g. `{"wifi": true, "hatch_token": false}`) |
| `last_weather_age_s` | int \| null | seconds since last successful weather fetch |

Additional keys are permitted; hosts **MUST** ignore unknown keys.

#### `mdb panic`
→ bare `OK` + single-line JSON (§4.2) describing crash retention:
`{"panics": <int>, "last_reason": <string|null>, "last_pc": <string|null>,
"backtrace": [<string>, …] | null, "coredump": <bool>}`.
`panics` is the persisted panic counter. Fields the build doesn't retain
are `null`; `coredump` says whether a coredump partition is present (the
dump itself is read over the bootloader/esptool path, not MDB, in v0.1).

#### `mdb logs`
→ counted lines (§4.3). The most recent lines from the firmware log ring,
oldest first. The ring is finite (reference: ~32 KB in PSRAM); a dump is a
snapshot — lines logged during the dump are not included.

### 5.2 Seeing

#### `mdb screenshot`
→ counted bytes (§4.4), the composited framebuffer.

#### `mdb touch`
→ stream (§4.5). `mdb touch stop` ends it.

### 5.3 Driving

Injected input enters the firmware's normal input path — the same queue
and event shape the physical touch controller and GPIO buttons feed — so
the UI cannot distinguish MDB input from physical input. Semantics match a
physical interaction exactly.

#### `mdb tap <x> <y>`
Press + release at (x, y). → `OK`
Out-of-panel coordinates → `ERR out_of_range`.

#### `mdb swipe <x1> <y1> <x2> <y2> [duration_ms]`
Press at (x1, y1), a sequence of move events along the straight line to
(x2, y2), release. `duration_ms` is optional, default 300, valid range
50–5000; outside → `ERR out_of_range`. → `OK`

#### `mdb press <button>`
Button ∈ {`boot`, `ptt`} on the reference hardware (the side/boot key and
the push-to-talk key). A press+release pair. Unknown name →
`ERR bad_args`. → `OK`

#### `mdb goto <screen>`
Navigate directly to a firmware screen by id, through the shell's normal
navigation (not simulated touches). Screen ids are firmware-defined;
reference ids: `clock`, `launcher`, `settings`, `chat`, `weather`,
`notifications`. Unknown id → `ERR bad_args`. → `OK screen=<id>`

#### `mdb open <app>`
Open an app by id through the shell's app launcher. Reference ids:
`muse`, `hermes`, `weather`, `settings`. Unknown id → `ERR bad_args`.
→ `OK app=<id>`

#### `mdb wake` / `mdb sleep`
Force the display power state (the same states the firmware's idle
power machine manages). → `OK panel=on` / `OK panel=off`

#### `mdb reboot`
Reboot the device. The reply `OK rebooting` is sent before reset; the
serial link drops and re-enumerates. Hosts **MUST** treat link loss after
this reply as success.

### 5.4 Config & provisioning

#### `mdb config get`
→ bare `OK` + single-line JSON (§4.2):

```json
{"wifi_ssid": {"present": true, "len": 10},
 "wifi_password": {"present": true, "len": 12},
 "hatch_token": {"present": false, "len": 0}}
```

**Redaction is absolute:** for every key, the reply contains only
`present` (bool) and `len` (int, length in characters/bytes of the stored
value, 0 when absent). Values **MUST NOT** appear — not in this reply, not
in `health` (presence booleans only), not in logs, not in error messages.
See §6.

#### `mdb config set <setup-string>`
The remainder of the line is a provisioning setup string in the firmware's
provisioning format (reference firmware: the AIWatchOS setup string parsed
by its net provisioning layer — `key=value` fields, e.g.
`aiwatchos://setup?ssid=…&pw=…` URLs are unwrapped by that parser).
The device parses and applies it atomically: on success
`OK applied=<n>` (n = fields applied); on any parse failure
`ERR bad_setup_string …` and **nothing** is applied. The device **MUST
NOT** echo the string or any of its values back, including in logs.
This is the sanctioned provisioning route: it replaces reflashing to
change Wi-Fi credentials or tokens.

## 6. Redaction rules (normative)

1. Config **values** cross the wire in exactly one direction: host → device,
   inside `mdb config set`. No MDB reply, of any command, may contain a
   config value or any substring of one.
2. `health` exposes presence as booleans only — not even lengths.
3. Firmware logging **MUST NOT** log config values, setup strings, tokens,
   or passwords, at any log level, MDB or not.
4. Host tools **MUST NOT** log the argument of `config set`. CLI history
   and shell transcripts are the host operator's responsibility; the
   reference CLI prints a warning before a `config set`.

## 7. Availability & gating

- MDB is debug tooling. In the reference firmware it is compiled in only
  under a debug Kconfig flag and is expected to be **absent or locked** in
  release builds. If the flag is off, `mdb …` lines are simply not
  recognised (hosts observe a timeout). If a build answers but policy
  disables commands, it replies `ERR locked …`.
- MDB **SHOULD** require the display-awake state for `goto`/`open` only
  insofar as navigation itself does; `screenshot`, `health`, `logs` work
  with the panel asleep.
- One host at a time: the serial stream is a single conversation. A second
  command sent while a counted payload is in flight corrupts framing —
  hosts **MUST** serialize commands (the reference client does).

## 8. Transport roadmap

- **v1 (this spec):** USB serial.
- **Later:** the identical grammar over the gadget links (Muse chat link /
  Hermes gadget channel as transports), and possibly BLE. Framing is
  unchanged; a future revision will define how replies are packetised on
  message-oriented transports and how (or whether) MDB is authenticated
  there. **On any non-cable transport, redaction (§6) becomes even more
  load-bearing, and `config set` requires an explicit firmware policy
  decision before it is enabled.**

## 9. Worked example

```
host →  mdb health
dev  →  I (4211) aiw-net: sta ip changed            ← log noise, skipped
dev  →  OK
dev  →  {"uptime_ms": 61234, "boot_count": 3, "reset_reason": "panic", …}

host →  mdb screenshot
dev  →  OK width=410 height=502 format=rgb565le bytes=411640
dev  →  <411640 raw bytes>

host →  mdb tap 999 10
dev  →  ERR out_of_range x=999 outside panel width 410

host →  mdb config get
dev  →  OK
dev  →  {"wifi_ssid": {"present": true, "len": 10}, "hatch_token": {"present": false, "len": 0}}
```
