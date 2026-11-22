# Governance validation record

This file records how the review gate, the release gate and the merge-method restriction were
validated end to end, and which limits were accepted. It complements
[governance.md](governance.md) and [release-process.md](release-process.md).

The evidence (collected JSON, scripts, logs and the independent reviews) is archived outside
the repository. Each package below is identified by its SHA256. The verdicts (PASS,
PASS_NARROW and so on) are those of an independent second-model review. Closures and risk
acceptances that the owner ratified explicitly are quoted with their identifier and date. As in
the gate introduction record, the owner and the agent share one GitHub identity: this file
documents the process, it does not prove it.

## Scope and environments

| Programme | Where | What it validates |
|---|---|---|
| RG-SB1–SB4 | Disposable sandbox `BRMff/lhr-rg-sandbox-20261106` | Required status precedence on the test merge commit, before the check was made required |
| RG-E1–E19 | This repository, PR #6 (synthetic `docs/gate-validation.md` only, closed without merge) | The review gate on the real `main`, ruleset and workflows |
| RL-E1–E10 | Replay sandbox `BRMff/lhr-rl-sandbox-20261111` | The release gate. The sandbox replays the real history (same commits, PR #4 as gate introduction) and runs `ci/` and `.github/` byte for byte as in production, **without rulesets** |
| MERGE-METHODS | This repository | Restricting `main` to merge commits |
| RR4 | This repository, PR #9 | The reconciler fix for RR4, after merge |
| CLEANUP C1–C3 | Both sandboxes and this repository | Archiving the sandboxes; deleting the merged branches of PRs #8 and #9 |
| ACTIONS-POLICY S1 | This repository | Requiring actions to be pinned to a full-length commit SHA |
| CC-GUARDRAILS | This repository, PR #10; Claude Code on the owner's machine | The local Claude Code guard, the runtime contract test and the privacy pre-review subagent |

The release-gate results show what the gate **detects**. They do not show what the production
rulesets **prevent**: the sandbox had no rulesets, so that scenarios RL-E9 and RL-E10 could
create the unsupported history they test.

## Review gate (RG)

| Scenario | Result |
|---|---|
| RG-SB1–SB4 | PASS. Finding SB-F1: a `success` on the test merge commit overrides a `failure` on the head (RR5, out of scope) |
| RG-E1 Opening the PR | PASS: `pending` marker on the head |
| RG-E2 Owner applies `review:passed` to commit A | PASS: `success` |
| RG-E3 New commit B | PASS: the approval does not move to B |
| RG-E4 Re-run of the old approving run after commit B | PASS: B stays `pending` |
| RG-E5 Label removed and re-applied to approve B | PASS: `success` on B |
| RG-E6 Label removed after approval | PASS: `failure` "approval withdrawn" |
| RG-E7 PR that tampers with the gate and deletes the trigger | PASS |
| RG-E8 Scheduled recovery | PASS_WITH_FINDINGS. L1: liveness depends on the schedule; gaps of up to about 10.5 hours observed |
| RG-E9 Status provenance | Closed through RG-E11 |
| RG-E10 Label and unlabel sequences | PASS. Finding E10-F1 (RR4), fixed after the validation |
| RG-E11 Required check | PASS. `review-gate` required in the ruleset; PR `clean` with `success`, `blocked` after the label was removed |
| RG-E12 Release-gate paths | PASS_NARROW, then closed with scope limitations by RL-E1–E10 |
| RG-E13 Closing PR #6 without merge | PASS_NARROW. The first scheduled reconciliation after the close reported `open PR heads: 0`. Note RG-E13-GOV-F1 (acknowledged by the owner): the close ran before the independent review of its preparation, and the authorisation was recorded after the run. Ratified: `LHR-RG-E13 = CLOSED_WITH_NOTES` (14 November 2026) |
| RG-E14b Two commits pushed about 2 s apart | PASS_NARROW: one trigger run and one reconciliation observed, for the second commit only; the first commit never got a status; no `success`. Two separate events were not observed |
| RG-E14c Event for a head that was already superseded | PASS: the reconciliation started by the older commit's event wrote no pre-write on it ("event SHA is not the head of an open PR") and set the current head to `pending` |
| RG-E15 Second PR on an approved SHA | PASS_NARROW. No new `success` while two PRs shared the SHA; the old `success` stayed about 19 s (RR1, accepted) |
| RG-E16 Close and reopen | PASS: after reopening, the head went back to `pending` ("label predates this head") |
| RG-E17b Draft and ready for review after approval | PASS_NARROW: `pending` "draft: not reviewable", then `pending` "label predates this head"; no `success`. The mergeability during the draft was not sampled |
| RG-E18 Trigger disabled, label removed | PASS_NARROW. Revocation by the schedule after 3 h 51 min (RR3, accepted) |
| RG-E18b Draft, ready for review, label removed (trigger enabled) | PASS_NARROW. Each transition reconciled in 16–20 s; no `success` after the draft; the old `success` stayed about 17 s during the draft (SG4) |
| RG-E19 Label removed, dispatch on `main`, re-run of the approving run | PASS |

## Release gate (RL)

| Scenario | Result |
|---|---|
| F0 Replay sandbox fidelity | PASS_WITH_FINDINGS. Code identical to production; one setup divergence (default branch), corrected before testing |
| RL-E1 Audit baseline | PASS: pre-gate tags reported as INFO |
| RL-E2 Negative preflights (P5, P4, P1, inputs, provenance) | PASS: each failed only on its condition; no evidence recorded |
| RL-E3a CI still running | PASS: only P2 failed |
| RL-E3b Valid preflight | PASS: P1–P5 and the gate introduction verified; evidence recorded |
| RL-E4 Valid lightweight tag | PASS: T1–T4 and audit |
| RL-E5 Tag without preflight | PASS: T4 failed; a later preflight failed P5 |
| RL-E6 Annotated tag after `main` moved | PASS: T1–T4 |
| RL-E7a / E7b / E7c Forged evidence (bot from another workflow; owner token; genuine run of another tag) | PASS: each rejected by T4. E7c also failed the time check, so its SHA and title checks were not isolated |
| RL-E8 Tag outside `main` with a neutralised gate | PASS as detection: green tag run, audit from `main` failed T2 |
| RL-E9 Unapproved merge | PASS: only P3 failed |
| RL-E10 Approved squash merge and direct push | PASS: three P3 failures (both commits and RL-E9), no recovery |
| RL-E-RACE-1 Change between `verify` and `record` | Not exercised end to end; unit tests only |
| Scheduled audit | Observed once, about 7 h 35 min after its cron time |

Ratified by the owner on 11 November 2026: `LHR-RL-E = CLOSED_WITH_NOTES` and
`LHR-RG-E12 = CLOSED_WITH_SCOPE_LIMITATIONS`.

## Merge-method restriction

`allowed_merge_methods` in the `main protection` ruleset changed from `merge, squash, rebase`
to `merge` on 11 November 2026, in a single update with verification before and after (no
other ruleset field changed). Notes: a squash or rebase was not attempted in production; the
repository-wide settings still allow squash and rebase; the rollback was prepared and not used.
Ratified by the owner on 11 November 2026: `LHR-MERGE-METHODS = CLOSED_WITH_NOTES`.

## RR4 fix

PR #9 (merge `d692af2`, 16 November 2026) makes the reconciler always give the head that got the
pre-write its final decision (unit tests U38–U41). The first run with the fix (scheduled, about
4 h 44 min after the merge) completed with `changes: 0; open PR heads: 0`: the new code runs in
production without error, but with no open pull request the fixed path (pre-write followed by a
stale read) was not exercised. Ratified by the owner: `LHR-RR4 = CLOSED_WITH_NOTES`.

## Sandbox and branch cleanup

| Step | What happened | Notes |
|---|---|---|
| C1 | Replay sandbox `BRMff/lhr-rl-sandbox-20261111` archived, with a description (17 November 2026) | The run started before the independent review of its preparation arrived (same class as RG-E13-GOV-F1). C1-POST-F1: secret scanning push protection changed from enabled to disabled, although no request asked for it |
| C2 | RG sandbox `BRMff/lhr-rg-sandbox-20261106` archived, with a description (19 November 2026) | C2-POST-F1: the same push-protection change, now observed on two archives without a request asking for it. Probable cause: archiving; not confirmed. Not blocking: both sandboxes are read-only |
| C3 | Merged branches of PRs #8 and #9 deleted (19 November 2026) | Both PRs stay merged and their heads reachable; `governance/gate-validation` kept; restorable from each PR page |

Ratified by the owner: `LHR-CLEANUP-C1 = CLOSED_WITH_NOTES`, `LHR-CLEANUP-C2 = CLOSED_WITH_NOTES`
and `LHR-CLEANUP-C3 = CLOSED_WITH_NOTES`.

## Actions policy: SHA pinning (S1)

Before the change, an inventory confirmed that all eight `uses:` references in the five workflows
are pinned to full-length commit SHAs (ratified: `LHR-ACTIONS-PINNING-P0 =
PINNING_COMPLETE_WITH_NOTES`, 20 November 2026).

On 20 November 2026 the owner set `sha_pinning_required` to `true` in the repository's Actions
permissions, in a single update with verification before and after; `enabled` stayed `true` and
`allowed_actions` stayed `all`. Observation until 22 November 2026:

| Criterion | Result |
|---|---|
| Scheduled `review-gate` runs | 9/9 success |
| Scheduled `release-audit` runs | 3/3 success |
| `tests.yml` (including `setup-python`) | Success on PR #10 and on the following push to `main`; observed from normal runs, without a manual dispatch |
| Dependabot version updates | Both weekly runs (`github_actions`, `pip`) success |
| Policy errors in logs and annotations | 0, with every run, job, log and annotation page read |

Dependabot's own jobs download `github/dependabot-action@main`, a branch reference, and still
ran: GitHub documents that Dependabot jobs bypass Actions policy checks, and this confirms it in
production for this setting.

Limits: the release gate was not exercised (no tag); the Dependency Graph job did not run (no
manifest change); no deliberately unpinned action was run, so the blocking itself was not
observed; the agent cannot read the Actions permissions, so the final state rests on the owner's
before and after records. The next step (S2, `allowed_actions: selected`) is not applied.
Ratified by the owner on 22 November 2026: `LHR-ACTIONS-POLICY-S1 = CLOSED_WITH_NOTES`.

## Claude Code guardrails (PR #10)

PR #10 (merge `de106db`, 22 November 2026; tree identical to the reviewed head `e1645c1`) adds the
guardrails described in [engineering-workflow.md](engineering-workflow.md#local-claude-code-guardrails).
The candidate went through three review rounds (two holds, then PASS_WITH_NOTES); the reviewer's
probes found bypasses of the static checker (an `os` alias, `from os import`, `Path.open()`, `os`
through `pathlib`, `open(**kwargs)`), which were fixed and turned into negative controls. CI passed
on Python 3.10–3.14.

Live check after the merge, on a fresh clone with Claude Code 2.1.330 (run by the owner): an edit
to `.github/workflows/tests.yml` was blocked; adding `import socket` to the package was blocked; an
edit to `README.md` was allowed (and reverted); the privacy subagent was recognised with its
read-only tools. Notes: the subagent was recognised but not yet invoked; `/agents` no longer
exists in that version; in auto mode, Bash commands that the guard allows run without
confirmation. Ratified by the owner on 22 November 2026: `LHR-CCSETUP-AB = CLOSED_WITH_NOTES`.

## Accepted residual risks

| ID | Decision | Summary |
|---|---|---|
| RR1 / RG-E15-F1 | `ACCEPTED_RESIDUAL_RISK`, ratified 11 November 2026 | Revocation of an existing `success` is not atomic |
| RR3 / RG-E18-F2 | `ACCEPTED_RESIDUAL_RISK`, ratified 11 November 2026 | Without the trigger, revocation depends on a best-effort schedule |

Both rely on the procedures in [governance.md](governance.md#residual-risks): revoke through
draft, and check head uniqueness before merging.

## Evidence packages

Packages kept by the owner outside the repository (SHA256 of each ZIP):

| Package | SHA256 |
|---|---|
| RG-E15 review | `65e32b4ab6216a182e44bc4deb32d94291a29ac6f313f93476baeaee16519b75` |
| RG-E18 / RG-E18b review | `ebf9b2b13e0c5a5769d3359084e1804198f5d7d11d5aea7c6ceaa0b55d419c1a` |
| RG-E13 preparation | `bb6fbcfb6b1d6771e5ee9ed7391b9e172569fe18361d283c28b783c0173855ac` |
| RG-E13 close | `bd167d76c9b318181f2c73e18b64e78ebe53c39379d07b724d2852122a92466c` |
| RG-E13 reconciliation | `0590a601f609eaaf47f151503855ad96117a25e1c162642b329658fb5afd7632` |
| RL-E F0 | `de0cd9a3e5bee2eff30c195d34602a3ede65549ec2e37de01afe653cedb670f7` |
| RL-E F1 | `e9fb28e5413169eb17b8414aeaf239eaf8188b9747d2a8e8b9e41d33044cbf1c` |
| RL-E F2 | `8208545f6e6425cd8abb4347518db54398c24139fba4b90e6c324837dd5aecaf` |
| RL-E F3 | `c86315d6afcae4184fcf81f2419aef6e1e2f81536d4bbdfecc30d33980cbd5b4` |
| MERGE-METHODS preparation (C1) | `60a06f35982addde1c8e4f224a0ea870c4bd40fd22df81cc56840bda9b63d70d` |
| MERGE-METHODS applied | `765abddf108aa679068fd5e2067f145faa4448154304fb592b7acb7e216f9848` |
| RR4 preparation | `9441dd398091ca0770e7beb5581d4e6d4e6a2dab64fd06765f3495ac178cd254` |
| RR4 first run after merge | `acd3f51f2d48e623ce207de31dcda626b83c01b43aeec47b29015124dfcf81c9` |
| Cleanup preparation | `d2bdad949b37a5c815a8dcc3a4d5896b2e4a0372c30ae44a54ee9b164c8f70e6` |
| Cleanup C1 package run (C1a) | `5e679c3c3255a36c4db8dd2e1fc17d057a410ac3926c23d7148498bc208c98be` |
| Cleanup C1 archived | `d21341bfe41b46a79e2e655a9509d0b991528052a4714cb72e62478352e11503` |
| Cleanup C2/C3 preparation | `0d70a0ac907964ba4db4d466186c914f08ba1f86f1af77b28abdc813219613fd` |
| Cleanup C2 archived | `1c4e790bf0da0dd2a81ad785a242064f6fc5d426c72e5336126b9da4ea9aabcc` |
| Cleanup C3 package run (C3a) | `e5eb312f99abae1d72dcf7ae8f4174158dde4872b21a057708000b3f8ce861f9` |
| Cleanup C3 deleted | `4076188d0111e50f6358eff3bee78222436070a32ebf212de17d338e4b5c5fc0` |
| Actions pinning inventory | `c934819a066e52b8a2ec82bcac953b00e3c897137c49f8270355c80ce837e6d5` |
| Actions policy S1 preparation (C2, run) | `8b85652babdb6f182fe0cc05920bcd98980b46e822a750a043c67bb861c92e46` |
| Actions policy S1 observation | `e87f443a20b906aad015f23adb753d097e53632aef6d82ee5457aa2b88353e24` |
| Actions policy S1 observation, addendum C4 | `d7f26a1358afa430babbcaf33f299a36fd9ba39b99acd6379e2876bfdfca4cc7` |
| Claude Code guardrails candidate (C2, approved) | `6c287215d6f2f59f5957bda9c4ab43fa0cee20b2ae374aa3a09965cb036a33a8` |
| Claude Code guardrails after merge | `e2c064dbe2d8f0288e170d375af159e1edb40593f3c3ecb7ec930e94dc80cf92` |

The other scenarios (RG-SB1–SB4, RG-E1–E12, RG-E14, RG-E16, RG-E17b, RG-E19) were reviewed
from JSON evidence delivered per scenario rather than as packages.

GitHub keeps workflow run logs for a limited time (90 days by default). After that, the run
links in the evidence stop showing logs; the archived packages remain the durable record.
