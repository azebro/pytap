# PRD-PYTAP-1.4.0-001 v9 r14 — Pre-port environment evidence

Criterion 1 reports. Each report covers one isolated environment installed on 2026-11-24 and verified on 2026-11-25. Raw logs, constraints, and freezes remain on the validation host's temporary evidence directory; they are not copied here.

Paths are relative to each environment's `site-packages` directory.

## Shared facts

- Validation host: Home Assistant OS 18.4 dev VM, SSH add-on Alpine 3.24.3, `Linux x86_64` (`Linux-6.18.58-haos-x86_64-with-musl1`).
- Closure seeds: `esphome`, `usb`, `zeroconf`, `assist_satellite`; traversal follows `dependencies` and `after_dependencies`. `assist_satellite` is seeded explicitly because ESPHome imports it without declaring it.
- No requirement in either closure has an environment marker; all 22 are exact `==` pins and all are applicable.
- Distribution count method: unique normalized names from `importlib.metadata.distributions()`. This includes `pip==26.2.2` and equals the line count of `pip freeze --all`. Earlier reports of 170/127 excluded `pip`.
- Verification script: `verify.py` (stored with its outputs in a separate dated verification directory next to the raw evidence directory), run as `<env>/bin/python -I verify.py closure-<version>.json`. It imports each module and resolves each symbol's defining file with `inspect.getsourcefile`; isolated mode (`-I`) keeps repository paths off `sys.path`, and no stubs or overrides are used.
- The exact installation commands were recovered on 2026-11-25 from the original shell history and independently matched to this session's dated tool calls. Their original success outputs establish exit status 0: the shell printed each success message only in the `rc == 0` branch (the first Core install instead used `set -e` with an explicit failure exit before its successful `pip check`). No installation was rerun for this recovery.
- In the commands below, `RAW` denotes the non-identifying **validation-host temporary evidence location**. Substituting it for the original absolute path is the only redaction; options, package versions, constraint files, requirement files, and output destinations are unchanged.

## Run `env-a-2026.10.4` (installed 2026-11-24, verified 2026-11-25)

| Fact | Value |
|---|---|
| Python / Core | 3.14.10 / 2026.10.4 |
| Constraints | `https://raw.githubusercontent.com/home-assistant/core/2026.10.4/homeassistant/package_constraints.txt`, tag `2026.10.4`, SHA-256 `7260404c34adcc5aac1fb4e7384f989292cfce0e30e3a7a46e18383f34a8059f` (host copy and upstream match) |
| `serialx` / Pydantic / harness | 1.11.0 / 2.13.5 / `pytest-homeassistant-custom-component` 0.13.375 |
| Distributions | 171 (170 excluding `pip`) |

**Installation (recovered commands; successful final installation, 2026-11-24 UTC):**

1. `python3 -m venv "$RAW/ha-2026.10.4"`; `"$RAW/ha-2026.10.4/bin/python" -m pip install --disable-pip-version-check -c "$RAW/constraints-2026.10.4.txt" homeassistant==2026.10.4 > "$RAW/install-core-2026.10.4.log" 2>&1` — exit 0; invocation 20:10:30Z, success confirmed 20:11:27Z.
2. `"$RAW/ha-2026.10.4/bin/python" -m pip install --disable-pip-version-check -c "$RAW/constraints-2026.10.4.txt" -r "$RAW/requirements-2026.10.4.txt" > "$RAW/install-closure-2026.10.4.log" 2>&1` — exit 0; final invocation 20:17:37Z, `closure installation passed` at 20:18:28Z.
3. `"$RAW/ha-2026.10.4/bin/python" -m pip install --disable-pip-version-check -c "$RAW/constraints-2026.10.4.txt" pytest-homeassistant-custom-component==0.13.375 > "$RAW/install-harness-2026.10.4.log" 2>&1` — exit 0; invocation 20:23:11Z, `harness installation passed` at 20:23:51Z. Installed Pydantic 2.13.5.

