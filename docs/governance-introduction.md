# Gate introduction record

This file records the out-of-band review and owner authorisation of pull request #4,
which introduced the review gate and the release gate. That pull request could not carry
a `review-gate` status, because the reconciler only runs from `main`. The release gate
accepts its merge only when the values below match the repository history
(see [release-process.md](release-process.md#gate-introduction-pr-4)).

The record is declared by the same GitHub identity that owns the repository. It documents
the review and the authorisation; it does not prove them.

## Validated values

The five lines below are read by `ci/release_gate.py`. Each key must appear exactly once
in this file.

```text
gate-introduction-pr: 4
head-sha: 7524f6d3dde0d5996799f49a1ca4b3b179068967
merge-commit-sha: ebbc91b0d831d68b7a897c0511cdabc0ddfdf81a
independent-review: LHR-GOV-PR4-R3 = PASS_NARROW
owner-authorization: LHR-GOV-PR4-MERGE = AUTHORIZED
```

## Context (not validated)

- The merge commit has `dea0ac9379cc688b3ff9edce16f17bf41c29d747` (`GATE_BASELINE`) as
  its first parent and the final head of pull request #4 as its second parent.
- Pull request #4 went through independent review rounds R1 to R4. Each round's findings
  were fixed on the same branch, without rewriting commits, before the next round.
- The owner chose the review result recorded above.
