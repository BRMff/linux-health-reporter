# Engineering Workflow: Human-Controlled AI-Assisted Development

## Purpose and boundaries

Linux Health Reporter is a small, read-only Linux CLI, **not** a multi-agent system. This document describes the human-directed, AI-assisted engineering workflow used to design, implement and review it. It is a portfolio example of engineering discipline and agent/tool governance, not evidence that the application autonomously operates agents.

The standalone code and synthetic fixtures were kept separate from private projects. Infrastructure identifiers, private telemetry, access details and internal source code are excluded from this public-facing documentation.

## Responsibilities

**Human owner — Fábio Folgado**
- Defined the intended behavior, privacy requirements, scope and acceptable outputs.
- Authorized individual implementation steps and cross-machine tests.
- Reviewed findings, decided which corrections to make and retained the final release decision.

**AI-assisted implementation**
- Helped draft source code, deterministic synthetic fixtures and regression tests.
- Operated within scoped, explicitly approved file changes.
- Produced reproducible evidence, including test results and file-integrity checks.

**Independent second-model review**
- Examined the implementation and test contracts separately from the coding assistant.
- Challenged assumptions with synthetic adversarial probes, including targeted mutations.
- Reported shortcomings and limitations for the human owner to reconcile.

Independence here means a different reviewing model and a separate review task. It does **not** mean a formal third-party audit or certified evaluation.

## Workflow

1. **Requirements:** define fixed collection targets, stdout-only reporting, error handling, and privacy constraints.
2. **Scope and approval:** allow writes only in the standalone project; do not grant agents general access to private services.
3. **Implementation:** keep runtime dependencies to Python's standard library, with injected readers and clocks for hermetic tests.
4. **Automated testing:** use synthetic content rather than live host measurements.
5. **Adversarial review:** inspect failure modes, identifier leakage, permissive schemas and negative-control quality.
6. **Correction and reconciliation:** fix bounded findings, re-run the suite and verify the exact changed-file inventory.
7. **Cross-environment checks:** compare the same SHA256-pinned sources and synthetic tests on two Linux machines.
8. **Human release gate:** perform licensing, public-content hygiene, attribution and final release approval separately.

This workflow is human-in-the-loop. AI-generated output is treated as material to verify, not automatically trusted.

### Local Claude Code guardrails

The repository ships project settings for Claude Code in `.claude/`. They apply only when Claude Code runs in a checkout of this repository.

- **Guard hook** (`.claude/hooks/guard.py`, PreToolUse): blocks edits to `.github/`, `ci/`, `.claude/` and `LICENSE`, which change only with a separate owner authorisation. Paths are checked both as written and with symlinks resolved. It also blocks package code that fails the runtime contract checker. Bash commands that name a protected path together with a writing command are blocked on a best-effort basis.
- **Two failure cases, both blocking:** an error inside the guard script makes it exit 2. A hook that cannot start or does not answer within its 30-second timeout is blocked by `"onFailure": "block"` in `.claude/settings.json`, which needs Claude Code v2.1.295 or later; older versions let the call through in that case.
- **Package contract in CI** (`tests/test_runtime_imports.py`): imports must come from a standard-library allowlist. Process, dynamic-code, host-identifier and file-writing APIs are rejected, including through `import os as …`, `from os import …`, `getattr()` and `Path.open()`. Checked with `ast` on every pull request. The guard carries the identical checker text, and a test fails if the two copies differ. The hook gives earlier feedback; the test is the enforced control.
- **What the static check does not prove:** it inspects syntax, not behaviour. It is not evidence that the package never writes, uses the network or collects identifiers; those guarantees rest on the privacy and collector tests and on review.
- **Privacy pre-review subagent** (`.claude/agents/privacy-adversarial-reviewer.md`): read-only (`Read`, `Grep`, `Glob`), on a different Claude model from the main session. It runs the step 5 checklist before the external review. It does **not** replace the independent second-model review: it is still a Claude model.

These are guardrails against mistakes, not a security boundary. A hook does not see every way a file can change, and Claude Code settings can be changed by whoever runs it. The controls that bind are the `main` ruleset and the owner review gate ([governance.md](governance.md)).

## Reproduction

From the project root, run:

    PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest discover -s tests -v

Expected for v0.2.0: **106 passing tests**. (The v0.1.0 baseline was 37.)

Test inputs simulate `/proc`, `statvfs`, clocks and CLI collection through dependency injection or mocks. They are not live system-health readings.

### Evidence snapshot: v0.2.0 (2026-11-01)

Merged through PR #1 (merge commit `a908457`, tree identical to the reviewed head `1ef507c`). CI on `main` after the merge: 6/6 jobs green.

Initial evidence, before review:

- **94/94 synthetic tests** passed locally on Python 3.10, 3.11, 3.12, 3.13 and 3.14 (interpreters provided by `uv`), and from an extracted sdist.
- **12 deliberate code mutations** (for example: treating permission errors as `unsupported`, counting `unsupported` as a failure, accepting PSI values above 100, reporting CPU `full`, reading `/proc/swaps`, reordering v0.1 keys, leaking an exception message) were each detected by the suite.
- A **contract test** compares v0.2 output with the unmodified v0.1.0 sample: all v0.1 keys keep their values, types and order; only `schema_version` differs.
- Package build was reproduced locally with setuptools (sdist → wheel), installed in a clean virtual environment and smoke-tested (`--help`, `--version`, invalid arguments, synthetic import checks). The CI job repeats this on GitHub-hosted runners with pinned, hash-verified build tools.
- No collection was run against a real host for this evidence.

