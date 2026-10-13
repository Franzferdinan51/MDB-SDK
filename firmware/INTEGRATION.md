# MDB Firmware Integration Guide

How to put a Muse Developer Bridge dispatcher in a watch firmware so the
host tools in `../host/` can see it, drive it, and provision it. Written
against the reference implementation landing in
[AIWatchOS](https://github.com/Franzferdinan51/AIWatchOS) (ESP32-S3), but
the pattern is portable to any RTOS firmware with a console stream and an
input queue. The wire contract itself lives in [`../PROTOCOL.md`](../PROTOCOL.md);
this guide is about the device side.

## 1. Shape of the thing

MDB in firmware is three pieces:

1. **A line reader** attached to the debug console stream (USB-CDC on the
   reference board). It peeks at incoming lines: lines not starting with
   `mdb ` belong to whatever else uses the console (shell, log input) and
   are passed on untouched; `mdb ` lines are claimed by the dispatcher.
2. **A dispatcher**: parse verb + args, look up a handler in a static
   command table, emit the reply. No dynamic allocation in the parse path.
3. **Handlers** that reach into existing subsystems through narrow seams:
   the compositor's framebuffer pointer, the input queue, the shell's
   navigation API, the config store, the log ring, the power machine.

MDB is **debug tooling**. It is compiled in only under a build flag
(reference: a Kconfig bool, e.g. `CONFIG_AIWATCHOS_MDB`, default **off**
for release builds, on for bringup builds). With the flag off there is no
parser, no task, no cost — `mdb …` lines are just unrecognised console
input. Do not ship MDB enabled in a release build without a deliberate
decision: it is a control surface.

## 2. Command table

Implement, in this priority order (inspection first — it's what unblocks
hardware bringup):

| verb | handler sketch |
|---|---|
| `version` | constant strings + build id |
| `health` | read subsystem state, format one JSON line (§5) |
| `logs` | copy out of the log ring (§6) |
| `screenshot` | framebuffer pointer + geometry (§3) |
| `panic` | persisted reset/panic records (§7) |
| `touch` | register/unregister a raw-touch observer (§4) |
| `tap` / `swipe` / `press` | post synthetic input events (§4) |
| `goto` / `open` | call the shell's navigation API directly |
| `wake` / `sleep` | drive the display power machine |
| `reboot` | reply first, flush, then reset |
| `config get` | presence + length from the config store (§8) |
| `config set` | hand the line remainder to the provisioning parser (§8) |

Any verb not implemented in a given build replies
`ERR not_implemented …` rather than failing silently — hosts (and agents)
plan around explicit answers.

## 3. Framebuffer requirements (screenshot)

- The handler needs a **stable pointer to the composited framebuffer**,
  its width/height, and its pixel format, readable at command time.
  Reference: a full-screen RGB565 buffer (410 × 502 × 2 B ≈ 402 KiB) living
  in **PSRAM**, owned by the compositor.
- The dump is the buffer **as composed, before the panel flush path's
  byte-swap**. On the reference panel (CO5300 over QSPI) the driver swaps
  bytes while flushing; the framebuffer itself is host-order RGB565 and
  the protocol declares it `rgb565le`. If your pipeline stores the buffer
  post-swap, declare `rgb565be` — never silently convert: conversion costs
  a second full-frame buffer or a slow in-place pass, and an honest
  format field costs nothing.
- **Stream it out in chunks.** Do not copy the framebuffer to a transmit
  buffer — on the reference part that would be ~402 KiB the SRAM budget
  does not have. Write the header line, then write the buffer to the
  console in slices (e.g. 4 KiB) directly from PSRAM.
- Taking a screenshot while the compositor is mid-frame can tear. Either
  accept it (fine for bringup) or synchronise to the compositor's frame
  boundary / take the pointer under the same lock the flush path uses.
  Document which you did. Reference: flush under a frame lock, screenshot
  takes the same lock briefly to grab pointer+geometry, then streams
  unlocked (a torn frame during active animation is acceptable).

## 4. Input injection seam

The one rule: **injected input must be indistinguishable from physical
input downstream of the injection point.**

- Find the single queue/pipe every input source already feeds — physical
  touch events, GPIO button events — and post synthetic events of exactly
  the same struct/type there. Do not call UI handlers directly, do not
  poke widget state.
- `tap` = a down event + an up event at (x, y), posted back-to-back (a
  small inter-event delay, ~30 ms, matches physical tap timing closely
  enough for UI debounce logic).
- `swipe` = down at start, a handful of move events interpolated along
  the line spread over the requested duration, up at the end. 8–16 move
  steps is plenty.
- `press <button>` posts the same event the GPIO button driver posts for
  that button (down + up).
- Coordinates are validated against panel geometry before posting:
  outside → `ERR out_of_range`, nothing posted.
- The `touch` observer hook taps this same stream *before* the queue:
  register a callback the input layer invokes for every physical event,
  format `TOUCH x=… y=… state=…` lines from it, and tag injected events
  `src=mdb` (or suppress them) so a watching host can tell human from
  synthetic. Unregister on `mdb touch stop` or on any other command.

## 5. health JSON

Emit exactly one line of compact JSON after a bare `OK`. Build it with a
fixed-size stack buffer and bounded `snprintf`-style appends — the schema
(see PROTOCOL.md §5.1) is ~20 keys; 1 KiB is comfortable. Rules:

- Presence, never values, for config (§8).
- `null` for things the build can't know (no battery gauge, no weather
  yet) — never omit a required key, never invent a number.
