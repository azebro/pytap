# PRD-PYTAP-1.4.0-001 v9 — r14 amendment

**Authorship:** GPT-family.

**Status:** Approved by the user on 2026-11-25 after independent reviews by Claude Opus 5.5 and Gemini 4 Carbon found no blocking issues. R14 is active for criterion 1 evidence format and related status reconciliation only; r13 remains historical for all unchanged requirements. Criterion 1 and the live-schema gate remain incomplete, so this approval does not authorize implementation or porting.

## 1. Exact criterion-1 replacement

**Only after explicit user approval**, replace criterion 1 in its entirety, from its numbered heading through the paragraph immediately preceding criterion 2, with:

> **1. Feasibility, environment, and dependency gate — checks and verified evidence required before implementation**
>
> Establish both isolated POSIX/Linux environments under *Compatibility gates*, using each Core release’s official constraints and unchanged installation rules:
>
> - **HA 2026.10.4:** Python 3.14.2 or newer, `serialx==1.11.0`, Pydantic 2.13.5, and `pytest-homeassistant-custom-component==0.13.375` (or a later harness only if permitted by the unchanged official-constraints compatibility rule).
> - **HA 2026.11.2:** Python 3.14.2 or newer, `serialx==1.15.1`, Pydantic 2.14.1, and **no** `pytest-homeassistant-custom-component`.
>
> In each environment, install the complete recursive manifest dependency closure seeded by `esphome`, `usb`, `zeroconf`, and `assist_satellite`, following both `dependencies` and `after_dependencies` and installing every visited manifest’s requirements under that release’s official constraints. Preserve every existing prohibition on dependency-resolution bypasses, forced conflicting versions, metadata falsification, patched/vendor substitutes, and stub modules.
>
> Require clean `pip check` and successful real-module imports/public-API checks:
>
> ```python
> import homeassistant.components.esphome
> import homeassistant.components.esphome.serial_proxy as p
> import homeassistant.components.assist_satellite
> import homeassistant.components.usb.serial_proxy_stub
> from homeassistant.helpers.selector import SerialPortSelector
> p.register_serialx_transport
> ```
>
> Also verify `p.build_url` on HA 2026.10.4, and `p.build_port_url` and `p.identity_port_info` on HA 2026.11.2.
>
> **Required concise evidence:** Persist both environment reports in the non-gitignored repository documentation file `docs/evidence/prd-pytap-1.4.0-001-v9-r14-preport.md`. Persistence means the file exists, is readable, and contains the verified reports; no commit is required now. This requirement authorizes neither staging nor alteration, reversion, or cleanup of existing uncommitted source/test diffs.
>
> Each report must derive **all facts from one specific environment instance and validation run**, identified by a non-identifying environment/run label and date. Do not combine installations, checks, inventories, or counts from different runs. Record:
>
> - POSIX OS/release and architecture; exact Python and Core versions.
> - Official constraints source URL, exact release tag, and SHA-256 of the actual constraints bytes used.
> - Every exact installation command, its exit status, and outcome.
> - Complete visited-component list and complete direct-requirement list, preserving requirement specifiers/markers and identifying each applicable requirement’s resolved distribution version. Explain any inapplicable marker.
> - The complete dependency/provider chain supplying `gazetteer_matcher`, including component/manifest edges, requirement edges, provider distribution, and resolved versions for every distribution in that chain.
> - Resolved `serialx`, Pydantic, and harness versions, or explicit verified harness absence; installed-distribution count and the exact command/counting method used.
> - Exact `pip check` and import/API verification commands, each command’s exit status, and explicit result. Record real-module origins and each required public symbol’s defining module/origin, with paths redacted as below, sufficient to distinguish installed implementations from repository shadows or injected substitutes.
>
> If any installation step is rerun, treat the resulting environment as a new validation run: refresh its inventories, versions, counts, and origins; rerun **all** criterion-1 checks for that environment; and report only that new run’s results.
>
> Full package-freeze files and raw transcripts need not be copied into Git. Preserve existing raw evidence until all required concise-report facts have been verified against it. This amendment grants **no cleanup or deletion authorization**.
>
> Retain the non-identifying raw-evidence locator **“validation-host temporary evidence location”**, linked to the applicable environment/run labels. This identifies the currently reported host-local evidence without committing its machine-specific path or connection details; it is not proof of recovery or verification.
>
> Reports must omit machine hostnames, IP addresses, usernames, SSH endpoints/ports, identifying local paths, credentials/tokens, credential-bearing package-index/proxy URLs, and live HA entry/device identifiers. Substitute consistent non-identifying placeholders while preserving command semantics, package identities, official constraints identity, and module-origin relationships. Keep official public source URLs unaltered where they contain no sensitive information.
>
> If a required fact cannot be recovered, stop before any implementation or porting and ask the user for a scope decision. Do not infer success or silently substitute another run. Any user-authorized rerun remains subject to all unchanged requirements; if it fails or cannot supply the required evidence, stop and ask again.
>
> Historical reports state that both closures and dependency/import checks passed on 2026-11-24; these are historically reported results, not newly verified facts. The evidence record remains outstanding until the reports above are verified and persisted. Earlier exact-#39 results—476 full-suite and 73 parser tests on HA 2026.10.4, and 7 smoke tests on HA 2026.11.2—are **baseline-only**, not integrated-candidate results or satisfaction of this gate.
>
> The approved **future** test scope remains the complete repository suite on HA 2026.10.4 and smoke/targeted compatibility tests only on HA 2026.11.2. The latter has no harness or complete-suite requirement because of the harness/Pydantic conflict. This amendment claims no candidate-suite execution.
>
> Completing criterion 1 does **not** satisfy criterion 2. All source/test/dependency/CI implementation, porting, deployment, and live-HA changes remain blocked until both pre-port gates and their required evidence are complete. The narrowly authorized isolated validation and read-only schema inspection remain permitted. Later deployment and live-HA actions additionally require their unchanged subsequent gates.