### Independent review: v0.2.0, round 1

A separate model performed a static review of PR #1 (verdict: hold, one major finding). Each finding was reproduced with synthetic inputs before being addressed:

- **R-01 (major), fixed:** a malformed CPU `full` line made CPU pressure `unavailable`. The line is now skipped before validation.
- **R-02, fixed:** the Markdown label for `unsupported` no longer asserts a kernel cause ("Interface not exposed").
- **R-03, fixed:** the v0.1 fixture is pinned by SHA-256; contract tests added for failure shapes and a second input set.
- **R-05, fixed:** the CI wheel check accepts only the expected `.dist-info` directory and an explicit member list, with unit tests.
- **R-06, fixed:** a PSI block marked `ok` without window objects counts as a failure for the exit code.
- **R-08, fixed:** byte formatting uses integer arithmetic; very large values no longer overflow Markdown rendering.
- **R-09, fixed:** the clock is read before any source again, matching v0.1.
- **R-04, fixed after an explicit owner decision:** actions pinned by commit SHA (verified against the official `actions/*` repositories), hash-pinned build tools from PyPI file details, a dedicated build environment with `--no-isolation`, a separate install environment, and weekly Dependabot proposals without auto-merge. `pyproject_hooks` was pinned at 1.2.0 because the 1.3.3 file details could not be retrieved from PyPI at the time; Dependabot proposed the upgrade after the merge.
- **R-07, fixed:** PR description updated with each CI run.

Every fix was verified by reverting it and confirming that at least one test fails. Of the reviewer's six suggested surviving mutations, five are now detected; the sixth describes the intended behaviour after R-01.

### Independent review: v0.2.0, rounds 2 and 3

- **Round 2** (`460b308..d68fdd9`): pass with reservations. The hold was lifted; R-04 remained open for an owner decision.
- **Round 3** (`d68fdd9..1ef507c`): pass. R-04 resolved within the agreed scope; **R-01 to R-09 closed**. The reviewer confirmed the action SHAs, the four wheel hashes against PyPI, and that the CI-tested merge commit had the same tree as the reviewed head.

Scope notes recorded by the reviewer:

- The PyPI JSON API and the file-details page are two views of the same authority, not independent sources.
- Pinning covers the agreed build dependencies only; the Python patch version, the runner image and the `pip` bundled in each virtual environment are not pinned.
- A deliberately wrong hash was not exercised in CI; the job relies on pip's documented `--require-hashes` verification, with no fallback.
- Claims about CI data are limited to this project: no collection of real metrics by Linux Health Reporter was identified in the reviewed steps.

### Evidence snapshot: v0.1.0 (2026-10-30)

- **37/37 synthetic tests** passed on two Linux/KDE neon machines, both using Python 3.12.3.
- **12/12 files from the earlier P2 snapshot** matched across machines by SHA256. Ten unchanged code/test/example/.gitignore files remain part of the 14-file release tree; the P2 README.md and pyproject.toml were later changed during P3, and LICENSE plus this workflow document were added afterwards. The final 14-file tree was verified locally, **not** hash-compared cross-host.
- The second machine used a temporary RAM-backed copy with cleanup verified.
- Python 3.10, 3.11 and 3.12 source syntax was checked; **only 3.12.3 was execution-tested**.
- A separate AI-model reviewer reported **41 in-memory mutation probes**, of which **34 were detected and 7 survived**. Those figures describe one exploratory review, not a mutation-testing certification or a comprehensive security score.
- No live host-telemetry validation was performed in those synthetic test runs.

## Known limitations and follow-up work

- From the v0.1.0 review: three low-severity test-coverage gaps (independent proof of the `total_bytes > 0` rule, acceptance of memory available equal to total, and explicit negative tests for invalid metric status/non-null unavailable fields) and some informational notes on permissive parsing of injected text. The v0.2 privacy and PSI tests add negative cases for invalid statuses and non-null values in non-`ok` blocks; the remaining items stay open.
- Python 3.10–3.14 are exercised in CI. Python 3.10 reaches end of life in October 2026; dropping it is a separate, future decision.
- PSI and swap behaviour inside containers is covered by simulated scenarios only; real values may describe the host.
- The CLI has not been benchmarked or deployed as a production monitoring service.

These items should remain visible rather than being converted into claims of comprehensive coverage.

## Privacy, security and attribution

The report schema is deliberately limited to a small allowlist of numeric fields and an optional UTC timestamp; this reduces, but does not eliminate, disclosure risk. Real reports may still reveal usage patterns. The collector reads only an explicit allowlist of sources (enforced by a test); `/proc/swaps` is excluded because it contains device names and paths.

Project code and documentation may be reused under the [MIT License](../LICENSE). The copyright holder is Fábio Folgado. Development assistance from AI tools and a separate AI-model review are disclosed to distinguish human responsibility from AI-generated contributions.
