"""Release gate tests (RL-U*). Synthetic repository model; no network."""

import base64
import copy
import inspect
import unittest
from unittest import mock

from tests.governance_support import BOT, OWNER, REPO, github_api, release_gate as rel, sha, ts

# Synthetic first-parent history of main:
#   C00 (v0.1.0) <- C0 (v0.2.0) <- C1 (GATE_BASELINE) <- C2 (merge of PR #4, gate introduction)
#   <- C3 (merge of PR #5) <- C4 (merge of PR #6); OFF is a commit outside main.
C00, C0, C1, C2, C3, C4, OFF = (sha(n) for n in (0x100, 0x101, 0x102, 0x103, 0x104, 0x105, 0x1FF))
H4, H5, H6 = sha(0x204), sha(0x205), sha(0x206)
RECORD = rel.GATE_INTRODUCTION_RECORD
REAL_ANCHORS = (dict(rel.PRE_GATE_TAGS), rel.GATE_BASELINE, rel.GATE_INTRODUCTION_PR)


def b64(text):
    return {"encoding": "base64", "content": base64.b64encode(text.encode()).decode()}


def record_text(pr="4", head=H4, merge=C2, review="LHR-GOV-PR4-R2 = PASS", auth="LHR-GOV-PR4-MERGE = AUTHORIZED"):
    return (
        "# Gate introduction record\n\n"
        f"gate-introduction-pr: {pr}\n"
        f"head-sha: {head}\n"
        f"merge-commit-sha: {merge}\n"
        f"independent-review: {review}\n"
        f"owner-authorization: {auth}\n"
    )


class FakeRepo:
    def __init__(self):
        self.head = C3
        self.parents = {C00: [], C0: [C00], C1: [C0, sha(0x302)], C2: [C1, H4], C3: [C2, H5], C4: [C3, H6], OFF: [C3]}
        self.files = {}
        for commit in (C00, C0, C1, C2, C3, C4, OFF):
            self.files[("linux_health_reporter/__init__.py", commit)] = '__version__ = "0.3.0"\n'
            self.files[("CHANGELOG.md", commit)] = "# Changelog\n\n## [0.3.0] — 2026-11-10\n"
        for commit in (C3, C4, OFF):
            self.files[(RECORD, commit)] = record_text()
        self.content_paths = []
        self.tags = {"v0.1.0": {"type": "commit", "sha": C00}, "v0.2.0": {"type": "commit", "sha": C0}}
        self.annotated = {}
        self.check_runs = {c: [self.run_check(name, i) for i, name in enumerate(rel.MAIN_REQUIRED_CHECKS)]
                           for c in (C0, C1, C2, C3, C4, OFF)}
        self.pulls = {
            C1: [self.pr(2, C1, sha(0x302))],
            C2: [self.pr(4, C2, H4)],
            C3: [self.pr(5, C3, H5)],
            C4: [self.pr(6, C4, H6)],
        }
        self.pull_details = {
            4: {"number": 4, "merged": True, "merged_at": ts(5), "merge_commit_sha": C2,
                "base": {"ref": "main", "repo": {"full_name": REPO}}, "head": {"sha": H4}},
        }
        self.statuses = {
            H5: [self.status("review-gate", "success", 50)],
            H6: [self.status("review-gate", "success", 60)],
            C3: [self.status("release-preflight/v0.3.0", "success", 100, url_run=500)],
        }
        self.runs = {
            500: {"id": 500, "path": ".github/workflows/release-gate.yml", "event": "workflow_dispatch",
                  "head_branch": "main", "head_sha": C3, "conclusion": "success",
                  "display_title": f"release-preflight v0.3.0 {C3}", "created_at": ts(90)},
            600: {"id": 600, "created_at": ts(200), "head_branch": "v0.3.0"},
        }
        self.tag_runs = {"v0.3.0": [self.runs[600]]}
        self.fail = set()
        self.writes = []
        self.api_calls = 0

    @staticmethod
    def run_check(name, i, conclusion="success", status="completed", app="github-actions"):
        return {"id": 1000 + i, "name": name, "status": status, "conclusion": conclusion, "app": {"slug": app}}

    @staticmethod
    def pr(number, merge, head):
        return {"number": number, "merge_commit_sha": merge, "merged_at": ts(1), "base": {"ref": "main"}, "head": {"sha": head}}

    @staticmethod
    def status(context, state, t, creator=BOT, url_run=None):
        return {"id": t, "context": context, "state": state, "created_at": ts(t), "creator": {"login": creator},
                "target_url": f"https://github.com/{REPO}/actions/runs/{url_run}" if url_run else ""}

    def _hit(self, path):
        self.api_calls += 1
        for marker in self.fail:
            if marker in path:
                raise github_api.ApiError("synthetic")

    def get_optional(self, path, params=None):
        self._hit(path)
        prefix = f"/repos/{REPO}"
        if path.startswith(prefix + "/git/ref/tags/"):
            obj = self.tags.get(path.rsplit("/", 1)[1])
            return None if obj is None else {"object": dict(obj)}
        if path.startswith(prefix + "/contents/"):
            self.content_paths.append(path[len(prefix + "/contents/"):])
            text = self.files.get((path[len(prefix + "/contents/"):], params["ref"]))
            return None if text is None else b64(text)
        raise AssertionError(path)

    def get(self, path, params=None):
        prefix = f"/repos/{REPO}"
        if path == prefix + "/git/ref/heads/main":
            self._hit(path)
            return {"object": {"sha": self.head}}
        if path.startswith(prefix + "/git/tags/"):
            self._hit(path)
            return {"object": dict(self.annotated[path.rsplit("/", 1)[1]])}
        if path.startswith(prefix + "/contents/"):
            data = self.get_optional(path, params)
            if data is None:
                raise github_api.ApiError("404")
            return data
        if path.startswith(prefix + "/actions/runs/"):
            self._hit(path)
            run = self.runs.get(int(path.rsplit("/", 1)[1]))
            if run is None:
                raise github_api.ApiError("404")
            return copy.deepcopy(run)
        if path.startswith(prefix + "/commits/"):
            self._hit(path)
            commit = path.rsplit("/", 1)[1]
            return {"sha": commit, "parents": [{"sha": p} for p in self.parents[commit]]}
        if path.startswith(prefix + "/pulls/"):
            self._hit(path)
            pr = self.pull_details.get(int(path.rsplit("/", 1)[1]))
            if pr is None:
                raise github_api.ApiError("404")
            return copy.deepcopy(pr)
        raise AssertionError(path)

    def get_all(self, path, params=None, key=None):
        self._hit(path)
        prefix = f"/repos/{REPO}"
        if path == prefix + "/tags":
            out = []
            for name, obj in self.tags.items():
                commit = obj["sha"] if obj["type"] == "commit" else self.annotated[obj["sha"]]["sha"]
                out.append({"name": name, "commit": {"sha": commit}})
            return out
        if path.endswith("/check-runs"):
            return copy.deepcopy(self.check_runs.get(path.split("/")[-2], []))
        if path.endswith("/pulls"):
            return copy.deepcopy(self.pulls.get(path.split("/")[-2], []))
        if path.endswith("/statuses"):
            return copy.deepcopy(list(reversed(self.statuses.get(path.split("/")[-2], []))))
        if path.endswith("/release-gate.yml/runs"):
            return copy.deepcopy(self.tag_runs.get(params["branch"], []))
        raise AssertionError(path)

    def create_status(self, sha_, state, context, description, target_url):
        self.writes.append((sha_, state, context, description, target_url))


