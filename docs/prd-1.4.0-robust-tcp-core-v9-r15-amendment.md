# PRD-PYTAP-1.4.0-001 v9 — r15 amendment (aggregate reset discrimination)

**Authorship:** Claude-family. **Reviews:** GPT-6.1 Sol (xhigh) and Gemini 4 Carbon (high) reviewed drafts 1 and 2; draft 3 (this text) resolves their findings (section 8).

**Status:** Candidate draft 3, not approved. Until the user explicitly approves it, r14 stays active and this amendment authorizes no implementation.

## 1. Problem

Two live upgrades on the dev v7 installation (Nov 25 and Nov 26) inflated aggregate energy. On Nov 26, net member production during the restart transition was about 2 Wh. Yet the aggregates jumped by +484.92 Wh daily and +821,230.95 Wh lifetime.

The per-member Recorder pairs are recorded in the [evidence](evidence/prd-pytap-1.4.0-001-v9-r14-candidate.md). Ten of 26 members came back 0.04–0.16 Wh *below* their last published values, both daily and lifetime. The r13/r14 rule credits the full post-decrease value of any decrease as a counter reset. Replaying that rule on the recorded values reproduces every observed string jump exactly. (Note that `c1`'s Recorder unit is kWh; internal counters are always Wh.)

The r13/r14 accepted-risk line, which rejected a reset threshold, proved release-blocking: one restart inflated installation lifetime energy by about 34%.

## 2. Users and goal

PyTap users with string/installation energy aggregates. **Goal:** small restore skew from restarts, reloads and upgrades must not inflate aggregate daily or lifetime energy, while genuine member-counter resets are still credited.

## 3. Requirement changes to v9

**3.1 Replace** the "Preserve exact post-reset credit…" requirement (v9 line 135) with:

> **Reset discrimination (daily and lifetime aggregates).** For a member with tracked baseline `b` and a nonnegative current value `v`:
>
> | Condition | Credit | New baseline |
> |---|---|---|
> | `v ≥ b` | `v − b` | `v` |
> | `0.5 × b ≤ v < b` (restore skew, including `v == 0.5 × b`) | `0` | `b` (held) |
> | `v < 0.5 × b` (genuine reset) | `v` | `v` |
>
> A held baseline is persisted unchanged in `extra_restore_state_data["baselines"]` and survives further restarts. While held, intermediate values below `b` credit nothing. The first value above `b` credits only the amount above `b`. Negative values are never credited. Precedence is unchanged: the new-member, removed/re-added-member, stale-daily-date and midnight-rollover rules apply first. A daily rollover zeroes baselines, which also clears any daily hold. The persisted schema is unchanged.

**3.2 Non-goals:** replace "Redesigning persistence to infer whether a lower restored counter is a reset or stale state, or introducing a guessed reset threshold" with: "Redesigning persistence or the member-counter restore path; any reset discriminator other than the fixed 50% rule in 3.1."

**3.3 Replace** the *Accounting risk carried forward* line (v9 line 195) with the residual risks in section 5.

## 4. Non-goals of this amendment

- Repairing the already-inflated dev aggregate totals or Recorder statistics from Nov 25 and Nov 26. Record this as a backlog item that needs its own decision.
- Changing why member counters restore slightly low, the restart-flush correction, or persisted schemas.

## 5. Residual accounting risks (accepted, bounded)

Let `s = b − v` be a member's restore skew:

- **Held skew:** the member resumes counting from `v`, so the first `s` of new production is not credited to the aggregate. Once the member passes `b`, the aggregate matches the members' increments from their pre-restart published values. The skew is energy the member's own counter lost; the aggregate double-counts nothing.
- **Near-zero baseline:** when `0 < b < 2s` (e.g. a daily counter just after dawn), a skew is classified as a reset: the aggregate credits `v` and rebases to `v`. Relative to real production, it double-counts `v` (less than `0.5 × b`). Relative to the members' increments from their pre-restart values, the aggregate runs ahead by up to `b` (less than `2s`) once the member recovers to `b`.
- **Large restore loss:** a restore that leaves less than half of a member's counter (a loss of more than half) is credited as a reset and can overcount by the post-loss value. PyTap's coordinator-integrated counters genuinely reset only to near zero.
- **Small genuine resets:** a genuine reset that leaves at least 50% of the prior value goes uncredited until the member exceeds its prior value.
- **Code-only rollback to exact #39 while a baseline is held:** #39 rebases on any decrease without credit. It can therefore re-credit the held gap (at most `s` per member) as the member recovers. It never credits a member's full value.

## 6. Acceptance criteria (additions and changes)

**Criterion 5 (*Accounting*) adds:**

- **5a. Exact regression.** Using the recorded 26-member Nov 26 daily and lifetime pairs (in Wh), drive the C, D and installation aggregates through the transition, then through one recovery reading per dipped member above its prior value. On the current candidate, the transition must reproduce the recorded jumps (C +255.46/+428,585.51 Wh, D +229.46/+392,645.44 Wh daily/lifetime). After the fix, each aggregate increment must equal the 3.1 expected credit within 0.01 Wh at stored precision: C +1.39/+1.38 Wh, D +1.46/+1.47 Wh, and the installation their sum. After recovery, the aggregate total must equal its pre-transition value plus the members' net increments from their pre-transition values.
- **5b. Rule cases** for both daily and lifetime aggregates:
  - a genuine reset is credited as before;
  - a skew dip credits zero;
  - partial recovery `v1 < v2 < b` still credits zero, then `v3 > b` credits `v3 − b`;
  - `v == 0.5 × b` holds;
  - `v == 0` with `b > 0` is a reset crediting zero;
  - a held baseline survives save → restore → recovery without double counting;
  - a held daily baseline is cleared by midnight rollover;
  - all existing criterion 5 cases still pass.
- **5c. Rollback.** The C7 exact-#39 cases pass unchanged. A new C7 scenario saves the candidate while a baseline is held, loads exact #39 and recovers the member. #39 must load the payload, never credit a member's full value, and overcount by at most the held gap.
- **5e. Harness-free smoke.** `tests/core_smoke.py` gains one native skew-hold → restart → recovery case, run in the existing smoke environments.

**Criterion 9 (*documentation*)** adds: `README.md`, `CHANGELOG.md`, `docs/architecture.md`, `docs/implementation.md` and the `sensor.py` aggregate docstring describe the 3.1 rule and the section 5 residual risks, replacing the "any decrease credits its post-reset value" wording.

**Criteria 11–12 (*live validation and soak*):**

- **5d. Lifetime and daily increment reconciliation.** Read unformatted `state.state` values and convert each to Wh using its `unit_of_measurement` (this conversion also applies to criterion 12's existing daily comparison). For each string and the installation:
  - (i) Across "immediately before cutover → first settled post-cutover report" and across "immediately before the soak restart → first settled report after it", the aggregate increment must equal the 3.1 expected credit computed per member. `b` is the aggregate's actual tracked baseline for that member, captured read-only from its restore data (`.storage/core.restore_state`, `baselines`) at the pre point. Exact #39 rebases on every update, so at cutover its baselines equal the members' last values; any mismatch must be recorded. If the baselines cannot be captured, the interval is unmeasurable.
  - (ii) Once every dipped member has passed its pre-cutover value in daylight, `aggregate − aggregate_pre-cutover` must equal the sum of `member − member_pre-cutover`.
  - **Daily** comparisons apply only when both endpoints fall in the same HA-local cycle. A midnight rollover between endpoints leaves only the lifetime check for that interval, and criterion 12's existing midnight checks still apply.
  - Each absolute difference must be ≤ 0.1 Wh. A failure or an unmeasurable interval invalidates the run (code-only #39 rollback only; no state restore).
- **Cutover precondition (added):** cut over only in daylight when every member's daily value is at least 1 Wh. This avoids the near-zero-baseline residual in section 5.

All other v9 r14 criteria, gates and prohibitions remain unchanged.

## 7. Status reconciliation on approval

Only after explicit user approval: update the v9 status and stable-reference lines to "v9 r15 (approved <date>)", noting that r15 supersedes r14 solely for the sections above. Add this file to v9's preservation list. Then update `docs/kanban.md` PYTAP-140-02 to reference r15. Approval authorizes test-first implementation of 3.1/6, not deployment, PR changes or release.

## 8. Review disposition

| Finding | Source | Disposition |
|---|---|---|
| 5d oracle ambiguous; would false-fail correct holding; units/formatting unspecified | Both (blocking) | Fixed: 6/5d defines baseline-aware expected credit, a recovery check, unformatted Wh conversion and absolute error; criterion 12 gets the conversion. |
| #39 rollback from a held baseline not characterized | GPT (blocking) | Fixed: 5c adds the scenario; section 5 bounds the re-credit to the held gap. |
| Near-zero baseline makes skew look like a reset | Both | Kept the approved 50% rule. Section 5 states the bounded error (less than `s`); the cutover precondition avoids it live. The suggested extra absolute floor would be a new user decision (not adopted). |
| 5a lacked per-member data | Gemini (blocking) | Fixed: 26-member pairs recorded in the evidence. 5a includes a recovery step. |
| "None is lost" overstated; lifetime overcount mislabeled | GPT | Fixed: sections 1 and 5 reworded. |
| Criterion 9 docs, smoke coverage, partial-recovery/restore-while-held cases, status reconciliation | Gemini | Fixed: criterion 9, 5e, 5b and section 7. |
| Unload-first or zero-production cutover as defense-in-depth | Gemini (suggestion) | Not adopted; it would be a separate operational decision. |
| Draft 2: oracle used the member's value, not the aggregate's actual baseline; daily check ignored midnight | GPT (re-check, blocking) | Fixed: 5d(i) uses captured restore-data baselines; daily checks are restricted to one local cycle. |
| Draft 2: near-zero-baseline bound wrong | Gemini (re-check, blocking) | Fixed: section 5 states both bounds. |
| Draft 2: control bytes in evidence | Gemini (re-check) | Fixed. |

GPT confirmed its rollback finding resolved; Gemini confirmed its three draft-1 blockers resolved. The draft-3 fixes follow the reviewers' specific recommendations and were not re-reviewed.

**Post-approval wording correction (2026-11-28):** the implementation's GPT-6.1 Sol code review noted that section 5 said "half or more" while 3.1 holds at exactly half. Section 5 now says "more than half". No behaviour or criterion changed.
