"""Review gate reconciler tests (RG-U*). Synthetic payloads only; no network."""

import inspect
import unittest

from tests.governance_support import (
    BOT, OWNER, PRODUCER_ID, REPO, FakeGitHub, env, event, make_pr, payload, review_gate as rg,
    sha, status, ts,
)

H = sha(0xA1)
H2 = sha(0xB2)
LABEL = "review:passed"


def approved_state(head=H, number=1, labels=(LABEL,), marker_t=10, label_t=20, extra_events=()):
    """A PR whose head has a gate marker and a later owner approval."""
    pr = make_pr(number, head, labels=labels)
    statuses = [status("pending", f"awaiting owner review of {head[:7]}", marker_t)]
    events = [event("labeled", label_t, label=LABEL)] + list(extra_events)
    return pr, statuses, events


class DecideTests(unittest.TestCase):
    def decide(self, prs, events=(), statuses=(), head=H):
        return rg.decide(head, list(prs), REPO, list(events), list(statuses))

    def test_u1_owner_approval_after_marker_is_success(self):
        pr, st, ev = approved_state()
        d = self.decide([pr], ev, st)
        self.assertEqual((d.state, d.description), ("success", f"approved by owner at {H[:7]}"))

    def test_u2_no_marker_is_pending_even_with_label(self):
        pr = make_pr(1, H, labels=(LABEL,))
        d = self.decide([pr], [event("labeled", 5, label=LABEL)], [])
        self.assertEqual(d.state, "pending")
        self.assertEqual(d.description, f"awaiting owner review of {H[:7]}")

    def test_u3_label_before_marker_does_not_approve(self):
        pr, st, _ = approved_state(marker_t=30)
        d = self.decide([pr], [event("labeled", 20, label=LABEL)], st)
        self.assertEqual((d.state, d.description), ("pending", rg.PREDATES))

    def test_u4_label_removed_after_success_is_withdrawn(self):
        pr = make_pr(1, H, labels=())
        st = [status("pending", "x", 10), status("success", f"approved by owner at {H[:7]}", 30)]
        d = self.decide([pr], [event("labeled", 20, label=LABEL), event("unlabeled", 40, label=LABEL)], st)
        self.assertEqual((d.state, d.description), ("failure", f"approval withdrawn at {H[:7]}"))

    def test_u4b_withdrawal_is_reported_after_this_runs_prewrite(self):
        pr = make_pr(1, H, labels=())
        st = [status("pending", "x", 10), status("success", f"approved by owner at {H[:7]}", 30),
              status("pending", f"reconciling {H[:7]}", 41)]
        d = self.decide([pr], [event("labeled", 20, label=LABEL), event("unlabeled", 40, label=LABEL)], st)
        self.assertEqual((d.state, d.description), ("failure", f"approval withdrawn at {H[:7]}"))

    def test_u5_label_absent_and_already_awaiting_is_unchanged(self):
        pr = make_pr(1, H)
        st = [status("pending", f"awaiting owner review of {H[:7]}", 10)]
        self.assertIsNone(self.decide([pr], [], st))

    def test_u6_non_owner_or_non_user_actor_never_approves(self):
        for login, kind in ((BOT, "Bot"), ("dependabot[bot]", "Bot"), ("someone-else", "User"), (OWNER, "Bot"), ("brmff", "User")):
            with self.subTest(login=login, kind=kind):
                pr, st, _ = approved_state()
                d = self.decide([pr], [event("labeled", 20, label=LABEL, login=login, actor_type=kind)], st)
                self.assertNotEqual(d.state, "success")

    def test_u7_last_review_event_unlabeled_does_not_approve(self):
        pr, st, _ = approved_state()
        evs = [event("labeled", 20, label=LABEL), event("unlabeled", 25, label=LABEL)]
        self.assertNotEqual(self.decide([pr], evs, st).state, "success")

    def test_u8_force_push_back_to_approved_sha_requires_new_approval(self):
        pr = make_pr(1, H, labels=(LABEL,))
        st = [status("pending", "x", 10), status("success", f"approved by owner at {H[:7]}", 21)]
        evs = [event("labeled", 20, label=LABEL), event("head_ref_force_pushed", 50)]
        d = self.decide([pr], evs, st)
        self.assertEqual((d.state, d.description), ("pending", f"awaiting owner review of {H[:7]}"))

    def test_u9_invalidation_events_revoke(self):
        for kind in ("base_ref_changed", "reopened", "convert_to_draft"):
            with self.subTest(kind=kind):
                pr = make_pr(1, H, labels=(LABEL,))
                st = [status("pending", "x", 10), status("success", f"approved by owner at {H[:7]}", 21)]
                d = self.decide([pr], [event("labeled", 20, label=LABEL), event(kind, 50)], st)
                self.assertEqual(d.state, "pending")

    def test_u10_fork_or_other_base_is_failure(self):
        cases = (
            make_pr(1, H, labels=(LABEL,), head_repo="someone/fork"),
            make_pr(1, H, labels=(LABEL,), head_repo=None),
            make_pr(1, H, labels=(LABEL,), base_ref="develop"),
            make_pr(1, H, labels=(LABEL,), base_repo="other/repo"),
        )
        for pr in cases:
            with self.subTest(pr=pr["base"]["ref"]):
                _, st, ev = approved_state()
                d = self.decide([pr], ev, st)
                self.assertEqual((d.state, d.description), ("failure", rg.INVALID_CONTEXT))

    def test_u11_draft_is_pending(self):
        pr, st, ev = approved_state()
        pr["draft"] = True
        self.assertEqual(self.decide([pr], ev, st), rg.Decision("pending", rg.DRAFT))

    def test_u12_no_open_pr_for_head_means_no_action(self):
        self.assertIsNone(self.decide([], [], []))

    def test_u13_label_name_must_match_exactly(self):
        for name in ("Review:Passed", "review:passed ", "review:passed2", " review:passed"):
            with self.subTest(name=name):
                pr = make_pr(1, H, labels=(name,))
                st = [status("pending", "x", 10)]
                d = self.decide([pr], [event("labeled", 20, label=name)], st)
                self.assertNotEqual(d.state, "success")

    def test_u13b_events_for_lookalike_labels_are_ignored(self):
        # The exact label predates the marker; a look-alike label applied later must not count.
        for name in ("Review:Passed", " review:passed", "review:passed "):
            with self.subTest(name=name):
                pr = make_pr(1, H, labels=(LABEL, name))
                st = [status("pending", "x", 10)]
                ev = [event("labeled", 5, label=LABEL), event("labeled", 20, label=name)]
                self.assertNotEqual(self.decide([pr], ev, st).state, "success")
        # A later unlabeled look-alike must not hide a valid exact approval.
        pr = make_pr(1, H, labels=(LABEL,))
        st = [status("pending", "x", 10)]
        ev = [event("labeled", 20, label=LABEL), event("unlabeled", 30, label="Review:Passed")]
        self.assertEqual(self.decide([pr], ev, st).state, "success")

    def test_u14_label_at_same_second_as_marker_does_not_approve(self):
        pr, st, _ = approved_state(marker_t=20)
        self.assertNotEqual(self.decide([pr], [event("labeled", 20, label=LABEL)], st).state, "success")

    def test_u15_unrelated_labels_keep_success(self):
        pr = make_pr(1, H, labels=(LABEL, "docs"))
        st = [status("pending", "x", 10), status("success", f"approved by owner at {H[:7]}", 21)]
        evs = [event("labeled", 20, label=LABEL), event("labeled", 30, label="docs"), event("unlabeled", 31, label="wip")]
        self.assertIsNone(self.decide([pr], evs, st))

    def test_u16_malformed_data_raises(self):
        pr = make_pr(1, H, labels=(LABEL,))
        bad = status("pending", "x", 10)
        bad["created_at"] = "yesterday"
        with self.assertRaises(ValueError):
            self.decide([pr], [], [bad])

    def test_u18_identical_result_is_not_rewritten(self):
        pr, st, ev = approved_state()
        st.append(status("success", f"approved by owner at {H[:7]}", 25))
        self.assertIsNone(self.decide([pr], ev, st))

    def test_marker_ignores_statuses_from_other_creators(self):
        pr = make_pr(1, H, labels=(LABEL,))
        st = [status("pending", "x", 10, creator=OWNER)]
        d = self.decide([pr], [event("labeled", 20, label=LABEL)], st)
        self.assertEqual(d.description, f"awaiting owner review of {H[:7]}")