def dispatch_env(tag="v0.3.0", sha_=C3, workflow_sha=None, ref="refs/heads/main", workflow="release-gate.yml"):
    return {
        "GITHUB_REPOSITORY": REPO, "GITHUB_SERVER_URL": "https://github.com", "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_REF": ref, "GITHUB_WORKFLOW_REF": f"{REPO}/.github/workflows/{workflow}@refs/heads/main",
        "GITHUB_WORKFLOW_SHA": workflow_sha or sha_, "GITHUB_RUN_ID": "500", "GATE_TAG": tag, "GATE_SHA": sha_,
    }


def push_env(tag="v0.3.0"):
    return {
        "GITHUB_REPOSITORY": REPO, "GITHUB_SERVER_URL": "https://github.com", "GITHUB_EVENT_NAME": "push",
        "GITHUB_REF": f"refs/tags/{tag}", "GITHUB_WORKFLOW_REF": f"{REPO}/.github/workflows/release-gate.yml@refs/tags/{tag}",
        "GITHUB_RUN_ID": "600",
    }


def audit_env():
    return {
        "GITHUB_REPOSITORY": REPO, "GITHUB_SERVER_URL": "https://github.com", "GITHUB_EVENT_NAME": "schedule",
        "GITHUB_REF": "refs/heads/main", "GITHUB_WORKFLOW_REF": f"{REPO}/.github/workflows/release-audit.yml@refs/heads/main",
    }


