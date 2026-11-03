# Release process

Releases are tags `vX.Y.Z` created by the owner. The `release tags` ruleset makes them
immutable once created. GitHub **cannot** make tag creation conditional on checks, so this
process combines a preventive preflight with detection after the tag is created.
Governance limits are described in [governance.md](governance.md).

## 1. Before the tag (preventive)

1. Merge everything for the release through reviewed pull requests.
2. Wait until CI on the `main` head is green.
3. Run the **release-gate** workflow (`workflow_dispatch`) **from `main`** with:
   - `tag`: `vX.Y.Z`
   - `sha`: the exact 40-character SHA of the **current** `main` head
4. The `verify` job must pass every condition:

   | # | Condition |
   |---|---|
   | P1 | `sha` is the current `main` head |
   | P2 | the required CI checks succeeded on `sha` |
   | P3 | every first-parent commit between `sha` and `GATE_BASELINE` is a supported merge (see below) of a PR whose head had `review-gate: success`. The walk always reaches `GATE_BASELINE` through the verified gate introduction; earlier release tags are never stopping points |
   | P4 | the tag matches `__version__`, and `CHANGELOG.md` has a dated entry for that version |
   | P5 | the tag does not exist yet |

   The run also checks its own provenance: it must be dispatched from `main` and run the
   `main` version of the workflow.
5. The `record` job then writes the evidence status `release-preflight/<tag>` on `sha`,
   linked to the run. If `verify` fails, no evidence is recorded.
6. The **owner authorises** the release in writing. This step is procedural; GitHub cannot
   tell the owner apart from an agent using the same identity.
7. The owner creates the tag and release in the GitHub interface, with target = **the
   exact SHA**, not the branch name. Add the preflight run URL to the release notes.

## 2. After the tag (detection)

Pushing a `v*` tag runs **release-gate** on the tag:

| # | Condition |
|---|---|
| T1 | the tag exists and resolves to commit `C` (lightweight or annotated) |
| T2 | `C` is on the first-parent history of `main`; `main` may have moved on since |
| T3 | P2–P4 hold for `C` |
| T4 | a valid `release-preflight/<tag>` status exists on `C`: success, created by `github-actions[bot]`, linked to a successful `workflow_dispatch` run of `release-gate.yml` on `main` for `C` with the expected title, and created **before** the tag run. **Missing evidence is a failure**, never a warning |

The daily **release-audit** applies T1–T4 to every `v*` tag from `main`. It therefore also
catches a tag whose commit carries a modified or missing gate.

## Pre-gate history and supported merges

Pre-gate status comes **only** from fixed anchors in `ci/release_gate.py`, never from the
presence or absence of a workflow file:

| Anchor | Value | Meaning |
|---|---|---|
| `PRE_GATE_TAGS` | `v0.1.0` → `2895ccd9…`, `v0.2.0` → `a0616c83…` (full SHAs in the code) | The only tags reported as pre-gate. A pinned tag that resolves to another commit is a **FAIL**; every other tag gets T1–T4 |
| `GATE_BASELINE` | `dea0ac9379cc688b3ff9edce16f17bf41c29d747` | Commits at or before it on the first-parent history of `main` are pre-gate |
| `GATE_INTRODUCTION_PR` | `4` | The single exception described below |

`main` is protected against force pushes and deletion, so these anchors stay verifiable.
API errors, invalid anchors, or a history that does not reach `GATE_BASELINE` are a **FAIL**.

P3 always walks the whole first-parent history back to `GATE_BASELINE` (at most
`MAX_WALK` = 1000 commits; beyond that it fails closed). It never stops at an earlier
release tag, valid or not, because a tag does not prove that the merges before it passed
the gate. Consequently, **one unapproved or unsupported merge on `main` makes P3 fail for
every later release.** There is no automatic recovery and no exception mechanism. Moving
the historical boundary requires a documented incident, an explicit owner decision, an
independent review and preserved evidence. The walk limit can be revised separately,
without changing the anchors.

**Only merge commits are supported.** Each first-parent commit after `GATE_BASELINE` must:

1. have exactly two parents;
2. be the `merge_commit_sha` of exactly one PR merged into `main`;
3. have the PR head as its second parent.

Squash merges, rebase merges, octopus merges and direct pushes are a **FAIL**.

### Gate introduction (PR #4)

The PR that introduced the gates cannot carry a `review-gate` status, because the
reconciler only runs from `main`. Its merge is accepted only if **all** of these hold:

- its first parent is `GATE_BASELINE`; if `main` moved before the merge, this fails and a
  new owner decision is required;
- PR #4 is merged into `main` of this repository;
- its `merge_commit_sha` is the commit being evaluated;
- its final head is the second parent;
- `docs/governance-introduction.md` exists at the commit being released and matches.

The record is added after the merge, through a normal gated PR, because it must name the
merge commit:

```text
gate-introduction-pr: 4
head-sha: <final head SHA of PR #4, 40 hex>
merge-commit-sha: <merge commit SHA of PR #4, 40 hex>
independent-review: <LHR-… identifier> = PASS | PASS_WITH_FINDINGS | PASS_NARROW
owner-authorization: <LHR-… identifier> = AUTHORIZED[_…]
```

Each key appears exactly once. Until the record exists, every release fails P3. The record
is self-declared by the same identity: it documents the out-of-band review, it does not
prove it.

## 3. If a violation is detected

The gate cannot undo a publication: the tag is protected against deletion.

1. Mark the release with a warning in its notes and do not promote it.
2. Fix the cause.
3. Publish a new patch release through this process.

## What GitHub enforces and what stays procedural

| Property | Enforced? |
|---|---|
| Tags `v*` cannot be changed or deleted | Yes (ruleset) |
| Merges to `main` need the required checks | Yes (ruleset) |
| Run metadata and status creator and time cannot be edited | Yes (platform) |
| Tag creation only after a passing preflight | **No**: detected by T4 and the audit |
| Owner vs agent authorisation | **No**: procedural |