class SharedHeadTests(unittest.TestCase):
    """RG-U19 to RG-U22: a status belongs to a SHA, not to a PR (RG-R2-F02)."""

    def test_u19_approved_and_unapproved_pr_on_same_sha(self):
        pr_a, st, ev = approved_state(number=1)
        pr_b = make_pr(2, H)
        d = rg.decide(H, [pr_a, pr_b], REPO, ev, st)
        self.assertEqual((d.state, d.description), ("failure", rg.MULTIPLE_PRS))

    def test_u20_both_approved_still_fails(self):
        pr_a, st, ev = approved_state(number=1)
        pr_b = make_pr(2, H, labels=(LABEL,))
        self.assertEqual(rg.decide(H, [pr_a, pr_b], REPO, ev, st).state, "failure")

    def test_u21_other_base_sharing_sha_fails(self):
        pr_a, st, ev = approved_state(number=1)
        pr_b = make_pr(2, H, base_ref="develop")
        self.assertEqual(rg.decide(H, [pr_a, pr_b], REPO, ev, st).state, "failure")

    def test_u22_after_other_pr_closes_approval_returns(self):
        pr_a, st, ev = approved_state(number=1)
        st.append(status("failure", rg.MULTIPLE_PRS, 30))
        d = rg.decide(H, [pr_a], REPO, ev, st)
        self.assertEqual(d.state, "success")

    def test_inventory_based_sharing_in_run(self):
        pr_a, st, ev = approved_state(number=1)
        gh = FakeGitHub(pulls=[pr_a, make_pr(2, H)], statuses={H: st}, events={1: ev})
        self.assertEqual(rg.run(env("schedule"), gh, None), 0)
        self.assertEqual(gh.latest(H)["state"], "failure")
        self.assertNotIn(("GET_ALL", f"/repos/{REPO}/issues/1/events"), gh.calls)


