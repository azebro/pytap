# PRD-PYTAP-1.4.0-001 v9 r14 - Integrated candidate evidence

**Status: the corrected candidate failed live energy-continuity checks and was code-only rolled back to exact #39.**
The graceful-restart code correction and approved automated gates passed, and
the candidate was deployed on 2026-11-26 after fresh daylight reports matched
the live v7 membership. Its first settled reports exposed large persistent
aggregate/member differences and an unexpected cumulative-total increase.
Exact #39 was restored; HA state was not manually repaired. The production
energy behavior remains unresolved and the feature is not release-ready.
The [delivery board](../kanban.md) owns task status.

The original results cover the integrated 1.4.0 candidate, not the historical
476-test baseline or the separate [pre-port environment evidence](prd-pytap-1.4.0-001-v9-r14-preport.md).
That candidate was committed locally on `azebro-legendary-spork` as
`3ea5aedc6544f59dd91f3fb4b9950bad916dcfd8`, above merge commit
`66ce47833034b338f862ae904d92b774abfe9fc2`.
Exact PR #39 is `a80698fca67bef4847a3c6ce5217f163fee94c01`; a fresh
`git cat-file -e 'a80698fca67bef4847a3c6ce5217f163fee94c01^{commit}'`
succeeded before the original artifact was frozen. No successful soak, release
or push is claimed.

## Failed live gate and recovery

On 2026-11-25 the candidate was deployed with checked Core stop/start, unchanged
v7 membership and fresh production readings. Its soak began at 15:31:10 UTC.
The required additional graceful restart completed at 15:33:44 UTC, after which
two settled samples showed daily excesses of 283.60 Wh and 432.08 Wh for the
strings, and 715.68 Wh for the installation. The installation cumulative total
increased by 253,156.87 Wh across the immediate restart comparison.
Membership remained unchanged; no PyTap ERROR/CRITICAL records were found.
The soak clock is invalid, not paused for later resumption.

The user selected code-only rollback to exact #39, followed by correction and
revalidation. Rollback completed at 17:12:32 UTC; native confirmation at
17:15:08 UTC showed manifest 1.2.0 on the unchanged Core 2026.11.2 and v7 entry.
Existing inflated stored totals were not repaired. That nighttime confirmation
did not prove fresh production. No backup was made or restored.

## Corrected-candidate live failure and rollback

The corrected archive was staged in a new private run on 2026-11-25. Daylight
reports returned on 2026-11-26. At 11:00:52 UTC, exact #39 reported all 26
configured members; all three aggregate-versus-optimizer daily comparisons
were 0 Wh, with installation daily energy 1,250.36 Wh and cumulative energy
2,436,050.30 Wh. Membership fingerprint was
`e42b73f2bb1d90ade3248ec9a11327b03d3728480e3d5f6135d53a5ed3c63bc1`.
Core 2026.11.3 had matching native/config versions.

The guarded deployment completed at 11:02 UTC after the helper was updated to
require the smoke-validated Core 2026.11.3; its focused unit suite passed
17 tests. The candidate reported manifest 1.4.0 on the same v7 membership.
At 11:02:42 UTC all 26 members were reporting, but aggregate/member differences
remained 254.55 Wh for string C, 228.38 Wh for string D and 482.93 Wh for the
installation, versus the 0.1 Wh acceptance bound. Installation daily energy
was 1,765.38 Wh, while member daily values summed to 1,282.45 Wh. Cumulative
installation energy was 3,257,311.31 Wh, up 821,261.01 Wh from the immediate
pre-deployment baseline. The candidate's first snapshot briefly showed 24/26
members reporting; this recovered to 26/26, but the energy discrepancies
persisted. Recent Core logs contained no PyTap ERROR/CRITICAL entries; this
does not negate the measured accounting failure.

The helper performed a code-only rollback. At 11:03:18 UTC, native diagnostics
confirmed manifest 1.2.0, Core 2026.11.3, all 26 members reporting and
unchanged membership. The 254.55/228.38/482.93 Wh differences remained in
stored state after rollback. No backup restore, energy-state repair, or soak
restart was performed. Keep the failed candidate and run as evidence. Root
cause of the live discrepancy is not yet established; do not treat the
automated pass or rollback as resolution.

The current code trace identifies a plausible path, not a confirmed cause:
`PyTapAggregateSensor._accumulate_energy` credits the current counter value
when a member value is lower than its saved baseline, preserving legitimate
counter-reset energy. That behavior can produce a positive aggregate/member
difference when a lower restored value is stale rather than a real reset, a
specific accepted accounting risk in the approved PRD. The Nov 26 snapshots
include all 26 per-member daily values: none is lower than its pre-deployment
value in either the first settled or follow-up sample. Read-only Recorder
history fills the transition between those snapshots: at 11:01:56 UTC, ten
member daily counters fell by a combined 0.86 Wh. Their prior values summed to
482.93 Wh and their lower values to 482.07 Wh. At that same timestamp the
string C aggregate moved from 654.48 to 909.94 Wh, string D from 610.27 to
839.73 Wh, and installation from 1,264.75 to 1,749.67 Wh. The first settled
sample at 11:02:05 already had the final 482.93 Wh member/aggregate gap. Its
size exactly matches the sum of the ten prior member values, confirming that
the aggregate reset-credit path explains the daily overcount.

