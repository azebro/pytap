# PRD-PYTAP-1.4.0-001 v9 r14 - Integrated candidate evidence

**Status: automated gates and independent reviews passed.**
The pytest, parser, strict native smoke/rollback, lint and whitespace gates
pass, including strict reruns after the final C4/C8 proof corrections. Both
independent reviewers accepted the affected proof with no material findings.
Deployment still requires the fresh live baseline in criterion 11; this report
does not claim deployment or soak acceptance.
The [delivery board](../kanban.md) owns task status.

This report covers the integrated 1.4.0 working tree, not the historical
476-test baseline or the separate [pre-port environment evidence](prd-pytap-1.4.0-001-v9-r14-preport.md).
The candidate changes are currently uncommitted on `azebro-legendary-spork`,
above merge commit `66ce47833034b338f862ae904d92b774abfe9fc2`.
Exact PR #39 is `a80698fca67bef4847a3c6ce5217f163fee94c01`; a fresh
`git cat-file -e 'a80698fca67bef4847a3c6ce5217f163fee94c01^{commit}'`
succeeded. No live deployment, backup/restore, soak, release or push is claimed.

## Frozen code artifacts

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

## Integrated runs

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
completed slices is being waived by the pending compatibility work.

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