class OrchestrationTests(unittest.TestCase):
    def test_u23_every_open_pr_head_is_reconciled_regardless_of_event_prs(self):
        prs = [make_pr(1, H), make_pr(2, H2), make_pr(3, sha(0xC3))]
        gh = FakeGitHub(pulls=prs)
        p = payload(H, pull_requests=[{"number": 99}])
        self.assertEqual(rg.run(env(), gh, p), 0)
        for head in (H, H2, sha(0xC3)):
            self.assertIsNotNone(gh.latest(head), head)

    def test_u24_sha_inside_a_pr_but_not_its_head_is_not_a_target(self):
        inside = sha(0xD4)
        prs = [make_pr(1, H)]
        self.assertIsNone(rg.prewrite_target(inside, prs, REPO))
        gh = FakeGitHub(pulls=prs)
        rg.run(env(), gh, payload(inside))
        self.assertNotIn(inside, gh.statuses)

    def test_u25_untrusted_running_workflow_makes_no_mutation(self):
        cases = (
            env(workflow_ref=f"{REPO}/.github/workflows/review-gate.yml@refs/heads/feature"),
            env(workflow_ref=f"{REPO}/.github/workflows/other.yml@refs/heads/main"),
            env("workflow_dispatch", ref="refs/heads/feature"),
            env("pull_request_target"),
            env("pull_request"),
        )
        for e in cases:
            with self.subTest(e=e["GITHUB_EVENT_NAME"] + e["GITHUB_WORKFLOW_REF"] + e["GITHUB_REF"]):
                gh = FakeGitHub(pulls=[make_pr(1, H)])
                self.assertEqual(rg.run(e, gh, payload(H)), 2)
                self.assertEqual(gh.calls, [])

    def test_u26_u27_invalid_event_context_skips_prewrite_but_reconciles(self):
        bad_payloads = (
            payload(H, path=".github/workflows/other.yml"),
            payload(H, event_type="pull_request_target"),
            payload(H, event_type="push"),
            payload(H, repo="other/repo"),
            payload("A" * 40),
            payload(H[:39]),
            {"workflow_run": "not a dict"},
            None,
        )
        for p in bad_payloads:
            with self.subTest(p=str(p)[:60]):
                gh = FakeGitHub(pulls=[make_pr(1, H)])
                self.assertEqual(rg.run(env(), gh, p), 0)
                self.assertFalse(any(desc.startswith("reconciling") for _, _, desc in gh.writes))
                self.assertIsNotNone(gh.latest(H))

    def test_u28_prewrite_is_first_mutation_after_validations(self):
        gh = FakeGitHub(pulls=[make_pr(1, H)])
        rg.run(env(), gh, payload(H))
        first_post = next(i for i, call in enumerate(gh.calls) if call[0] == "POST")
        before = gh.calls[:first_post]
        self.assertIn(("GET", f"/repos/{REPO}/actions/workflows/review-gate-trigger.yml"), before)
        self.assertIn(("GET_ALL", f"/repos/{REPO}/pulls"), before)
        self.assertEqual(gh.writes[0], (H, "pending", f"reconciling {H[:7]}"))

    def test_u29_valid_producer_paths(self):
        for path in (
            ".github/workflows/review-gate-trigger.yml",
            ".github/workflows/review-gate-trigger.yml@refs/heads/main",
            ".github/workflows/review-gate-trigger.yml@refs/pull/7/merge",
            ".github/workflows/review-gate-trigger.yml@refs/tags/v0.3.0",
        ):
            with self.subTest(path=path):
                self.assertTrue(rg.is_producer_path(path))
                self.assertEqual(rg.event_head_sha(payload(H, path=path), REPO, PRODUCER_ID), H)

    def test_u30_invalid_producer_paths(self):
        for path in (
            ".github/workflows/Review-Gate-Trigger.yml",
            "./.github/workflows/review-gate-trigger.yml",
            ".github/workflows/../workflows/review-gate-trigger.yml",
            "x/.github/workflows/review-gate-trigger.yml",
            ".github/workflows/review-gate-trigger.yml@@refs/heads/main",
            ".github/workflows/review-gate-trigger.yml@refs/heads/a b",
            ".github/workflows/review-gate-trigger.yml@main",
            ".github/workflows/review-gate-trigger.yml@refs/heads/a..b",
            ".github/workflows/review-gate-trigger.yml@",
            ".github/workflows/review-gate-trigger.yml.bak",
            ".github/workflows/review-gate-trigger.yml ",
            "dynamic/dependabot/dependabot-updates",
            "",
            None,
        ):
            with self.subTest(path=path):
                self.assertFalse(rg.is_producer_path(path))
                self.assertIsNone(rg.event_head_sha(payload(H, path=path), REPO, PRODUCER_ID))

    def test_u31_wrong_workflow_id(self):
        for wid in (PRODUCER_ID + 1, str(PRODUCER_ID), None, True):
            with self.subTest(wid=wid):
                self.assertIsNone(rg.event_head_sha(payload(H, workflow_id=wid), REPO, PRODUCER_ID))

    def test_u32_wrong_name(self):
        for name in ("review-gate", "Review-Gate-Trigger", "review-gate-trigger ", None):
            with self.subTest(name=name):
                self.assertIsNone(rg.event_head_sha(payload(H, name=name), REPO, PRODUCER_ID))

    def test_u33_producer_lookup_failure_is_fail_closed(self):
        for producer_id in (None,):
            gh = FakeGitHub(pulls=[make_pr(1, H)], producer_id=producer_id)
            self.assertEqual(rg.run(env(), gh, payload(H)), 0)
            self.assertFalse(any(desc.startswith("reconciling") for _, _, desc in gh.writes))
        gh = FakeGitHub(pulls=[make_pr(1, H)])
        gh.fail.add("/actions/workflows/")
        rg.run(env(), gh, payload(H))
        self.assertFalse(any(desc.startswith("reconciling") for _, _, desc in gh.writes))

    def test_u34_head_of_open_pr_gets_prewrite(self):
        self.assertEqual(rg.prewrite_target(H, [make_pr(1, H)], REPO), H)

    def test_u35_merge_commit_sha_never_gets_prewrite(self):
        merge = sha(0xEE)
        prs = [make_pr(1, H, merge=merge), make_pr(2, merge)]
        self.assertIsNone(rg.prewrite_target(merge, prs, REPO))

    def test_u36_sha_without_open_pr_gets_no_prewrite(self):
        self.assertIsNone(rg.prewrite_target(H2, [make_pr(1, H)], REPO))
        self.assertIsNone(rg.prewrite_target(H, [], REPO))
        self.assertIsNone(rg.prewrite_target(H, [make_pr(1, H, base_repo="other/repo")], REPO))

    def test_u37_invalid_candidate_or_inventory_failure(self):
        self.assertIsNone(rg.prewrite_target(None, [make_pr(1, H)], REPO))
        self.assertIsNone(rg.prewrite_target(H.upper(), [make_pr(1, H.upper())], REPO))
        gh = FakeGitHub(pulls=[make_pr(1, H)])
        gh.fail.add("/pulls")
        self.assertEqual(rg.run(env(), gh, payload(H)), 1)
        self.assertEqual(gh.writes, [])

    def test_u17_descriptions_never_contain_contributor_text(self):
        marker = "SECRET-<script>-$(id)"
        pr = make_pr(1, H, labels=(LABEL, marker), title=marker, branch=marker)
        gh = FakeGitHub(pulls=[pr], events={1: [event("labeled", 20, label=marker)]})
        rg.run(env(), gh, payload(H))
        for _, _, desc in gh.writes:
            self.assertNotIn("SECRET", desc)


