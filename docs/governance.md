# Governance: review gate

This document describes how changes reach `main` and what the controls do and do
**not** guarantee. Release tagging is covered in [release-process.md](release-process.md).

## Threat model and limits

- The owner and the AI agent that assists with development act through **the same
  GitHub identity**. None of these controls separates their authority: anything the
  owner can do, an agent holding the owner's credentials can also do.
- The goal is **process control**:
  - prevent merges by oversight;
  - prevent stale or reused approvals;
  - keep an auditable trail (SHA, event, author, run).
- **Agent rule (procedural).** The agent never applies the `review:passed` label, never
  merges and never creates tags or releases without explicit authorisation from the
  owner. This rule is procedural and does **not** prevent deliberate abuse of a shared
  identity.
- **Out of scope (deliberate abuse):**
  - a pull request that adds a workflow designed to forge the `review-gate` status;
  - a status forged on the test merge commit (see "Required status and the test merge commit").

  The mitigation is human review of every change under `.github/`.

## How the review gate works

| Piece | Role |
|---|---|
| `.github/workflows/review-gate-trigger.yml` | Signal only. Runs on `pull_request` events (`opened`, `reopened`, `synchronize`, `edited`, `labeled`, `unlabeled`, `converted_to_draft`, `ready_for_review`) with no permissions, no checkout and no code. Editing or deleting it in a PR can only prevent reconciliation, which fails closed. |
| `.github/workflows/review-gate.yml` | Trusted reconciler. Runs from `main` on `workflow_run` (when the trigger completes), every 30 minutes (`schedule`) and on demand (`workflow_dispatch`). It is in one global concurrency group, `review-gate`, so its runs never overlap. |
| `ci/review_gate.py` | Decision logic (standard library only). It never executes pull-request code. |
| Label `review:passed` | Owner approval. Applied **only by the owner**, after reviewing the exact head commit. |
| Commit status `review-gate` | Written on the head SHA of each open PR. Becomes a required check once activated in the ruleset. |

`pull_request_target` is deliberately not used. Since 2 December 2026 it is disabled by
default in public repositories without an event policy, and this design does not need it.

### Order inside a run

1. **Check provenance.** The running workflow must be `review-gate.yml` from `main`. If not,
   the run stops with **no mutation at all**.
2. **Validate the event context** (`workflow_run` only). The producer is identified by
   exact path (with or without `@<ref>`), name and `workflow_id`. If validation fails,
   there is no pre-write, but full reconciliation still runs.
3. **Read the complete inventory of open PRs** (every page). If any page fails, the run
   stops with **no mutation**.
4. **Pre-write `pending`** on the event SHA. This is the first mutation, and only happens
   if that SHA is the exact head of an open PR and not a test merge commit.
5. **Reconcile the head of every open PR** from current state. An API error on a head
   writes `pending` ("reconciliation incomplete") for it, never `success`.

### Decision for each head SHA

| Condition | Status |
|---|---|
| More than one open PR has this SHA as head | `failure`: approvals are never shared between PRs |
| Head in a fork, or base is not `main` | `failure` |
| No gate status since the head became current (no "marker") | `pending`: awaiting owner review |
| Draft PR | `pending` |
| Label absent | `failure` "approval withdrawn" if it was approved, otherwise `pending` |
| Label present and the latest `review:passed` event is `labeled` by the owner (a user account), **strictly after** the marker | `success` |
| Any other case | `pending`: remove and re-apply the label |

A new commit, a force push, a base change, reopening the PR or converting it to draft
all invalidate an earlier approval.

## Guarantees

### Safety (does not depend on runs being delivered)

- **SG1.** `success` is written only if, at the time of writing, a valid owner approval
  exists for that exact SHA and that SHA is the head of exactly one open PR.
- **SG2.** An approval never moves to another SHA. A new commit has no `review-gate`
  status, and a missing required status blocks the merge.
- **SG3.** A SHA shared by several open PRs never receives `success`.
- **SG4.** **Draft PRs cannot be merged**, whatever their statuses. This is enforced by GitHub.
- **SG5.** Changes inside a PR cannot alter the reconciler that evaluates it.

### Liveness (depends on runs)

- **LG1.** Every *processed* event leads to a reconciliation that starts after it. A newer
  queued run can cancel the pending one, but it also starts after the event and
  reconciles every open PR.
- **LG2.** Removing the label takes effect once that reconciliation writes successfully.
- **LG3.** The scheduled run (every 30 minutes) and `workflow_dispatch` repair anything
  missed, including events that were never processed. In public repositories GitHub
  disables schedules after 60 days without activity.
- **LG4.** The run summary lists every change it made.

**How to revoke an approval reliably:** **convert the PR to draft** (SG4) *and* remove the
label. Removing only the label depends on liveness.

**Before authorising a merge**, the owner checks that the `review-gate` status on the head
reads "approved by owner at `<sha7>`" and that its run is the latest one for that head.

### Residual risks

| ID | Risk | Status |
|---|---|---|
| RR1 | Non-atomic window: the label can change between a run's read and its write. A stale `success` lasts until the next run in the serialised group | Accepted; revoke via draft |
| RR2 | GitHub documents that *"a required status check must have completed successfully in the chosen repository during the past seven days"* | Activate the ruleset within seven days of a real `success`. Whether an existing status expires is not demonstrated |
| — | Required status and the test merge commit (see the next section) | Pending sandbox proof |

### Required status and the test merge commit

GitHub documents that when the test merge commit of a pull request has a status, that
commit must pass; otherwise the head commit must pass. The reconciler never writes to
merge commits, and no other workflow writes the `review-gate` context. A status forged on
the merge commit is deliberate abuse, outside scope. This behaviour must be confirmed in
a disposable sandbox before the check is made required (tests RG-SB1–SB4).

## Supported merge method

Only **merge commits** are supported. The release gate rejects squash merges, rebase
merges, octopus merges and direct pushes on `main`, because the approved head must be the
second parent of the merge (see [release-process.md](release-process.md#pre-gate-history-and-supported-merges)).
Restricting `allowed_merge_methods` to `merge` in the ruleset is a separate owner decision.
Until then, this rule is enforced by detection, not prevention.

## Activation (owner actions, each separately authorised)

1. Merge this governance change (PR #4) with a merge commit while `main` is still at
   `GATE_BASELINE` (`dea0ac9`). If `main` has moved, stop: a new owner decision is required.
   The reconciler only runs from `main`, so it does not evaluate its own pull request.
2. Create the label `review:passed`.
3. Add `docs/governance-introduction.md` through a normal gated PR, recording the final head,
   the merge commit, the independent review and the owner's authorisation (format in
   [release-process.md](release-process.md#gate-introduction-pr-4)). Releases fail P3 until it exists.
4. Run the sandbox tests (RG-SB1–SB4, release gate) and the production validation PR (RG-E1–E19).
5. Add `review-gate` as a required status check in the `main protection` ruleset, within
   seven days of a real `success` (RR2). Confirm with RG-E11. Roll back if anything diverges.

## Changing required checks

When a PR changes the CI matrix or job names:

1. Get CI green on that PR.
2. The owner updates the ruleset's required checks immediately before merging.
3. Merge.
4. Confirm CI on `main`.

Also update `MAIN_REQUIRED_CHECKS` in `ci/release_gate.py`; a test keeps it in sync with
`tests.yml`.
