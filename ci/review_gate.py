"""Review gate reconciler (governance; not part of the package).

Runs only from the default branch (workflow_run, schedule, workflow_dispatch).
It never executes pull-request code. Every run reconciles the ``review-gate``
commit status of the head of **every** open pull request from current state,
so a lost or cancelled run cannot leave a stale approval behind once any later
run completes. See docs/governance.md for the model and its limits.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
import os
import re
import sys

from github_api import ApiError, GitHubClient

OWNER = "BRMff"
APPROVAL_LABEL = "review:passed"
CONTEXT = "review-gate"
GATE_BOT = "github-actions[bot]"
BASE_BRANCH = "main"
PRODUCER_PATH = ".github/workflows/review-gate-trigger.yml"
PRODUCER_FILE = "review-gate-trigger.yml"
PRODUCER_NAME = "review-gate-trigger"
RECONCILER_PATH = ".github/workflows/review-gate.yml"
ALLOWED_EVENTS = ("workflow_run", "schedule", "workflow_dispatch")
INVALIDATION_EVENTS = ("head_ref_force_pushed", "base_ref_changed", "reopened", "convert_to_draft")

SHA_RE = re.compile(r"[0-9a-f]{40}")
REF_RE = re.compile(r"refs/(heads|pull|tags)/[A-Za-z0-9._/-]+")
TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

# Status descriptions: fixed text plus a short SHA only. Never PR titles,
# branch names, label names or any other text controlled by contributors.
AWAITING = "awaiting owner review of {sha7}"
RECONCILING = "reconciling {sha7}"
MULTIPLE_PRS = "SHA is head of multiple open PRs"
INVALID_CONTEXT = "invalid PR context"
DRAFT = "draft: not reviewable"
WITHDRAWN = "approval withdrawn at {sha7}"
APPROVED = "approved by owner at {sha7}"
PREDATES = "label predates this head: remove and re-apply"
INCOMPLETE = "reconciliation incomplete (API error)"


@dataclass(frozen=True)
class Decision:
    state: str
    description: str


def parse_time(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("timestamp is not a string")
    return datetime.strptime(value, TIME_FORMAT)


def _require_sha(value: object) -> str:
    if not isinstance(value, str) or not SHA_RE.fullmatch(value):
        raise ValueError("invalid sha")
    return value


# --------------------------------------------------------------------------
# Provenance and event context (validated before any mutation)
# --------------------------------------------------------------------------

def running_workflow_is_trusted(env: dict) -> bool:
    """The running workflow is review-gate.yml from main (GOV-R3-F05)."""
    repo = env.get("GITHUB_REPOSITORY", "")
    event = env.get("GITHUB_EVENT_NAME", "")
    if event not in ALLOWED_EVENTS:
        return False
    if env.get("GITHUB_WORKFLOW_REF") != f"{repo}/{RECONCILER_PATH}@refs/heads/{BASE_BRANCH}":
        return False
    if event == "workflow_dispatch" and env.get("GITHUB_REF") != f"refs/heads/{BASE_BRANCH}":
        return False
    return True


def is_producer_path(path: object) -> bool:
    """Exact match of the producer path, optionally followed by '@<ref>'.

    No substring, prefix, suffix or regex search over the path (GOV-R4-F01).
    """
    if not isinstance(path, str):
        return False
    base, separator, ref = path.partition("@")
    if base != PRODUCER_PATH:
        return False
    if not separator:
        return True
    return REF_RE.fullmatch(ref) is not None and ".." not in ref


def event_head_sha(payload: object, repo: str, producer_workflow_id: object) -> str | None:
    """The workflow_run head SHA if the producer's identity is unambiguous."""
    if not isinstance(payload, dict) or not isinstance(producer_workflow_id, int):
        return None
    run = payload.get("workflow_run")
    if not isinstance(run, dict):
        return None
    repository = run.get("repository")
    head_sha = run.get("head_sha")
    workflow_id = run.get("workflow_id")
    checks = (
        is_producer_path(run.get("path")),
        run.get("name") == PRODUCER_NAME,
        run.get("event") == "pull_request",
        isinstance(repository, dict) and repository.get("full_name") == repo,
        isinstance(head_sha, str) and SHA_RE.fullmatch(head_sha) is not None,
        type(workflow_id) is int and workflow_id == producer_workflow_id,
    )
    return head_sha if all(checks) else None


# --------------------------------------------------------------------------
# Open pull request inventory
# --------------------------------------------------------------------------