The same ten lifetime counters fell by a combined 0.81004 Wh; their previous
Recorder values summed to 737,197.80526 Wh. The installation lifetime
aggregate jumped from 2,436,064.70 to 3,257,295.65 Wh at 11:01:56
(+821,230.95 Wh). A direct read-only query of the live HA Recorder on
2026-11-27 covered all 26 optimizer lifetime entities and the three aggregate
entities immediately before Core start (11:01:52 UTC) and at their first
post-start update. It found ten member decreases; all 26 had a recorded pair.
Using the existing aggregation rule (for a decrease add the post-reset
reading; otherwise add the positive increment) gives:

| Group | Members | Decreases | Aggregate increase (Wh) | Expected from member states (Wh) | Unexplained excess (Wh) |
|---|---:|---:|---:|---:|---:|
| String C | 13 | 5 | 428,585.51000 | 344,554.40522 | 84,031.10478 |
| String D | 13 | 5 | 392,645.44000 | 392,645.44000 | 0 |
| Installation | 26 | 10 | 821,230.95000 | 737,199.84522 | 84,031.10478 |

The member total increased by 2.03996 Wh in this interval; ordinary
non-decreasing readings contributed 2.85000 Wh, while decreased readings
contributed -0.81004 Wh to the member sum. The prior-decrease-value sum
(737,197.80526 Wh) subtracted directly from the aggregate jump produced the
earlier 84,033.14 Wh raw remainder. Accounting for the ordinary member
increments refines the unexplained aggregate-vs-member excess to
84,031.10478 Wh, entirely in string C. String D now reconciles to the member
transitions. The direct query establishes the excess is not caused by an
unseen member lifetime entity or an omitted decrease in the queried interval;
it does not identify which persisted aggregate baseline or other state caused
the string C excess.

The follow-up grouped query on the same Recorder window returned C member
sums of 1,042,699.09526 → 1,042,700.03522 Wh, against a string C aggregate
change from 1,210,794.75 → 1,639,380.26 Wh; D member sums were
1,056,180.79 → 1,056,181.89 Wh, against a string D aggregate change from
1,225,269.95 → 1,617,915.39 Wh. The `c1` lifetime row was 84.11526 Wh at
11:01:36 and 84.11522 Wh at 11:01:56 UTC, so that member did not make an
84,031 Wh jump. These are Recorder states, not the missing persisted
aggregate baseline map or the original power-report payloads.

**Correction (2026-11-27 19:33 UTC): the string C excess was a unit error in
this analysis.** A read-only Recorder attribute query shows
`sensor.tigo_ts4_c1_total_energy` is recorded in **kWh** (a display-unit
override), while every other member and all aggregates are in Wh. `c1` was
therefore 84,115.26 → 84,115.22 Wh, a 0.04 Wh decrease. The "unexplained"
84,031.10478 Wh equals exactly 999 × 84.11522. With `c1` in Wh, string C's
expected aggregate increase is 428,585.51000 Wh, matching the observed
increase exactly; the installation also reconciles exactly. **The whole
Nov 26 daily and lifetime divergence is explained by one mechanism:** ten
members' lifetime counters restored 0.04–0.16 Wh below their last published
values at the #39→candidate restart, and the aggregate's reset rule credited each one's
entire post-decrease value as new energy. The same mechanism explains the
Nov 25 failure. The historical baseline map is no longer needed for
attribution. The tables above that list an unexplained excess are superseded
by this correction.

Per-member Recorder pairs for that transition (last state before 11:01:52 UTC, first state at or after it; read-only query 2026-11-27, unformatted states converted to Wh). These values are the r15 criterion 5a fixture. Replaying the current rule (`v < b` credits `v`) on them reproduces every observed aggregate jump exactly: string C daily +255.46 Wh and lifetime +428,585.51 Wh; string D daily +229.46 Wh and lifetime +392,645.44 Wh. The r15 rule would credit C +1.39/+1.38 Wh and D +1.46/+1.47 Wh (daily/lifetime) instead. The largest dip is 0.16 Wh.

