# Handoff: validated-candidate checkpoint (NOT release completion)

- **Status:** completed and sealed (immutable). Corrections belong in a new journal entry or a new snapshot.
- **Repository:** azebro/pytap (Home Assistant custom integration for Tigo TAP solar optimizers)
- **Branch:** `azebro-legendary-spork`
- **Base HEAD at this checkpoint:** `66ce47833034b338f862ae904d92b774abfe9fc2` (the standard merge of exact #39 into the branch; its tree equals exact #39)
- **Exact #39 commit:** `a80698fca67bef4847a3c6ce5217f163fee94c01`
- **Header base of the journal (historical):** `d9960ae` (Merge pull request #24 from azebro/dev)
- **Device label:** windows-pc (Windows host; WSL Ubuntu for tests)
- **Session ID:** 72475d47-6f0e-4206-9387-b6e4948802a4
- **Created:** 2026-11-25T13:20:16Z
- **Preceding repository handoff:** none (this is the first handoff snapshot; the handoffs directory was empty)
- **Journal:** `.copilot-memory/journals/20261122T145221Z_72475d47.md`
- **Final journal update ID at this checkpoint:** U217 (U0–U217)
- **Provenance:** every fact below is as reported by the coordinating main session and recorded in the journal. The logger did not audit the repository, board, documents or hosts. Items the coordinator has not reported are marked "not reported".

## 1. Current goal

Release PyTap 1.4.0 from the #39-based stack (PRs #32–#39 by timlaing, evaluated in this session), with fixes, under PRD-PYTAP-1.4.0-001 v9 r14. The code candidate is **validated locally and in review-complete state**. What remains: live deployment to the dev HA box, verification, a 24 h soak, then merge and a new bare `1.4.0` tag. **No live deployment has happened.** The live runtime is still #39.

## 2. Approved requirements

- **PRD:** `docs/prd-1.4.0-robust-tcp-core-v9.md`, version **v9 r14**.
  - r13 approved 2026-11-24 (journal U118) after two independent reviews. r13 is the historical baseline.
  - r14 approved by the user 2026-11-25 (journal U183). Amendment artifact: `docs/prd-1.4.0-robust-tcp-core-v9-r14-amendment.md`. It changes **only** criterion 1's evidence format (concise per-run reports) and status reconciliation. It waives no gate and authorizes no backup or restore.
- **Preserved from r13 (all still binding):** TDD, every runtime gate, the exact-v7 config-entry check immediately before deployment, the code-only rollback proof, the 24 h soak, and the new bare `1.4.0` tag.
- **Settled design decisions:**
  - Home Assistant minimum 2026.10.4; `-portN` `port_udev_id` URLs rejected there.
  - Preserve #39 behaviour and config-entry v7.
  - Fix the three #37 defects and the #38 parent-symlink escape, each with regression tests.
  - Storage default 1024 MiB, configurable 16–4096 MiB, no pruning.
  - ESPHome URLs use `port_name` on 2026.10.4; identity filters and `port_udev_id` are 2026.11+ only.
  - Manual-claim policy: disable only the conflicting barcode, keep healthy modules running, warn once.
  - C7 design: `Pacific/Auckland`, mutable clock, D at least 2 local days in the future, `ENERGY_GAP_THRESHOLD_SECONDS = 120`.
  - Test scope: the complete suite on 2026.10.4; smoke plus bounded targeted compatibility on 2026.11.2.
- **User directives in force:** use the dev environment and the provided dev access freely within the gates; proceed autonomously toward release ("features before procedures"). The coordinator states autonomy waives no test, rollback, v7 or no-backup/no-restore requirement, and a failed gate still stops for a user decision (applied at journal U207–U208).

## 3. Board (`docs/kanban.md`, coordinator-owned)

| Task | Slice | Status |
|---|---|---|
| PYTAP-140-00 | PRD and plan | Done |
| PYTAP-140-01 | Pre-port evidence (criterion 1) and schema gate | Done |
| PYTAP-140-02 | Aggregate energy continuity | Done |
| PYTAP-140-03 | Discovery ownership correctness | Done |
| PYTAP-140-04 | Safe capture and replay | Done |
| PYTAP-140-07 | Compatibility (transport, manifest 1.4, translations, runner, native C1–C8) | **Done** |
| PYTAP-140-05 | Live v7 validation, deployment and 24 h soak | **Ready** |
| PYTAP-140-06 | Merge and release (new bare `1.4.0` tag) | Backlog |

Slice completion is not integrated completion: the integrated deployment, soak and release criteria are still open.

## 4. Source and Git state

- Branch `azebro-legendary-spork`; base HEAD `66ce478…` as above. The product source, tests, scripts, documentation and memory for this candidate are **pending a LOCAL Git commit** that the main session will make before live task 05. **No push, PR, merge or tag action is implied.** The target PR and merge confirmation remains pending.
- All product and pytest-collected source is unchanged since the 520-pytest and 73-parser runs; the final native 30 of 30 runs and the Ruff and `git diff --check` checks passed after that.
- Candidate contents (as reported): const and manifest version 1.4.0; HACS minimum 2026.10.4; `scripts/prepare_ha_tests.py`; CI matrix on the official constraints (2026.10.4 full, 2026.11.2 smoke); the `serialx` test pin removed; seven P1/P9/capture translation entries; `capture.py` and `capture_services.py` with `trusted_root` plumbing; three-way options reconciliation and reservation snapshot (discovery); aggregate energy continuity in `sensor.py`; updated README, CHANGELOG, `docs/architecture.md`, `docs/implementation.md`.
- **Preserved original edits:** git stash `707e2a8badc75d4626a26a02fda7e8d89ffc7541` (eight earlier uncommitted #35 fixes) and the session-folder patch `files/preserved-preport.patch`. Both are local-only. The parser indentation change was ported; the documentation items were reconciled in this candidate. Whether any stashed edit still needs reconciliation was not itemized.
- `.copilot-memory/` is untracked unless and until the coordinator commits it.

## 5. Acceptance evidence

| Criterion area | Result (coordinator-reported) | Reference |
|---|---|---|
| Criterion 1: pre-port dependency and import evidence | HA 2026.10.4 and 2026.11.2 isolated environments on the dev host, `pip check` 0, closure 21 components and 22 requirements, real module origins, 171 and 128 distributions including `pip`, constraints hashes verified; exact install commands recovered from the host shell history and from this session's original requests | `docs/evidence/prd-pytap-1.4.0-001-v9-r14-preport.md` |
| Criterion 2: schema | exactly one PyTap entry, v7 minor 1 (read 2026-11-24 and again 2026-11-25) | same report |
| Criterion 10 (candidate evidence) | complete per the coordinator | `docs/evidence/prd-pytap-1.4.0-001-v9-r14-candidate.md` |
| Full pytest, minimum Core 2026.10.4 | **520 passed** in 109.33 s (after the first run's single failure, below) | candidate report; machine-local log |
| Parser suite (minimum) | **73 passed** in 3.39 s | candidate report |
| `pip check` | 0 in both WSL Core environments, no installs | candidate report |
| Strict native Core smoke, C1–C8 incl. three C7 scenarios | **30 of 30 passed** on both Cores, zero skips, errors, failures, expected failures and unexpected successes (final authoritative runs: 86.832 s on 2026.10.4, 130.807 s on 2026.11.2) | candidate report |
| Lint and whitespace | full Ruff passes; `git diff --check` passes | candidate report |
| Slice tests | energy 69 passed; discovery and lifecycle 107; capture and discovery and config flow 209 combined; C7 outside-the-checkout runs pass on both Cores | journal U199–U207 |
| Exact #39 baseline | commit present; tree identity confirmed; live runtime content matches #39 apart from CRLF/LF and omitted bundled parser tests (31 files, no missing or extra runtime paths) | journal U191, U209, U210 |
| Live read-only energy preflight (2026-11-25, not acceptance) | one v7.1 entry; 26 members in 2 strings of 13; installation and both string daily aggregates equal the optimizer sums (0.00 Wh) | journal U214; raw values machine-local |

**First formal pytest gate failure and decision (journal U207–U208):** 519 passed, 1 failed (`tests/test_diagnostics.py::test_serial_diagnostics_redact_complete_endpoint`, a nameless ESPHome URL now correctly rejected on 2026.10.4). The user chose "Update the fixture and rerun validation (Recommended)". Only `port_name=private-port` was added to the synthetic URL; production behaviour and every redaction assertion were kept.

## 6. Reviews and resolutions

Author families: energy and capture by MAI Code 2; discovery, CI, scripts, docs and metadata by the main session (GPT); compatibility (-07) by MAI Code 2 with main-session C7 correction.

- **-02 energy:** Claude Opus 5.5 clean; GPT clean.
- **-03 discovery:** Claude found one medium issue (Options "Done" losing runtime-only discovered-module edits on reload); fixed test-first and re-reviewed clean; Gemini 4 Carbon clean.
- **-04 capture:** GPT clean; Claude clean (with the integrated trusted root and the Options fix); translation `capture_path_unsafe` verified in both JSON files.
- **-07 compatibility:** the first Claude and GPT reviews found no production bug but incomplete proof (C7 values and time anchor, plus C2, C3, C4, C6, C8). The mandatory C7 outside-the-repository placement gap (deferred by the worker) was fixed by the main session, with no waiver. A second round (GPT) found C8 mock-only logging and C4 negative-counter and transport-cleanup gaps; Claude found a low encoded-S2v no-op possibility. All were corrected in `tests/core_smoke.py` only. The **final affected reviews by Claude (journal U216) and GPT (U217) report no material findings.** All findings are closed.

## 7. Frozen archives (local, built from repository source only)

- Candidate: `pytap-1.4.0-candidate-20261125T124456Z.zip` (37 files, 142,817 bytes, manifest 1.4.0).
- Exact #39 code-only rollback: `pytap-pr39-a80698fca67b-20261125T124456Z.zip` (37 files, 140,493 bytes, manifest 1.2.0).
- **Full identities (including full SHA-256 values) are in `docs/evidence/prd-pytap-1.4.0-001-v9-r14-candidate.md`;** they are intentionally not copied here. Machine-local metadata `pytap-deployment-artifacts-20261125T124456Z.json`. The archives' storage location was not reported to the logger; see the candidate report.
- These are **not a live backup.**

## 8. Live environment (dev HA box)

- HA OS with Core **2026.11.2** (Python 3.14.10 on the host). The installed integration is still **#39** (manifest 1.2.0), config entry **v7 minor 1**, 26 configured members across 2 strings of 13. The `pytap` runtime content matches exact #39 apart from line endings and the omitted parser tests.
- The migration to v7 from the earlier v4 is one-way and already done.
- The SSH add-on (Alpine) runs with protection mode on. Authorized earlier installs (still present): `g++` and 13 packages; a Python r2 upgrade with `python3-dev` and `pkgconf`.
- Raw pre-port evidence and the verification directory remain on the host's temporary location. Machine-local, host-held, not transferred through Git.

## 9. Next authorized steps (PYTAP-140-05, then -06)

1. **Local Git checkpoint** of the verified source, documentation and memory (main session). No push.
2. **Immediately before deployment, a fresh baseline** (criterion 11): exact-v7 config-entry check, membership, and aggregate totals. The U214 preflight does not substitute for it.
3. **Deploy the candidate, code only,** from the verified candidate archive, with a verified restart. Do not use the existing `deploy.sh` (it disables host-key checking and hides restart failures; it was inspected and left unchanged).
4. **Verify** after deployment: no PyTap errors, entities available, energy continuous, config entry still v7, no migration, membership preserved.
5. **24 h soak,** scheduled on the live runtime's local clock (the runtime timezone was read; the value is in the session evidence only).
6. **Task 06 after a passed soak:** merge and the new bare `1.4.0` tag. The existing release workflow, on a published release, zips the component-root contents (excluding cache directories) and uploads `pytap.zip`. **The target-PR and merge confirmation is still pending;** push, PR, merge, tag and release need the user's confirmation or the coordinator's established authority.
7. **Code-only rollback** to the exact #39 archive is allowed only if the v7 config and persisted state stay backward-compatible (proved by the C7 rollback scenarios). Stop and ask the user if a gate fails.

## 10. Standing constraints

- **No backup and no restore.** The user declined a fresh current-state backup and prohibited restoring the pre-PR backup.
- **Do not alter HA Core, and keep add-on protection mode on.**
- Cleanup of the host evidence, the verification directory and the add-on package sets is **not authorized.**
- A failed gate stops for a user decision; autonomy does not waive it.
- No credentials, addresses, host names, key material or full checksums in committed records.

## 11. Known limitations and open items

- The merge commit, the stash, the session patch, the archives and the machine-local logs are not transferred by Git. This checkpoint is **local-only** until the coordinator commits and pushes.
- The candidate and pre-port evidence reports contain the exact identities. If they are lost, the machine-local session folder (`files/`) and the host's temporary evidence location are the other sources.
- Documentation consistency items I recorded earlier (PRD lines 3 and 383 wording on criterion 1; the board's historical Gate paragraph) were not reported as resolved. Not verified by the logger.
- The resource impact on the dev box of the two full Core installs is unmeasured. A MAC-algorithm mismatch seen on an early SSH attempt has no established cause.
- The 24 h soak, the live-deployment verification and the release have not started.

## 12. Machine-local evidence (session folder, `files/`; not in Git)

`candidate-pytest-2026.10.4.log` (first, failed run), `candidate-pytest-2026.10.4-approved-rerun.log`, `candidate-parser-2026.10.4.log`, `candidate-runtime-minimum-check.log`, `candidate-runtime-current-check.log`, `candidate-core-smoke-2026.10.4.log`, `candidate-core-smoke-2026.11.2.log` (the earlier run), `candidate-core-smoke-2026.10.4-proof-rerun.log`, `candidate-core-smoke-2026.11.2-proof-rerun.log` (authoritative), `prepare-minimum.log`, `prepare-current.log`, `preserved-preport.patch`, and the archives metadata JSON. The worker's own abbreviated temporary log names are not retrievable artifacts.