def validate_inventory(pulls: object, repo: str) -> list[dict]:
    """Structurally validate the complete list of open PRs (ValueError otherwise)."""
    if not isinstance(pulls, list):
        raise ValueError("inventory is not a list")
    seen = set()
    for pr in pulls:
        if not isinstance(pr, dict) or type(pr.get("number")) is not int:
            raise ValueError("malformed pull request")
        if pr["number"] in seen:
            raise ValueError("duplicate pull request in inventory")
        seen.add(pr["number"])
        if pr.get("state") != "open" or type(pr.get("draft")) is not bool:
            raise ValueError("malformed pull request state")
        head, base = pr.get("head"), pr.get("base")
        if not isinstance(head, dict) or not isinstance(base, dict):
            raise ValueError("malformed pull request refs")
        _require_sha(head.get("sha"))
        if not isinstance(base.get("ref"), str) or not isinstance(base.get("repo"), dict):
            raise ValueError("malformed base")
        if head.get("repo") is not None and not isinstance(head.get("repo"), dict):
            raise ValueError("malformed head repo")
        merge_sha = pr.get("merge_commit_sha")
        if merge_sha is not None:
            _require_sha(merge_sha)
        labels = pr.get("labels")
        if not isinstance(labels, list) or not all(
            isinstance(label, dict) and isinstance(label.get("name"), str) for label in labels
        ):
            raise ValueError("malformed labels")
    return pulls


def heads_of(inventory: list[dict]) -> dict[str, list[dict]]:
    heads: dict[str, list[dict]] = {}
    for pr in inventory:
        heads.setdefault(pr["head"]["sha"], []).append(pr)
    return heads


def prewrite_target(candidate: str | None, inventory: list[dict], repo: str) -> str | None:
    """The candidate only if it is the exact head of an open PR of this
    repository and not the test merge commit of any open PR (GOV-R4-F02)."""
    if candidate is None or not SHA_RE.fullmatch(candidate):
        return None
    merge_commits = {pr.get("merge_commit_sha") for pr in inventory}
    if candidate in merge_commits:
        return None
    for pr in inventory:
        if pr["head"]["sha"] == candidate and pr["base"]["repo"].get("full_name") == repo:
            return candidate
    return None


# --------------------------------------------------------------------------
# Pure decision for one head SHA
# --------------------------------------------------------------------------

def _ordered(items: list[dict]) -> list[dict]:
    return sorted(items, key=lambda item: (parse_time(item.get("created_at")), item.get("id", 0)))


def decide(head_sha: str, prs: list[dict], repo: str, events: list[dict], statuses: list[dict]) -> Decision | None:
    """The status to write on ``head_sha``, or None if nothing must change.

    ``prs`` are the open PRs whose head is exactly ``head_sha``, taken from the
    complete inventory. ``events`` are the issue events of the single PR and
    ``statuses`` the commit statuses of ``head_sha`` (all pages).
    """
    _require_sha(head_sha)
    sha7 = head_sha[:7]
    gate_statuses = _ordered([s for s in statuses if s.get("context") == CONTEXT])
    latest = gate_statuses[-1] if gate_statuses else None
    # The decision state before any "reconciling" pre-write: a pre-write must
    # not hide that an approval existed (so its withdrawal is still reported).
    decided = [s for s in gate_statuses if not str(s.get("description", "")).startswith("reconciling ")]
    previous = decided[-1] if decided else None

    if not prs:
        return None
    if len(prs) > 1:
        desired = Decision("failure", MULTIPLE_PRS)
    else:
        pr = prs[0]
        head_repo = pr["head"].get("repo")
        if (
            pr["base"].get("ref") != BASE_BRANCH
            or pr["base"]["repo"].get("full_name") != repo
            or not isinstance(head_repo, dict)
            or head_repo.get("full_name") != repo
        ):
            desired = Decision("failure", INVALID_CONTEXT)
        else:
            desired = _decide_single(pr, sha7, events, gate_statuses, previous)

    if latest is not None and latest.get("state") == desired.state and latest.get("description") == desired.description:
        return None
    return desired