| Member | Daily before (Wh) | Daily after (Wh) | Lifetime before (Wh) | Lifetime after (Wh) |
|---|---:|---:|---:|---:|
| c1 (Recorder kWh ×1000) | 48.82 | 48.78 | 84115.26 | 84115.22 |
| c2 | 53.94 | 54.16 | 94330.9 | 94331.11 |
| c3 | 54.07 | 54.33 | 93382.21 | 93382.47 |
| c4 | 54.7 | 54.9 | 90021.26 | 90021.46 |
| c5 | 48.42 | 48.42 | 83342.74 | 83342.74 |
| c6 | 47.59 | 47.76 | 85798.26 | 85798.43 |
| c7 | 56.0 | 55.89 | 95523.24 | 95523.13 |
| c8 | 53.01 | 53.21 | 90508.67 | 90508.87 |
| c9 | 52.94 | 53.14 | 89915.78 | 89915.98 |
| c10 | 53.91 | 53.75 | 88957.5 | 88957.34 |
| c11 | 48.33 | 48.24 | 83135.2 | 83135.11 |
| c12 | 35.26 | 35.4 | 70845.81 | 70845.95 |
| c13 | 47.49 | 47.41 | 76853.41 | 76853.33 |
| d1 | 47.93 | 48.11 | 83412.79 | 83412.97 |
| d2 | 36.47 | 36.43 | 76564.82 | 76564.78 |
| d3 | 47.78 | 48.0 | 84916.17 | 84916.39 |
| d4 | 49.27 | 49.19 | 83167.46 | 83167.38 |
| d5 | 46.95 | 47.13 | 76384.98 | 76385.16 |
| d6 | 48.86 | 49.03 | 86247.5 | 86247.68 |
| d7 | 47.5 | 47.67 | 84606.72 | 84606.89 |
| d8 | 38.19 | 38.34 | 78807.27 | 78807.42 |
| d9 | 39.04 | 38.96 | 78530.9 | 78530.83 |
| d10 | 47.59 | 47.77 | 76350.17 | 76350.35 |
| d11 | 47.24 | 47.16 | 68358.76 | 68358.68 |
| d12 | 56.36 | 56.26 | 86022.4 | 86022.3 |
| d13 | 57.09 | 57.3 | 92810.85 | 92811.06 |

The Recorder does not retain the aggregate's `extra_restore_state_data`
baseline map as historical state attributes. The current `.storage` restore
file reflects later state and no pre-restart baseline snapshot was retained
in the reviewed evidence. A search of the retained session evidence found no
`extra_restore_state_data`, `core.restore_state`, or `baselines` payload. The
Nov 26 cutover record contains aggregate totals/groups and member readings,
but not the aggregate's persisted per-member baseline map. The history
confirms the reset-credit path explains
the daily overcount and most lifetime accounting, but does not prove why the
string C aggregate credited a further 84,031.10478 Wh. A cross-version
reproduction with the exact pre-restart aggregate baselines is still needed.
A synthetic two-member unload/final-save case passes before a native Core
restart on HA 2026.10.4 and 2026.11.3, but does not reproduce the exact
#39-to-candidate transition. Do not remove reset credit, mask the difference
with a tolerance, or repair live statistics.

For the Nov 25 failure, read-only recorder history identified the trigger:
five daily and three cumulative optimizer counters restored 0.01 Wh lower,
immediately before the aggregate credited them as resets. Correcting
persistence must preserve legitimate reset accounting, not hide decreases
with a numerical tolerance.
The original archives and failed run remain evidence, not deployment defaults
for the corrected candidate.

Private, machine-local evidence includes `pytap-live-soak.json`,
`pytap-restart-energy-history-20261125T153230-153530.json`,
`pytap-live-operation-rollback-20261125T171232Z.json` and
`pytap-live-snapshot-rollback-native-confirmation-2026-11-25T171508.389956Z0000.json`.

## Restart correction: focused evidence

Native HA registers executor futures created by its background tasks for
shutdown cancellation. PyTap used that helper inside its background listener,
so HA cancelled both the wrapper and the worker future before firing its stop
event. Awaiting that cancelled future raised `CancelledError` and bypassed
the existing final coordinator save. Shielding the waiter alone did not prevent
HA from cancelling the future directly.

The correction keeps the completion future from the public event loop's
default executor, while retaining the existing background wrapper, explicit
interrupt, bounded join, port ownership and tracked final-save task. Aggregate
reset arithmetic and persisted schemas are unchanged.

| Focused case | Before correction | After correction |
|---|---|---|
| `test_native_shutdown_flushes_energy_processed_by_cancelled_worker` | Final Store save awaited zero times; failed in 3.20 s | Included in 91 passing lifecycle/persistence tests, 18.63 s |
| `CoreSmoke.test_graceful_stop_persists_latest_energy_before_native_restart` | Real native restart restored 0.01 Wh instead of 0.02 Wh; failed on HA 2026.11.2 in 2.067 s | Passed on HA 2026.10.4 in 1.319 s and HA 2026.11.2 in 1.324 s |

