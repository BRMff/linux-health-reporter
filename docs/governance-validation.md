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
| RG-E10 Label and unlabel sequences | PASS. Finding E10-F1 (RR4) |
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

The other scenarios (RG-SB1–SB4, RG-E1–E12, RG-E14, RG-E16, RG-E17b, RG-E19) were reviewed
from JSON evidence delivered per scenario rather than as packages.

GitHub keeps workflow run logs for a limited time (90 days by default). After that, the run
links in the evidence stop showing logs; the archived packages remain the durable record.
