# Changelog

## [Unreleased]

### Added

- **Stale reading behaviour** option (**Configure → Change stale reading behaviour**): choose what live panel readings do once a panel stops sending valid reports — **Hold last value** (default for new and existing entries, restored after a restart, so behaviour is unchanged for upgrading users unless they opt in), **Unavailable**, or **Zero** (power, voltage, current and performance report 0; other live readings become unavailable) — and the timeout (60–600 s, default 120 s; in averaged reporting mode the reporting interval is added). With **Unavailable** or **Zero**, string and installation power counts only reporting panels; with **Zero**, their performance still divides by every panel's rating, counting stale panels as 0 W. After a restart, those two modes don't show saved live readings as current until the first new report. Energy totals are never affected.
- Per-optimizer diagnostic entities from previously undecoded power-report bytes: **High output mode** binary sensor (byte 7 top bit, set almost only above ~0.5 A input current), **Status code** sensor (byte 9 raw value shown as hex, e.g. `0x64` normal, `0x68` low current around dawn/dusk, `0xE4` low input voltage) and **Low voltage flag** binary sensor (byte 9 top bit). Names describe captured behaviour; Tigo has not documented these bits. They only record a state when their value changes, and are saved so **Hold** shows them after a restart.
- **Reading fault** problem binary sensor per optimizer. It turns on when a report is physically impossible (output voltage more than 2 V above input, either voltage outside 0–100 V, or temperature outside −40…125 °C) and clears on the next valid report. Impossible readings are discarded: they don't replace the last good values, add energy, or count as a fresh report. Attributes `fault_reasons` and `last_fault_at`; the state is kept across restarts.
- Optimizer devices, and string devices without optimizers, can be deleted from the Home Assistant UI. Deleting an optimizer (configured or discovered, even while PyTap isn't loaded) excludes its barcode from automatic discovery.
- `pytap.delete_capture` admin action deletes a stopped capture session and its exported archives, including archives left behind after the session folder was removed.
- Opt-in, admin-only raw capture with bounded asynchronous writing, timestamp/connection indexes, private archives, rotation/storage/duration limits, and explicit loss/error reporting. Off after every restart/reload; a running capture is finalised when Home Assistant shuts down. Captures use manifest format 2; format 1 captures can still be listed, replayed and exported, with their event log reported as unverified. Deeply nested manifests are rejected cleanly.
- Capture export: interrupted captures (even with an index ending in corrupt rows) can be exported, with `recovery.json` listing warnings and unverified files. The archive must fit the storage budget left by the instance's other captures and exports (the capture being exported doesn't count against itself; default budget: the capture's own) and free disk space. Inputs are opened without following symlinks. Export/delete jobs are tracked across reloads, so no capture, export or deletion can start while one is still running, and removing the entry waits for it before deleting the captures.
- Capture action errors are translated: invalid requests and invalid evidence are validation errors, runtime failures (e.g. disk errors, insufficient space) are Home Assistant errors. Diagnostics don't include the capture directory path.
- Standalone frame inspection/replay retaining unsupported frames, bad CRCs, unknown/extra payload bytes, and using recorded timestamps rather than replay time. Each frame is flagged when the live parser would drop it (`live_parser_would_drop`, `giant`, also for oversized frames ending in an invalid escape); the verdict is unknown (`null`) until the live parser's framing state can be determined after a mid-connection capture start or a gap.
- Passive automatic optimizer discovery, enabled by default for new setups. Setup can finish before any modules are seen; sensors and aggregate membership grow dynamically as barcodes arrive. Each barcode is tracked by only one PyTap entry (claims are domain-wide; an entry's manually configured modules are claimed at setup, before discovered ones). Discovery saves don't trigger reconnections.
- Discovery status diagnostics, module metadata editing (edits keep modules discovered while the options flow is open), and persistent ignore rules for explicitly removed optimizers.
- Home Assistant ESPHome serial proxies and local serial adapters, selectable with the native serial-port picker during setup and connection changes. Symlinked paths (e.g. `/dev/serial/by-id/…`) count as the same port as their target. Local paths are normalised and must resolve to a character device under `/dev/` (not `/dev/fd`, `/dev/std*`, `/dev/shm` or `/dev/mqueue`).
- Transport diagnostics with serial endpoints redacted, and bounded serial open/read/close operations using `serialx`.
- **Configurable reporting mode** — New "Live reporting" toggle (default on) under **Configure → Change reporting settings**. When disabled, readings are cached and delivered as a time-weighted average every *reporting interval* (5–300 s, default 30), reducing CPU and recorder load on installations with many panels. Energy totals are always integrated from raw readings, so only the instantaneous values are averaged.

### Changed

- Performance entities (per panel, string and installation) are disabled while any panel they cover has no rated power, and re-enabled once every rating is set. Entities you disabled yourself are left alone.
- String and installation energy totals now add up each member's increase since it was last seen (restored across restarts), so moving a panel between strings or discovering a new panel no longer adds its lifetime energy. The daily aggregate resets at local midnight and reports the start of its own cycle as `last_reset`.
- Packet captures are stored in the Home Assistant configuration directory (`pytap_captures/<entry_id>`), so they are included in Home Assistant backups, and are deleted when the PyTap entry is removed.
- Capture manifests are rewritten only when they change (at most every 10 s while recording, plus at rotation/stop), after the data they describe has been flushed to disk. `events.jsonl` now has a checksum in the manifest.
- Entities are re-rendered only when their data changes: each publish updates just the entities of panels whose data changed and the string/installation aggregates containing them, and a panel starting or stopping reporting updates only that panel's entities and aggregates. Global changes (new modules, gateway or node-table changes) still update every entity.
- An unreachable endpoint in the setup or connection-change form now shows "cannot connect"; submitting the same settings again saves them and PyTap keeps retrying in the background.
- Connection loss is logged once (with the error type and errno) instead of on every retry, until data flows again; reconnecting after a silent link is likewise warned about once per outage.
- Config entries are migrated to version 7: existing connections are marked as TCP, and existing installations keep manual module selection (automatic discovery stays off until enabled in options). Automatically discovered modules use barcode-based names and unknown panel ratings until edited; performance is not guessed from a default rating.
- Home Assistant 2026.11+ is now required; existing TCP connections migrate automatically without changing entity IDs or energy history.
- Connection settings now offer TCP or serial/proxy; serial defaults to 38400 baud, 8N1, with no flow control or bus writes.
- Reporting interval default changed from 5 to 30 seconds; allowed range tightened from 1–300 to 5–300 seconds.
- Config entry schema bumped to version 5 (adds `live_reporting`, default `True`, via automatic migration).
- The reporting interval is now collected on its own follow-up screen that appears only when live reporting is turned off, so the interval is never shown (effectively greyed out) while live reporting is enabled.

### Fixed

- The listener thread now keeps its own state and Home Assistant only receives published copies, so averaged reporting mode no longer occasionally shows raw readings.
- Unloading or reloading no longer gets stuck when the listener is slow to stop: setup is retried until the old listener has released its port, so it is never shared. A stop that times out still cancels timers and pending saves; the stuck listener's port or host counts as in use for new entries until it exits, and it then cleans up after itself. Removing an entry waits (bounded) for it before deleting the saved state.
- Stopping a TCP connection wakes the blocked socket read immediately.
- The midnight reset of daily values is DST-safe.
- Invalid saved connection settings (e.g. an out-of-range port from an older version) now give a clear setup error instead of retrying forever; saving corrected settings under **Configure → Change connection settings** retries setup.
- If saved state can't be read, it is no longer replaced with empty state (losing energy totals and mappings): PyTap logs a warning and doesn't save until the next reload.
- Devices of removed (excluded) optimizers and of strings without modules are removed at setup. Other unknown optimizer devices are kept and can be deleted from the UI; nothing is removed when the saved state can't be read, and empty strings are kept with automatic discovery when no saved state was restored.
- An invalid slot counter in a receive-response header no longer crashes the parser (an `IndexError` escaped `Parser.feed` and forced a reconnect). Such a frame never updates the slot clock and keeps its reports only when the packet walk consumes the payload exactly and every packet is well-formed; otherwise the whole frame is dropped. Like any power report, kept reports are still dropped while the gateway has no slot clock yet. Power reports with an invalid slot counter are dropped. New parser counters `invalid_slot_frames_kept`, `invalid_slot_frames_dropped` and `no_slot_clock_drops` sit alongside `invalid_slots`. The "no slot clock" warning is logged once per gateway (per connection), then at debug level, instead of for every discarded report.
- Finish setup in empty manual mode now shows a validation error instead of silently returning to the menu.
- Live reporting no longer buffers readings internally, preventing unbounded memory growth over time in the default mode.
- Live reporting now publishes an immutable copy of node data to Home Assistant, avoiding rare races where consumers (sensors, diagnostics) could observe the data changing mid-read.
- Averaged interval mode now flushes buffered readings on schedule during quiet periods (e.g. at dusk), instead of waiting for the next reading or a reconnect.
- The version 5 migration clamps any pre-existing reporting interval below 5 seconds up to the new minimum, so the reporting settings form stays submittable after upgrade.

## [1.2.0] - 2026-03-07

### Changed

- Refactor code structure for improved readability and maintainability

## [1.1.1] - 2026-03-05

### Added

- Proactive midnight reset for daily energy accumulators

### Changed

- Reorder datetime imports for improved clarity
- Refactor code structure for improved readability and maintainability

### Documentation

- Document `sensor.tigo_max_power` and Page 3 (Panel Power) in pv_production dashboard

## [1.1.0] - Previous release

See [v1.1.0 tag](https://github.com/azebro/pytap/releases/tag/v1.1.0).

## [1.0.0] - Initial release

See [v1.0.0 tag](https://github.com/azebro/pytap/releases/tag/v1.0.0).