The native case uses real Core shutdown, coordinator accumulation, Store
persistence and a fresh native instance with real aggregate entities. Its
controlled listener supplies the final report during shutdown; it does not
manually save that latest state or substitute the final-save path.
These focused results are not the required complete-suite or live gates.

Private logs are `restart-persistence-red-lifecycle.log`,
`restart-persistence-native-red.log`, `restart-persistence-green-focused-suite.log`,
`restart-persistence-native-green-2026.10.4.log` and
`restart-persistence-native-green-2026.11.2.log`. Fresh dependency checks and
real public imports also passed on both unchanged environments, recorded in
`restart-runtime-minimum-check.log` and `restart-runtime-current-check.log`;
the current-Core environment still has no pytest harness.
Claude Opus 5.5 and GPT-6.1 Sol independently accepted the production correction
and its root-cause evidence with no material production findings. Both noted
a midnight-sensitive fixture and unbounded readiness waits. Their additional
test suggestions distinguish joining the worker before saving from saving
early, and durable aggregate restore from reconstruction using member values.
The regression proof now includes a deterministic worker barrier: native
shutdown remains pending, and final Store persistence has not started, until
the listener worker is released. The separate native restart/restore case
continues to exercise real Core shutdown and a fresh integration instance.
Timezone restoration and the public restored aggregate checks are also
covered. No aggregate arithmetic, persisted schema, or dependency changed.

The final smoke harness performs bounded ESPHome/PyTap test cleanup: it waits
for pending HA tasks, unloads entries left loaded by a case, and asserts that
the coordinator listener and worker futures are cleared. This cleanup addresses
cross-case C8 call leakage; it is not native-shutdown proof and does not replace
the separate restart/restore regression. Main checked the captured prior C8
failure logs for `PyTap shutdown timed out` and `PyTap listener did not stop`;
neither marker was present.

The complete minimum-Core suite, parser suite and strict C1-C8 smoke gates have
now passed after these test refinements. Both final read-only reviewers
accepted the production correction and test proof with no remaining material
findings. The optional, unverified capture-preparation cancellation concern
remains in task PYTAP-140-09, outside this correction.

The WSL distro restarted and removed its temporary virtual environments after
the initial worker handoff. Main recreated both environments in a unique
temporary location using Python 3.14.4 and the official constraint files whose
SHA-256 values match the table above. No system packages or repository
dependencies were changed. `prepare_ha_tests.py` and its real-import checks
pass on both restored environments; the minimum retains the pinned test
harness and serialx 1.11.0, while the current-Core environment has no harness
and serialx 1.15.1. Main independently reran the focused lifecycle/persistence
selection (91 passed in 16.66 s) and the new native restart/restore case on
HA 2026.10.4 (1 passed in 1.680 s) and 2026.11.2 (1 passed in 1.958 s).
These were targeted proof runs, not criterion 10's complete gates; final
corrected-candidate results are recorded below.

## Original frozen code artifacts

The following archives are local preparation artifacts, not published release
assets or live backups. Each contains 37 component files with `manifest.json`
at its root, matching the release workflow's layout. Every archived byte was
compared with its source; ZIP integrity checks passed.

| Artifact | Manifest | SHA-256 |
|---|---|---|
| `pytap-1.4.0-candidate-20261125T124456Z.zip` | 1.4.0 | `64939fabff67dc4be0fc949d674f549ce75def97fc7eded6b6aabdf3f22facf5` |
| `pytap-pr39-a80698fca67b-20261125T124456Z.zip` | 1.2.0, exact PR #39 source | `a1f6ec7fe09685fedc21a45a81d33b21211f2d3659b5b1f80f8492e668c24963` |

Candidate source-tree SHA-256 is
`80c7ec70144db39004109ad716d9e78429f7ad2a83866eabeb72b1b367817a7f`.
It hashes sorted component-relative names, a NUL separator, each file's SHA-256
hex digest and a newline. Production source did not change during the later
native-test refinements. Machine-local
`pytap-deployment-artifacts-20261125T124456Z.json` records both archive identities.

## Corrected candidate archive

The corrected component-only candidate was frozen after the production and
regression-test changes. Its 37 archived files were byte-compared with the
component source, `manifest.json` is at the archive root, and ZIP integrity
passed. This is a new artifact; the original failed candidate remains preserved
with its original hash and is not a deployment default.

| Artifact | Manifest | SHA-256 |
|---|---|---|
| `pytap-1.4.0-corrected-candidate-20261125T191343Z.zip` | 1.4.0 | `80190ac85ad672bcf7ede0a2142f2fe32364ad7a6d1c3298d068461f1dd7de13` |

