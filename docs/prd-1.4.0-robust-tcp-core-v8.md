# PyTap 1.4.0 — Robust TCP Core and PR #39 Integration

**Status:** Approved by the user for implementation; feasibility and deployment gates remain mandatory

**Stable reference:** PRD-PYTAP-1.4.0-001, v8

**Date:** 2026-11-23

**Baseline:** PR #39, commit `a80698fca67bef4847a3c6ce5217f163fee94c01`

## Change from approved v7

The approved v7 PRD covers the aggregate-energy fixes on the older v5-entry / HA 2025.3 baseline. This v8 draft carries those approved accounting requirements forward, but changes the integration baseline to PR #39, preserves its v7 config-entry schema and intended features, sets HA 2026.10.4 as the minimum, and incorporates the confirmed #37 ownership/options and #38 capture-path defects. The approved v7 document remains unchanged; v8 requires its own independent review and explicit approval before implementation.

## Problem and users

PyTap users need reliable optimizer, string, and installation energy totals through counter resets, midnight rollovers, reloads, and incomplete reports. They also need the live PR #39 v7 configuration and its intended transport, discovery, capture/replay, and diagnostic behavior to remain usable. The previously prepared 1.4.0 candidate is based on PR #35 and cannot load the current v7 entry, so simply deploying it would not validate the approved work on the dev VM.

## Goals and success measures

- Integrate the approved 1.4.0 energy-accounting behavior on the exact PR #39 feature baseline without downgrading the config entry.
- Support HA 2026.10.4 as the declared minimum while preserving PR #39's intended TCP, local-serial, ESPHome-proxy, discovery, capture/replay, and diagnostic behavior.
- Prevent duplicate barcode ownership, stale options-flow writes, and capture operations escaping through a symlinked parent directory.
- Preserve and document the existing capture-storage policy rather than adding automatic pruning or changing its limits.
- Validate the candidate on the HA minimum and on the current dev VM before starting the approved 24-hour soak.
- Release metadata and user-facing documentation must consistently describe 1.4.0 and the supported HA floor.

## Confirmed decisions

- Use PR #39 as the integration baseline and preserve its v7 config-entry schema and intended features.
- Apply the approved aggregate-energy, reset, restore, timezone, changelog, documentation, and release requirements carried forward from v7.
- Require HA 2026.10.4 minimum. If full PR #39 behavior cannot be supported at that version, stop and request a renewed scope decision; do not silently raise the minimum or drop a feature.
- Fix all three confirmed #37 ownership/options defects and the #38 parent-symlink escape, with regression tests.
- Preserve PR #39 capture storage behavior: `max_storage_mib` defaults to 1024 MiB and accepts 16–4096 MiB per start; existing captures and archives count toward later budgets, while export sizing excludes only the source session directory. A capture and its archive can therefore together use roughly twice the configured limit. There is no automatic pruning, and deleting a capture also deletes its archives.
- Do not take a fresh current-state backup before replacing the #39 deployment, and do not restore the pre-PR backup.
- Do not start the live soak until the candidate is loaded with the v7 entry and confirmed to be receiving readings.

## Verified facts

