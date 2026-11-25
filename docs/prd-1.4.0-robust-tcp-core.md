# PyTap 1.4.0 — Robust TCP Core

**Status:** Approved v7

**Stable reference:** PRD-PYTAP-1.4.0-001, v7

**Scope basis:** Slice 1 decisions, complete multi-optimizer midnight accounting, the accepted restore-skew risk, and the user's decision to run the hardware soak on the current dev-box state without restoring the pre-PR backup.

**Change from approved v6:** The pre-PR backup restore is removed from the hardware-soak prerequisite because this is a dev box. The soak still validates recovery from the current persisted state across a graceful restart, but not recovery starting from the pre-PR backup; targeted restore edge cases remain covered by automated tests.

## Confirmed Decisions

- Keep the declared Home Assistant minimum at 2025.3 and config-entry version at v5; no migration is needed.
- Document `reporting_count` as current reporting activity, including the stale-timeout plus reporting-interval allowance in averaged mode.
- Correct aggregate energy accounting for nonnegative member-counter resets and daily rollovers, including mixed-day multi-optimizer reports, both midnight event orders, and temporarily omitted members.
- Keep exact post-reset credit on nonnegative total and daily member-counter decreases, accepting and documenting the rare case where a stale coordinator restore can make either decrease look like a reset.
- On restore, discard baselines for members no longer in the aggregate and roll daily state to the current HA-local date even before any current member reports.
- For unattached per-optimizer and aggregate sensors, resolve `_timezone()` from the coordinator's configured Home Assistant timezone; this governs `last_reset` and `_today()` where present.
- Keep the intentional first-reading rebase for a genuinely new/late-joining optimizer and document it.
- Correct the retrospective 1.3.0 changelog entry and add 1.4.0 release notes.
- Release this slice as 1.4.0.
- Because the target is a dev box, skip restoring the pre-PR backup and run the hardware soak against its current HA state. The soak validates a graceful restart from current persisted state, not recovery starting from the pre-PR backup.

## Verified Facts

- On the merged main tree, a member total-counter decrease is rebased without crediting the member's post-reset value. Daily report-first handling can also lose or misattribute energy when a new-day snapshot is compared with yesterday's baselines or contains mixed reset dates.
- Aggregate baselines and coordinator member totals are persisted separately. If the coordinator stops before its delayed save, its restored member total can be slightly lower than the aggregate's restored baseline; the user chose to retain exact reset credit for total and daily counters and accept this rare overcount risk because the current state has no reliable discriminator.
- On reload, restored baselines can still contain removed barcodes, and a restored daily aggregate's cycle date may remain stale until a current member has an energy value to trigger an update.
- Unattached per-optimizer and aggregate entities currently fall back to UTC instead of the coordinator's HA timezone; this can misalign daily-cycle dates or `last_reset` timestamps.
- Callback-first midnight behavior already works and must remain a regression check, not an artificially failing pre-fix test.
- `origin/main` is config-entry v5 and declares HA 2025.3; `manifest.json` still says 1.2.0. The published `1.3.0` build at `d9960ae` is v5, while tag `v1.2.0` is v4.
- `README.md` and `hacs.json` already agree on the 2025.3 floor on `origin/main`; `docs/architecture.md` and `docs/implementation.md` need review for stale behavior and version statements.
- In averaged mode, `reporting_count` allows the configured write interval in addition to the stale timeout. It may be zero overnight or after restart when no recent reports exist, even in hold mode.
- The changelog's current `[Unreleased]` entries include behavior already shipped in 1.3.0; that release has no changelog section. The latest tag format is `1.3.0` without a `v` prefix.
- The parser indentation finding is cosmetic and does not prevent parsing.

## Constraints and Edge Cases