Corrected candidate source-tree SHA-256 is
`134f197bd111c90dabba054304a59d34203644b68ea0ae990ff4c32cfd4c08a4`.
The machine-local metadata now points to this archive and preserves the exact
PR #39 rollback archive identity. The private control helper's defaults and
remote candidate hash pin were updated. Its 17 focused tests passed after
updating the exact-Core guard for the validated live Core 2026.11.3. The helper
staged this candidate and exact-#39 rollback archive in a new private run
recording source commit `06b5461`; it deployed the candidate on 2026-11-26,
then performed a guarded code-only rollback after live accounting failed.
Detailed results are in "Corrected-candidate live failure and rollback".

The 2026-11-25 19:19:16 UTC pre-deployment snapshot reports manifest 1.2.0,
Core 2026.11.3 with matching configuration, and 26 configured entries. Its
membership fingerprint matches the 17:15 rollback confirmation. However,
`reporting_count` is zero and both energy and node data are last updated at
16:28:22 UTC. The three group differences remain 283.60, 432.08 and 715.68 Wh,
with zero groups within 0.1 Wh. This is a fresh snapshot, not a fresh energy
baseline; deployment remained gated at that time. Read-only checks at 19:22:21
and 19:25:43 UTC returned the same latest-report timestamps, zero reporting
entities and group differences. Daylight reports resumed the next morning;
the fresh baseline and subsequent candidate failure are recorded above. The
stored totals were not repaired.

The live host's Core version has advanced from the validated 2026.11.2 to
2026.11.3. An isolated strict native smoke run using the 2026.11.3 official
constraints passed 31/31 with zero skips, failures or errors in 144.979 s.
This additional compatibility result cleared the current-Core automated test
gate. The later corrected-candidate deployment nevertheless failed live energy
acceptance, as detailed above.

## Candidate validation environments

Both are isolated Linux virtual environments under WSL, not the remote
pre-port environments. Preparation used `scripts/prepare_ha_tests.py` with the
matching official Core constraints. No forced dependencies, module stubs,
package-metadata patches or vendor fixes were used.

| Fact | Minimum Core | Current Core |
|---|---|---|
| Core | 2026.10.4 | 2026.11.2 |
| Platform | Linux 6.18.58.1-microsoft-standard-WSL2, x86_64 | Same |
| Python | 3.14.4 | 3.14.4 |
| serialx | 1.11.0 | 1.15.1 |
| Pydantic | 2.13.5 | 2.14.1 |
| pytest-homeassistant-custom-component | 0.13.375 | Absent |
| Unique distributions, including pip | 171 | 128 |
| Official constraints SHA-256 | `7260404c34adcc5aac1fb4e7384f989292cfce0e30e3a7a46e18383f34a8059f` | `d7f1647f03e911b19e1e7bef48edae2765156374a5c0860bc427a00303401e30` |
| Fresh `python -m pip check` | Exit 0: no broken requirements | Exit 0: no broken requirements |

Constraints originate at
`https://raw.githubusercontent.com/home-assistant/core/<version>/homeassistant/package_constraints.txt`.
Preparation compared their bytes with the installed Core's own constraints.

Closure seeds are `esphome`, `usb`, `zeroconf`, and `assist_satellite`, following
both `dependencies` and `after_dependencies`. Both closures contain 21
components: api, assist_pipeline, assist_satellite, bluetooth, conversation,
esphome, ffmpeg, hassio, http, intent, llm, media_player, network, repairs, stt,
tag, tts, usb, wake_word, websocket_api, zeroconf.

All 22 direct manifest requirements resolved to their exact declared pins:

| Distribution | HA 2026.10.4 | HA 2026.11.2 |
|---|---|---|
| aioesphomeapi | 46.6.0 | 46.25.0 |
| aiohasupervisor | 0.6.0 | 0.8.1 |
| aiousbwatcher | 1.1.2 | 1.1.2 |
| bleak-esphome | 4.0.0 | 4.0.0 |
| bleak-retry-connector | 4.7.1 | 4.7.1 |
| bleak | 3.0.2 | 3.0.2 |
| bluetooth-adapters | 2.4.0 | 2.4.0 |
| bluetooth-auto-recovery | 1.6.4 | 1.6.4 |
| bluetooth-data-tools | 1.29.24 | 1.29.24 |
| dbus-fast | 5.0.22 | 5.0.22 |
| esphome-dashboard-api | 1.4.0 | 1.4.0 |
| gazetteer-matcher | 1.1.0 | 1.1.0 |
| ha-ffmpeg | 3.2.2 | 3.2.2 |
| habluetooth | 7.1.2 | 7.1.2 |
| hassil | 3.12.1 | 3.12.1 |
| home-assistant-intents | 2026.10.6 | 2026.10.27 |
| ifaddr | 0.2.0 | 0.2.0 |
| mutagen | 1.48.1 | 1.48.1 |
| pymicro-vad | 1.0.1 | 1.0.1 |
| pyspeex-noise | 1.0.2 | 1.0.2 |
| serialx | 1.11.0 | 1.15.1 |
| zeroconf | 0.151.5 | 0.152.0 |