def _decide_single(pr: dict, sha7: str, events: list[dict], gate_statuses: list[dict], previous: dict | None) -> Decision:
    ordered_events = _ordered([e for e in events if isinstance(e, dict)])
    invalidations = [parse_time(e["created_at"]) for e in ordered_events if e.get("event") in INVALIDATION_EVENTS]
    invalidated_at = invalidations[-1] if invalidations else None

    marker = None
    for status in gate_statuses:
        creator = status.get("creator") or {}
        created = parse_time(status.get("created_at"))
        if creator.get("login") == GATE_BOT and (invalidated_at is None or created > invalidated_at):
            marker = created
            break

    awaiting = Decision("pending", AWAITING.format(sha7=sha7))
    withdrawn = Decision("failure", WITHDRAWN.format(sha7=sha7))
    if marker is None:
        return awaiting
    if pr["draft"]:
        return Decision("pending", DRAFT)
    labels = {label["name"] for label in pr["labels"]}
    if APPROVAL_LABEL not in labels:
        if previous is not None and (
            previous.get("state") == "success" or previous.get("description") == withdrawn.description
        ):
            return withdrawn
        return awaiting

    review_events = [
        e for e in ordered_events
        if e.get("event") in ("labeled", "unlabeled")
        and isinstance(e.get("label"), dict)
        and e["label"].get("name") == APPROVAL_LABEL
    ]
    last = review_events[-1] if review_events else None
    actor = (last or {}).get("actor") or {}
    if (
        last is not None
        and last.get("event") == "labeled"
        and actor.get("login") == OWNER
        and actor.get("type") == "User"
        and parse_time(last.get("created_at")) > marker
    ):
        return Decision("success", APPROVED.format(sha7=sha7))
    return Decision("pending", PREDATES)


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------

def run(env: dict, client, payload: object) -> int:
    """Reconcile every open PR head. Returns a process exit code."""
    if not running_workflow_is_trusted(env):
        print("review-gate: untrusted workflow provenance; no mutation performed")
        return 2
    repo = env["GITHUB_REPOSITORY"]
    run_url = f"{env.get('GITHUB_SERVER_URL', 'https://github.com')}/{repo}/actions/runs/{env.get('GITHUB_RUN_ID', '')}"
    errors = False
    report: list[str] = []

    candidate = None
    if env.get("GITHUB_EVENT_NAME") == "workflow_run":
        try:
            producer = client.get(f"/repos/{repo}/actions/workflows/{PRODUCER_FILE}")
            producer_id = producer.get("id") if isinstance(producer, dict) else None
        except ApiError:
            producer_id = None
        candidate = event_head_sha(payload, repo, producer_id)
        if candidate is None:
            report.append("event context not validated: no pre-write")

    try:
        inventory = validate_inventory(client.get_all(f"/repos/{repo}/pulls", {"state": "open"}), repo)
    except (ApiError, ValueError):
        print("review-gate: open PR inventory unavailable or incomplete; no mutation performed")
        return 1

    target = prewrite_target(candidate, inventory, repo)
    if target is not None:
        try:
            client.create_status(target, "pending", CONTEXT, RECONCILING.format(sha7=target[:7]), run_url)
            report.append(f"pre-write pending on {target[:7]}")
        except ApiError:
            errors = True
            report.append(f"pre-write failed on {target[:7]}")
    elif candidate is not None:
        report.append("event SHA is not the head of an open PR: no pre-write")

    changes = 0
    for head_sha, prs in sorted(heads_of(inventory).items()):
        numbers = ",".join(str(pr["number"]) for pr in prs)
        try:
            statuses = client.get_all(f"/repos/{repo}/commits/{head_sha}/statuses")
            events = client.get_all(f"/repos/{repo}/issues/{prs[0]['number']}/events") if len(prs) == 1 else []
            decision = decide(head_sha, prs, repo, events, statuses)
        except (ApiError, ValueError, KeyError, TypeError, AttributeError):
            errors = True
            decision = Decision("pending", INCOMPLETE)
        if decision is None:
            report.append(f"{head_sha[:7]} PR {numbers}: unchanged")
            continue
        changes += 1
        try:
            client.create_status(head_sha, decision.state, CONTEXT, decision.description, run_url)
            report.append(f"{head_sha[:7]} PR {numbers}: {decision.state} ({decision.description})")
        except ApiError:
            errors = True
            report.append(f"{head_sha[:7]} PR {numbers}: write failed")

    report.append(f"changes: {changes}; open PR heads: {len(heads_of(inventory))}")
    _publish(report, env)
    return 1 if errors else 0


def _publish(lines: list[str], env: dict) -> None:
    text = "\n".join(["## review-gate reconciliation", ""] + [f"- {line}" for line in lines]) + "\n"
    print(text)
    summary = env.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write(text)


def main() -> int:
    env = dict(os.environ)
    payload = None
    event_path = env.get("GITHUB_EVENT_PATH")
    if event_path:
        try:
            with open(event_path, encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, ValueError):
            payload = None
    try:
        client = GitHubClient.from_env()
    except ApiError:
        print("review-gate: client configuration invalid; no mutation performed")
        return 2
    return run(env, client, payload)


if __name__ == "__main__":
    sys.exit(main())
