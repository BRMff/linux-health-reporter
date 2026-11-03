"""Release gate (governance; not part of the package).

Modes:
  gate    workflow_dispatch: pre-tag preflight (P1-P5); push of a v* tag:
          post-tag verification (T1-T4).
  record  workflow_dispatch only, after a passing ``gate`` job: records the
          preflight evidence as the commit status ``release-preflight/<tag>``.
  audit   all v* tags (T1-T4); scheduled from main.

Inputs are validated before any API call. Every API error is a FAIL.
GitHub cannot make tag creation conditional on this gate: the post-tag check
and the audit detect violations, they do not prevent them.
"""

from __future__ import annotations

import base64
from datetime import datetime
import os
import re
import sys

from github_api import ApiError, GitHubClient

TAG_RE = re.compile(r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")
SHA_RE = re.compile(r"[0-9a-f]{40}")
REF_RE = re.compile(r"refs/(heads|pull|tags)/[A-Za-z0-9._/-]+")
TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
BASE_BRANCH = "main"
GATE_BOT = "github-actions[bot]"
RELEASE_GATE_PATH = ".github/workflows/release-gate.yml"
RELEASE_AUDIT_PATH = ".github/workflows/release-audit.yml"
REVIEW_CONTEXT = "review-gate"
PREFLIGHT_CONTEXT = "release-preflight/{tag}"
MAX_WALK = 1000

# Historical anchors (F01/F02). Pre-gate status is decided only by these
# fixed, verifiable values, never by the presence or absence of a workflow
# file. main is protected against force pushes and deletion, so these
# commits stay on its first-parent history.
#
# Tags published before the gates existed, pinned to their full commit SHA.
# Any other v* tag gets the complete T1-T4 verification.
PRE_GATE_TAGS = {
    "v0.1.0": "2895ccd9864e8c4d02bd9e5dd473717c3d61c5f8",
    "v0.2.0": "a0616c83560ed40bd24280465014292a268872c4",
}
# The main head on which the governance pull request was based. Commits
# at or before it on the first-parent history of main are pre-gate.
GATE_BASELINE = "dea0ac9379cc688b3ff9edce16f17bf41c29d747"
# The single exception (C3): the merge of the pull request that introduced
# the gates. It cannot carry a review-gate status because the reconciler
# only runs from main. Its first parent must be GATE_BASELINE, and the
# owner's record of the out-of-band review must be present.
GATE_INTRODUCTION_PR = 4
GATE_INTRODUCTION_RECORD = "docs/governance-introduction.md"
# Closed list of accepted review results (R3-N1), matched with fullmatch().
RECORD_REVIEW_RE = re.compile(r"LHR-[A-Z0-9-]+ = PASS(_WITH_FINDINGS|_NARROW)?")
RECORD_AUTHORIZATION_RE = re.compile(r"LHR-[A-Z0-9-]+ = AUTHORIZED(_[A-Z_]+)?")

# Required checks of pushes to main. Must match the job names of
# .github/workflows/tests.yml (enforced by tests/test_governance_workflows.py).
MAIN_REQUIRED_CHECKS = (
    "Python 3.10",
    "Python 3.11",
    "Python 3.12",
    "Python 3.13",
    "Python 3.14",
    "Build, install and smoke test",
)


class Report:
    def __init__(self) -> None:
        self.lines: list[tuple[str, str]] = []

    def ok(self, message: str) -> None:
        self.lines.append(("PASS", message))

    def fail(self, message: str) -> None:
        self.lines.append(("FAIL", message))

    def info(self, message: str) -> None:
        self.lines.append(("INFO", message))

    @property
    def failed(self) -> bool:
        return any(level == "FAIL" for level, _ in self.lines)


def parse_time(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("timestamp is not a string")
    return datetime.strptime(value, TIME_FORMAT)


def valid_tag(tag: object) -> bool:
    return isinstance(tag, str) and TAG_RE.fullmatch(tag) is not None


def valid_sha(sha: object) -> bool:
    return isinstance(sha, str) and SHA_RE.fullmatch(sha) is not None


def workflow_path_matches(path: object, expected: str) -> bool:
    if not isinstance(path, str):
        return False
    base, separator, ref = path.partition("@")
    if base != expected:
        return False
    return not separator or (REF_RE.fullmatch(ref) is not None and ".." not in ref)


# --------------------------------------------------------------------------
# Repository queries (all through the injected client)
# --------------------------------------------------------------------------

def main_head(client, repo: str) -> str:
    ref = client.get(f"/repos/{repo}/git/ref/heads/{BASE_BRANCH}")
    sha = (ref.get("object") or {}).get("sha") if isinstance(ref, dict) else None
    if not valid_sha(sha):
        raise ApiError("main head unavailable")
    return sha


def tag_commit(client, repo: str, tag: str) -> str | None:
    ref = client.get_optional(f"/repos/{repo}/git/ref/tags/{tag}")
    if ref is None:
        return None
    obj = ref.get("object") if isinstance(ref, dict) else None
    for _ in range(4):
        if not isinstance(obj, dict) or not valid_sha(obj.get("sha")):
            raise ApiError("malformed tag reference")
        if obj.get("type") == "commit":
            return obj["sha"]
        if obj.get("type") != "tag":
            raise ApiError("tag points to a non-commit object")
        annotated = client.get(f"/repos/{repo}/git/tags/{obj['sha']}")
        obj = annotated.get("object") if isinstance(annotated, dict) else None
    raise ApiError("tag nesting too deep")


def anchors_valid() -> bool:
    return (
        valid_sha(GATE_BASELINE)
        and isinstance(GATE_INTRODUCTION_PR, int)
        and GATE_INTRODUCTION_PR > 0
        and isinstance(PRE_GATE_TAGS, dict)
        and bool(PRE_GATE_TAGS)
        and all(valid_tag(tag) and valid_sha(commit) for tag, commit in PRE_GATE_TAGS.items())
    )


def commit_parents(client, repo: str, sha: str) -> list[str]:
    commit = client.get(f"/repos/{repo}/commits/{sha}")
    parents = commit.get("parents") if isinstance(commit, dict) else None
    if not isinstance(parents, list):
        raise ApiError("malformed commit")
    shas = [p.get("sha") if isinstance(p, dict) else None for p in parents]
    if not all(valid_sha(p) for p in shas):
        raise ApiError("malformed parent")
    return shas


def first_parent(client, repo: str, sha: str) -> str | None:
    parents = commit_parents(client, repo, sha)
    return parents[0] if parents else None


def on_main_first_parent(client, repo: str, sha: str) -> bool:
    current: str | None = main_head(client, repo)
    for _ in range(MAX_WALK):
        if current is None:
            return False
        if current == sha:
            return True
        current = first_parent(client, repo, current)
    raise ApiError("first-parent walk limit reached")


def file_exists(client, repo: str, path: str, ref: str) -> bool:
    return client.get_optional(f"/repos/{repo}/contents/{path}", {"ref": ref}) is not None


def file_text(client, repo: str, path: str, ref: str) -> str:
    data = client.get(f"/repos/{repo}/contents/{path}", {"ref": ref})
    if not isinstance(data, dict) or data.get("encoding") != "base64":
        raise ApiError("unexpected contents encoding")
    return base64.b64decode(data.get("content", "")).decode("utf-8")


def check_required_checks(client, repo: str, sha: str, report: Report) -> None:
    runs = client.get_all(f"/repos/{repo}/commits/{sha}/check-runs", key="check_runs")
    latest: dict[str, dict] = {}
    for run in runs:
        if not isinstance(run, dict) or (run.get("app") or {}).get("slug") != "github-actions":
            continue
        name = run.get("name")
        if name in MAIN_REQUIRED_CHECKS and (name not in latest or run.get("id", 0) > latest[name].get("id", 0)):
            latest[name] = run
    for name in MAIN_REQUIRED_CHECKS:
        run = latest.get(name)
        if run is not None and run.get("status") == "completed" and run.get("conclusion") == "success":
            report.ok(f"P2 check '{name}' success at {sha[:7]}")
        else:
            report.fail(f"P2 check '{name}' missing or not successful at {sha[:7]}")


def check_version(client, repo: str, sha: str, tag: str, report: Report) -> None:
    version = tag[1:]
    init = file_text(client, repo, "linux_health_reporter/__init__.py", sha)
    match = re.search(r'^__version__ = "([^"]+)"\s*$', init, re.MULTILINE)
    if match and match.group(1) == version:
        report.ok(f"P4 __version__ == {version}")
    else:
        report.fail(f"P4 __version__ does not match {tag}")
    changelog = file_text(client, repo, "CHANGELOG.md", sha)
    pattern = r"^## \[" + re.escape(version) + r"\] [—-] [0-9]{4}-[0-9]{2}-[0-9]{2}\s*$"
    if re.search(pattern, changelog, re.MULTILINE):
        report.ok(f"P4 CHANGELOG has a dated entry for {version}")
    else:
        report.fail(f"P4 CHANGELOG has no dated entry for {version}")


def merged_pull(client, repo: str, commit: str) -> dict | None:
    pulls = client.get_all(f"/repos/{repo}/commits/{commit}/pulls")
    merged = [
        pr for pr in pulls
        if isinstance(pr, dict)
        and pr.get("merge_commit_sha") == commit
        and pr.get("merged_at")
        and (pr.get("base") or {}).get("ref") == BASE_BRANCH
    ]
    return merged[0] if len(merged) == 1 else None


def read_record(text: str) -> dict[str, str] | None:
    """Parse the gate-introduction record: one ``key: value`` line per key,
    each key exactly once."""
    keys = ("gate-introduction-pr", "head-sha", "merge-commit-sha", "independent-review", "owner-authorization")
    record = {}
    for key in keys:
        found = re.findall(r"^" + re.escape(key) + r": (.+?)[ \t]*$", text, re.MULTILINE)
        if len(found) != 1:
            return None
        record[key] = found[0]
    return record


def check_gate_introduction(client, repo: str, commit: str, parents: list[str], evidence_ref: str, report: Report) -> bool:
    """C3: the single exception, for the merge of the PR that introduced the
    gates. Every condition must hold; anything else is a FAIL."""
    label = f"P3 gate introduction PR #{GATE_INTRODUCTION_PR} at {commit[:7]}"
    if parents[0] != GATE_BASELINE:
        report.fail(f"{label}: first parent is not GATE_BASELINE; main moved before the merge, a new owner decision is required")
        return False
    pr = client.get(f"/repos/{repo}/pulls/{GATE_INTRODUCTION_PR}")
    problems = []
    if not isinstance(pr, dict) or pr.get("number") != GATE_INTRODUCTION_PR:
        problems.append("pull request number")
        pr = {}
    if pr.get("merged") is not True or not pr.get("merged_at"):
        problems.append("not merged")
    if (pr.get("base") or {}).get("ref") != BASE_BRANCH or ((pr.get("base") or {}).get("repo") or {}).get("full_name") != repo:
        problems.append("base is not main")
    if pr.get("merge_commit_sha") != commit:
        problems.append("merge_commit_sha")
    if (pr.get("head") or {}).get("sha") != parents[1]:
        problems.append("second parent is not the final PR head")
    if not file_exists(client, repo, GATE_INTRODUCTION_RECORD, evidence_ref):
        problems.append(f"{GATE_INTRODUCTION_RECORD} missing")
    else:
        record = read_record(file_text(client, repo, GATE_INTRODUCTION_RECORD, evidence_ref))
        if record is None:
            problems.append("record malformed")
        else:
            if record["gate-introduction-pr"] != str(GATE_INTRODUCTION_PR):
                problems.append("record PR")
            if record["head-sha"] != parents[1]:
                problems.append("record head-sha")
            if record["merge-commit-sha"] != commit:
                problems.append("record merge-commit-sha")
            if not RECORD_REVIEW_RE.fullmatch(record["independent-review"]):
                problems.append("record independent-review")
            if not RECORD_AUTHORIZATION_RE.fullmatch(record["owner-authorization"]):
                problems.append("record owner-authorization")
    if problems:
        report.fail(f"{label} invalid: {', '.join(problems)}")
        return False
    report.ok(f"{label} verified (reviewed out of band, recorded in {GATE_INTRODUCTION_RECORD})")
    return True


def check_merge(client, repo: str, commit: str, parents: list[str], evidence_ref: str, report: Report) -> bool:
    """One first-parent commit after GATE_BASELINE. Only merge commits are
    supported: two parents, the merge of exactly one PR into main, and the
    second parent equal to that PR's head. Returns True only for a verified
    gate introduction."""
    if len(parents) != 2:
        report.fail(
            f"P3 commit {commit[:7]} has {len(parents)} parent(s): unsupported merge method "
            "(only merge commits; squash, rebase, octopus and direct pushes are rejected)"
        )
        return False
    pr = merged_pull(client, repo, commit)
    if pr is None:
        report.fail(f"P3 commit {commit[:7]} is not the merge of exactly one PR into main")
        return False
    number = pr.get("number")
    head = (pr.get("head") or {}).get("sha")
    if not valid_sha(head) or head != parents[1]:
        report.fail(f"P3 PR #{number} at {commit[:7]}: second parent is not the PR head")
        return False
    if number == GATE_INTRODUCTION_PR:
        return check_gate_introduction(client, repo, commit, parents, evidence_ref, report)
    if latest_status_ok(client, repo, head, REVIEW_CONTEXT):
        report.ok(f"P3 PR #{number} review-gate success at {head[:7]}")
    else:
        report.fail(f"P3 PR #{number} without review-gate success at {head[:7]}")
    return False


def check_reviews(client, repo: str, sha: str, report: Report) -> None:
    """P3: walk the whole first-parent history from ``sha`` back to
    GATE_BASELINE, verifying every merge on the way (R2-F04). Earlier release
    tags are never stopping points, valid or not: a tag does not prove that
    the merges before it passed the gate. The baseline must be reached
    through the verified gate introduction; reaching it any other way, the
    root, or the walk limit is a FAIL."""
    current = sha
    came_from_introduction = False
    for _ in range(MAX_WALK):
        if current == GATE_BASELINE:
            if came_from_introduction:
                report.info(f"P3 history at or before GATE_BASELINE {GATE_BASELINE[:7]} is pre-gate")
            else:
                report.fail(f"P3 reached GATE_BASELINE {GATE_BASELINE[:7]} without the gate introduction merge")
            return
        parents = commit_parents(client, repo, current)
        if not parents:
            report.fail(f"P3 history from {sha[:7]} does not reach GATE_BASELINE {GATE_BASELINE[:7]}")
            return
        came_from_introduction = check_merge(client, repo, current, parents, sha, report)
        current = parents[0]
    raise ApiError("first-parent walk limit reached")


def statuses_for(client, repo: str, sha: str, context: str) -> list[dict]:
    statuses = client.get_all(f"/repos/{repo}/commits/{sha}/statuses")
    selected = [s for s in statuses if isinstance(s, dict) and s.get("context") == context]
    return sorted(selected, key=lambda s: (parse_time(s.get("created_at")), s.get("id", 0)))


def latest_status_ok(client, repo: str, sha: str, context: str) -> bool:
    statuses = statuses_for(client, repo, sha, context)
    if not statuses:
        return False
    latest = statuses[-1]
    return latest.get("state") == "success" and (latest.get("creator") or {}).get("login") == GATE_BOT


def check_preflight_evidence(client, repo: str, server: str, tag: str, commit: str, before: datetime, report: Report) -> None:
    """T4: a valid release-preflight/<tag> status on the commit, created by
    the preflight run of release-gate.yml before the tag was pushed."""
    statuses = statuses_for(client, repo, commit, PREFLIGHT_CONTEXT.format(tag=tag))
    if not statuses:
        report.fail(f"T4 no preflight evidence for {tag} at {commit[:7]}")
        return
    status = statuses[-1]
    problems = []
    if status.get("state") != "success":
        problems.append("state")
    if (status.get("creator") or {}).get("login") != GATE_BOT:
        problems.append("creator")
    if not parse_time(status.get("created_at")) < before:
        problems.append("created after the tag")
    match = re.fullmatch(re.escape(f"{server}/{repo}/actions/runs/") + r"([0-9]+)", status.get("target_url") or "")
    if not match:
        problems.append("target_url")
    else:
        run = client.get(f"/repos/{repo}/actions/runs/{match.group(1)}")
        expected = {
            "event": "workflow_dispatch",
            "head_branch": BASE_BRANCH,
            "head_sha": commit,
            "conclusion": "success",
            "display_title": f"release-preflight {tag} {commit}",
        }
        if not isinstance(run, dict) or not workflow_path_matches(run.get("path"), RELEASE_GATE_PATH):
            problems.append("run workflow")
        else:
            problems.extend(f"run {key}" for key, value in expected.items() if run.get(key) != value)
    if problems:
        report.fail(f"T4 preflight evidence for {tag} invalid: {', '.join(problems)}")
    else:
        report.ok(f"T4 preflight evidence for {tag} at {commit[:7]}")


# --------------------------------------------------------------------------
# Modes
# --------------------------------------------------------------------------

def provenance_ok(env: dict, client, repo: str, workflow_path: str, report: Report) -> bool:
    if env.get("GITHUB_REF") != f"refs/heads/{BASE_BRANCH}":
        report.fail("provenance: not dispatched from main")
        return False
    if env.get("GITHUB_WORKFLOW_REF") != f"{repo}/{workflow_path}@refs/heads/{BASE_BRANCH}":
        report.fail("provenance: workflow file is not the main version")
        return False
    if env.get("GITHUB_WORKFLOW_SHA") != main_head(client, repo):
        report.fail("provenance: workflow SHA is not the current main head")
        return False
    report.ok("provenance: main")
    return True


def preflight(env: dict, client, repo: str, tag: str, sha: str, report: Report) -> None:
    if not provenance_ok(env, client, repo, RELEASE_GATE_PATH, report):
        return
    head = main_head(client, repo)
    if sha == head:
        report.ok(f"P1 {sha[:7]} is the current main head")
    else:
        report.fail(f"P1 {sha[:7]} is not the current main head")
        return
    check_required_checks(client, repo, sha, report)
    check_reviews(client, repo, sha, report)
    check_version(client, repo, sha, tag, report)
    if tag_commit(client, repo, tag) is None:
        report.ok(f"P5 {tag} does not exist")
    else:
        report.fail(f"P5 {tag} already exists")


def post_tag(env: dict, client, repo: str, server: str, tag: str, report: Report) -> None:
    commit = tag_commit(client, repo, tag)
    if commit is None:
        report.fail(f"T1 {tag} does not exist")
        return
    report.ok(f"T1 {tag} -> {commit[:7]}")
    if on_main_first_parent(client, repo, commit):
        report.ok(f"T2 {commit[:7]} is on the main first-parent history")
    else:
        report.fail(f"T2 {commit[:7]} is not on the main first-parent history")
        return
    check_required_checks(client, repo, commit, report)
    check_reviews(client, repo, commit, report)
    check_version(client, repo, commit, tag, report)
    this_run = client.get(f"/repos/{repo}/actions/runs/{env.get('GITHUB_RUN_ID', '')}")
    check_preflight_evidence(client, repo, server, tag, commit, parse_time(this_run.get("created_at")), report)


def audit(env: dict, client, repo: str, server: str, report: Report) -> None:
    if env.get("GITHUB_WORKFLOW_REF") != f"{repo}/{RELEASE_AUDIT_PATH}@refs/heads/{BASE_BRANCH}":
        report.fail("provenance: audit workflow is not the main version")
        return
    if env.get("GITHUB_EVENT_NAME") == "workflow_dispatch" and env.get("GITHUB_REF") != f"refs/heads/{BASE_BRANCH}":
        report.fail("provenance: audit not dispatched from main")
        return
    for item in client.get_all(f"/repos/{repo}/tags"):
        tag = item.get("name") if isinstance(item, dict) else None
        if not valid_tag(tag):
            continue
        commit = tag_commit(client, repo, tag)
        if commit is None:
            report.fail(f"T1 {tag} could not be resolved")
            continue
        pinned = PRE_GATE_TAGS.get(tag)
        if pinned is not None:
            if commit == pinned:
                report.info(f"{tag} predates the release gate (pre-gate, pinned at {pinned[:7]})")
            else:
                report.fail(f"T1 pre-gate tag {tag} resolves to {commit[:7]}, pinned {pinned[:7]}")
            continue
        if not on_main_first_parent(client, repo, commit):
            report.fail(f"T2 {tag} ({commit[:7]}) is not on the main first-parent history")
            continue
        check_required_checks(client, repo, commit, report)
        check_reviews(client, repo, commit, report)
        check_version(client, repo, commit, tag, report)
        runs = client.get_all(
            f"/repos/{repo}/actions/workflows/release-gate.yml/runs",
            {"event": "push", "branch": tag},
            key="workflow_runs",
        )
        times = [parse_time(r.get("created_at")) for r in runs if isinstance(r, dict) and r.get("head_branch") == tag]
        if not times:
            report.fail(f"T4 no post-tag gate run found for {tag}")
            continue
        check_preflight_evidence(client, repo, server, tag, commit, min(times), report)


def record(env: dict, client, repo: str, server: str, tag: str, sha: str, report: Report) -> None:
    if not provenance_ok(env, client, repo, RELEASE_GATE_PATH, report):
        return
    if main_head(client, repo) != sha:
        report.fail("P1 main moved after verification: nothing recorded")
        return
    if tag_commit(client, repo, tag) is not None:
        report.fail("P5 tag created after verification: nothing recorded")
        return
    run_url = f"{server}/{repo}/actions/runs/{env.get('GITHUB_RUN_ID', '')}"
    client.create_status(sha, "success", PREFLIGHT_CONTEXT.format(tag=tag), f"preflight PASS for {tag} at {sha[:7]}", run_url)
    report.ok(f"recorded release-preflight/{tag} on {sha[:7]}")


def run(mode: str, env: dict, client_factory) -> int:
    report = Report()
    repo = env.get("GITHUB_REPOSITORY", "")
    server = env.get("GITHUB_SERVER_URL", "https://github.com")
    event = env.get("GITHUB_EVENT_NAME", "")
    tag = sha = None

    # Input validation happens before any API call.
    if event == "workflow_dispatch" and mode in ("gate", "record"):
        tag, sha = env.get("GATE_TAG"), env.get("GATE_SHA")
        if not valid_tag(tag) or not valid_sha(sha):
            report.fail("inputs: tag must be vX.Y.Z and sha a 40-character lowercase hex SHA")
            return _finish(report, env)
    elif event == "push" and mode in ("gate", "record"):
        ref = env.get("GITHUB_REF", "")
        prefix, _, tag = ref.partition("refs/tags/")
        if prefix or not valid_tag(tag):
            report.fail("inputs: push ref is not a vX.Y.Z tag")
            return _finish(report, env)
        if mode == "record":
            report.info("record is not applicable to tag pushes")
            return _finish(report, env)
    elif mode != "audit" or event not in ("schedule", "workflow_dispatch"):
        report.fail("inputs: unsupported mode or event")
        return _finish(report, env)

    if not anchors_valid():
        report.fail("anchors: pre-gate tags, GATE_BASELINE or gate introduction are inconsistent")
        return _finish(report, env)

    try:
        client = client_factory()
        if mode == "audit":
            audit(env, client, repo, server, report)
        elif mode == "record":
            record(env, client, repo, server, tag, sha, report)
        elif event == "workflow_dispatch":
            preflight(env, client, repo, tag, sha, report)
        else:
            post_tag(env, client, repo, server, tag, report)
    except (ApiError, ValueError, KeyError, TypeError, AttributeError):
        report.fail("API error or malformed data: failing closed")
    return _finish(report, env)


def _finish(report: Report, env: dict) -> int:
    lines = ["## release gate", ""] + [f"- **{level}** {message}" for level, message in report.lines]
    text = "\n".join(lines) + "\n"
    print(text)
    summary = env.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write(text)
    return 1 if report.failed or not report.lines else 0


def main(argv: list[str]) -> int:
    if len(argv) != 1 or argv[0] not in ("gate", "record", "audit"):
        print("usage: release_gate.py gate|record|audit")
        return 2
    return run(argv[0], dict(os.environ), GitHubClient.from_env)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