- **Problem and users:** PyTap users need accurate optimizer, string, and installation energy totals through optimizer resets and midnight; trustworthy reporting diagnostics; and compatibility with the declared HA floor.
- **Goals and measures:** Preserve energy through resets, event ordering, stale-day readings, incomplete snapshots, reloads, and timezone boundaries, subject to the explicitly accepted rare restore-skew risk for both total and daily counters. Clarify reporting-count and late-join semantics. Keep the 1.3.0 and 1.4.0 release records and artifacts internally consistent.
- **In scope:** Add focused aggregate-energy regression tests and the minimum code change that satisfies the invariants below; on restore, prune removed-member baselines and roll daily state to the current HA-local date before the first report; resolve `_timezone()` for unattached `PyTapSensor` and `PyTapAggregateSensor` from the coordinator's HA timezone, governing `last_reset` and `_today()` where present; update `docs/architecture.md`, current-version statements in `docs/implementation.md`, and directly relevant code comments while preserving historical milestones; correct the parser indentation; verify README/HACS version claims remain aligned; move already-shipped changelog notes to a retrospective 1.3.0 section and add 1.4.0 notes including baseline/restart and `last_reset` behavior; bump `manifest.json` to 1.4.0.
- **Out of scope:** A new persistence protocol or heuristic to distinguish true counter resets from stale restored counters; new transports, discovery, packet capture, panel-health/binary sensors, precision/`last_update` changes, config-entry schema changes, and unrelated refactoring.
- **Accepted risk:** Exact reset credit for either total or daily counters can overcount if HA restores an aggregate baseline newer than a coordinator member value after an abrupt stop before the delayed store save. The false-reset overcount may be as large as the restored member-counter value—potentially that member's lifetime total for total energy or its current-day total for daily energy. The observed data has no reliable discriminator; do not introduce a guessed threshold. The production soak covers a graceful HA restart; abrupt stops are outside its acceptance criteria.
- **Soak limitation:** At the user's direction, use the current HA dev-box state without restoring the pre-PR backup. The soak still validates a graceful restart from current persisted state; it does not validate recovery starting from the pre-PR backup. Targeted restore edge cases remain covered by automated tests.
- **Dependencies:** Before implementation, update this worktree to merged `origin/main` without overwriting the untracked `.copilot-memory/` journal. No membership changes are allowed during the soak.
- **Open questions:** None identified for this slice.

## Implementation Boundaries

1. Use TDD. Tests that reproduce a nonnegative member-counter decrease and report-first rollover—including a new-day value equal to or above yesterday's baseline, mixed reset dates across multiple optimizers, a temporarily omitted configured optimizer, and restore with no current node data—must fail before the fix and pass afterward. Callback-first ordering, normal increases after a reset, genuine late-join rebasing, and removed/re-added membership are required non-regression tests; they need not fail before the fix.
2. Accounting invariants:
   - For a same-cycle member total or daily-counter decrease with `0 <= value < previous`, credit exactly `value` as energy since reset, rebase to `value`, and count subsequent increases by their delta only. Do not credit negative values or double-count the pre-reset total. This exact-credit rule has the accepted stale-restore overcount risk described above.
   - On every daily-aggregate update—including initial seeding, cycle-date rollover, restore, and subsequent updates within the same cycle—do not add a member's reading unless its `daily_reset_date` matches the current cycle date. A current-day reading contributes its full current-day amount even if it is greater than yesterday's baseline; stale prior-day readings contribute nothing to today's aggregate. Update static date fixtures in affected tests to match the test's HA-local cycle date.
   - Reset daily contributions/baselines for current members at rollover and when restoring a stale cycle date. Retain a current member's baseline if it is temporarily absent from a snapshot, but discard baselines for members removed from the aggregate's membership. If a current member is present with a stale `daily_reset_date`, skip that reading but retain a zero baseline for the current cycle so its first matching current-day reading is credited in full. Only a genuinely new or re-added membership uses the intentional initial rebase without credit.
3. Before any current member has node data, restored energy must still prune removed-member baselines and advance a stale daily cycle to the HA-local current date, setting its total and tracked baselines to zero while preserving restored availability.
4. When unattached, both `PyTapSensor` and `PyTapAggregateSensor` resolve `_timezone()` from the coordinator's configured HA timezone; this governs `last_reset` and `_today()` where present. Test daily reset/restore and reset timestamps around a local-midnight boundary.
5. Run repository CI and `pytest tests/` in a one-off Python 3.13 environment pinned to Home Assistant 2025.3.x with a compatible pinned `pytest-homeassistant-custom-component`. Record the resolved test-package version and command. A permanent CI job is not required for this slice.
6. Using the current HA dev-box state without restoring the pre-PR backup, run a soak of at least 24 hours covering midnight and one graceful Home Assistant restart. Abrupt stops are outside this soak's acceptance criteria. With no membership changes, confirm no PyTap ERROR-level log entries, daily energy resets, and hold mode works overnight. Record the numeric total aggregate immediately before restart and after the first settled post-restart report; the post-restart value must not be less than the pre-restart value. Take both a settled, nonzero end-of-production-day sample after restart and before the next midnight, and a quiet sample after midnight. At each sample, compare unformatted numeric HA state values for every string/installation daily aggregate with the sum of its corresponding optimizer daily-energy states; each difference must be at most 0.1 Wh. This soak validates a graceful restart from current persisted state, not recovery starting from the pre-PR backup.
7. Keep config-entry v5. Verify a code-only rollback using the 1.3.0 build at `d9960ae` loads a v5 entry; a rollback to `v1.2.0` requires restoring the matching `.storage` backup because that build is v4.
8. Publish the exact git tag `1.4.0` (following the latest `1.3.0` tag format). Confirm both `manifest.json` and the manifest inside the release `pytap.zip` report 1.4.0.
