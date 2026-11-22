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
| `.github/workflows/review-gate.yml` | Trusted reconciler. Runs from `main` on `workflow_run` (when the trigger completes), on a 30-minute schedule (`schedule`, best effort: see LG3) and on demand (`workflow_dispatch`). It is in one global concurrency group, `review-gate`, so its runs never overlap. |
| `ci/review_gate.py` | Decision logic (standard library only). It never executes pull-request code. |
| Label `review:passed` | Owner approval. Applied **only by the owner**, after reviewing the exact head commit. |
| Commit status `review-gate` | Written on the head SHA of each open PR. Required check in the `main protection` ruleset since 8 November 2026 (RG-E11). |

`pull_request_target` is deliberately not used, and this design does not need it. On
2 November 2026 GitHub announced that, starting 2 December 2026, it will block
`pull_request_target` events by default in public repositories unless an Actions event policy
allows them ([GitHub Changelog](https://github.blog/changelog/2026-11-02-pull_request_target-blocked-by-default-december-2)).

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
   writes `pending` ("reconciliation incomplete") for it, never `success`. A head whose
   status already matches the decision is left unchanged, except the head that got the
   pre-write: its decision is always written, because the statuses read may not include
   the pre-write yet (RR4).

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
- **SG3.** When a reconciliation obtains the complete inventory of open PRs, it never writes
  `success` for a SHA that is the head of more than one open PR, and writes `failure` for it.
  A `success` written before the second PR was opened stays visible until that reconciliation
  succeeds (about 19 seconds in RG-E15). Revocation is not atomic (RR1).
- **SG4.** **Draft PRs cannot be merged**, whatever their statuses. This is enforced by GitHub,
  not by the reconciler: after a conversion to draft the old `success` stays on the head until
  the next reconciliation (about 17 seconds in RG-E18b).
- **SG5.** Changes inside a PR cannot alter the reconciler that evaluates it.

### Liveness (depends on runs)

- **LG1.** Every *processed* event leads to a reconciliation that starts after it. A newer
  queued run can cancel the pending one, but it also starts after the event and
  reconciles every open PR.
- **LG2.** Removing the label takes effect once that reconciliation writes successfully.
- **LG3.** The scheduled run and `workflow_dispatch` repair anything missed, including events
  that were never processed. The schedule is **best effort**: GitHub delays or drops scheduled
  runs. Observed gaps between scheduled reconciler runs reached about 10.5 hours (RG-E8), and
  with the trigger disabled an approval withdrawal took 3 h 51 min to take effect (RG-E18).
  There is no demonstrated upper bound. In public repositories GitHub disables schedules
  after 60 days without activity.
- **LG4.** The run summary lists every change it made.

**How to revoke an approval urgently** (removing only the label depends on liveness):

1. Convert the PR to draft. GitHub then refuses the merge (SG4).
2. Remove the `review:passed` label.
3. If the trigger may not have run, dispatch `review-gate.yml` on `main`.
4. Confirm on the exact head SHA that `review-gate` is no longer `success` and that the PR is
   blocked. A dispatch does not replace this check.

The trigger stays enabled in normal operation. Disabling it is an explicit owner decision.

**Before authorising a merge**, the owner checks that:

- the `review-gate` status on the head reads "approved by owner at `<sha7>`" and its run is
  the latest one for that head, and the other required checks passed;
- the head SHA is the head of **exactly one** open PR. List every open PR and compare
  `.head.sha` exactly; a search by SHA also finds PRs that merely contain the commit:

  ```bash
  gh api --paginate 'repos/BRMff/linux-health-reporter/pulls?state=open&per_page=100' \
    --jq '.[] | {number, head_sha: .head.sha}'
  ```

  An incomplete listing or an API error means no authorisation.

These checks are procedural. They do not remove the race between the check and the merge.

### Residual risks

| ID | Risk | Status |
|---|---|---|
| RR1 | Non-atomic window: the label or the PR inventory can change between a run's read and its write. A stale `success` lasts until the next successful reconciliation. Observed: a second PR opened on an approved SHA (about 19 s, RG-E15) and a conversion to draft (about 17 s, RG-E18b) | Accepted (`LHR-RG-E15-F1 = ACCEPTED_RESIDUAL_RISK`); revoke via draft; check head uniqueness before merging |
| RR2 | GitHub documents that *"a required status check must have completed successfully in the chosen repository during the past seven days"*. Whether an existing status expires after seven days is not demonstrated | Activation done on 8 November 2026, less than two days after the first real `success` (6 November, RG-E2). The expiry question stays open |
| RR3 | Without the trigger, revocation depends on the best-effort schedule: 3 h 51 min observed in RG-E18, no upper bound | Accepted (`LHR-RG-E18-F2 = ACCEPTED_RESIDUAL_RISK`); urgent revocation procedure above |
| RR4 | After the pre-write, a stale read can make the final decision equal the previous status ("unchanged"), so the "reconciling `<sha7>`" `pending` stays visible until the next run (RG-E10, finding E10-F1). Availability only: the status is `pending`, never `success` | Fixed in PR #9: the head that got the pre-write always receives its final decision (unit tests U38–U41). The first run after the merge succeeded, but with no open PR the fixed path was not exercised end to end. Closed: `LHR-RR4 = CLOSED_WITH_NOTES` |
| RR5 | A `success` on the test merge commit takes precedence over a `failure` on the head (sandbox RG-SB3, finding SB-F1) | Out of scope (deliberate abuse): no workflow writes `review-gate` to merge commits |

### Required status and the test merge commit

GitHub documents that when the test merge commit of a pull request has a status, that
commit must pass; otherwise the head commit must pass. The reconciler never writes to
merge commits, and no other workflow writes the `review-gate` context. A status forged on
the merge commit is deliberate abuse, outside scope. The sandbox tests RG-SB1–SB4 (PASS)
confirmed this precedence before the check was made required (RR5). The test merge commit
also gets a new SHA when a pull request is reopened.

## Supported merge method

Only **merge commits** are supported. The release gate rejects squash merges, rebase
merges, octopus merges and direct pushes on `main`, because the approved head must be the
second parent of the merge (see [release-process.md](release-process.md#pre-gate-history-and-supported-merges)).
Since 11 November 2026 the `main protection` ruleset allows only `merge`
(`allowed_merge_methods: ["merge"]`), so GitHub refuses a squash or rebase merge into `main`
even for an approved PR. The repository-wide settings still allow squash and rebase; the
restriction applies to `main` through the ruleset. The release gate keeps detecting any
unsupported commit that reaches `main` (RL-E9, RL-E10).

## Activation (completed)

Each step was a separate owner action, authorised separately.

1. PR #4 merged with a merge commit while `main` was at `GATE_BASELINE` (merge `ebbc91b`).
2. Label `review:passed` created.
3. `docs/governance-introduction.md` added through PR #5 (merge `ee9a1e8`).
4. Sandbox tests RG-SB1–SB4, production validation RG-E1–E19 on PR #6 (closed without merge),
   and release-gate validation RL-E1–E10 in a replay sandbox. Results and accepted risks are in
   [governance-validation.md](governance-validation.md).
5. `review-gate` added as a required status check in the `main protection` ruleset on
   8 November 2026, confirmed by RG-E11.
6. Merge methods for `main` restricted to `merge` on 11 November 2026.

## GitHub Actions policy

Since 20 November 2026 the repository's Actions permissions require every action to be pinned
to a full-length commit SHA (`sha_pinning_required: true`; `allowed_actions` stays `all`). A
workflow that references an action by tag, branch or short SHA fails, as GitHub documents (the
blocking itself has not been exercised here), so new or updated workflows must use full SHAs, with
the version in a comment, as the current ones do. Dependabot update proposals are expected to keep
that format; none has been opened since the setting changed.

Dependabot's own jobs are an exception: they use `github/dependabot-action@main` and, as GitHub
documents, bypass Actions policy checks. The pull requests Dependabot opens run the normal
workflows and are subject to the policy and to the review gate. Restricting `allowed_actions` to
selected actions is a possible next step, not applied. The change and its observation are recorded
in [governance-validation.md](governance-validation.md#actions-policy-sha-pinning-s1).

## Changing required checks

When a PR changes the CI matrix or job names:

1. Get CI green on that PR.
2. The owner updates the ruleset's required checks immediately before merging.
3. Merge.
4. Confirm CI on `main`.

Also update `MAIN_REQUIRED_CHECKS` in `ci/release_gate.py`; a test keeps it in sync with
`tests.yml`.