- The dev VM currently reports HA 2026.11.2 and the PyTap config entry is loaded. The read-only config-entry API response did not expose the entry's stored schema version; v7 is the last verified version.
- The exact PR #39 head declares config-entry v7 (`__init__.py:73`, `config_flow.py:248`) and HA 2026.11+ in `hacs.json:5` and `README.md:179`; `manifest.json:14` requires `serialx>=1.15.1`.
- HA 2026.10.4 pins `serialx==1.11.0` ([official constraints](https://github.com/home-assistant/core/blob/2026.10.4/homeassistant/package_constraints.txt#L68)); therefore the PR #39 dependency declaration conflicts with the selected minimum.
- The test harness `pytest-homeassistant-custom-component==0.13.375` targets HA 2026.10.4 but its requirements override `serialx` to 1.15.1 (`requirements-test.txt:4-6`); the current CI validation uses HA 2026.11.0 (`.github/workflows/validate.yml:56-65`). Neither currently proves operation with HA 2026.10.4's official `serialx==1.11.0` constraint.
- PyTap validates and passes ESPHome serial-proxy URLs to `serialx.async_serial_for_url()`; it does not construct the proxy URLs or call the ESPHome handler directly (`connection.py:33-43,95-108`). HA 2026.10.4 requires `port_name` in ESPHome proxy URLs; its [handler](https://github.com/home-assistant/core/blob/2026.10.4/homeassistant/components/esphome/serial_proxy.py#L73-L76) rejects filter-only URLs that omit it. HA 2026.11+ supports both `port_name` URLs and identity-filter URLs, including `port_udev_id` ([2026.11 handler](https://github.com/home-assistant/core/blob/2026.11.0/homeassistant/components/esphome/serial_proxy.py#L94-L96)). `SerialPortSelector` exists in [HA 2026.10.4](https://github.com/home-assistant/core/blob/2026.10.4/homeassistant/helpers/selector.py#L1980-L2004).
- The exact #39 code still has three confirmed #37 defects: discovery relies on active coordinator claims and misses unloaded-entry ownership (`coordinator.py:168-170,274-278,573-598`; unload releases claims in `__init__.py:200-203`); Options → Done can save a stale module snapshot and resurrect externally deleted entries (`config_flow.py:417,431-481`; device deletion updates the entry in `__init__.py:266-274`); and a failed manual claim can leave a barcode active for duplicate tracking (`coordinator.py:248-278,1331-1351`).
- Capture code checks the leaf root for symlinks, but does not protect against a symlinked parent redirecting create/delete operations (`capture.py:77-79,230-242,1023-1032`; root is set in `capture_services.py:30-32`).
- The capture service defaults `max_storage_mib` to 1024 and accepts 16–4096 MiB (`capture_services.py:146`, `coordinator.py:675`). `_tree_size` counts regular files, including ZIP archives; capture start subtracts existing root contents (`capture.py:118-120,232-241`). Export sizing excludes only the source session directory (`capture.py:925-950`), so the source and archive can together approach twice the limit; the archive counts toward later budgets. Deleting a capture removes its archives (`capture.py:991-995`). There is no automatic pruning, and capture files are included in HA backups.
- The new HA-config-directory containment policy rejects a symlinked `pytap_captures` root beneath the config directory, which the current leaf-only check can allow. Existing external capture data must not be deleted or silently migrated; users need an actionable error and a release note explaining how to move the data into the supported directory.
- In #39, the coordinator's per-entry store is `pytap_{entry_id}_coordinator` (`coordinator.py:195`). Aggregate sensor restore data, including `native_value`, `baselines`, and `cycle_date`, is stored through HA's sensor restore mechanism (`sensor.py:762-788`); it is not in the coordinator store.
- The current candidate's v5 ceiling and refusal to load newer config entries are not acceptable for the live v7 entry.

## In-scope features

### Compatibility and PR #39 behavior

- Build on the exact PR #39 commit and preserve its intended feature set, including TCP and serial/proxy paths, config-entry v7, discovery, capture/replay, and diagnostic/fault entities.
- Declare HA 2026.10.4 as the minimum in the manifest, HACS metadata, README, test configuration, and CI validation.
- Resolve the `serialx` requirement conflict and test against HA 2026.10.4's official constraints without a `serialx` override.
- Use version-native ESPHome proxy URLs: HA 2026.10.4 requires `port_name` and rejects filter-only URLs; HA 2026.11+ supports both `port_name` and identity-filter URLs, including `port_udev_id`. If a stored URL is unsupported by the running HA version, surface a clear, actionable error rather than silently failing.
- Preserve the v5→v6→v7 migrations for older entries. Never downgrade a v7 entry, and do not restart the dev VM to force an incompatible candidate to load.

### Aggregate-energy behavior carried forward from v7

- Preserve exact post-reset credit for nonnegative total and daily member-counter decreases: credit the current nonnegative value, rebase to it, and count later increases by delta only. Do not credit negative values or double-count the pre-reset total.
- On every daily-aggregate update, include a member only when its `daily_reset_date` matches the current HA-local cycle date. A matching current-day reading contributes its full value; stale prior-day readings contribute nothing.
- At rollover or restore of a stale cycle, reset daily contributions/baselines for current members. Retain the baseline of a temporarily omitted configured member; prune removed-member baselines. If a member is present with a stale reset date, skip its reading and retain a zero baseline so its first matching current-day value is credited in full.
- Before any current member has node data, a restore must still prune removed-member baselines and advance a stale daily cycle to the current HA-local date, resetting the daily total and tracked baselines while preserving restored availability.
- Retain the intentional first-reading rebase for a genuinely new or re-added optimizer.
- Resolve the HA timezone for unattached per-optimizer and aggregate sensors; use it for `last_reset` and daily-cycle calculations where applicable.
- Preserve the documented `reporting_count` semantics, including the stale-timeout plus reporting-interval allowance in averaged mode.

### Discovery, options, and ownership

- Discovery must not assign a barcode already configured by another entry, including an entry whose coordinator is unloaded. Claim checks and updates must prevent concurrent duplicate ownership.
- Options → Done must reconcile with the latest entry/device state; a deletion made while the options flow is open must not be resurrected by a stale snapshot.
- If a configured/manual barcode cannot be claimed, disable only that barcode; do not create its entities or process its data. Keep healthy modules active, prevent duplicate tracking, and surface one clear warning or diagnostic without emitting an ERROR-level log for this expected conflict.
- Preserve user-selected exclusions and existing same-entry additions/removals when reconciling changes.

### Capture/replay safety and storage

- Treat `hass.config.config_dir` as the trusted path boundary. Reject symlinked path components beneath it that could redirect capture access; do not reject legitimate symlinked ancestors above the HA config directory.
- Apply path validation to capture creation, listing, directory resolution, archive export, capture deletion, and removal of the per-entry capture root during config-entry deletion.
- A previously symlinked `pytap_captures` root beneath the HA config directory must fail with a clear, actionable error. Do not delete or silently migrate its data; document the required move into a supported directory in the 1.4.0 release notes.
- Keep the selected storage policy: `max_storage_mib` defaults to 1024 MiB and is configurable from 16–4096 MiB per start. Existing captures and archives count toward later budgets; an export excludes only its source session directory during its size calculation, so a source plus archive can together approach twice the limit. No automatic pruning; deleting a capture removes its archives; artifacts remain included in HA backups.
- Document that the per-entry budget is not a strict combined cap during export, and that retained captures and archives can increase HA backup size.

## Non-goals

- Restoring the pre-PR backup, creating a fresh current-state backup before replacing #39, or using the old #35/v5 candidate on the live v7 entry.
- Raising the HA minimum above 2026.10.4 or dropping an intended #39 feature without a renewed user decision.
- Changing the capture budget's default or supported range, adding auto-pruning, or imposing a new integration-wide storage cap.
- Introducing new transports, discovery modes, sensors, diagnostics, or capture formats beyond the #39 baseline.
- Redesigning persistence to infer whether a lower restored counter is a reset or stale state, or introducing a guessed reset threshold.
- Unrelated refactoring, unrelated dependency changes, or changes to HA state before PRD v8 approval.

## Constraints, dependencies, and accepted risks

- **Compatibility gates:** HA 2026.10.4 support is plausible only with compatibility changes and remains unverified. Establish isolated test environments for HA 2026.10.4 and 2026.11.2 using each version's official constraints. The minimum-version environment must use Python 3.14.2 or newer and resolve `serialx==1.11.0` without a `serialx==1.15.1` override; the current test harness alone is insufficient because that override can produce a false pass. If either environment or required compatibility evidence cannot be produced, stop before porting and ask for a scope decision.
- **ESPHome version behavior:** HA 2026.10.4 requires `port_name` and rejects filter-only proxy URLs. HA 2026.11 and newer support both `port_name` and identity-filter URLs, including `port_udev_id`. An endpoint stored in a format unsupported by the running HA version must produce a clear, actionable error; do not silently fail or use private HA internals.
- **Config-entry gate:** before porting, read the PyTap entry's schema version from HA diagnostics (`entry.as_dict()`) or a read-only inspection of `.storage/core.config_entries`, with unrelated data and secrets omitted. Proceed only when the version is 7 or lower and the migration path is known; if it is above 7 or cannot be verified, stop and resolve the discrepancy. Recheck immediately before deployment and require exactly v7 then. Unit tests cover v5→v6→v7; do not downgrade a v7 entry.
- **Code-only recovery:** keep the config-entry schema at v7 and preserve both the per-entry coordinator store (`pytap_{entry_id}_coordinator`) and aggregate sensor restore data (`native_value`, `baselines`, and `cycle_date`) in formats readable by the exact PR #39 artifact. The integrated candidate must pass the rollback-readability test before live deployment. If deployment fails, redeploying that artifact is the only planned rollback; do not restore old state. If backward-compatible code-only recovery cannot be proven, stop before deployment and ask for a new decision.
- **No-backup risk:** the user declined a fresh backup before replacing #39. The available pre-#39 backup predates the v7 migration; restoring it would reverse that migration and lose energy continuity since 2026-11-22 14:47 UTC. Treat replacement as lacking a rollback copy of the current #39 state.
- **Accounting risk carried forward:** if HA restores an aggregate baseline newer than a coordinator member value after an abrupt stop before delayed storage, exact reset credit can overcount by the restored member-counter value. There is no reliable discriminator in the observed data; do not add a guessed threshold.
- **Deployment and soak:** the graceful restart needed to load the candidate is a deployment restart, outside the soak window. Record membership and total aggregate before deployment and after its first settled report. Start the 24-hour soak only after the candidate is loaded and stable; include one additional graceful restart during the soak. No membership changes are allowed during the soak.
- **External dependencies:** isolated test environments honoring HA 2026.10.4 and 2026.11.2 constraints, plus the current HA 2026.11.2 dev VM for deployment and live validation. Do not change the dev VM's HA version as a substitute for isolated compatibility testing.
- **Release path:** publish only after soak acceptance and merge through the repository's approved workflow. The target PR and merge action must be confirmed before creating or merging a PR; this PRD alone does not authorize either action.
- **Open user questions:** none. Compatibility, the live schema, and code-only recovery remain verification gates, not permission to change scope.

## Testable acceptance criteria

1. **Feasibility and harness gate:** before porting code, establish isolated HA 2026.10.4 and 2026.11.2 test environments using each version's official Core constraints. The 2026.10.4 environment must use Python 3.14.2 or newer and resolve `serialx==1.11.0`; minimum-version requirements must not override it with `serialx==1.15.1`. Record resolved packages and commands. If either environment or required evidence cannot be produced, stop before porting and ask for a scope decision.
2. **Schema gate:** before porting, verify the live entry's schema via HA diagnostics (`entry.as_dict()`) or read-only `.storage/core.config_entries` inspection, omitting unrelated data and secrets. Proceed only if its version is 7 or lower with a known migration path; stop if it is above 7 or unreadable. Recheck immediately before deployment and require exactly v7. Migration tests cover v5→v6→v7; no v7 downgrade is allowed.
3. **HA compatibility and transports:** CI and automated tests cover HA 2026.10.4 and 2026.11.2 with each version's official constraints. Validate TCP and local serial with the actual `serialx` version resolved for each HA version (a PTY or loopback endpoint is acceptable). Exercise each version's public ESPHome selector/handler using a mocked `aioesphomeapi` or equivalent endpoint; do not rely on physical ESPHome hardware.
4. **ESPHome URL compatibility:** on HA 2026.10.4, tests prove `port_name` URLs work and that filter-only URLs without `port_name` return a clear, actionable error. On HA 2026.11.2, tests prove both `port_name` and PR #39 identity-filter URLs (including `port_udev_id`) work using that version's public selector/handler. Test the stored URL forms against both HA versions and return a clear error for unsupported forms. The live VM gate confirms the candidate's v7 entry and readings; it does not depend on physical ESPHome hardware being present. Never silently fail.
5. **Accounting:** regression tests cover same-cycle total/daily counter decreases, normal post-reset increases, mixed reset dates, both midnight event orders, stale prior-day readings, temporarily omitted members, removed/re-added membership, restore before the first current report, and unattached-sensor timezone behavior. Required bug reproductions fail before their fixes and pass afterward; callback-first ordering and genuine late-join behavior remain non-regressions.
6. **Discovery/options:** tests reproduce and prevent each of the three #37 cases: unloaded-entry barcode ownership, external deletion while Options is open, and manual-claim failure followed by duplicate tracking. Ownership checks account for all configured entries as well as active claims. On conflict, disable only the conflicting barcode, keep healthy modules active, prevent duplicate processing, and surface one clear warning/diagnostic without an ERROR-level log.
7. **Capture path safety:** use `hass.config.config_dir` as the trusted containment boundary. Tests prove symlinks beneath it cannot redirect capture creation, listing, directory resolution, export, capture deletion, or per-entry root removal outside the intended directory. Legitimate symlinked ancestors above the HA config directory must not be rejected. A previously symlinked `pytap_captures` root beneath the boundary must produce a clear, actionable error; do not delete or silently migrate its data, and document the required move in the 1.4.0 release notes.
8. **Capture storage behavior:** preserve and test the exact PR #39 semantics: `max_storage_mib` defaults to 1024 MiB and accepts 16–4096 MiB per start; existing regular files, including earlier ZIP exports, count toward later budgets; export sizing excludes only the source session directory, so source plus archive may together approach twice the limit; the archive counts toward later budgets; no automatic pruning; deleting a capture removes its archives; artifacts remain in HA backups. Documentation must not describe exports as fully unbudgeted or claim a strict combined cap.
9. **Documentation and metadata:** before deployment, update HACS metadata, README, manifest, CI/test configuration, architecture and implementation docs for HA 2026.10.4, v7, and version-specific ESPHome URL behavior. Resolve the README's “Version 1.5” statement; set `const.py`'s `INTEGRATION_VERSION` and package metadata to 1.4.0. Correct the retrospective 1.3.0 changelog entry and add 1.4.0 notes, including the symlinked-capture-root behavior; preserve historical milestones and align current-version claims.
10. **Automated validation and rollback proof:** after all feature slices and metadata changes are integrated, run the complete repository test suite on isolated HA 2026.10.4 and 2026.11.2 environments using official constraints. Include a regression test that writes candidate coordinator-store and sensor-restore data, then reads it using the exact PR #39 artifact; prove restored aggregate energy does not decrease or double-credit a member, including stale-member zero baselines and pruned baselines. If the rollback test or either required environment fails, stop before live deployment and ask for a decision. Record resolved versions and commands; Ruff and `git diff --check` must pass. Live VM checks are part of criterion 11, not this test gate.
11. **Deployment gate:** immediately before replacing #39, reverify that the live entry is exactly v7 and record membership and the unformatted numeric total aggregate. After the deployment restart and first settled report, confirm the tested 1.4.0 candidate is active, membership is unchanged, and total aggregate energy has not decreased. If the candidate fails to load or misbehaves, redeploy the exact #39 artifact only if criterion 10's integrated rollback proof passes. Do not start the soak against the old #39 build or the v5 candidate.
12. **Production soak:** start the clock only after the candidate is loaded with the v7 entry and readings are stable. Run at least 24 hours across midnight, with exactly one additional graceful restart during the soak (separate from the deployment restart) and no membership changes. Confirm no PyTap ERROR-level logs, daily reset behavior, and overnight hold behavior. Record the total aggregate immediately before the soak restart and after the first settled post-restart report; post-restart total must be greater than or equal to pre-restart. After that restart, take a settled, nonzero end-of-production-day sample before the next midnight and a quiet sample after midnight. Compare unformatted numeric HA state values for each string/installation daily aggregate against the sum of corresponding optimizer daily-energy states; each difference must be at most 0.1 Wh.
13. **Release:** only after the soak passes and the change is merged, create a new bare `1.4.0` tag/release using the repository's existing tag format, then confirm the uploaded `pytap.zip` manifest and `manifest.json` report version 1.4.0.

## Implementation boundaries

- The user explicitly approved this exact v8 for implementation. Complete its feasibility gates before porting code, dependency, infrastructure, or live-HA changes; approval does not waive any gate or authorize the declined backup/restore actions.
- After approval, complete the HA 2026.10.4/2026.11.2 environment and initial schema gates before porting. A failed gate returns to the user before other implementation proceeds. After slices 02–04 and metadata are integrated, complete criterion 10's exact-#39 rollback proof before deployment; if it fails, stop and ask for a new decision.
- After approval, use TDD for each behavior fix: add a focused regression test, confirm it fails for the intended defect, implement the smallest correction, and rerun the targeted tests before broader validation.
- The current worktree is at PR #35 and contains uncommitted changes in `CHANGELOG.md`, `custom_components/pytap/manifest.json`, `custom_components/pytap/pytap/core/parser.py`, `custom_components/pytap/sensor.py`, `docs/architecture.md`, `docs/implementation.md`, `tests/test_review2_data.py`, and `tests/test_sensor.py`. Treat these as source material: port the relevant approved v7 fixes onto PR #39; do not blindly preserve the PR #35 diffs, reset, or discard them. Preserve `.copilot-memory/`, `docs/prd-1.4.0-robust-tcp-core.md`, this v8 draft, and `docs/kanban.md`.
- Do not take the explicitly declined fresh backup before replacing #39, and do not restore the pre-PR backup. Any other backup or restore action requires a new user decision. Complete criterion 9's metadata and documentation updates before candidate deployment. Do not restart or replace the live integration until integrated validation and the rollback proof pass and the v7 schema is reverified.
- The deployment restart is separate from the single graceful restart inside the soak. The soak is not started until the candidate passes the minimum/current-version checks and is confirmed active on the v7 entry with readings. Release/tag work follows soak acceptance and merge.