class ApiFailureTests(unittest.TestCase):
    """RG-U38: incomplete data can never produce an approval."""

    def test_inventory_failure_or_malformed_inventory_means_no_mutation(self):
        broken = (
            [make_pr(1, H), make_pr(1, H2)],          # duplicate PR
            [dict(make_pr(1, H), head={"sha": "xyz", "repo": None})],
            [dict(make_pr(1, H), state="closed")],
            [dict(make_pr(1, H), draft="no")],
            [dict(make_pr(1, H), labels="review:passed")],
        )
        for pulls in broken:
            with self.subTest(pulls=str(pulls)[:50]):
                gh = FakeGitHub(pulls=pulls)
                self.assertEqual(rg.run(env("schedule"), gh, None), 1)
                self.assertEqual(gh.writes, [])

    def test_status_or_event_read_failure_gives_pending_never_success(self):
        for failing in (f"/commits/{H}/statuses", "/issues/1/events"):
            with self.subTest(failing=failing):
                pr, st, ev = approved_state()
                gh = FakeGitHub(pulls=[pr, make_pr(2, H2)], statuses={H: st}, events={1: ev})
                gh.fail.add(failing)
                self.assertEqual(rg.run(env("schedule"), gh, None), 1)
                self.assertEqual(gh.latest(H)["description"], rg.INCOMPLETE)
                self.assertIsNotNone(gh.latest(H2))  # other heads still reconciled

    def test_write_failure_is_reported(self):
        gh = FakeGitHub(pulls=[make_pr(1, H)])
        gh.fail.add(f"/statuses/{H}")
        self.assertEqual(rg.run(env("schedule"), gh, None), 1)