- State strings are the firmware's own state-machine names. Hosts compare
  them loosely; keep them stable within a firmware line.

## 6. Log ring

Keep a ring buffer of recent log output in PSRAM (reference: ~32 KiB),
fed by the platform's log hook (ESP-IDF: `esp_log_set_vprintf`-style
redirect or a custom `vprintf` shim that also writes the ring). The
`logs` handler counts the complete lines in the ring at snapshot time,
replies `OK lines=<n>`, then streams them oldest-first. Lines written
during the dump are simply not in the snapshot. Never let the MDB
dispatcher hold the log path's lock while streaming — deadlock against
the very logs you're printing is the classic failure here.

## 7. Panic retention

Persist, in RTC memory and/or NVS: a panic counter, the last reset
reason, and (if the platform retains it) the faulting PC / backtrace of
the last crash. Increment-and-record belongs in the earliest boot path
(read the reset reason, bump the counter if it was a panic, stash the
record). `mdb panic` just formats what's retained. If the build has a
coredump partition, report `"coredump": true`; retrieving the dump
itself stays on the esptool path in protocol v0.1.

## 8. Config: redaction is the feature

- `config get` iterates the config store's known keys and emits, per key,
  `{"present": bool, "len": int}`. `len` is the stored value's character
  length (0 when absent). **The value is never read into the reply path
  at all** — implement it so the handler physically cannot print one
  (query presence/length metadata from the store, not the value).
- `config set <setup-string>` passes the remainder of the line to the
  firmware's existing provisioning parser (the same one that handles
  setup strings from any other channel) and applies **atomically**: the
  parser validates everything first; on any error, reply
  `ERR bad_setup_string …` with nothing applied. Reply `OK applied=<n>`
  with the field count on success.
- **Never log the setup string or any value from it** — not at debug
  level, not in an error path. The provisioning parser's logging gets the
  same treatment: field names and counts only. This rule is why MDB can
  be the provisioning route at all.
- `health`'s `config` object is presence booleans only — not even
  lengths. Lengths exist in `config get` because "did my 40-character
  token actually land?" is a legitimate bringup question; values are
  where the line sits.

## 9. Resource discipline (ESP32-S3 class parts)

The watch has 512 KiB of SRAM and it is the scarce resource; PSRAM is
plentiful. MDB's budget:

| thing | where | size |
|---|---|---|
| command line buffer | static / stack | ≤ 1 KiB (spec cap) + NUL |
| health JSON scratch | stack | ~1 KiB |
| log ring | PSRAM | ~32 KiB (pre-existing facility) |
| framebuffer | PSRAM | pre-existing; MDB allocates **no** copy |
| dispatcher task stack | internal, or PSRAM-backed where the RTOS allows | 3–4 KiB — the one real DRAM cost; account for it in the build's DRAM budget like any other task |
| per-command heap allocations | — | **zero** in parse/reply paths |

If the console is already served by an existing task, run the dispatcher
in that task's context and the DRAM cost drops to the line buffer.
Screenshot streaming from PSRAM over USB-CDC: expect the full frame to
take a moment; hosts use a generous timeout for it (the reference client
does), so don't "optimise" by double-buffering into SRAM.

## 10. Dispatcher sketch

```c
// Pseudocode — adapt to the firmware's console plumbing.
static const mdb_cmd_t kCommands[] = {
    { "version",  cmd_version  },
    { "health",   cmd_health   },
    { "logs",     cmd_logs     },
    { "screenshot", cmd_shot   },
    { "panic",    cmd_panic    },
    { "touch",    cmd_touch    },
    { "tap",      cmd_tap      },
    { "swipe",    cmd_swipe    },
    { "press",    cmd_press    },
    { "goto",     cmd_goto     },
    { "open",     cmd_open     },
    { "wake",     cmd_wake     },
    { "sleep",    cmd_sleep    },
    { "reboot",   cmd_reboot   },
    { "config",   cmd_config   },  // sub-verbs: get | set
    { NULL, NULL },
};

void mdb_console_line(const char *line) {
    if (strncmp(line, "mdb ", 4) != 0) return;           // not ours
    // tokenise in place on a bounded copy; find handler; run it.
    // handlers emit exactly one reply per PROTOCOL.md §3.
}
```

Keep the handler signatures uniform — `(int argc, char **argv, writer_fn
out)` works well, with `out` abstracting the console write so the same
handlers can ride a future message transport (gadget link) unchanged.

## 11. Bring-up checklist

1. `mdb version` answers on the bench build. ✔ framing works.
2. `mdb health` — JSON parses, presence flags match the config you
   flashed. ✔ state seams work.
3. `mdb screenshot` → PNG opens, shows the composed UI, colours true.
   (Wrong colours here with right colours on glass — or vice versa —
   means the declared format/swap is wrong; §3.)
4. Photo of the glass next to the screenshot: identical layout ⇒
   composition and panel both healthy; different ⇒ the fault is in the
   flush/panel layer, and MDB just proved it isn't the UI code.
5. `mdb touch` + physical taps: coordinates track the finger; corners
   read ~(0,0) / (w−1,h−1). Wrong corner mapping = touch transform bug,
   now measurable instead of vibes.
6. `mdb tap` on a known button: UI responds. ✔ injection seam works.
7. `mdb config set` a *wrong* string: `ERR bad_setup_string`, config
   unchanged. Then a right one: `applied=<n>`, `config get` lengths move.
8. `mdb reboot`, reconnect, `mdb panic`: counters sane.
