# Changelog

## [Unreleased]

### Added

- **Configurable reporting mode** — New "Live reporting" toggle (default on) under **Configure → Change reporting settings**. When disabled, readings are cached and delivered as a time-weighted average every *reporting interval* (5–300 s, default 30), reducing CPU and recorder load on installations with many panels. Energy totals are always integrated from raw readings, so only the instantaneous values are averaged.

### Changed

- Entities are re-rendered only when their data changes: each publish updates just the entities of panels whose data changed and the string/installation aggregates containing them. Global changes (gateway or node-table changes) still update every entity.
- An unreachable gateway in the setup or connection-change form now shows "cannot connect"; submitting the same settings again saves them and PyTap keeps retrying in the background.
- Connection loss is logged once (with the error type and errno) instead of on every retry, until data flows again; reconnecting after a silent link is likewise warned about once per outage.
- Home Assistant 2025.3+ is now required.
- Reporting interval default changed from 5 to 30 seconds; allowed range tightened from 1–300 to 5–300 seconds.
- Config entry schema bumped to version 5 (adds `live_reporting`, default `True`, via automatic migration).
- The reporting interval is now collected on its own follow-up screen that appears only when live reporting is turned off, so the interval is never shown (effectively greyed out) while live reporting is enabled.

### Fixed

- The listener thread now keeps its own state and Home Assistant only receives published copies, so averaged reporting mode no longer occasionally shows raw readings.
- Unloading or reloading no longer gets stuck when the listener is slow to stop: setup is retried until the old listener has released its connection, so it is never shared. A stop that times out still cancels timers and pending saves; the stuck listener's host/port counts as in use for new entries until it exits, and it then cleans up after itself. Removing an entry waits (bounded) for it before deleting the saved state.
- Stopping a TCP connection wakes the blocked socket read immediately.
- The midnight reset of daily values is DST-safe.
- Invalid saved connection settings (e.g. an out-of-range port from an older version) now give a clear setup error instead of retrying forever; saving corrected settings under **Configure → Change connection settings** retries setup.
- If saved state can't be read, it is no longer replaced with empty state (losing energy totals and mappings): PyTap logs a warning and doesn't save until the next reload.
- An invalid slot counter in a receive-response header no longer crashes the parser (an `IndexError` escaped `Parser.feed` and forced a reconnect). Such a frame never updates the slot clock and keeps its reports only when the packet walk consumes the payload exactly and every packet is well-formed; otherwise the whole frame is dropped. Like any power report, kept reports are still dropped while the gateway has no slot clock yet. Power reports with an invalid slot counter are dropped. New parser counters `invalid_slot_frames_kept`, `invalid_slot_frames_dropped` and `no_slot_clock_drops` sit alongside `invalid_slots`. The "no slot clock" warning is logged once per gateway (per connection), then at debug level, instead of for every discarded report.
- Finish setup with no modules now shows a validation error instead of silently returning to the menu.
- Diagnostics now also redact the entry title, which contains the gateway host.
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