class PaginationTests(unittest.TestCase):
    """RG-U38: GitHubClient.get_all returns data only when every page succeeded."""

    def client(self, pages):
        from tests.governance_support import github_api
        client = github_api.GitHubClient("t", REPO)
        responses = iter(pages)

        def fake_request(method, url, body=None):
            item = next(responses)
            if isinstance(item, Exception):
                raise item
            return item

        client._request = fake_request
        return client, github_api

    def link(self, page):
        return f'<https://api.github.com/repos/{REPO}/pulls?page={page}>; rel="next"'

    def test_full_pagination(self):
        client, _ = self.client([(200, [1, 2], self.link(2)), (200, [3], self.link(3)), (200, [4], "")])
        self.assertEqual(client.get_all(f"/repos/{REPO}/pulls"), [1, 2, 3, 4])

    def test_failure_on_page_two_raises(self):
        for failure in ((500, None, ""), (403, None, ""), (200, {"not": "a list"}, "")):
            with self.subTest(failure=failure[0]):
                client, api = self.client([(200, [1], self.link(2)), failure])
                with self.assertRaises(api.ApiError):
                    client.get_all(f"/repos/{REPO}/pulls")

    def test_network_error_raises(self):
        client, api = self.client([(200, [1], self.link(2)), api_error()])
        with self.assertRaises(api.ApiError):
            client.get_all(f"/repos/{REPO}/pulls")

    def test_next_link_outside_api_is_refused(self):
        from tests.governance_support import github_api
        client = github_api.GitHubClient("t", REPO)
        self.assertRaises(github_api.ApiError, client._request, "GET", "https://evil.example/x")

    def test_keyed_pages(self):
        client, api = self.client([(200, {"check_runs": [1]}, self.link(2)), (200, {"check_runs": [2]}, "")])
        self.assertEqual(client.get_all("/x", key="check_runs"), [1, 2])
        client, api = self.client([(200, {"total_count": 1}, "")])
        with self.assertRaises(api.ApiError):
            client.get_all("/x", key="check_runs")