## 2. Conditional cross-reference and status reconciliation

**Apply all changes below only after explicit user approval of r14.** Until then, leave r13 and the current worktree unchanged. Line numbers refer to the reviewed r13 baseline.

- **Lines 3, 5, 13, 20, 199, and 340:** Update only references identifying the *current active revision* from r13 to r14, recording the actual explicit user-approval date. State that r14 supersedes r13 solely for this evidence-format amendment and associated status reconciliation. Preserve historical r13 approval/review facts and all existing scope, permissions, and gate restrictions. Do not imply the r13 reviewers reviewed r14 or invent an approval date.

- **Line 48:** Replace its evidence-status sentence with:
  > Historical reports describe successful official-constraints closures and public-import checks on 2026-11-24; these are not newly verified facts. Criterion 1 remains incomplete until its verified, single-run concise reports are persisted at the designated documentation path.

- **Line 89:** Change the status label to **“ESPHome dependency closure — historically reported; verified concise reports outstanding.”** Retain the earlier failed-import history and baseline-only distinction, but describe the subsequent results as:
  > Historically reported results for 2026-11-24 describe successful closure installation in both official-constraints environments, covering 21 components and 22 direct requirements including `gazetteer-matcher`, followed by clean `pip check` and successful required public imports/API checks without stubs. These historical reports are not newly verified facts. Criterion 1’s verified concise reports remain outstanding; neither these summaries nor the baseline test results satisfy that criterion. Raw evidence is reported at the validation-host temporary evidence location; its recovery and contents remain to be verified.

- **Lines 205–208:** These fall within the complete criterion-1 replacement. Do not retain superseded full-package-list/raw-transcript transfer requirements or their contradictory outstanding-record conclusion.

- **Line 341:** Replace **only its first sentence** with:
  > After explicit r14 approval, complete criterion 1, including both environments and its verified, persisted concise reports, and separately complete criterion 2’s initial live-schema gate and evidence before any source/test/dependency/infrastructure/CI implementation, porting, or live-HA changes; deployment and live-HA changes additionally remain subject to all subsequent gates; approval alone waives no gate, nor does approval authorize the declined backup/restore actions.

  **Retain its existing remaining sentences unchanged:**
  > A failed gate returns to the user before other implementation proceeds. After slices 02–04 and metadata are integrated, complete criterion 10 — including C7 on both versions — before deployment; if it fails, stop and ask for a new decision.

- **Line 343:** Append `docs/evidence/prd-pytap-1.4.0-001-v9-r14-preport.md` to the existing file-preservation list. Preserve all existing entries and worktree-preservation instructions. This addition requires no staging or commit and authorizes no cleanup, reset, or discard.

Other references to criterion 1’s “full” or “persisted” environment record mean its complete **concise report**, not mandatory transfer of package freezes or raw transcripts. No other criterion or evidence requirement changes.

**Tradeoff:** Reduced evidence-transfer and repository-storage burden, with less complete historical reconstruction and independent audit detail; later investigation may require renewed verification.