The same closure command had failed at 20:12:01Z (missing C++ compiler) and 20:15:39Z (missing Python headers). Those incomplete attempts are not the reported final installation. The previously authorized add-on build prerequisites were supplied before the successful final attempt; all inventories and checks here are post-harness, not intermediate results.

**Components (21):** api, assist_pipeline, assist_satellite, bluetooth, conversation, esphome, ffmpeg, hassio, http, intent, llm, media_player, network, repairs, stt, tag, tts, usb, wake_word, websocket_api, zeroconf.

**Direct requirements (22, each resolved to exactly the pinned version):** aioesphomeapi==46.6.0, aiohasupervisor==0.6.0, aiousbwatcher==1.1.2, bleak-esphome==4.0.0, bleak-retry-connector==4.7.1, bleak==3.0.2, bluetooth-adapters==2.4.0, bluetooth-auto-recovery==1.6.4, bluetooth-data-tools==1.29.24, dbus-fast==5.0.22, esphome-dashboard-api==1.4.0, gazetteer-matcher==1.1.0, ha-ffmpeg==3.2.2, habluetooth==7.1.2, hassil==3.12.1, home-assistant-intents==2026.10.6, ifaddr==0.2.0, mutagen==1.48.1, pymicro-vad==1.0.1, pyspeex-noise==1.0.2, serialx==1.11.0, zeroconf==0.151.5.

**`gazetteer_matcher` provider chain:** `esphome` / `assist_satellite` → (dependency) `assist_pipeline` → (dependency) `conversation` → (manifest requirement) `gazetteer-matcher==1.1.0` → distribution `gazetteer-matcher` 1.1.0 → module `gazetteer_matcher/__init__.py`. No other installed distribution requires it.

**Checks (2026-11-25):**

| Command | Exit | Result |
|---|---|---|
| `python -m pip check` | 0 | `No broken requirements found.` |
| `python -I verify.py closure-2026.10.4.json` | 0 | All imports and symbols below resolved |

| Module / symbol | Origin |
|---|---|
| `gazetteer_matcher` | `gazetteer_matcher/__init__.py` |
| `homeassistant.components.esphome` | `homeassistant/components/esphome/__init__.py` |
| `homeassistant.components.esphome.serial_proxy` | `homeassistant/components/esphome/serial_proxy.py` |
| `homeassistant.components.assist_satellite` | `homeassistant/components/assist_satellite/__init__.py` |
| `homeassistant.components.usb.serial_proxy_stub` | `homeassistant/components/usb/serial_proxy_stub.py` |
| `SerialPortSelector` | `homeassistant/helpers/selector.py` |
| `serial_proxy.register_serialx_transport` | `homeassistant/components/esphome/serial_proxy.py` |
| `serial_proxy.build_url` | `homeassistant/components/esphome/serial_proxy.py` |

## Run `env-b-2026.11.2` (installed 2026-11-24, verified 2026-11-25)

| Fact | Value |
|---|---|
| Python / Core | 3.14.10 / 2026.11.2 |
| Constraints | `https://raw.githubusercontent.com/home-assistant/core/2026.11.2/homeassistant/package_constraints.txt`, tag `2026.11.2`, SHA-256 `d7f1647f03e911b19e1e7bef48edae2765156374a5c0860bc427a00303401e30` (host copy and upstream match) |
| `serialx` / Pydantic / harness | 1.15.1 / 2.14.1 / absent (verified: `importlib.metadata` reports no distribution) |
| Distributions | 128 (127 excluding `pip`) |

**Installation (recovered commands; 2026-11-24 UTC):**