def api_error():
    from tests.governance_support import github_api
    return github_api.ApiError("network")


class StaticSourceTests(unittest.TestCase):
    def test_wf15_producer_path_uses_exact_comparison(self):
        source = inspect.getsource(rg.is_producer_path)
        for forbidden in ("startswith", "endswith", " in path", "re.search", ".search("):
            self.assertNotIn(forbidden, source)
        self.assertIn("base != PRODUCER_PATH", source)
        event_source = inspect.getsource(rg.event_head_sha)
        self.assertIn('run.get("name") == PRODUCER_NAME', event_source)
        self.assertIn("workflow_id == producer_workflow_id", event_source)

    def test_wf16_prewrite_only_uses_validated_target(self):
        source = inspect.getsource(rg.run)
        self.assertLess(source.index("target = prewrite_target("), source.index("RECONCILING"))
        self.assertIn("client.create_status(target, \"pending\", CONTEXT, RECONCILING", source)
        self.assertEqual(source.count("create_status("), 2)

    def test_wf13_validations_precede_first_write(self):
        source = inspect.getsource(rg.run)
        first_write = source.index("create_status(")
        for step in ("running_workflow_is_trusted(", "event_head_sha(", "validate_inventory("):
            self.assertLess(source.index(step), first_write, step)

    def test_wf14_writes_only_to_validated_heads(self):
        source = inspect.getsource(rg.run)
        writes = [line.strip() for line in source.splitlines() if "create_status(" in line]
        self.assertEqual(len(writes), 2)
        self.assertTrue(writes[0].startswith("client.create_status(target,"))
        self.assertTrue(writes[1].startswith("client.create_status(head_sha,"))
        module_source = inspect.getsource(rg)
        self.assertNotIn("refs/pull/", module_source.replace("refs/(heads|pull|tags)", ""))


if __name__ == "__main__":
    unittest.main()