Preparation imported real `gazetteer_matcher`, ESPHome, its `serial_proxy`,
Assist Satellite, USB's `serial_proxy_stub`, and `homeassistant.helpers.selector`.
Their origins resolved to their corresponding package files under that
environment's `site-packages`, not repository substitutes.
`SerialPortSelector` was callable. `register_serialx_transport` and minimum
Core's `build_url`, or current Core's `build_port_url` and `identity_port_info`,
resolved to the real `homeassistant/components/esphome/serial_proxy.py`.

## Original candidate integrated runs

Commands below are portable equivalents of the recorded WSL invocations,
executed from the Git root. `PY_MIN` and `PY_CURRENT` denote the corresponding
environment's interpreter; their machine-local paths are not shared records.

| Gate | Command | Result |
|---|---|---|
| Minimum pytest, first run | `"$PY_MIN" -m pytest tests -q --tb=short --show-capture=no` | 519 passed, one diagnostics-fixture failure, 110.52 s |
| Minimum pytest, approved rerun | Same command | 520 passed, 109.33 s, exit 0 |
| Minimum parser | `PYTHONPATH=custom_components/pytap "$PY_MIN" -c 'import pytap, pytest; raise SystemExit(pytest.main(["custom_components/pytap/pytap/tests", "--import-mode=importlib", "-q", "--tb=short"]))'` | 73 passed, 3.39 s, exit 0 |
| Minimum strict smoke, including C7 | `"$PY_MIN" scripts/run_core_smoke.py` | 30 tests passed, 86.832 s, exit 0 |
| Current strict smoke, including C7 | `"$PY_CURRENT" scripts/run_core_smoke.py` | 30 tests passed, 130.807 s, exit 0 |
| Current full pytest/parser | Not run | Intentionally excluded by the approved harness/Pydantic split |
| Metadata/translations | Parse manifest, constants, HACS metadata and both translation JSON files | Integration and manifest 1.4.0; minimum HA 2026.10.4; seven required P1/P9/capture entries match |
| Ruff and whitespace | `python -m ruff check custom_components/pytap tests scripts`; `git diff --check` | Both exit 0 after proof corrections |

The first pytest failure occurred before diagnostics execution: its synthetic
ESPHome URL lacked the now-required `port_name` on HA 2026.10.4. The user
explicitly approved adding that field and rerunning validation. Only the test
URL changed; production validation and every redaction assertion were retained.

Worker smoke runs reporting 29 passes per Core and targeted C7 runs reporting
three passing scenarios per Core are preliminary, not final gate acceptance.
The first full 30-test runs passed in 92.284 s / 131.139 s before the final
C4/C8 assertion refinements. The final reruns above execute those refinements.
The runner requires
more than zero tests and rejects failures, errors, skips, expected failures and
unexpected successes; both final runs satisfy all those checks.

### Executed native case mapping

All names below are in `tests/core_smoke.py` and ran on both Cores. Version
expectations differ inside the relevant cases; no cases are skipped.

| Criterion | Named tests |
|---|---|
| C1, original seven smoke tests | `test_async_transport_bridge_on_supported_core`, `test_capture_lifecycle_on_supported_core`, `test_empty_auto_setup_and_discovery_on_supported_core`, `test_native_config_flow_serial_picker_and_entry`, `test_native_migration_and_options_preserve_entry`, `test_native_proxy_stub_is_retryable_and_cleans_up`, `test_real_serial_transport_releases_virtual_uart` |
| C2, selectors | `test_native_config_flow_serial_picker_and_entry`, `test_c2_options_serial_schema_uses_public_selector` |
| C3, manifest and official constraint | `test_c3_manifest_accepts_the_running_serialx_pin` |
| C4, accepted/mismatched URL classes and builders | `test_c4_acceptance_equivalence_classes`, `test_c4_all_supplied_matchers_are_enforced`, `test_c4_backend_entry_errors_are_typed_and_retryable`, `test_c4_canonical_builder_branches`, `test_c4_esphome_name_url_opens_real_handler` |
| C4, prevalidation | `test_c4_ordinary_url_grammar_is_unchanged`, `test_p1_prevalidation_is_version_specific`, `test_p9_prevalidation_decodes_and_precedes_p1` |
| C5, TCP and local serial | `test_c5_real_tcp_transport_opens_and_releases`, `test_real_serial_transport_releases_virtual_uart` |
| C6, migration and stored URLs | `test_native_migration_and_options_preserve_entry`, `test_c6_invalid_stored_settings_are_fatal_setup_errors`, `test_c6_version_unsupported_stored_urls_are_actionable`, `test_c6_v7_and_version_supported_urls_open_without_downgrade` |
| C6, background retry | `test_c6_backend_failures_stay_on_background_retry`, `test_c6_matcher_failures_stay_on_background_retry`, `test_c6_tcp_failures_keep_background_retrying` |
| C7, exact-#39 rollback | `test_c7_exact_pr39_rollback_readability`, with current-cycle, removed-member and stale-member scenarios |
| C8, config/options flows | `test_c8_backend_error_flows_retry_and_resubmit`, `test_c8_config_and_options_accept_supported_urls`, `test_c8_name_mismatch_never_saves_or_advances`, `test_c8_prevalidation_errors_never_probe_or_advance` |
| P1/P9/capture translations | `test_p1_p9_and_capture_translation_keys_are_synchronized` |

