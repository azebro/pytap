# Changelog

## [Unreleased]

### Added

- **Configurable reporting mode** — New "Live reporting" toggle (default on) under **Configure → Change reporting settings**. When disabled, readings are cached and delivered as a time-weighted average every *reporting interval* (5–300 s, default 30), reducing CPU and recorder load on installations with many panels. Energy totals are always integrated from raw readings, so only the instantaneous values are averaged.

### Changed

- Reporting interval default changed from 5 to 30 seconds; allowed range tightened from 1–300 to 5–300 seconds.
- Config entry schema bumped to version 5 (adds `live_reporting`, default `True`, via automatic migration).
- The reporting interval is now collected on its own follow-up screen that appears only when live reporting is turned off, so the interval is never shown (effectively greyed out) while live reporting is enabled.

### Fixed

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
