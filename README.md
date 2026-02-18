# PyTap

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![HA Version](https://img.shields.io/badge/Home%20Assistant-2024.1%2B-blue.svg)](https://www.home-assistant.io/)

A Home Assistant custom component for monitoring **Tigo TAP solar energy systems**. PyTap connects to your Tigo gateway over TCP, passively listens to the RS-485 bus protocol, and exposes real-time per-optimizer sensor entities — power, voltage, current, temperature, and more.

---

## Features

- **Real-time streaming** — Push-based data delivery with sub-second latency (no polling).
- **Per-optimizer sensors** — 7 sensor entities per Tigo TS4 module: power, voltage in/out, current, temperature, DC-DC duty cycle, and RSSI.
- **Menu-driven setup** — Add optimizer modules one at a time with guided form fields.
- **Barcode-based identification** — Stable hardware barcodes as entity identifiers (survives gateway restarts).
- **Discovery logging** — Unconfigured barcodes seen on the bus are logged for easy identification.
- **No external dependencies** — The protocol parser library is fully embedded; nothing to install from PyPI.
- **Options flow** — Add or remove optimizer modules at any time without reconfiguring.

## Sensors

Each configured Tigo TS4 optimizer exposes the following sensors:

| Sensor | Unit | Device Class |
|--------|------|-------------|
| Power | W | `power` |
| Voltage In | V | `voltage` |
| Voltage Out | V | `voltage` |
| Current | A | `current` |
| Temperature | °C | `temperature` |
| DC-DC Duty Cycle | % | — |
| RSSI | dBm | `signal_strength` |

---

## Installation

### Manual

1. Copy the `custom_components/pytap` folder into your Home Assistant `config/custom_components/` directory.
2. Restart Home Assistant.

### HACS (coming soon)

HACS distribution is planned for a future release.

---

## Configuration

PyTap is configured entirely through the Home Assistant UI — no YAML needed.

### Step 1: Add the Integration

1. Go to **Settings → Devices & Services → Add Integration**.
2. Search for **PyTap**.
3. Enter your Tigo gateway's **host** (IP address or hostname) and **port** (default: 502).

> **Note:** The connection test is non-blocking. If the gateway is temporarily unreachable (e.g., powered off at night), setup will proceed and connect when the gateway becomes available.

### Step 2: Add Modules

After entering connection details, you'll see a modules menu:

1. Click **"Add a module"**.
2. Fill in the fields:
   - **String group** — Optional label to group optimizers by string (e.g., `A`, `East`).
   - **Name** — A friendly name for the optimizer (e.g., `Roof_Panel_01`).
   - **Barcode** — The Tigo optimizer barcode printed on the module (e.g., `A-1234567B`).
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

- **Add** new optimizer modules.
- **Remove** modules you no longer want to track.
- **Save and close** to apply changes.

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
│   ├── sensor.py                # Sensor platform (7 entity types)
│   ├── manifest.json            # Integration metadata
│   ├── strings.json             # UI strings
│   ├── translations/en.json     # English translations
│   └── pytap/                   # Embedded protocol parser library
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

### Documentation

- **[Architecture](docs/architecture.md)** — System design, module responsibilities, data flow, and design decisions.
- **[Implementation](docs/implementation.md)** — Current implementation state, module details, test coverage, and development history.

---

## Acknowledgements

Inspired by the [taptap Home Assistant add-on](https://github.com/litinoveweedle/hassio-addons) which pioneered the barcode-driven module configuration approach for Tigo systems.

---

## License

[MIT](LICENSE) © Adam Zebrowski
