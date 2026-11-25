# PyTap

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![HA Version](https://img.shields.io/badge/Home%20Assistant-2026.10.4%2B-blue.svg)](https://www.home-assistant.io/)
[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz/)
[![Validate](https://github.com/azebro/pytap/actions/workflows/validate.yml/badge.svg)](https://github.com/azebro/pytap/actions/workflows/validate.yml)

A Home Assistant custom integration for monitoring **Tigo TAP solar energy systems**. PyTap connects over TCP, a local serial adapter, or a Home Assistant ESPHome serial proxy, passively listens to the RS-485 bus protocol, and exposes real-time per-optimizer sensor entities — power, voltage, current, temperature, and more.

---

## Hardware requirements
You need an RS-485 receiver connected to the Tigo CCA/TAP bus: an RS-485-to-TCP converter, a local serial adapter, or an ESPHome device exposing an RS-485 serial proxy. For bus connection details, see [taptap](https://github.com/willglynn/taptap).

Use a suitably rated RS-485 transceiver; never connect ESP GPIO pins directly to the differential bus. Keep the receiver passive, with its transmit driver disabled. PyTap never writes bytes or toggles RTS/DTR, but hardware wiring must also prevent bus transmission. Local serial adapters require device permissions in the HA host/container.

## Features

- **Real-time streaming** — Push-based data delivery with sub-second latency (no polling).
- **Configurable reporting** — Choose **live reporting** (default; every reading delivered as it arrives) or a **time-weighted averaged interval** that caches readings and pushes a single representative value every few seconds to reduce CPU and recorder load on large installations.
- **Per-optimizer entities** — 13 sensors per Tigo TS4 module (performance,
  power, voltage in/out, current in/out, temperature, DC-DC duty cycle, RSSI,
  daily energy, total energy, readings today, status code) plus 3 diagnostic
  binary sensors (high output mode, low voltage flag, reading fault).
- **Aggregate sensors** — 4 sensors per string and 4 for the full installation: performance, power, daily energy, total energy.
- **Performance tracking** — Per-optimizer, per-string, and installation-wide performance percentage based on configurable peak panel power (Wp).
- **Diagnostics download** — Integration diagnostics export includes parser counters, infrastructure/mapping state, and per-node summaries.
- **Menu-driven setup** — Add optimizer modules one at a time with guided form fields.
- **Barcode-based identification** — Stable hardware barcodes as entity identifiers (survives gateway restarts).
- **Discovery logging** — Unconfigured barcodes seen on the bus are logged for easy identification.
- **Persistent barcode mapping** — Discovered barcodes and node mappings are
    saved across restarts. When you add a previously-discovered barcode,
    it resolves instantly without waiting for the next gateway enumeration.
- **Stale-reading handling** — Choose what live readings do when a panel stops
  reporting (for example at night): hold the last value (default), become
  unavailable, or show zero. See [Stale readings](#stale-readings).
- **Reading fault detection** — Physically impossible reports are discarded and
  flagged instead of being shown or added to energy.
- **Energy accumulation** — Trapezoidal Wh integration with proactive midnight reset and monotonic lifetime totals. Daily sensors zero at exactly midnight local time. Energy totals are restored after a restart.
- **Embedded parser** — The protocol parser is bundled; Home Assistant installs `serialx` for serial access automatically.
- **Native serial selection** — Choose local adapters and ESPHome proxy ports using Home Assistant's serial-port picker.
- **Passive module discovery** — New setups can finish with no modules. Optimizer sensors and aggregate membership are created as barcode-bearing bus data arrives, and discovered modules survive restarts.
- **Options flow** — Add or remove optimizer modules at any time without reconfiguring.

## Sensors

Each tracked Tigo TS4 optimizer (configured or discovered) exposes the following sensors:

| Sensor | Unit | Device Class |
| --- | --- | --- |
| Performance | % | — |
| Power | W | `power` |
| Voltage In | V | `voltage` |
| Voltage Out | V | `voltage` |
| Current In | A | `current` |
| Current Out | A | `current` |
| Temperature | °C | `temperature` |
| DC-DC Duty Cycle | % | — |
| RSSI | dBm | `signal_strength` |
| Daily Energy | Wh | `energy` |
| Total Energy | Wh | `energy` |
| Readings Today | — | — (diagnostic) |
| Status Code | — | — (diagnostic) |

and these diagnostic binary sensors:

| Binary sensor    | Device Class |
| ---------------- | ------------ |
| High Output Mode | —            |
| Low Voltage Flag | —            |
| Reading Fault    | `problem`    |

**Status entities.** Status code, Low voltage flag and High output mode come
from power-report bytes that Tigo has not documented. Their names describe
patterns seen in captured data, not confirmed meanings:

- **Status code** — the raw byte shown as hex. `0x64` is normal; `0x68` is
  typical at low current around dawn/dusk; `0xE4` at low input voltage.
- **Low voltage flag** — the top bit of that status byte.
- **High output mode** — a separate bit, set almost only above about 0.5 A
  input current.

These three only record a new state when their value changes, so they don't
add a database row for every report.

**Reading fault** turns on when a report is physically impossible: output
voltage more than 2 V above input, either voltage outside 0–100 V, or
temperature outside −40…125 °C. Such readings are discarded: they don't
replace the last good values, add energy, or count as a fresh report. The
fault clears on the next valid report. It is unavailable while PyTap has no
data for the panel (no report yet and nothing saved), and unknown while the
panel has data but no fault result yet (for example a newly discovered panel).
It keeps its state across restarts, and has `fault_reasons` and
`last_fault_at` attributes.

PyTap also creates aggregate virtual devices:

- **Tigo String <name>** — `performance`, `power`, `daily_energy`, `total_energy`
- **Tigo Installation** — `performance`, `power`, `daily_energy`, `total_energy`

When automatic discovery is enabled, the installation device also has a
diagnostic **Module discovery** sensor (see [Step 2](#step-2-add-modules)).

String and installation energy totals add up each member panel's production
since it was last seen. A counter reset credits its new nonnegative value once;
new or re-added members establish a baseline without adding their existing
energy. Temporarily missing members retain their baseline; removed members do
not. The daily aggregate resets at HA-local midnight and ignores prior-day
readings, including after restore.

Member counters and aggregate baselines are saved separately. After an abrupt
stop, an older restored member counter can look like a reset and overcount by
as much as that restored value (potentially its lifetime total for total energy,
or its current-day total for daily energy). Use graceful restarts to reduce this
accepted persistence-skew risk. Graceful shutdown uses a bounded listener join
before flushing final counters; a timed-out stop is not a completed save.
This does not make the two stores atomic or repair previously affected totals.

---

## Installation

### Manual

1. Copy the `custom_components/pytap` folder into your Home Assistant `config/custom_components/` directory.
2. Restart Home Assistant.

### HACS (Recommended)

1. Open **HACS** in your Home Assistant instance.
2. Click the **three dots** menu in the top right and select **Custom repositories**.
3. Add `https://github.com/azebro/pytap` with category **Integration**.
4. Search for **PyTap** and click **Download**.
5. Restart Home Assistant.

---

## Configuration

PyTap is configured entirely through the Home Assistant UI — no YAML needed.

### Step 1: Add the Integration

1. Go to **Settings → Devices & Services → Add Integration**.
2. Search for **PyTap**.
3. Choose **TCP gateway** or **Serial / Home Assistant proxy**.
   - **TCP:** enter the gateway's **host** and **port** (default: 502).
   - **Serial:** select the actual adapter/proxy port and **baud rate** (default: 38400). Framing is 8 data bits, no parity, one stop bit (8N1), with no flow control.

For a proxy, first add its ESPHome device to HA and configure `serial_proxy` in its firmware. Each port on a multi-port device is listed separately. The picker also permits a manual local device path or an `esphome-hass://` URL for an offline port; prefer selecting the discovered port so its full identity is retained. Direct `esphome://`, `socket://`, and credential-bearing URLs are not accepted by the serial form; raw TCP converters use the TCP form.

> **Note:** The connection test runs off the event loop and is bounded. If the gateway is temporarily
> unreachable (e.g., powered off at night), the form shows **Could not connect**. Submit the same
> settings again to save them anyway; PyTap keeps retrying in the background and connects
> when the gateway becomes available. Local serial paths are normalised and must resolve to a
> character device under `/dev/` (not `/dev/fd`, `/dev/std*`, `/dev/shm` or `/dev/mqueue`).

### ESPHome serial-proxy example

Add this fragment to an ESPHome device's configuration (adjust the RX pin for your board and keep the RS-485 driver disabled in hardware):

```yaml
logger:
  baud_rate: 0 # Do not send UART log output to the bus.

api:
  encryption:
    key: !secret api_encryption_key

uart:
  id: tigo_uart
  rx_pin: GPIO17
  baud_rate: 38400
  data_bits: 8
  parity: NONE
  stop_bits: 1

serial_proxy:
  - id: tigo_proxy
    uart_id: tigo_uart
    name: Tigo Bus
    port_type: RS485
```

This uses the current [ESPHome serial proxy component](https://esphome.io/components/serial_proxy/), which is experimental. Configure the ESPHome device's board/network normally, flash it, and add it to Home Assistant. Select **Tigo Bus** in PyTap; API credentials remain managed by HA. Match the selected baud rate to the bus.

Serial ports are exclusive: another PyTap entry cannot use the same endpoint (a symlink such as `/dev/serial/by-id/…` counts as the same port as its target), and another application/integration may also hold it busy. A port or TCP host/port still held by a PyTap listener that is shutting down also counts as in use until it is released. An absent proxy is retried, never replaced automatically with a different port. If you remove and recreate the ESPHome config entry, reselect its port in PyTap. No serial data at night does not make the saved settings invalid.

PyTap requires **HA 2026.10.4+**. Existing TCP entries migrate automatically without changing their entities or energy history; config entries remain version 7.

On HA **2026.10.4**, two ESPHome proxy URL forms need a newly selected port or
an upgrade to **HA 2026.11+**: URLs whose decoded `port_udev_id` ends in a
`-port<number>` suffix, and other URLs without `port_name`. Setup and connection
changes explain the appropriate remedy rather than silently dropping identity
filters. Other valid identity filters remain supported; malformed URLs are
rejected on both versions.

### Step 2: Add Modules

After entering connection details, you'll see a modules menu:

**Automatic discovery is enabled by default for new setups.** You can immediately click **Finish setup**, even with no modules added. PyTap starts listening and creates each optimizer's sensors as its barcode appears in a node table or power report. It does not send scan commands or wait for discovery to complete before finishing setup. The installation's diagnostic **Module discovery** sensor shows **Waiting for modules** until a module is known.

Discovered modules use the name **Optimizer <barcode>**, the **Discovered** string group, and an unknown rated power (**0 Wp**). No physical string layout or panel rating is inferred from bus traffic. Power and energy readings work immediately; optimizer/group performance percentages are not calculated while the relevant panel rating is unknown, and those performance entities are disabled until it is set (an entity you disabled yourself stays disabled). Edit the module later to enter its actual name, string, and rated Wp.

Discovery is gradual and depends on gateway traffic; a quiet/offline bus or missing node table may delay it. Repeated reports do not create duplicate sensors, and a barcode already tracked by another PyTap entry is not adopted (each barcode is claimed by one entry; manually configured modules are claimed at setup, before discovered ones). Discovered metadata is stored with coordinator state, so admitting modules does not repeatedly reload the connection.

For manual selection, open **Module discovery settings**, disable automatic discovery, then:

1. Click **"Add a module"**.
2. Fill in the fields:
    - **String group** — Required label to group optimizers by string (e.g., `A`, `East`).
   - **Name** — A friendly name for the optimizer (e.g., `Roof_Panel_01`).
   - **Barcode** — The Tigo optimizer barcode printed on the module (e.g., `A-1234567B`).
   - **Peak power (Wp)** — The panel's peak power rating in watts (default: 455 Wp). Used to calculate performance percentage.
3. Repeat for each optimizer you want to monitor.
4. Click **"Finish setup"** when done.

Manual setup requires at least one module; finishing without one shows an explicit error and the add-module form. Installations upgraded from earlier versions keep manual mode, unless you enable automatic discovery in options.

### Finding Barcodes

Barcodes are printed on each Tigo TS4 optimizer module. If you can't physically access them, PyTap will log any **unconfigured** barcodes it discovers on the bus:

```
INFO: Discovered unconfigured Tigo optimizer barcode: A-9999999Z (gateway=1, node=55).
```

With automatic discovery enabled, PyTap then starts tracking it (`Automatically tracking Tigo optimizer A-9999999Z`); in manual mode, add it as a module.

Check your Home Assistant logs after the gateway has been running for a while (up to 24 hours for full discovery).

### Managing Modules After Setup

Go to **Settings → Devices & Services → PyTap → Configure** to:

- **Change connection** — Switch between TCP and serial/proxy, or update the endpoint/baud rate. Connection changes save immediately and reload PyTap without changing module settings, entity IDs, or stored energy. The current serial port is not probed again while the listener owns it. If setup failed (for example because of invalid saved settings), saving corrected settings retries it.
- **Change reporting settings** — Toggle live reporting or set the averaged reporting interval (see [Reporting modes](#reporting-modes)).
- **Change stale reading behaviour** — Choose what live readings do when a panel stops reporting, and after how long (see [Stale readings](#stale-readings)).
- **Change module discovery** — Enable/disable discovering new optimizers. Disabling discovery keeps modules already known. Enabling it also admits barcodes logged in earlier manual sessions.
- **Edit a module** — Change its friendly name, string group, and rated panel power without changing barcode-based entity IDs or stored energy. Enter **0 Wp** if the rating is unknown.
- **Add** new optimizer modules, or explicitly restore an ignored barcode.
- **Remove** modules you no longer want to track. Removed barcodes are ignored by automatic discovery until you explicitly add them again.
- **Save and close** to apply changes.

Module/discovery edits are saved together on **Save and close**. Modules discovered while the options flow is open are merged into the saved catalogue. Concurrent deletions and ignore rules are preserved, and unrelated metadata edits are not overwritten by an older options form.

A barcode configured in another PyTap entry stays reserved even while that
entry is unloaded. If two entries configure the same barcode, only the owning
entry tracks it; the other logs one warning and continues with its healthy
modules instead of creating duplicate entities.

You can also delete an optimizer device, or a string device that no longer contains any optimizers, from its device page in Home Assistant. Deleting an optimizer (configured or discovered) works like **Remove**: its barcode is ignored by automatic discovery. The installation device can't be deleted. When PyTap starts, it also removes devices of removed (ignored) optimizers and of empty strings; other optimizer devices it doesn't recognise are kept, so you can delete them here. If the saved state can't be read, no devices are removed.

### Reporting modes

PyTap can deliver readings to Home Assistant in two ways, selectable under **Configure → Change reporting settings**:

- **Live reporting (default)** — Each reading is pushed to Home Assistant as soon as it arrives from the gateway, giving sub-second latency. Best for most installations.
- **Averaged interval** — When live reporting is turned off, readings are cached and a **time-weighted average** is pushed once per *reporting interval* (5–300 seconds, default 30). Weighting each reading by how long it remained the latest value yields a representative average even when readings arrive at uneven intervals. This reduces event-loop, CPU, and recorder/database load on installations with many panels.

> **Setting the interval:** The reporting interval only applies in averaged mode, so it is hidden while **Live reporting** is enabled. To change it, open **Change reporting settings**, untick **Live reporting**, and click **Submit** — a follow-up **Reporting Interval** screen then lets you enter the value. Leaving **Live reporting** ticked skips that screen entirely.

Energy totals (daily and lifetime) are always integrated from the raw readings, so the reporting mode only affects the instantaneous values (power, voltage, current, temperature) shown on dashboards — never the accumulated energy figures.

### Stale readings

Optimizers stop reporting at night and during radio dropouts. Under **Configure → Change stale reading behaviour** you choose what each panel's live readings do once no valid report has arrived within the **timeout** (60–600 seconds, default 120). The default, **Hold last value**, applies to new and existing entries and keeps the behaviour of earlier versions; **Unavailable** and **Zero** are opt-in.

| Action                    | Panel readings                                                                                                                                                                                     | String / installation power and performance                      |
| ------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------- |
| **Unavailable**           | Power, voltages, currents, temperature, duty cycle, RSSI, performance, status code, high output mode and low voltage flag become unavailable.                                                      | Count only panels that are reporting; unavailable when none are. |
| **Zero**                  | Power, voltage in/out, current in/out and performance show 0 (performance stays unknown if the panel rating is unknown). Temperature, duty cycle, RSSI and the status entities become unavailable. | Stale panels count as 0 W; show 0 when none are.                 |
| **Hold last value**       | Keep the last values, including after a restart. Status code, high output mode and low voltage flag are restored too.                                                                              | Include every panel's last values.                               |

Energy sensors (daily energy, total energy, readings today) and the reading-fault sensor are not affected by any action. In averaged reporting mode the reporting interval is added to the timeout, so panels don't flicker between updates. PyTap checks for stale panels every 5 seconds.

After a restart, saved readings aren't treated as current: with **Unavailable** they stay unavailable, and with **Zero** they show 0, until each panel's first new report arrives. Energy totals are restored either way.

### Diagnostics Download

PyTap diagnostics are available from the integration page:

1. Go to **Settings → Devices & Services → Integrations**.
2. Open **PyTap**.
3. Use the menu (⋮) and select **Download diagnostics**.

The diagnostics JSON includes parser counters, infrastructure and node mappings,
discovered barcodes, connection state, packet-capture status, and per-node summaries (including whether each panel is currently reporting, its status code and any reading fault). Host/IP, complete serial endpoints and capture file paths are not included; transport type, baud rate, and retry state remain visible.

---

## How It Works

```
Tigo Gateway (RS-485 bus)
    │
    │  TCP stream / local serial / ESPHome serial proxy
    ▼
PyTap Coordinator
    │
    ├── Embedded protocol parser (pytap library)
    │   └── Parses frames → PowerReport, Infrastructure, Topology events
    │
    ├── Optimizer filter
    │   ├── Configured or discovered → update entities
    │   └── Unconfigured (discovery off, or removed) → log only
    │
    └── Push copies to Home Assistant event loop
        └── Sensor and binary sensor entities update in real time
```

PyTap uses a background listener thread that streams data, parses protocol frames, and keeps its own working copy of the readings. Home Assistant only ever receives finished copies, so entities never see half-updated data. Serial connections use `serialx`'s async transport on HA's loop, bridged to the same parser thread with bounded reads. Unload waits (up to a time limit) for the worker to release its port; if it is slow, setup is retried until it has stopped, so a replacement listener never shares the port. Timers and pending saves are cancelled either way, the slow listener's port or host counts as in use for new entries, and it finishes its own cleanup once its worker exits. Only configured or automatically discovered optimizers get entities. Each publish re-renders only the entities of panels whose data changed (and the string/installation sensors containing them); global changes such as new modules or gateway updates refresh every entity. In the default **live reporting** mode each update is forwarded immediately; in **averaged interval** mode updates are buffered and flushed as a time-weighted average every few seconds (see [Reporting modes](#reporting-modes)).

On startup, PyTap restores persisted coordinator state (barcode mappings, discovered modules, energy accumulators, last node snapshots and reading-fault state). Energy sensors show their restored totals straight away. Whether live readings show their saved values depends on the [stale-reading action](#stale-readings): only **Hold last value** shows them before new reports arrive. If the saved state can't be read, PyTap logs a warning and doesn't overwrite it until the next reload. If the connection drops, PyTap logs it once and keeps retrying quietly until data flows again.

---

## Development

### Raw packet capture (opt-in)

Version 1.4.0 adds admin-only actions under **Tools → Actions**. Capture is **off
by default**, and never resumes automatically after a restart or integration
reload. It copies reads from the existing listener before decoding, without
opening another connection or transmitting any bus commands. A running capture
is stopped and finalised when Home Assistant shuts down.

1. Call **`pytap.start_capture`**, select the PyTap instance, and choose:
   - `duration_minutes`: maximum duration, default **1440** (24 hours).
   - `max_storage_mib`: storage budget, default **1024** (1 GiB), range **16–4096**.
2. Use **`pytap.capture_status`** to check progress, stop reason, queue/disk
   errors, and dropped bytes/records. Capture status is also in diagnostics.
3. Call **`pytap.stop_capture`** when finished, or let the limit stop it.
4. **`pytap.list_captures`** lists the latest 20 sessions, including those from
   previous runs. Pass a returned `capture_id` to status/export for an older
   session, or omit it to use the current session.
5. **`pytap.export_capture`** validates and creates a private ZIP, returning its
   local archive path. Retrieve it through Samba/your existing file-management
   app. It is deliberately **not published at a web endpoint**. Stop recording
   before exporting. The ZIP counts toward the storage budget together with the
   instance's other captures and exports, but not the capture being exported
   (optional `max_storage_mib`, default that capture's own budget). It must also
   fit in free disk space (keeping 64 MiB spare); otherwise the export is
   refused. Because the source is excluded only for this export calculation,
   the source plus its archive can occupy approximately twice the chosen budget;
   this is not a hard cap on their combined size.
6. **`pytap.delete_capture`** permanently deletes one stopped session and its
   exported ZIPs, including ZIPs left behind after the session folder was
   removed. The active session can't be deleted.

The writer uses a bounded, non-blocking queue. On backpressure it stops recording
and reports loss rather than slowing the parser. On storage exhaustion it stops
instead of deleting earlier evidence. Raw segments rotate at **16 MiB**; the
budget includes existing recordings/exports for that config entry and reserves
space for metadata. Captures are never deleted automatically while the PyTap
entry exists; free budget with `delete_capture` after copying what you need.
Deleting the PyTap entry deletes all of its captures; if an export is still
running, they are deleted once it finishes. Exports and deletions keep running
across an integration reload, and new captures, exports and deletions are
refused until they finish.

Files live in the Home Assistant configuration directory, below
`/config/pytap_captures/<config_entry_id>/<capture_id>/` (ZIPs are written next
to the session directory). All capture path operations reject symlinks below
the configured Home Assistant directory, including linked capture roots,
entry/session directories and export targets. Symlinked ancestors above that
trusted directory are allowed. Refused external paths are never followed,
migrated or deleted.

If `pytap_captures` was previously a symlink, finish any capture/export jobs,
then manually move the recordings into a real `pytap_captures` directory
beneath the HA configuration directory before retrying.

Captures are therefore **included in Home Assistant
backups**; delete captures, and older backups, to remove them fully. Each
session directory contains:

| File                   | Contents                                                                                                                                                          |
| ---------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `bus-000000.bin`, …    | Unchanged bytes: unknown types, bad CRCs, escaped bytes, extra report fields, and noise                                                                           |
| `chunks-000000.bin`, … | UTC/monotonic nanoseconds, logical byte offset, length, connection generation (`<QQQII`, little-endian, 32 bytes/record)                                          |
| `events.jsonl`         | Connection open/close/error markers (error type only), without endpoints or exception text                                                                        |
| `manifest.json`        | Source/framing/software versions, initial infrastructure, SHA-256 checksums/counts for every file (including `events.jsonl`), completeness/loss/error information |

New captures use manifest format 2. Format 1 captures (from earlier versions of
this feature) can still be listed, replayed and exported, but their
`events.jsonl` has no checksum and is reported as unverified.

The manifest is rewritten only when something changed (at most every 10 seconds
while recording, plus at rotation and stop), and only after the data it
describes has been flushed to disk.

Timestamps describe **when a source read returned**, not exact UART-byte airtime.
Rotation can split a read into index records with the same timestamp. `complete`
only asserts that the local recorder finalized without known loss; it cannot
prove that the ESPHome UART, network, or hardware did not drop bytes upstream.
Failed disk chunks count bytes not confirmed written and indexed, even when an
unindexed raw tail remains available for forensic inspection.

Captures contain hardware identifiers and possibly diagnostic/configuration
text. Directories/files are private; metadata excludes endpoints/credentials.
Do not put recordings in `/config/www`, upload automatically, or publish without
reviewing them. Record ordinary traffic plus a natural enumeration/startup
cycle. Check status rather than assuming a full 24 hours fits the chosen budget.

### Offline replay and inspection

Download/extract a session and run from this repository with Python 3.11+:

```bash
python3 scripts/replay_capture.py /path/to/session --output /path/to/new-export
```

No HA installation is needed. Source files are never changed and an existing
output directory is never overwritten. The tool streams evidence and produces:

- `chunks.jsonl`: readable observation times, offsets, lengths and connections.
- `frames.jsonl`: supported/unknown packets, wire bytes, complete headers and
  payloads, CRC flags, unknown/extra measurement bytes, noise and incomplete
  frames. Invalid-CRC measurements are inspection-only. Each frame also says
  whether the live parser would have dropped it (`live_parser_would_drop`,
  with a `live_parser_drop_reason` such as `crc_mismatch` or `giant`); frames
  larger than the live parser accepts are flagged `giant`, also when they end
  in an invalid escape. If the capture started while PyTap was already
  connected, or bytes were lost within a connection, the live parser's framing
  state is unknown: until it can be determined, frames get
  `live_parser_would_drop: null` with reason `unknown_at_capture_start` or
  `unknown_after_capture_gap`.
- `events.jsonl`: current decoder events, using recorded UTC observations for
  slot-clock reconstruction rather than replay time. Correlation resets at
  reconnects/gaps; the recorded infrastructure snapshot bootstraps discovery.
- `summary.json`: completeness/loss, format version, inspection counts and
  counts of frames the live parser would drop or whose verdict is unknown.
  `integrity_verified` is only true for complete format 2 captures.

Incomplete captures require `--allow-incomplete`. Stale checksums/counts and
partial rows produce warnings and an unverified/incomplete summary, never
invented bytes. Unindexed tails remain in raw files/ZIP but are not replayed as
timestamped chunks. A checksum mismatch in a supposedly complete capture is
always an error, even with that override. Oversized corrupt frames are exported
as bounded fragments; their full byte stream remains in the raw evidence.

An interrupted capture can still be exported, even if its index ends in
corrupt rows (for example after a power cut); replay stops at the first invalid
row. If a crash created rotated segments before updating the manifest, ZIP
export also preserves those unlisted files. ZIPs of incomplete captures, and of
captures with validation warnings or unverified files (such as a format 1
event log), include
`recovery.json`, which lists any validation warnings and unlisted files and
records whether the archive could be verified. Unlisted files are not automatically timestamp-replayed. The initial parser
state is snapshotted in the listener immediately before the first admitted
read, so infrastructure changes during file preparation cannot silently assign
new measurements to stale barcodes. Power reports with an invalid slot counter
remain inspectable as raw frames but are dropped by the measurement decoder. A
receive-response frame with an invalid header slot counter never updates the
slot clock; its reports are kept only if its packets parse exactly and are all
well-formed (otherwise the whole frame is dropped), and like any report they
are dropped while the gateway has no slot clock yet. The parser counts these in
`invalid_slots`, `invalid_slot_frames_kept`, `invalid_slot_frames_dropped` and
`no_slot_clock_drops`.

This repository includes a dev container with Home Assistant pre-installed for local development and testing.

### Project Structure

```
pytap/
├── custom_components/pytap/     # The HA custom component
│   ├── __init__.py              # Integration lifecycle, migrations, device removal
│   ├── binary_sensor.py         # Binary sensor platform (status flags, reading fault)
│   ├── capture_services.py      # Packet-capture actions (start/stop/status/list/export/delete)
│   ├── config_flow.py           # Config & options flows
│   ├── connection.py            # TCP/serial settings validation and serial source
│   ├── const.py                 # Constants
│   ├── coordinator.py           # Push-based data coordinator
│   ├── diagnostics.py           # Diagnostics download
│   ├── energy.py                # Energy accumulation helpers
│   ├── entity.py                # Shared optimizer device info
│   ├── sensor.py                # Sensor platform (13 per-optimizer types + aggregates)
│   ├── services.yaml            # Action descriptions
│   ├── manifest.json            # Integration metadata
│   ├── strings.json             # UI strings
│   ├── translations/en.json     # English translations
│   └── pytap/                   # Embedded protocol parser library
│       └── core/
│           ├── capture.py       # Raw packet capture writer, validation, export
│           └── replay.py        # Offline frame inspection and replay
├── scripts/
│   └── replay_capture.py        # Offline capture inspection/replay tool
├── deploy/                      # Deployment tooling
│   ├── deploy.sh                # Deploy script (SSH-based)
│   └── .deploy.env.example      # Example credentials config
├── tests/                       # Integration tests
├── docs/
│   ├── architecture.md          # Architecture & design document
│   └── implementation.md        # Implementation details & history
├── config/                      # Dev HA config directory
├── requirements.txt             # Home Assistant Core dependencies
├── requirements-test.txt        # Test dependencies (custom-component test harness)
└── pytest.ini                   # Test configuration
```

### Running Home Assistant (Dev)

```bash
python3 -m homeassistant --config config/ --debug
```

### Running Tests

```bash
# From the repository root in an isolated Python 3.14.2+ environment.
# Windows: run these POSIX commands in WSL.
CORE=2026.10.4
python3 -m venv .venv
. .venv/bin/activate
curl -fsSL "https://raw.githubusercontent.com/home-assistant/core/$CORE/homeassistant/package_constraints.txt" \
  -o .venv/ha-constraints.txt
python -m pip install -c .venv/ha-constraints.txt "homeassistant==$CORE" pydantic -r requirements-test.txt
python scripts/prepare_ha_tests.py --constraints .venv/ha-constraints.txt

# The rollback checks need this exact baseline object, including in shallow clones.
git fetch origin a80698fca67bef4847a3c6ce5217f163fee94c01

# Integration tests
python -m pytest tests/ -vv --tb=short

# Parser library tests
PYTHONPATH=custom_components/pytap python -c 'import pytap, pytest; raise SystemExit(pytest.main(["custom_components/pytap/pytap/tests/", "--import-mode=importlib", "-vv"]))'

# Real supported-Core APIs, transports and exact-baseline rollback
python scripts/run_core_smoke.py
```

Repeat the environment preparation in a **separate** environment with
`CORE=2026.11.2`, omitting `-r requirements-test.txt`; run only the smoke command
there. The published harness pins HA 2026.10.4 and conflicts with 2026.11's
Pydantic requirement. Never override either Core's official pins.

`prepare_ha_tests.py` follows both manifest dependency kinds from ESPHome, USB,
Zeroconf and Assist Satellite, installs their full requirement closure under
the official constraints, runs `pip check`, and checks real module/API origins.
The smoke runner rejects zero tests, skips, failures and expected failures.
CI uses this same two-environment matrix. Real firmware restart/reconnection
and frontend picker labels still need hardware acceptance testing.

### Linting

```bash
python3 -m ruff check custom_components/pytap/ tests/ scripts/
```

### Deploying to a Home Assistant Instance

A deployment script is provided in `deploy/` to push the component to a remote HA OS installation via SSH.

**Setup:**

```bash
# Copy the example config and fill in your credentials
cp deploy/.deploy.env.example deploy/.deploy.env
# Edit deploy/.deploy.env with your host, username, and password
```

> **Note:** `deploy/.deploy.env` is gitignored — credentials are never committed.

**Deploy:**

```bash
# Using the config file
./deploy/deploy.sh

# Or pass credentials directly
./deploy/deploy.sh --host 192.168.1.184 --user 'user' --password 'yourpass'

# Deploy without restarting HA
./deploy/deploy.sh --no-restart
```

The script uploads the component via tar-over-SSH (compatible with HA OS SSH add-on), verifies the deployment, and optionally restarts Home Assistant Core.

Run `./deploy/deploy.sh --help` for all options.

### Documentation

- **[Architecture](docs/architecture.md)** — System design, module responsibilities, data flow, and design decisions.
- **[Implementation](docs/implementation.md)** — Current implementation state, module details, test coverage, and development history.

---

## Acknowledgements

Inspired by the [taptap](https://github.com/litinoveweedle/taptap) and [taptap Home Assistant add-on](https://github.com/litinoveweedle/hassio-addons).

---

## License

[MIT](LICENSE) © Adam Zebrowski