## Independent reviews and proof corrections

Claude Opus 5.5 and GPT-6.1 Sol independently reviewed the same compatibility
slice and approved criteria. Neither identified a production bug. Both found
that C7 did not fully assert restored cumulative totals/parsed payloads and
used mismatched save/restore clocks in two scenarios.

Their additional findings cover options-selector validation (C2), reading the
official serialx constraint (C3), native open/factory/port-release assertions
(C4), the stored percent-encoded P9 representative (C6), and M1 log redaction
(C8). These were missing proofs of approved requirements, not additional
product scope. Corrections were delivered only in `tests/core_smoke.py`.
The worker first reproduced the current/removed C7 clock mismatch
(`00:01:00` saved versus the required `00:00:30` restore anchor), then corrected
the schedule and completed the additional assertions. Its targeted runs passed
on both Cores; the main session subsequently ran both full strict gates above.
Both reviewers accepted the corrected C7, C2 and C3 proof. Claude accepted the
other original corrections and noted that the encoded S2v fixture needs a
non-identity encoding guard. GPT identified two remaining material proof gaps:
patching `_LOGGER.warning` is not actual multi-level log capture, and a
negative-case delivery counter is uninformative without offering data and
verifying failed-transport/subscription cleanup. Main accepted these findings
as in-scope assertion corrections; no production bug was demonstrated. The
affected C4/C8 assertions and encoded fixture guard were corrected before
re-review. The worker delivered real formatted-log capture,
failed-transport close/wait/subscription checks with post-failure data probes,
and the encoding guard. Its ten targeted C4/C8 tests passed on each Core.
Main's full strict reruns and Ruff/whitespace checks also pass. Claude and GPT
independently accepted the final corrections with no remaining material
findings. Both verified that forwarding spies preserve the real transport
behavior and that the code, not the test, performs cleanup before the offered
post-failure data probe. Both accepted real formatted log records, submitted
entry-ID checks and logger restoration, plus the non-identity encoding guard.

Energy and capture each passed independent Claude/GPT review. Main's
discovery/options/CI/documentation integration passed Claude/Gemini review after
the runtime-discovery edit-preservation correction. No open finding in those
completed slices is being waived.

For the restart correction, both final reviewers (Claude Opus 5.5 and
GPT-6.1 Sol) accepted the production change and refined regression proof.
Claude verified the worker references clear only after join; GPT confirmed the
explicit-unload cleanup boundary does not substitute for the native shutdown
test. Both found no remaining material concern.

## Evidence availability

Detailed outputs are machine-local in this session's `files` directory:
`prepare-minimum.log`, `prepare-current.log`,
`candidate-runtime-minimum-check.log`, `candidate-runtime-current-check.log`,
`candidate-pytest-2026.10.4.log`,
`candidate-pytest-2026.10.4-approved-rerun.log`, and
`candidate-parser-2026.10.4.log`, plus
`candidate-core-smoke-2026.10.4.log`, `candidate-core-smoke-2026.11.2.log`,
`candidate-core-smoke-2026.10.4-proof-rerun.log`, and
`candidate-core-smoke-2026.11.2-proof-rerun.log`.
They are not represented as Git-transferred
artifacts. This concise report and the repository journal remain local-only
until included in an authorized Git checkpoint.

## Final corrected-candidate validation

| Gate | Command | Result |
|---|---|---|
| Focused lifecycle/persistence | `"$PY_MIN" -m pytest tests/test_listener_lifecycle.py -q` | 91 passed |
| Complete minimum-Core suite | `"$PY_MIN" -m pytest tests -q --tb=short --show-capture=no` | 521 passed, 129.60 s |
| Minimum parser suite | `PYTHONPATH=custom_components/pytap "$PY_MIN" -c 'import pytap, pytest; raise SystemExit(pytest.main(["custom_components/pytap/pytap/tests", "--import-mode=importlib", "-q", "--tb=short"]))'` | 73 passed, 4.03 s |
| HA 2026.10.4 strict native smoke | `"$PY_MIN" scripts/run_core_smoke.py` | 31 passed; zero skips, failures or errors; 98.468 s |
| HA 2026.11.2 strict native smoke | `"$PY_CURRENT" scripts/run_core_smoke.py` | 31 passed; zero skips, failures or errors; 134.812 s |
| HA 2026.11.3 supplemental strict native smoke | `"$PY_2026_11_3" scripts/run_core_smoke.py` | 31 passed; zero skips, failures or errors; 144.979 s |
| Ruff and whitespace | `python -m ruff check custom_components/pytap tests scripts`; `git diff --check` | Both exit 0 |