1. `python3 -m venv "$RAW/ha-2026.11.2"`; `"$RAW/ha-2026.11.2/bin/python" -m pip install --disable-pip-version-check -c "$RAW/constraints-2026.11.2.txt" homeassistant==2026.11.2 > "$RAW/install-core-2026.11.2.log" 2>&1` — exit 0; invocation 20:19:10Z, `core installation passed` at 20:20:00Z.
2. `"$RAW/ha-2026.11.2/bin/python" -m pip install --disable-pip-version-check -c "$RAW/constraints-2026.11.2.txt" -r "$RAW/requirements-2026.11.2.txt" > "$RAW/install-closure-2026.11.2.log" 2>&1` — exit 0; invocation 20:20:12Z, `closure installation passed` at 20:20:52Z.
3. `"$RAW/ha-2026.11.2/bin/python" -m pip install --disable-pip-version-check -c "$RAW/constraints-2026.11.2.txt" pydantic==2.14.1 > "$RAW/install-pydantic-2026.11.2.log" 2>&1` — exit 0; invocation 20:22:07Z, constrained-install success at 20:22:11Z. Installed Pydantic 2.14.1 and pydantic-core 2.50.1.

**Components (21):** identical to run A.

**Direct requirements (22, each resolved to exactly the pinned version):** aioesphomeapi==46.25.0, aiohasupervisor==0.8.1, aiousbwatcher==1.1.2, bleak-esphome==4.0.0, bleak-retry-connector==4.7.1, bleak==3.0.2, bluetooth-adapters==2.4.0, bluetooth-auto-recovery==1.6.4, bluetooth-data-tools==1.29.24, dbus-fast==5.0.22, esphome-dashboard-api==1.4.0, gazetteer-matcher==1.1.0, ha-ffmpeg==3.2.2, habluetooth==7.1.2, hassil==3.12.1, home-assistant-intents==2026.10.27, ifaddr==0.2.0, mutagen==1.48.1, pymicro-vad==1.0.1, pyspeex-noise==1.0.2, serialx==1.15.1, zeroconf==0.152.0.

**`gazetteer_matcher` provider chain:** same edges as run A, resolving `gazetteer-matcher` 1.1.0. The only manifest difference in the closure is that `hassio` additionally depends on `network`.

**Checks (2026-11-25):**

| Command | Exit | Result |
|---|---|---|
| `python -m pip check` | 0 | `No broken requirements found.` |
| `python -I verify.py closure-2026.11.2.json` | 0 | All imports and symbols below resolved |

| Module / symbol | Origin |
|---|---|
| `gazetteer_matcher` | `gazetteer_matcher/__init__.py` |
| `homeassistant.components.esphome` | `homeassistant/components/esphome/__init__.py` |
| `homeassistant.components.esphome.serial_proxy` | `homeassistant/components/esphome/serial_proxy.py` |
| `homeassistant.components.assist_satellite` | `homeassistant/components/assist_satellite/__init__.py` |
| `homeassistant.components.usb.serial_proxy_stub` | `homeassistant/components/usb/serial_proxy_stub.py` |
| `SerialPortSelector` | `homeassistant/helpers/selector.py` |
| `serial_proxy.register_serialx_transport` | `homeassistant/components/esphome/serial_proxy.py` |
| `serial_proxy.build_port_url` | `homeassistant/components/esphome/serial_proxy.py` |
| `serial_proxy.identity_port_info` | `homeassistant/components/esphome/serial_proxy.py` |

## Evidence correction

The earlier report incorrectly described the exact installation commands as unavailable and treated a general preference for lean delivery as acceptance of that gap. The commands and exit-dependent success records have now been recovered; no waiver is needed or claimed. Both reports describe one final installed environment each, reverified without reinstalling.

## Schema gate (criterion 2)

The earlier read-only live inspection (journal U137) found one PyTap entry at schema version 7, minor version 1. A fresh read-only inspection on 2026-11-25 before porting again returned exactly one PyTap entry with version 7, minor version 1; only its schema fields were emitted. Criterion 2's initial gate passes. Reverify exactly v7 immediately before deployment.
