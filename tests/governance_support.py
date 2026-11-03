"""Shared helpers for the governance tests (synthetic data only, no network)."""

from __future__ import annotations

import copy
import importlib
import sys
from datetime import datetime, timedelta
from pathlib import Path

CI_DIR = Path(__file__).resolve().parents[1] / "ci"
if str(CI_DIR) not in sys.path:
    sys.path.insert(0, str(CI_DIR))

github_api = importlib.import_module("github_api")
review_gate = importlib.import_module("review_gate")
release_gate = importlib.import_module("release_gate")

# Keep test output clean: the scripts print reports meant for CI logs.
review_gate.print = lambda *args, **kwargs: None
release_gate.print = lambda *args, **kwargs: None

REPO = "BRMff/linux-health-reporter"
OWNER = "BRMff"
BOT = "github-actions[bot]"
PRODUCER_ID = 4242
_EPOCH = datetime(2026, 11, 3, 10, 0, 0)


def ts(seconds: int) -> str:
    return (_EPOCH + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha(n: int) -> str:
    return f"{n:040x}"


def make_pr(number, head, labels=(), draft=False, base_ref="main", base_repo=REPO, head_repo=REPO,
            merge=None, title="synthetic", branch="feature"):
    return {
        "number": number,
        "state": "open",
        "draft": draft,
        "title": title,
        "head": {"sha": head, "ref": branch, "repo": None if head_repo is None else {"full_name": head_repo}},
        "base": {"ref": base_ref, "repo": {"full_name": base_repo}},
        "merge_commit_sha": merge,
        "labels": [{"name": name} for name in labels],
    }


def status(state, description, t, creator=BOT, ident=None, context="review-gate", target_url=""):
    return {
        "id": ident if ident is not None else t,
        "state": state,
        "description": description,
        "context": context,
        "created_at": ts(t),
        "creator": {"login": creator},
        "target_url": target_url,
    }


def event(kind, t, label=None, login=OWNER, actor_type="User", ident=None):
    item = {"id": ident if ident is not None else t, "event": kind, "created_at": ts(t),
            "actor": {"login": login, "type": actor_type}}
    if label is not None:
        item["label"] = {"name": label}
    return item


def env(event_name="workflow_run", workflow_ref=None, ref="refs/heads/main"):
    return {
        "GITHUB_REPOSITORY": REPO,
        "GITHUB_EVENT_NAME": event_name,
        "GITHUB_WORKFLOW_REF": workflow_ref or f"{REPO}/.github/workflows/review-gate.yml@refs/heads/main",
        "GITHUB_REF": ref,
        "GITHUB_SERVER_URL": "https://github.com",
        "GITHUB_RUN_ID": "1",
    }


def payload(head, path=".github/workflows/review-gate-trigger.yml", name="review-gate-trigger",
            event_type="pull_request", repo=REPO, workflow_id=PRODUCER_ID, pull_requests=None):
    return {"workflow_run": {
        "path": path, "name": name, "event": event_type, "repository": {"full_name": repo},
        "head_sha": head, "workflow_id": workflow_id, "pull_requests": pull_requests or [],
    }}


class FakeGitHub:
    """In-memory stand-in for the review-gate API surface.

    Records every call in order. ``fail`` holds substrings of paths whose calls
    raise ApiError. ``now`` is the server time used for new statuses.
    """

    def __init__(self, pulls=(), statuses=None, events=None, producer_id=PRODUCER_ID, now=1000):
        self.pulls = [copy.deepcopy(p) for p in pulls]
        self.statuses = {k: list(v) for k, v in (statuses or {}).items()}
        self.events = {k: list(v) for k, v in (events or {}).items()}
        self.producer_id = producer_id
        self.now = now
        self.fail: set[str] = set()
        self.calls: list[tuple] = []
        self.writes: list[tuple] = []

    def _check(self, path):
        for marker in self.fail:
            if marker in path:
                raise github_api.ApiError("synthetic failure")

    def get(self, path, params=None):
        self.calls.append(("GET", path))
        self._check(path)
        if path == f"/repos/{REPO}/actions/workflows/review-gate-trigger.yml":
            if self.producer_id is None:
                raise github_api.ApiError("404")
            return {"id": self.producer_id, "path": ".github/workflows/review-gate-trigger.yml"}
        raise AssertionError(f"unexpected GET {path}")

    def get_all(self, path, params=None, key=None):
        self.calls.append(("GET_ALL", path))
        self._check(path)
        if path == f"/repos/{REPO}/pulls":
            return copy.deepcopy(self.pulls)
        if path.startswith(f"/repos/{REPO}/commits/") and path.endswith("/statuses"):
            return list(reversed(self.statuses.get(path.split("/")[-2], [])))
        if path.startswith(f"/repos/{REPO}/issues/") and path.endswith("/events"):
            return list(self.events.get(int(path.split("/")[-2]), []))
        raise AssertionError(f"unexpected GET_ALL {path}")

    def create_status(self, sha_, state, context, description, target_url):
        self.calls.append(("POST", sha_))
        self._check(f"/statuses/{sha_}")
        self.now += 1
        record = status(state, description, self.now, creator=BOT, ident=10_000 + self.now,
                        context=context, target_url=target_url)
        self.statuses.setdefault(sha_, []).append(record)
        self.writes.append((sha_, state, description))

    def latest(self, sha_):
        items = [s for s in self.statuses.get(sha_, []) if s["context"] == "review-gate"]
        return max(items, key=lambda s: (s["created_at"], s["id"])) if items else None
