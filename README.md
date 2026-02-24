# PyTap

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![HA Version](https://img.shields.io/badge/Home%20Assistant-2024.1%2B-blue.svg)](https://www.home-assistant.io/)
[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz/)
[![Validate](https://github.com/azebro/pytap/actions/workflows/validate.yml/badge.svg)](https://github.com/azebro/pytap/actions/workflows/validate.yml)

A Home Assistant custom integration for monitoring **Tigo TAP solar energy systems**. PyTap connects to your Tigo gateway over TCP, passively listens to the RS-485 bus protocol, and exposes real-time per-optimizer sensor entities — power, voltage, current, temperature, and more.

---

## Hardware requirements
Please note that this integration requires RS485 to TCP converter that needs to be connected to Tigo CCA to read the data from TAPs. It should be connected exactly the same way the TAP is connected to CCA. For detailed hardware requirements and connection details review this repo: [taptap](https://github.com/willglynn/taptap).

## Features

- **Real-time streaming** — Push-based data delivery with sub-second latency (no polling).
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
- **Restart-safe availability** — Last known node readings are persisted and
    restored on startup, so sensors stay available with the most recent value
    even before the first live frame arrives (for example after a night restart).
- **Energy accumulation** — Trapezoidal Wh integration with daily reset semantics and monotonic lifetime totals.
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
| Readings Today | — | — |

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

> **Note:** The connection test is non-blocking. If the gateway is temporarily
> unreachable (e.g., powered off at night), setup will proceed and connect
> when the gateway becomes available.

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

### Finding Barcodes

Barcodes are printed on each Tigo TS4 optimizer module. If you can't physically access them, PyTap will log any **unconfigured** barcodes it discovers on the bus:

```
INFO: Discovered unconfigured Tigo optimizer barcode: A-9999999Z
      (gateway=1, node=55). Add it to your PyTap module list to start tracking.
```

Check your Home Assistant logs after the gateway has been running for a while (up to 24 hours for full discovery).

### Managing Modules After Setup

Go to **Settings → Devices & Services → PyTap → Configure** to:

- **Change connection** — Update the gateway IP address and port.
- **Add** new optimizer modules (if the barcode was previously seen on the bus, it resolves instantly).
- **Remove** modules you no longer want to track.
- **Save and close** to apply changes.

### Diagnostics Download

PyTap diagnostics are available from the integration page:

1. Go to **Settings → Devices & Services → Integrations**.
2. Open **PyTap**.
3. Use the menu (⋮) and select **Download diagnostics**.

The diagnostics JSON includes parser counters, infrastructure and node mappings,
discovered barcodes, connection state, and per-node summaries. Host/IP is redacted.

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
    └── Push to Home Assistant event loop
        └── Sensor entities update in real time
```

PyTap uses a background listener thread that streams data from the gateway, parses protocol frames, and dispatches events to the Home Assistant event loop. Only events matching your configured barcodes create or update sensor entities.

On startup, PyTap restores persisted coordinator state (barcode mappings, energy accumulators, and last node snapshots). Sensor entities also use Home Assistant restore fallback if needed, so a restart during low/no production does not force entities into unavailable when prior data exists.

---

## Development

This repository includes a dev container with Home Assistant pre-installed for local development and testing.

### Project Structure

```
pytap/
├── custom_components/pytap/     # The HA custom component
│   ├── __init__.py              # Integration lifecycle
│   ├── config_flow.py           # Config & options flows
│   ├── const.py                 # Constants
│   ├── coordinator.py           # Push-based data coordinator
│   ├── sensor.py                # Sensor platform (12 entity types)
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
├── requirements.txt             # HA + dev dependencies
└── pytest.ini                   # Test configuration
```

### Running Home Assistant (Dev)

```bash
python3 -m homeassistant --config config/ --debug
```

### Running Tests

```bash
# Integration tests (config flow + sensor platform)
python3 -m pytest tests/ -vv --tb=short

# Parser library tests
python3 -m pytest custom_components/pytap/pytap/tests/ -vv
```

### Linting

```bash
python3 -m ruff check custom_components/pytap/
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
