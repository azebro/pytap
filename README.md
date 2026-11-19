# PyTap

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![HA Version](https://img.shields.io/badge/Home%20Assistant-2025.3%2B-blue.svg)](https://www.home-assistant.io/)
[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz/)
[![Validate](https://github.com/azebro/pytap/actions/workflows/validate.yml/badge.svg)](https://github.com/azebro/pytap/actions/workflows/validate.yml)

A Home Assistant custom integration for monitoring **Tigo TAP solar energy systems**. PyTap connects to your Tigo gateway over TCP, passively listens to the RS-485 bus protocol, and exposes real-time per-optimizer sensor entities — power, voltage, current, temperature, and more.

---

## Hardware requirements
Please note that this integration requires RS485 to TCP converter that needs to be connected to Tigo CCA to read the data from TAPs. It should be connected exactly the same way the TAP is connected to CCA. For detailed hardware requirements and connection details review this repo: [taptap](https://github.com/willglynn/taptap).

## Features

- **Real-time streaming** — Push-based data delivery with sub-second latency (no polling).
- **Configurable reporting** — Choose **live reporting** (default; every reading delivered as it arrives) or a **time-weighted averaged interval** that caches readings and pushes a single representative value every few seconds to reduce CPU and recorder load on large installations.
- **Per-optimizer sensors** — 12 sensor entities per Tigo TS4 module: performance,
  power, voltage in/out, current in/out, temperature, DC-DC duty cycle, RSSI,
  daily energy, total energy, and readings today.
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
- **Energy accumulation** — Trapezoidal Wh integration with proactive midnight reset and monotonic lifetime totals. Daily sensors zero at exactly midnight local time. Energy totals are restored after a restart.
- **No external dependencies** — The protocol parser library is fully embedded; nothing to install from PyPI.
- **Options flow** — Add or remove optimizer modules at any time without reconfiguring.

## Sensors

Each configured Tigo TS4 optimizer exposes the following sensors:

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

PyTap also creates aggregate virtual devices:

- **Tigo String <name>** — `performance`, `power`, `daily_energy`, `total_energy`
- **Tigo Installation** — `performance`, `power`, `daily_energy`, `total_energy`

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
3. Enter your Tigo gateway's **host** (IP address or hostname) and **port** (default: 502).

> **Note:** The connection test runs off the event loop and is bounded. If the gateway is temporarily
> unreachable (e.g., powered off at night), the form shows **Could not connect**. Submit the same
> settings again to save them anyway; PyTap keeps retrying in the background and connects
> when the gateway becomes available.

A gateway host/port still held by a PyTap listener that is shutting down counts as in use until it is released.

### Step 2: Add Modules

After entering connection details, you'll see a modules menu:

1. Click **"Add a module"**.
2. Fill in the fields:
    - **String group** — Required label to group optimizers by string (e.g., `A`, `East`).
   - **Name** — A friendly name for the optimizer (e.g., `Roof_Panel_01`).
   - **Barcode** — The Tigo optimizer barcode printed on the module (e.g., `A-1234567B`).
   - **Peak power (Wp)** — The panel's peak power rating in watts (default: 455 Wp). Used to calculate performance percentage.
3. Repeat for each optimizer you want to monitor.
4. Click **"Finish setup"** when done.

Setup requires at least one module; finishing without one shows an explicit error and the add-module form.

### Finding Barcodes

Barcodes are printed on each Tigo TS4 optimizer module. If you can't physically access them, PyTap will log any **unconfigured** barcodes it discovers on the bus:

```
INFO: Discovered unconfigured Tigo optimizer barcode: A-9999999Z
      (gateway=1, node=55). Add it to your PyTap module list to start tracking.
```

Check your Home Assistant logs after the gateway has been running for a while (up to 24 hours for full discovery).

### Managing Modules After Setup

Go to **Settings → Devices & Services → PyTap → Configure** to:

- **Change connection** — Update the gateway IP address and port. Connection changes save immediately and reload PyTap without changing module settings, entity IDs, or stored energy. If setup failed (for example because of invalid saved settings), saving corrected settings retries it.
- **Change reporting settings** — Toggle live reporting or set the averaged reporting interval (see [Reporting modes](#reporting-modes)).
- **Change stale reading behaviour** — Choose what live readings do when a panel stops reporting, and after how long (see [Stale readings](#stale-readings)).
- **Add** new optimizer modules (if the barcode was previously seen on the bus, it resolves instantly).
- **Remove** modules you no longer want to track.
- **Save and close** to apply changes.

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
| **Unavailable**           | Power, voltages, currents, temperature, duty cycle, RSSI and performance become unavailable.                                                                                                       | Count only panels that are reporting; unavailable when none are. |
| **Zero**                  | Power, voltage in/out, current in/out and performance show 0. Temperature, duty cycle and RSSI become unavailable.                                                                                 | Stale panels count as 0 W; show 0 when none are.                 |
| **Hold last value**       | Keep the last values, including after a restart.                                                                                                                                                   | Include every panel's last values.                               |

Energy sensors (daily energy, total energy, readings today) are not affected by any action. In averaged reporting mode the reporting interval is added to the timeout, so panels don't flicker between updates. PyTap checks for stale panels every 5 seconds.

After a restart, saved readings aren't treated as current: with **Unavailable** they stay unavailable, and with **Zero** they show 0, until each panel's first new report arrives. Energy totals are restored either way.

### Diagnostics Download

PyTap diagnostics are available from the integration page:

1. Go to **Settings → Devices & Services → Integrations**.
2. Open **PyTap**.
3. Use the menu (⋮) and select **Download diagnostics**.

The diagnostics JSON includes parser counters, infrastructure and node mappings,
discovered barcodes, connection state, and per-node summaries (including whether each panel is currently reporting). Host/IP is redacted, including in the entry title.

Power reports with an invalid slot counter are dropped. A receive-response
frame with an invalid header slot counter never updates the slot clock; its
reports are kept only if its packets parse exactly and are all well-formed
(otherwise the whole frame is dropped), and like any report they are dropped
while the gateway has no slot clock yet. The parser counts these in
`invalid_slots`, `invalid_slot_frames_kept`, `invalid_slot_frames_dropped` and
`no_slot_clock_drops`.

---

## How It Works

```
Tigo Gateway (RS-485 bus)
    │
    │  TCP stream (port 502)
    ▼
PyTap Coordinator
    │
    ├── Embedded protocol parser (pytap library)
    │   └── Parses frames → PowerReport, Infrastructure, Topology events
    │
    ├── Barcode allowlist filter
    │   ├── Configured → update sensor entities
    │   └── Unconfigured → log for discovery
    │
    └── Push copies to Home Assistant event loop
        └── Sensor entities update in real time
```

PyTap uses a background listener thread that streams data, parses protocol frames, and keeps its own working copy of the readings. Home Assistant only ever receives finished copies, so entities never see half-updated data. Unload waits (up to a time limit) for the worker to release its gateway connection; if it is slow, setup is retried until it has stopped, so a replacement listener never shares the connection. Timers and pending saves are cancelled either way, the slow listener's host/port counts as in use for new entries, and it finishes its own cleanup once its worker exits. Only events matching your configured barcodes create or update sensor entities. Each publish re-renders only the entities of panels whose data changed (and the string/installation sensors containing them); global changes such as gateway updates refresh every entity. In the default **live reporting** mode each update is forwarded immediately; in **averaged interval** mode updates are buffered and flushed as a time-weighted average every few seconds (see [Reporting modes](#reporting-modes)).

On startup, PyTap restores persisted coordinator state (barcode mappings, energy accumulators and last node snapshots). Energy sensors show their restored totals straight away. Whether live readings show their saved values depends on the [stale-reading action](#stale-readings): only **Hold last value** shows them before new reports arrive. If the saved state can't be read, PyTap logs a warning and doesn't overwrite it until the next reload. If the connection drops, PyTap logs it once and keeps retrying quietly until data flows again.

---

## Development

This repository includes a dev container with Home Assistant pre-installed for local development and testing.

### Project Structure

```
pytap/
├── custom_components/pytap/     # The HA custom component
│   ├── __init__.py              # Integration lifecycle, migrations
│   ├── config_flow.py           # Config & options flows
│   ├── connection.py            # Connection settings validation and source opening
│   ├── const.py                 # Constants
│   ├── coordinator.py           # Push-based data coordinator
│   ├── diagnostics.py           # Diagnostics download
│   ├── energy.py                # Energy accumulation helpers
│   ├── sensor.py                # Sensor platform (12 per-optimizer types + aggregates)
│   ├── manifest.json            # Integration metadata
│   ├── strings.json             # UI strings
│   ├── translations/en.json     # English translations
│   └── pytap/                   # Embedded protocol parser library
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
# Python 3.13 (as in CI); install the published custom-component test harness.
pip install -r requirements-test.txt

# Integration tests
python3 -m pytest tests/ -vv --tb=short

# Parser library tests
PYTHONPATH=custom_components/pytap python3 -c 'import pytap, pytest; raise SystemExit(pytest.main(["custom_components/pytap/pytap/tests/", "--import-mode=importlib", "-vv"]))'
```

### Linting

```bash
python3 -m ruff check custom_components/pytap/ tests/
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