`PY_2026_11_3` denotes the machine-local isolated interpreter prepared with
HA 2026.11.3's official package constraints.

The strict native runs include the graceful-stop persistence regression and
exact-#39 C7 rollback-readability cases. The supplemental Core 2026.11.3
environment used Python 3.14.4, serialx 1.15.1, Pydantic 2.14.1 and the
official constraints SHA-256
`cf1de9c4cd9ff73ae2a09e8dce0c1989734eeb81ce5c4f91d022c57a5b81ada7`;
manifest-closure, dependency and real-public-API checks passed. These
automated results validate the corrected code, not live deployment or soak
acceptance. The final run output, helper-unit-test output and live snapshots
remain machine-local session evidence; the summarized results above are the
repository record.

## Follow-up two-member unload/restart validation

The focused `test_unload_flushes_pending_multi_member_energy_before_reload`
case passed on HA 2026.10.4 (1 test, 4.62 s). The native
`CoreSmoke.test_unload_flushes_multi_member_energy_before_native_restart`
case unloads a two-member entry with pending energy, waits for the listener
worker and final save, stops Core, then starts a fresh native Core instance and
checks restored member totals and aggregate values/baselines.

| Gate | Result |
|---|---|
| HA 2026.10.4 strict native smoke, including the new case | 32 tests passed, 129.900 s |
| HA 2026.11.3 strict native smoke, including the new case | 32 tests passed, 178.985 s |
| Exact PR #39 unload/restart case on HA 2026.11.3 | 1 test passed, 2.715 s |
| Ruff: `ruff check custom_components/pytap tests scripts` | Passed |
| `git diff --check` | Passed |

For the PR #39 check, the two-member test was copied into a detached temporary
worktree at exact commit `a80698fca67bef4847a3c6ce5217f163fee94c01`; the
integration source remained exact #39. Explicit config-entry unload joined
the listener and completed the pending save before native Core stop; a fresh
HA instance then reloaded the stored state and verified member totals plus
aggregate values and baselines. Both startup phases used #39, so this is not
cross-version #39-to-candidate evidence.

This proves the synthetic candidate-to-candidate unload/final-save path, not
the exact #39-to-candidate transition or live-v7 recovery. The 2026.11.2
31-test smoke result predates this new case and was not rerun because the
available legacy environment resolves to HA 2026.2.2. A later read-only live
Recorder query found the 84,031.10478 Wh lifetime excess is confined to
string C; the missing pre-restart aggregate baseline map still prevents root
cause confirmation. No production code, live HA state, or energy statistics
changed. The exact-#39 test supports explicit unload as a safe-save mechanism
on that release, but does not establish a safe cross-version upgrade or clear
the lifetime-accounting blocker. Task 02 stays in progress, live validation
remains blocked, and no redeployment, soak, or release is authorized.

## Bounded offline upgrade investigation status — 2026-11-27

The user authorized one offline investigation cycle. The already-connected
dev validation terminal supplied read-only Recorder state pairs for all 26
member lifetime entities and the three aggregates; the grouped totals and the
`c1` observation above are now recorded. The historical aggregate baseline map
and original power-report payloads remain unavailable, so any replay from
these state pairs must be labeled reconstructed and cannot establish the
historical root cause.

The local Python launcher did not recognize `-3.14`; the existing
`.venv-ha-2026.10.4` interpreter was used to create a disposable environment.
The configured package mirror does not list HA 2026.11.3. Official PyPI
confirms that release, but direct wheel retrieval failed with a TLS handshake
error. HA 2026.11.3 was installed from the official Core source archive using
the verified official constraints (SHA-256
`cf1de9c4cd9ff73ae2a09e8dce0c1989734eeb81ce5c4f91d022c57a5b81ada7`), and
`pip check` reported no broken requirements. The approved manifest-dependency
preparation then stopped because the configured mirror returned HTTP 404 for
`aioesphomeapi==46.25.0`; therefore the isolated test dependency closure is
incomplete and no paired experiment or test ran. Do not bypass TLS verification,
try an alternate package source, or substitute another Core version within
this bounded cycle; park the release pending a new decision.

No production code, live HA state, energy statistics, PR, or release state
changed.