class Base(unittest.TestCase):
    def setUp(self):
        # Synthetic anchors for the synthetic history; the real values are pinned in AnchorTests.
        for name, value in (("PRE_GATE_TAGS", {"v0.1.0": C00, "v0.2.0": C0}), ("GATE_BASELINE", C1), ("GATE_INTRODUCTION_PR", 4)):
            patcher = mock.patch.object(rel, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.repo = FakeRepo()

    def run_mode(self, mode, env_):
        captured = []
        original = rel._finish

        def finish(report, env):
            captured.append(report)
            return original(report, env)

        rel._finish = finish
        try:
            code = rel.run(mode, env_, lambda: self.repo)
        finally:
            rel._finish = original
        return code, captured[0]

    def messages(self, report, level):
        return [m for lvl, m in report.lines if lvl == level]

    def tag_v030(self, annotated=False):
        if annotated:
            self.repo.annotated[sha(0x900)] = {"type": "commit", "sha": C3}
            self.repo.tags["v0.3.0"] = {"type": "tag", "sha": sha(0x900)}
        else:
            self.repo.tags["v0.3.0"] = {"type": "commit", "sha": C3}


class PreflightTests(Base):
    def test_u1_all_conditions_pass(self):
        code, report = self.run_mode("gate", dispatch_env())
        self.assertEqual(code, 0, report.lines)
        self.assertTrue(any(m.startswith("P5") for m in self.messages(report, "PASS")))

    def test_u5_walk_ends_at_baseline_through_gate_introduction(self):
        _, report = self.run_mode("gate", dispatch_env())
        passed = self.messages(report, "PASS")
        self.assertTrue(any("gate introduction PR #4" in m and "verified" in m for m in passed))
        self.assertTrue(any("PR #5 review-gate success" in m for m in passed))
        self.assertTrue(any("GATE_BASELINE" in m and "pre-gate" in m for m in self.messages(report, "INFO")))
        self.assertFalse(any("PR #2" in m for _, m in report.lines))

    def test_u2_sha_on_main_but_not_head_fails(self):
        code, report = self.run_mode("gate", dispatch_env(sha_=C2, workflow_sha=C3))
        self.assertEqual(code, 1)
        self.assertTrue(any(m.startswith("P1") for m in self.messages(report, "FAIL")))

    def test_u3_required_checks(self):
        mutations = {
            "missing": lambda runs: runs.pop(0),
            "failure": lambda runs: runs[0].update(conclusion="failure"),
            "pending": lambda runs: runs[0].update(status="in_progress", conclusion=None),
            "other app": lambda runs: runs[0].update(app={"slug": "someone"}),
            "older success, newer failure": lambda runs: runs.append(dict(runs[0], id=9999, conclusion="failure")),
        }
        for name, mutate in mutations.items():
            with self.subTest(case=name):
                self.repo = FakeRepo()
                mutate(self.repo.check_runs[C3])
                code, report = self.run_mode("gate", dispatch_env())
                self.assertEqual(code, 1)
                self.assertTrue(any(m.startswith("P2") for m in self.messages(report, "FAIL")))

    def test_u4_post_gate_pr_without_review_success(self):
        cases = {
            "no status": [],
            "failure": [FakeRepo.status("review-gate", "failure", 50)],
            "forged creator": [FakeRepo.status("review-gate", "success", 50, creator=OWNER)],
            "success then withdrawn": [FakeRepo.status("review-gate", "success", 50), FakeRepo.status("review-gate", "failure", 51)],
        }
        for name, statuses in cases.items():
            with self.subTest(case=name):
                self.repo = FakeRepo()
                self.repo.statuses[H5] = statuses
                code, report = self.run_mode("gate", dispatch_env())
                self.assertEqual(code, 1)
                self.assertTrue(any(m.startswith("P3 PR #5") for m in self.messages(report, "FAIL")))

    def test_u4b_commit_without_merged_pr_fails(self):
        self.repo.pulls[C3] = []
        code, report = self.run_mode("gate", dispatch_env())
        self.assertEqual(code, 1)

    def test_u6_version_and_changelog(self):
        for path, text in (
            ("linux_health_reporter/__init__.py", '__version__ = "0.3.1"\n'),
            ("CHANGELOG.md", "## [0.3.0] — unreleased\n"),
            ("CHANGELOG.md", "## [0.3.0]\n"),
        ):
            with self.subTest(path=path, text=text):
                self.repo = FakeRepo()
                self.repo.files[(path, C3)] = text
                code, report = self.run_mode("gate", dispatch_env())
                self.assertEqual(code, 1)
                self.assertTrue(any(m.startswith("P4") for m in self.messages(report, "FAIL")))

    def test_u7_existing_tag_fails(self):
        self.tag_v030()
        code, report = self.run_mode("gate", dispatch_env())
        self.assertEqual(code, 1)
        self.assertTrue(any(m.startswith("P5") for m in self.messages(report, "FAIL")))

    def test_u11_invalid_inputs_fail_without_api_calls(self):
        def no_client():
            raise AssertionError("API must not be used with invalid inputs")
        for tag, sha_ in (("v0.3", C3), ("0.3.0", C3), ("v0.3.0;rm -rf", C3), ("v01.3.0", C3), ("v0.3.0", C3[:39]),
                          ("v0.3.0", "AB" * 20), ("v0.3.0", "g" * 40), (None, C3), ("v0.3.0", None)):
            with self.subTest(tag=tag, sha=sha_):
                e = dispatch_env()
                e["GATE_TAG"], e["GATE_SHA"] = tag, sha_
                self.assertEqual(rel.run("gate", e, no_client), 1)
                self.assertEqual(rel.run("record", e, no_client), 1)
        e = push_env()
        e["GITHUB_REF"] = "refs/heads/v0.3.0"
        self.assertEqual(rel.run("gate", e, no_client), 1)

    def test_u12_provenance(self):
        for e in (
            dispatch_env(ref="refs/heads/feature"),
            dispatch_env(workflow="other.yml"),
            dispatch_env(workflow_sha=C2),
        ):
            with self.subTest(e=(e["GITHUB_REF"], e["GITHUB_WORKFLOW_REF"], e["GITHUB_WORKFLOW_SHA"])):
                self.repo = FakeRepo()
                code, report = self.run_mode("gate", e)
                self.assertEqual(code, 1)
                self.assertTrue(any(m.startswith("provenance") for m in self.messages(report, "FAIL")))
                self.assertFalse(any(m.startswith("P1") for m in self.messages(report, "PASS")))

    def test_api_error_fails_closed(self):
        self.repo.fail.add("/check-runs")
        code, report = self.run_mode("gate", dispatch_env())
        self.assertEqual(code, 1)


class RecordTests(Base):
    def test_record_writes_evidence(self):
        code, _ = self.run_mode("record", dispatch_env())
        self.assertEqual(code, 0)
        self.assertEqual(self.repo.writes, [(C3, "success", "release-preflight/v0.3.0", f"preflight PASS for v0.3.0 at {C3[:7]}",
                                             f"https://github.com/{REPO}/actions/runs/500")])

    def test_record_refuses_when_main_moved_or_tag_exists(self):
        self.repo.head = C4
        code, _ = self.run_mode("record", dispatch_env(workflow_sha=C4))
        self.assertEqual((code, self.repo.writes), (1, []))
        self.repo = FakeRepo()
        self.tag_v030()
        code, _ = self.run_mode("record", dispatch_env())
        self.assertEqual((code, self.repo.writes), (1, []))

    def test_record_is_not_applicable_to_tag_pushes(self):
        self.tag_v030()
        code, report = self.run_mode("record", push_env())
        self.assertEqual((code, self.repo.writes), (0, []))
        self.assertTrue(self.messages(report, "INFO"))


class PostTagTests(Base):
    def setUp(self):
        super().setUp()
        self.tag_v030()

    def test_post_tag_pass(self):
        code, report = self.run_mode("gate", push_env())
        self.assertEqual(code, 0, report.lines)

    def test_u8_missing_tag(self):
        del self.repo.tags["v0.3.0"]
        code, report = self.run_mode("gate", push_env())
        self.assertEqual(code, 1)
        self.assertTrue(any(m.startswith("T1") for m in self.messages(report, "FAIL")))

    def test_u9_main_moved_after_tag_still_passes(self):
        self.repo.head = C4
        code, report = self.run_mode("gate", push_env())
        self.assertEqual(code, 0, report.lines)

    def test_u10_commit_off_main_fails(self):
        self.repo.tags["v0.3.0"] = {"type": "commit", "sha": OFF}
        code, report = self.run_mode("gate", push_env())
        self.assertEqual(code, 1)
        self.assertTrue(any(m.startswith("T2") for m in self.messages(report, "FAIL")))

    def test_u14_annotated_tag(self):
        self.tag_v030(annotated=True)
        code, report = self.run_mode("gate", push_env())
        self.assertEqual(code, 0, report.lines)

    def test_u15_missing_preflight_is_fail_not_warning(self):
        self.repo.statuses[C3] = []
        code, report = self.run_mode("gate", push_env())
        self.assertEqual(code, 1)
        self.assertTrue(any(m.startswith("T4 no preflight") for m in self.messages(report, "FAIL")))

    def test_u16_preflight_for_other_tag_or_commit(self):
        self.repo.statuses[C3] = [FakeRepo.status("release-preflight/v0.3.1", "success", 100, url_run=500)]
        self.assertEqual(self.run_mode("gate", push_env())[0], 1)
        self.repo = FakeRepo()
        self.tag_v030()
        self.repo.statuses[C2] = self.repo.statuses.pop(C3)
        self.assertEqual(self.run_mode("gate", push_env())[0], 1)

    def test_u17_forged_status_by_user_token(self):
        self.repo.statuses[C3] = [FakeRepo.status("release-preflight/v0.3.0", "success", 100, creator=OWNER, url_run=500)]
        code, report = self.run_mode("gate", push_env())
        self.assertEqual(code, 1)
        self.assertTrue(any("creator" in m for m in self.messages(report, "FAIL")))

    def test_u18_run_criteria(self):
        changes = {
            "path": ".github/workflows/other.yml", "event": "push", "head_branch": "feature", "head_sha": C2,
            "conclusion": "failure", "display_title": "release-preflight v0.3.1 " + C3,
        }
        for key, value in changes.items():
            with self.subTest(key=key):
                self.repo = FakeRepo()
                self.tag_v030()
                self.repo.runs[500][key] = value
                code, report = self.run_mode("gate", push_env())
                self.assertEqual(code, 1)
                self.assertTrue(any(m.startswith("T4 preflight evidence") for m in self.messages(report, "FAIL")))
        self.repo = FakeRepo()
        self.tag_v030()
        self.repo.runs[500]["path"] = ".github/workflows/release-gate.yml@refs/heads/main"
        self.assertEqual(self.run_mode("gate", push_env())[0], 0)
        for url in ("https://evil.example/actions/runs/500", f"https://github.com/{REPO}/actions/runs/500/x", ""):
            with self.subTest(url=url):
                self.repo = FakeRepo()
                self.tag_v030()
                self.repo.statuses[C3][0]["target_url"] = url
                self.assertEqual(self.run_mode("gate", push_env())[0], 1)

    def test_u19_preflight_created_after_tag(self):
        self.repo.statuses[C3] = [FakeRepo.status("release-preflight/v0.3.0", "success", 300, url_run=500)]
        code, report = self.run_mode("gate", push_env())
        self.assertEqual(code, 1)
        self.assertTrue(any("created after the tag" in m for m in self.messages(report, "FAIL")))


class AuditTests(Base):
    def test_u13_pre_gate_tags_are_informational(self):
        code, report = self.run_mode("audit", audit_env())
        self.assertEqual(code, 0, report.lines)
        self.assertEqual(len([m for m in self.messages(report, "INFO") if "pre-gate" in m and m.startswith("v0.")]), 2)

    def test_audit_valid_and_invalid_tags(self):
        self.tag_v030()
        self.assertEqual(self.run_mode("audit", audit_env())[0], 0)
        self.repo.tag_runs["v0.3.0"] = []
        code, report = self.run_mode("audit", audit_env())
        self.assertEqual(code, 1)
        self.assertTrue(any("no post-tag gate run" in m for m in self.messages(report, "FAIL")))

    def test_audit_detects_tag_off_main(self):
        self.repo.tags["v0.3.0"] = {"type": "commit", "sha": OFF}
        code, report = self.run_mode("audit", audit_env())
        self.assertEqual(code, 1)

    def test_audit_provenance(self):
        e = audit_env()
        e["GITHUB_WORKFLOW_REF"] = f"{REPO}/.github/workflows/release-audit.yml@refs/heads/feature"
        self.assertEqual(self.run_mode("audit", e)[0], 1)
        e = audit_env()
        e["GITHUB_EVENT_NAME"], e["GITHUB_REF"] = "workflow_dispatch", "refs/heads/feature"
        self.assertEqual(self.run_mode("audit", e)[0], 1)

    def test_unsupported_modes(self):
        e = audit_env()
        e["GITHUB_EVENT_NAME"] = "push"
        self.assertEqual(rel.run("audit", e, lambda: self.repo), 1)


class PreGateAnchorTests(Base):
    """F01: pre-gate status comes only from pinned tags and GATE_BASELINE."""

    def test_f01a_pinned_tag_on_another_commit_fails(self):
        self.repo.tags["v0.2.0"] = {"type": "commit", "sha": C1}
        code, report = self.run_mode("audit", audit_env())
        self.assertEqual(code, 1)
        self.assertTrue(any("pre-gate tag v0.2.0" in m for m in self.messages(report, "FAIL")))

    def test_f01b_later_tag_without_workflow_files_is_fully_verified(self):
        # No workflow file exists anywhere in the synthetic repository, and none is consulted.
        self.repo.head = C4
        self.repo.tags["v0.3.1"] = {"type": "commit", "sha": C4}
        code, report = self.run_mode("audit", audit_env())
        self.assertEqual(code, 1)
        self.assertFalse(any("v0.3.1" in m and "pre-gate" in m for m in self.messages(report, "INFO")))
        self.assertTrue(any("no post-tag gate run found for v0.3.1" in m for m in self.messages(report, "FAIL")))
        self.assertFalse([p for p in self.repo.content_paths if p.startswith(".github/")])

    def test_f01c_new_tag_on_pre_gate_commit_is_not_pre_gate(self):
        for commit in (C00, C0, C1):
            with self.subTest(commit=commit[-3:]):
                self.repo = FakeRepo()
                self.repo.tags["v0.2.1"] = {"type": "commit", "sha": commit}
                self.repo.tag_runs["v0.2.1"] = [{"id": 601, "created_at": ts(200), "head_branch": "v0.2.1"}]
                code, report = self.run_mode("audit", audit_env())
                self.assertEqual(code, 1)
                self.assertFalse(any("v0.2.1" in m and "pre-gate" in m for m in self.messages(report, "INFO")))
                self.assertTrue(any(m.startswith("P3") for m in self.messages(report, "FAIL")))

    def test_f01d_release_at_baseline_fails_p3(self):
        self.repo.head = C1
        code, report = self.run_mode("gate", dispatch_env(sha_=C1))
        self.assertEqual(code, 1)
        self.assertTrue(any("without the gate introduction" in m for m in self.messages(report, "FAIL")))

    def test_f01e_inconsistent_anchors_fail_before_any_api_call(self):
        def no_client():
            raise AssertionError("API must not be used with inconsistent anchors")
        cases = (
            ("GATE_BASELINE", C1[:39]), ("GATE_BASELINE", None), ("PRE_GATE_TAGS", {}),
            ("PRE_GATE_TAGS", {"v0.2.0": "abc"}), ("PRE_GATE_TAGS", {"latest": C0}),
            ("GATE_INTRODUCTION_PR", 0), ("GATE_INTRODUCTION_PR", "4"),
        )
        for name, value in cases:
            with self.subTest(name=name, value=value), mock.patch.object(rel, name, value):
                for mode, e in (("gate", dispatch_env()), ("gate", push_env()), ("audit", audit_env())):
                    self.assertEqual(rel.run(mode, e, no_client), 1)

    def test_f01f_history_that_never_reaches_baseline_fails(self):
        with mock.patch.object(rel, "GATE_BASELINE", sha(0xABC)):
            code, report = self.run_mode("gate", dispatch_env())
        self.assertEqual(code, 1)
        self.assertTrue(any("does not reach GATE_BASELINE" in m for m in self.messages(report, "FAIL")))

    def test_f01g_api_error_in_history_fails_closed(self):
        self.repo.fail.add(f"/commits/{C2}")
        code, report = self.run_mode("gate", dispatch_env())
        self.assertEqual(code, 1)
        self.assertTrue(any("failing closed" in m for m in self.messages(report, "FAIL")))


class MergeMethodTests(Base):
    """F02: only merge commits with two parents, one merged PR and second parent == PR head."""

    def assert_p3_fail(self, needle):
        code, report = self.run_mode("gate", dispatch_env())
        self.assertEqual(code, 1)
        self.assertTrue(any(m.startswith("P3") and needle in m for m in self.messages(report, "FAIL")), report.lines)

    def test_f02a_squash_merge_fails(self):
        self.repo.parents[C3] = [C2]
        self.assert_p3_fail("unsupported merge method")

    def test_f02b_rebase_merge_with_several_commits_fails(self):
        r1 = sha(0x401)
        self.repo.parents[C3], self.repo.parents[r1] = [r1], [C2]
        self.assert_p3_fail("unsupported merge method")

    def test_f02c_octopus_merge_fails(self):
        self.repo.parents[C3] = [C2, H5, sha(0x402)]
        self.assert_p3_fail("unsupported merge method")

    def test_f02d_direct_push_fails(self):
        self.repo.parents[C3] = [C2]
        self.repo.pulls[C3] = []
        self.assert_p3_fail("unsupported merge method")

    def test_f02e_second_parent_is_not_pr_head(self):
        self.repo.parents[C3] = [C2, sha(0x403)]
        self.assert_p3_fail("second parent is not the PR head")

    def test_f02f_merge_commit_of_zero_or_two_prs(self):
        for pulls in ([], [FakeRepo.pr(5, C3, H5), FakeRepo.pr(7, C3, H5)], [dict(FakeRepo.pr(5, C3, H5), base={"ref": "dev"})]):
            with self.subTest(n=len(pulls)):
                self.repo = FakeRepo()
                self.repo.pulls[C3] = pulls
                self.assert_p3_fail("not the merge of exactly one PR")

    def test_f02g_approved_head_still_needs_merge_topology(self):
        # The PR head carries review-gate success, but the merge is a squash: still FAIL.
        self.repo.parents[C3] = [C2]
        self.repo.pulls[C3] = [FakeRepo.pr(5, C3, H5)]
        code, report = self.run_mode("gate", dispatch_env())
        self.assertEqual(code, 1)
        self.assertFalse(any("PR #5 review-gate success" in m for m in self.messages(report, "PASS")))


class GateIntroductionTests(Base):
    """C3: one exception, only for the verified merge of PR #4."""

    def assert_intro_fail(self, needle):
        code, report = self.run_mode("gate", dispatch_env())
        self.assertEqual(code, 1)
        fails = self.messages(report, "FAIL")
        self.assertTrue(any("gate introduction" in m and needle in m for m in fails), report.lines)
        self.assertFalse(any("gate introduction" in m for m in self.messages(report, "PASS")))

    def test_c3a_main_moved_before_the_merge(self):
        mid, hmid = sha(0x110), sha(0x210)
        self.repo.parents[C2] = [mid, H4]
        self.repo.parents[mid] = [C1, hmid]
        self.repo.pulls[mid] = [FakeRepo.pr(3, mid, hmid)]
        self.repo.statuses[hmid] = [FakeRepo.status("review-gate", "success", 40)]
        self.assert_intro_fail("new owner decision")

    def test_c3b_no_exception_by_position_for_another_pr(self):
        self.repo.pulls[C2] = [FakeRepo.pr(7, C2, H4)]
        code, report = self.run_mode("gate", dispatch_env())
        self.assertEqual(code, 1)
        self.assertTrue(any("PR #7 without review-gate success" in m for m in self.messages(report, "FAIL")))
        self.assertFalse(any("gate introduction" in m for m in self.messages(report, "PASS")))

    def test_c3c_pull_request_resource_must_match(self):
        changes = {
            "pull request number": {"number": 5},
            "not merged": {"merged": False},
            "base is not main": {"base": {"ref": "dev", "repo": {"full_name": REPO}}},
            "merge_commit_sha": {"merge_commit_sha": C3},
            "second parent is not the final PR head": {"head": {"sha": sha(0x404)}},
        }
        for needle, change in changes.items():
            with self.subTest(needle=needle):
                self.repo = FakeRepo()
                self.repo.pull_details[4].update(change)
                self.assert_intro_fail(needle)
        self.repo = FakeRepo()
        self.repo.pull_details[4]["base"]["repo"]["full_name"] = "someone/fork"
        self.assert_intro_fail("base is not main")

    def test_c3d_pull_request_lookup_failure_fails_closed(self):
        del self.repo.pull_details[4]
        code, report = self.run_mode("gate", dispatch_env())
        self.assertEqual(code, 1)
        self.assertTrue(any("failing closed" in m for m in self.messages(report, "FAIL")))

    def test_c3e_record_missing(self):
        del self.repo.files[(RECORD, C3)]
        self.assert_intro_fail("missing")

    def test_c3f_record_contents(self):
        cases = {
            "record PR": record_text(pr="5"),
            "record head-sha": record_text(head=sha(0x405)),
            "record merge-commit-sha": record_text(merge=C3),
            "record independent-review": record_text(review="LHR-GOV-PR4-R2 = HOLD_WITH_FINDINGS"),
            "record owner-authorization": record_text(auth="LHR-GOV-PR4-MERGE = PENDING"),
            "record malformed": record_text() + "head-sha: " + H4 + "\n",
        }
        for needle, text in cases.items():
            with self.subTest(needle=needle):
                self.repo = FakeRepo()
                self.repo.files[(RECORD, C3)] = text
                self.assert_intro_fail(needle)
        self.repo = FakeRepo()
        self.repo.files[(RECORD, C3)] = record_text().replace("owner-authorization", "owner")
        self.assert_intro_fail("record malformed")

    def test_c3g_accepted_record_variants(self):
        for review, auth in (("LHR-GOV-PR4-R2 = PASS_WITH_FINDINGS", "LHR-GOV-PR4-MERGE = AUTHORIZED_MERGE"),):
            with self.subTest(review=review):
                self.repo = FakeRepo()
                self.repo.files[(RECORD, C3)] = record_text(review=review, auth=auth)
                self.assertEqual(self.run_mode("gate", dispatch_env())[0], 0)

    def test_c3j_independent_review_results(self):
        # R3-N1: closed list PASS, PASS_WITH_FINDINGS, PASS_NARROW, matched as a whole value.
        for review in ("LHR-GOV-PR4-R3 = PASS", "LHR-GOV-PR4-R3 = PASS_WITH_FINDINGS", "LHR-GOV-PR4-R3 = PASS_NARROW"):
            with self.subTest(accepted=review):
                self.repo = FakeRepo()
                self.repo.files[(RECORD, C3)] = record_text(review=review)
                self.assertEqual(self.run_mode("gate", dispatch_env())[0], 0)
        rejected = (
            "LHR-GOV-PR4-R3 = HOLD", "LHR-GOV-PR4-R3 = FAIL", "LHR-GOV-PR4-R2 = HOLD_NARROW_R2_F04",
            "LHR-GOV-PR4-R3 = PASSED", "LHR-GOV-PR4-R3 = PASS_", "LHR-GOV-PR4-R3 = PASS_NARROW_X",
            "LHR-GOV-PR4-R3 = PASS_WITH_FINDINGS_X", "LHR-GOV-PR4-R3 = PASS_NARROWER", "LHR-GOV-PR4-R3 = PASS_OTHER",
            "LHR-GOV-PR4-R3 = NOT_PASS", "LHR-GOV-PR4-R3 = pass_narrow", "LHR-GOV-PR4-R3 = PASS NARROW",
            "XLHR-GOV-PR4-R3 = PASS", "LHR-GOV-PR4-R3 = PASS; FAIL", "LHR-GOV-PR4-R3 =PASS", "PASS_NARROW",
        )
        for review in rejected:
            with self.subTest(rejected=review):
                self.repo = FakeRepo()
                self.repo.files[(RECORD, C3)] = record_text(review=review)
                self.assert_intro_fail("record independent-review")

    def test_c3h_releasing_the_introduction_commit_without_record_fails(self):
        self.repo.head = C2
        code, report = self.run_mode("gate", dispatch_env(sha_=C2))
        self.assertEqual(code, 1)
        self.assertTrue(any("missing" in m for m in self.messages(report, "FAIL")))

    def test_c3i_post_tag_and_audit_apply_the_same_rules(self):
        self.tag_v030()
        self.assertEqual(self.run_mode("gate", push_env())[0], 0)
        self.assertEqual(self.run_mode("audit", audit_env())[0], 0)
        self.repo.parents[C3] = [C2]
        self.assertEqual(self.run_mode("gate", push_env())[0], 1)
        self.assertEqual(self.run_mode("audit", audit_env())[0], 1)


class PreviousTagTests(Base):
    """R2-F04: earlier release tags are never stopping points for P3."""

    def setUp(self):
        super().setUp()
        # PR #5 (C3) was merged without approval and tagged v0.3.0; PR #6 (C4) is approved.
        self.repo.statuses[H5] = []
        self.repo.tags["v0.3.0"] = {"type": "commit", "sha": C3}
        self.repo.head = C4
        self.repo.files[("linux_health_reporter/__init__.py", C4)] = '__version__ = "0.3.1"\n'
        self.repo.files[("CHANGELOG.md", C4)] = "# Changelog\n\n## [0.3.1] — 2026-11-20\n"
        self.repo.statuses[C4] = [FakeRepo.status("release-preflight/v0.3.1", "success", 100, url_run=510)]
        self.repo.runs[510] = dict(self.repo.runs[500], id=510, head_sha=C4, display_title=f"release-preflight v0.3.1 {C4}")
        self.repo.runs[610] = {"id": 610, "created_at": ts(200), "head_branch": "v0.3.1"}
        self.repo.tag_runs["v0.3.1"] = [self.repo.runs[610]]

    def unapproved_pr5(self, report):
        return [m for m in self.messages(report, "FAIL") if m.startswith("P3 PR #5 without review-gate success")]

    def test_r2f04a_preflight_does_not_stop_at_invalid_earlier_tag(self):
        code, report = self.run_mode("gate", dispatch_env(tag="v0.3.1", sha_=C4))
        self.assertEqual(code, 1)
        self.assertEqual(len(self.unapproved_pr5(report)), 1, report.lines)
        self.assertTrue(any("PR #6 review-gate success" in m for m in self.messages(report, "PASS")))

    def test_r2f04b_post_tag_does_not_stop_at_invalid_earlier_tag(self):
        self.repo.tags["v0.3.1"] = {"type": "commit", "sha": C4}
        e = push_env("v0.3.1")
        e["GITHUB_RUN_ID"] = "610"
        code, report = self.run_mode("gate", e)
        self.assertEqual(code, 1)
        self.assertEqual(len(self.unapproved_pr5(report)), 1, report.lines)
        self.assertTrue(any(m.startswith("T4 preflight evidence for v0.3.1 at") for m in self.messages(report, "PASS")))

    def test_r2f04c_audit_flags_the_later_release_too(self):
        self.repo.tags["v0.3.1"] = {"type": "commit", "sha": C4}
        code, report = self.run_mode("audit", audit_env())
        self.assertEqual(code, 1)
        # Once for v0.3.0 itself and once for v0.3.1, whose walk goes through C3.
        self.assertEqual(len(self.unapproved_pr5(report)), 2, report.lines)

    def test_r2f04d_valid_earlier_tag_does_not_shorten_the_walk(self):
        self.repo.statuses[H5] = [FakeRepo.status("review-gate", "success", 50)]
        code, report = self.run_mode("gate", dispatch_env(tag="v0.3.1", sha_=C4))
        self.assertEqual(code, 0, report.lines)
        passed = self.messages(report, "PASS")
        for needle in ("PR #6 review-gate success", "PR #5 review-gate success", "gate introduction PR #4"):
            self.assertTrue(any(needle in m for m in passed), needle)
        self.assertTrue(any("GATE_BASELINE" in m for m in self.messages(report, "INFO")))

    def test_r2f04e_tag_listing_is_not_used_by_p3(self):
        self.repo.statuses[H5] = [FakeRepo.status("review-gate", "success", 50)]
        self.repo.fail.add(f"/repos/{REPO}/tags")
        code, report = self.run_mode("gate", dispatch_env(tag="v0.3.1", sha_=C4))
        self.assertEqual(code, 0, report.lines)

    def test_r2f04f_walk_limit_fails_closed(self):
        self.repo.statuses[H5] = [FakeRepo.status("review-gate", "success", 50)]
        with mock.patch.object(rel, "MAX_WALK", 2):
            code, report = self.run_mode("gate", dispatch_env(tag="v0.3.1", sha_=C4))
        self.assertEqual(code, 1)
        self.assertTrue(any("failing closed" in m for m in self.messages(report, "FAIL")))


class AnchorTests(unittest.TestCase):
    def test_real_anchors_are_pinned(self):
        tags, baseline, pr = REAL_ANCHORS
        self.assertEqual(tags, {
            "v0.1.0": "2895ccd9864e8c4d02bd9e5dd473717c3d61c5f8",
            "v0.2.0": "a0616c83560ed40bd24280465014292a268872c4",
        })
        self.assertEqual(baseline, "dea0ac9379cc688b3ff9edce16f17bf41c29d747")
        self.assertEqual(pr, 4)
        self.assertTrue(rel.anchors_valid())

    def test_no_classification_by_workflow_presence(self):
        source = inspect.getsource(rel)
        self.assertNotIn("RELEASE_GATE_PATH, commit", source)
        self.assertNotIn("review-gate.yml", source)


class StaticTests(unittest.TestCase):
    def test_wf8_inputs_validated_before_any_client(self):
        source = inspect.getsource(rel.run)
        self.assertLess(source.index("valid_tag(tag)"), source.index("client_factory()"))
        self.assertLess(source.index("valid_sha(sha)"), source.index("client_factory()"))

    def test_only_preflight_context_is_written(self):
        module = inspect.getsource(rel)
        writes = [line.strip() for line in module.splitlines() if "create_status(" in line]
        self.assertEqual(len(writes), 1)
        self.assertIn("PREFLIGHT_CONTEXT.format(tag=tag)", writes[0])


if __name__ == "__main__":
    unittest.main()
