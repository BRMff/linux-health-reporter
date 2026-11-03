"""Deterministic simulation of GitHub concurrency for the review gate (RG-S*).

Model (docs/governance.md, section 1.4):
* one global concurrency group: at most one run executing and one pending;
  a newly queued run cancels the pending one; the last queued is never cancelled;
* a run reads a snapshot when it starts, applies its pre-write immediately and
  its other writes when it ends (the non-atomic window, RR1);
* triggers can be lost and runs can fail before or after the pre-write.
The real ``review_gate.run`` is executed for every reconciler run.
"""

from __future__ import annotations

import copy
import heapq
import random
import unittest

from tests.governance_support import (
    BOT, OWNER, PRODUCER_ID, REPO, env, make_pr, payload, review_gate as rg, ts,
)

LABEL = "review:passed"
INVALIDATION = rg.INVALIDATION_EVENTS


class SimRepo:
    def __init__(self):
        self.now = 0
        self.prs: dict[int, dict] = {}
        self.events: dict[int, list] = {}
        self.statuses: dict[str, list] = {}
        self.head_since: dict[int, int] = {}
        self.history: dict[int, list] = {}
        self.ids = 0
        self.sha_counter = 0

    def new_id(self):
        self.ids += 1
        return self.ids

    def new_sha(self):
        self.sha_counter += 1
        return f"{self.sha_counter:040x}"

    # --- state changes -------------------------------------------------
    def open_pr(self, number, head=None):
        head = head or self.new_sha()
        self.prs[number] = make_pr(number, head)
        self.events[number] = []
        self.head_since[number] = self.now
        self.history[number] = [head]
        return head

    def _event(self, number, kind, label=None, login=OWNER, actor_type="User"):
        item = {"id": self.new_id(), "event": kind, "created_at": ts(self.now),
                "actor": {"login": login, "type": actor_type}}
        if label:
            item["label"] = {"name": label}
        self.events[number].append(item)

    def label(self, number, name=LABEL, login=OWNER, actor_type="User"):
        labels = self.prs[number]["labels"]
        if all(l["name"] != name for l in labels):
            labels.append({"name": name})
            self._event(number, "labeled", name, login, actor_type)

    def unlabel(self, number, name=LABEL, login=OWNER):
        labels = self.prs[number]["labels"]
        if any(l["name"] == name for l in labels):
            self.prs[number]["labels"] = [l for l in labels if l["name"] != name]
            self._event(number, "unlabeled", name, login)

    def push(self, number):
        head = self.new_sha()
        self.prs[number]["head"]["sha"] = head
        self.head_since[number] = self.now
        self.history[number].append(head)

    def force_back(self, number):
        if len(self.history[number]) < 2:
            return
        head = self.history[number][-2]
        self.prs[number]["head"]["sha"] = head
        self.head_since[number] = self.now
        self.history[number].append(head)
        self._event(number, "head_ref_force_pushed")

    def draft(self, number, value):
        if self.prs[number]["draft"] != value:
            self.prs[number]["draft"] = value
            self._event(number, "convert_to_draft" if value else "ready_for_review")

    def close(self, number):
        self.prs[number]["state"] = "closed"

    def head(self, number):
        return self.prs[number]["head"]["sha"]

    def inventory(self):
        return [copy.deepcopy(p) for p in self.prs.values() if p["state"] == "open"]

    # --- reference oracle (independent of review_gate.decide) -----------
    def open_with_head(self, head):
        return [n for n, p in self.prs.items() if p["state"] == "open" and p["head"]["sha"] == head]

    def ideal_valid(self, number):
        """Safety oracle: an approval by the owner for the current head."""
        pr = self.prs[number]
        if pr["state"] != "open" or pr["draft"] or len(self.open_with_head(pr["head"]["sha"])) != 1:
            return False
        if all(l["name"] != LABEL for l in pr["labels"]):
            return False
        review = [e for e in self.events[number] if e["event"] in ("labeled", "unlabeled") and e.get("label", {}).get("name") == LABEL]
        if not review or review[-1]["event"] != "labeled" or review[-1]["actor"] != {"login": OWNER, "type": "User"}:
            return False
        label_t = review[-1]["created_at"]
        invalid = [e["created_at"] for e in self.events[number] if e["event"] in INVALIDATION]
        return label_t > ts(self.head_since[number]) and (not invalid or label_t > max(invalid))

    def marker_time(self, number):
        head = self.head(number)
        invalid = [e["created_at"] for e in self.events[number] if e["event"] in INVALIDATION]
        cutoff = max(invalid) if invalid else ""
        for s in sorted(self.statuses.get(head, []), key=lambda s: (s["created_at"], s["id"])):
            if s["context"] == "review-gate" and s["creator"]["login"] == BOT and s["created_at"] > cutoff:
                return s["created_at"]
        return None

    def latest(self, head):
        items = [s for s in self.statuses.get(head, []) if s["context"] == "review-gate"]
        return max(items, key=lambda s: (s["created_at"], s["id"])) if items else None

    def mergeable(self, number):
        """Model of the ruleset after activation: SG4 drafts never merge."""
        pr = self.prs[number]
        latest = self.latest(pr["head"]["sha"])
        return pr["state"] == "open" and not pr["draft"] and latest is not None and latest["state"] == "success"


class SimClient:
    """Reads from the snapshot taken at run start; pre-write is immediate,
    other writes are buffered until the run ends."""

    def __init__(self, repo: SimRepo, sim):
        self.repo = repo
        self.sim = sim
        self.snapshot_pulls = repo.inventory()
        self.snapshot_statuses = copy.deepcopy(repo.statuses)
        self.snapshot_events = copy.deepcopy(repo.events)
        self.snapshot_valid = {n: repo.ideal_valid(n) for n, p in repo.prs.items() if p["state"] == "open"}
        self.snapshot_multi = {h for h in {p["head"]["sha"] for p in self.snapshot_pulls}
                               if len(repo.open_with_head(h)) > 1}
        self.buffer: list[tuple] = []

    def get(self, path, params=None):
        assert path.endswith("/actions/workflows/review-gate-trigger.yml")
        return {"id": PRODUCER_ID}

    def get_all(self, path, params=None, key=None):
        if path == f"/repos/{REPO}/pulls":
            return copy.deepcopy(self.snapshot_pulls)
        if path.endswith("/statuses"):
            return list(reversed(self.snapshot_statuses.get(path.split("/")[-2], [])))
        if path.endswith("/events"):
            return copy.deepcopy(self.snapshot_events.get(int(path.split("/")[-2]), []))
        raise AssertionError(path)

    def create_status(self, sha_, state, context, description, target_url):
        if description.startswith("reconciling"):
            record = self.sim.write(sha_, state, description)
            self.snapshot_statuses.setdefault(sha_, []).append(record)
        else:
            self.buffer.append((sha_, state, description))


class Simulator:
    def __init__(self, seed=0, drop_trigger=0.0, fail_before=0.0, fail_after=0.0, trigger_delay=None, run_duration=None):
        self.repo = SimRepo()
        self.trigger_delay = trigger_delay
        self.run_duration = run_duration
        self.rng = random.Random(seed)
        self.drop_trigger = drop_trigger
        self.fail_before = fail_before
        self.fail_after = fail_after
        self.queue: list = []
        self.seq = 0
        self.running = None
        self.pending = None
        self.intervals: list[tuple[int, int]] = []
        self.write_log: list[tuple[int, int]] = []  # (run start, write time)
        self.cancelled = 0
        self.current_start = None
        self.violations: list[str] = []

    # --- scheduling helpers -------------------------------------------
    def at(self, time, action, *args):
        self.seq += 1
        heapq.heappush(self.queue, (time, self.seq, action, args))

    def write(self, sha_, state, description):
        repo = self.repo
        record = {"id": repo.new_id(), "state": state, "description": description, "context": "review-gate",
                  "created_at": ts(repo.now), "creator": {"login": BOT}, "target_url": ""}
        repo.statuses.setdefault(sha_, []).append(record)
        self.write_log.append((self.current_start, repo.now))
        return record

    # --- runs -------------------------------------------------------------
    def enqueue(self, run):
        if self.running is None:
            self.start(run)
        else:
            if self.pending is not None:
                self.cancelled += 1
            self.pending = run

    def start(self, run):
        start = self.repo.now
        self.current_start = start
        duration = self.run_duration or self.rng.randint(1, 4)
        outcome = "ok"
        roll = self.rng.random()
        if roll < self.fail_before:
            outcome = "fail_before"
        elif roll < self.fail_before + self.fail_after:
            outcome = "fail_after"
        client = None
        if outcome != "fail_before":
            client = SimClient(self.repo, self)
            event_name = run["kind"] if run["kind"] != "rerun" else "workflow_run"
            rg.run(env(event_name), client, run.get("payload"))
        self.running = (run, start, start + duration, client, outcome)
        self.at(start + duration, self.finish)

    def finish(self):
        run, start, end, client, outcome = self.running
        self.current_start = start
        if client is not None and outcome == "ok":
            for sha_, state, description in client.buffer:
                if state == "success":
                    numbers = [p["number"] for p in client.snapshot_pulls if p["head"]["sha"] == sha_]
                    if sha_ in client.snapshot_multi or not any(client.snapshot_valid.get(n) for n in numbers):
                        self.violations.append(f"SG1 success without valid approval at {start}")
                self.write(sha_, state, description)
        self.intervals.append((start, end))
        self.running = None
        if self.pending is not None:
            run, self.pending = self.pending, None
            self.start(run)

    def trigger(self, number):
        if self.rng.random() < self.drop_trigger:
            return
        head = self.repo.head(number)
        delay = self.trigger_delay or self.rng.randint(1, 3)
        self.at(self.repo.now + delay, self.enqueue, {"kind": "workflow_run", "payload": payload(head)})

    # --- driver -------------------------------------------------------------
    def user(self, time, action, number, *args):
        def apply():
            getattr(self.repo, action)(number, *args)
            if action != "close":
                self.trigger(number)
        self.at(time, apply)

    def run_until_idle(self):
        while self.queue:
            time, _, action, args = heapq.heappop(self.queue)
            self.repo.now = max(self.repo.now, time)
            action(*args)

    def recover(self):
        """A scheduled recovery run after everything else has finished."""
        self.repo.now += 10
        saved = (self.fail_before, self.fail_after)
        self.fail_before = self.fail_after = 0.0
        self.enqueue({"kind": "schedule"})
        self.run_until_idle()
        self.fail_before, self.fail_after = saved

    # --- properties -----------------------------------------------------
    def check_safety(self, test):
        test.assertEqual(self.violations, [])
        for number, pr in self.repo.prs.items():
            if pr["state"] != "open":
                continue
            latest = self.repo.latest(pr["head"]["sha"])
            if latest is not None and latest["state"] == "success":
                test.assertTrue(self.repo.ideal_valid(number), f"stale success on PR {number}")

    def check_liveness(self, test):
        for number, pr in self.repo.prs.items():
            if pr["state"] != "open" or not self.repo.ideal_valid(number):
                continue
            review = [e for e in self.repo.events[number] if e["event"] == "labeled" and e["label"]["name"] == LABEL]
            marker = self.repo.marker_time(number)
            if marker is not None and review[-1]["created_at"] > marker:
                latest = self.repo.latest(pr["head"]["sha"])
                test.assertEqual(latest["state"], "success", f"approval not applied on PR {number}")

    def check_serialised(self, test):
        intervals = sorted(self.intervals)
        for (s1, e1), (s2, e2) in zip(intervals, intervals[1:]):
            test.assertLessEqual(e1, s2, "runs overlap")
        starts = [start for start, _ in sorted(self.write_log, key=lambda item: item[1])]
        test.assertEqual(starts, sorted(starts), "writes out of run order")


class ScenarioTests(unittest.TestCase):
    def sim(self, **kwargs):
        kwargs.setdefault("trigger_delay", 1)
        kwargs.setdefault("run_duration", 5)
        sim = Simulator(seed=7, **kwargs)
        sim.repo.open_pr(1)
        sim.at(0, sim.trigger, 1)
        return sim

    def approve(self, sim, t=20):
        sim.user(t, "label", 1)
        sim.run_until_idle()
        self.assertEqual(sim.repo.latest(sim.repo.head(1))["state"], "success")

    def test_s1_unlabeled_run_cancelled_by_unrelated_label(self):
        sim = self.sim()
        self.approve(sim)
        sim.user(100, "unlabel", 1)
        sim.user(100, "label", 1, "docs")
        sim.user(100, "label", 1, "wip")
        sim.run_until_idle()
        self.assertNotEqual(sim.repo.latest(sim.repo.head(1))["state"], "success")
        sim.check_safety(self)

    def test_s2_late_success_is_revoked_by_the_next_run(self):
        sim = self.sim()
        sim.run_until_idle()
        sim.user(50, "label", 1)
        sim.user(52, "unlabel", 1)
        sim.user(53, "label", 1, "docs")
        sim.run_until_idle()
        self.assertNotEqual(sim.repo.latest(sim.repo.head(1))["state"], "success")
        sim.check_safety(self)

    def test_s3_push_after_approval_never_inherits(self):
        sim = self.sim()
        self.approve(sim)
        sim.user(100, "push", 1)
        sim.user(100, "label", 1, "docs")
        sim.run_until_idle()
        self.assertNotEqual(sim.repo.latest(sim.repo.head(1))["state"], "success")
        sim.check_safety(self)

    def test_s4_unrelated_labels_keep_success(self):
        sim = self.sim()
        self.approve(sim)
        sim.user(100, "label", 1, "docs")
        sim.user(101, "unlabel", 1, "docs")
        sim.run_until_idle()
        self.assertEqual(sim.repo.latest(sim.repo.head(1))["state"], "success")

    def test_s6_single_unlabeled_revokes_by_itself(self):
        sim = self.sim()
        self.approve(sim)
        sim.user(100, "unlabel", 1)
        sim.run_until_idle()
        self.assertEqual(sim.repo.latest(sim.repo.head(1))["state"], "failure")

    def test_s7_superseded_unlabeled_run_still_revoked(self):
        sim = self.sim()
        self.approve(sim)
        sim.user(100, "label", 1, "docs")   # long-running reconciliation starts
        sim.user(102, "unlabel", 1)         # its run waits as pending
        sim.user(102, "label", 1, "wip")    # replaces the pending run
        sim.run_until_idle()
        self.assertEqual(sim.cancelled, 1)  # the unlabeled run was superseded
        self.assertNotEqual(sim.repo.latest(sim.repo.head(1))["state"], "success")

    def test_s8_lost_trigger_is_a_liveness_gap_recovered_by_schedule(self):
        sim = self.sim()
        self.approve(sim)
        sim.drop_trigger = 1.0
        sim.user(100, "unlabel", 1)
        sim.run_until_idle()
        self.assertEqual(sim.repo.latest(sim.repo.head(1))["state"], "success")  # residual, documented
        self.assertFalse(sim.repo.ideal_valid(1))
        sim.recover()
        self.assertEqual(sim.repo.latest(sim.repo.head(1))["state"], "failure")

    def test_s9_failure_before_prewrite_recovered_by_schedule(self):
        sim = self.sim()
        self.approve(sim)
        sim.fail_before = 1.0
        sim.user(100, "unlabel", 1)
        sim.run_until_idle()
        sim.recover()
        self.assertEqual(sim.repo.latest(sim.repo.head(1))["state"], "failure")

    def test_s10_failure_after_prewrite_leaves_pending(self):
        sim = self.sim()
        self.approve(sim)
        sim.fail_after = 1.0
        sim.user(100, "unlabel", 1)
        sim.run_until_idle()
        latest = sim.repo.latest(sim.repo.head(1))
        self.assertEqual((latest["state"], latest["description"][:11]), ("pending", "reconciling"))

    def test_s11_draft_is_never_mergeable_even_with_stale_success(self):
        sim = self.sim()
        self.approve(sim)
        sim.drop_trigger = 1.0
        sim.user(100, "draft", 1, True)
        sim.run_until_idle()
        self.assertEqual(sim.repo.latest(sim.repo.head(1))["state"], "success")
        self.assertFalse(sim.repo.mergeable(1))

    def test_s12_twin_pr_on_approved_sha(self):
        sim = self.sim()
        self.approve(sim)
        head = sim.repo.head(1)
        sim.at(100, lambda: (sim.repo.open_pr(2, head), sim.trigger(2)))
        sim.run_until_idle()
        self.assertEqual(sim.repo.latest(head)["state"], "failure")
        sim.at(200, lambda: (sim.repo.close(2), sim.trigger(1)))
        sim.run_until_idle()
        self.assertEqual(sim.repo.latest(head)["state"], "success")
        sim.check_safety(self)

    def test_s13_schedule_overlapping_event_run_is_serialised(self):
        sim = self.sim()
        sim.user(10, "label", 1, "docs")
        sim.at(11, sim.enqueue, {"kind": "schedule"})
        sim.at(11, sim.enqueue, {"kind": "workflow_dispatch"})
        sim.run_until_idle()
        sim.check_serialised(self)

    def test_s14_rerun_of_old_labeled_event_grants_nothing(self):
        sim = self.sim()
        self.approve(sim)
        old_head = sim.repo.head(1)
        sim.user(100, "push", 1)
        sim.user(110, "unlabel", 1)
        sim.run_until_idle()
        sim.at(200, sim.enqueue, {"kind": "rerun", "payload": payload(old_head)})
        sim.run_until_idle()
        self.assertNotEqual(sim.repo.latest(sim.repo.head(1))["state"], "success")
        sim.check_safety(self)

    def test_s15_other_pr_event_revokes_cancelled_pr(self):
        sim = Simulator(seed=3, trigger_delay=1, run_duration=5)
        sim.repo.open_pr(1)
        sim.repo.open_pr(2)
        sim.at(0, sim.trigger, 1)
        sim.user(20, "label", 1)
        sim.run_until_idle()
        sim.user(100, "label", 2, "docs")  # running
        sim.user(101, "unlabel", 1)        # pending (X)
        sim.user(101, "label", 2, "wip")   # replaces X's pending run (Y)
        sim.run_until_idle()
        self.assertEqual(sim.cancelled, 1)  # X's pending run was replaced by Y's
        self.assertNotEqual(sim.repo.latest(sim.repo.head(1))["state"], "success")
        sim.check_safety(self)


ACTIONS = ("label", "label", "unlabel", "other_label", "other_unlabel", "push", "force_back", "draft_on", "draft_off",
           "bot_label", "twin", "close_twin")


def random_scenario(seed, drop=0.0, fail_before=0.0, fail_after=0.0, writers=False):
    sim = Simulator(seed=seed, drop_trigger=drop, fail_before=fail_before, fail_after=fail_after)
    rng = random.Random(seed * 7919 + 1)
    sim.repo.open_pr(1)
    sim.at(0, sim.trigger, 1)
    t = 0
    twin_open = False
    for _ in range(rng.randint(3, 12)):
        t += rng.randint(0, 6)
        action = rng.choice(ACTIONS)
        if action == "label":
            sim.user(t, "label", 1)
        elif action == "unlabel":
            sim.user(t, "unlabel", 1)
        elif action == "other_label":
            sim.user(t, "label", 1, "docs")
        elif action == "other_unlabel":
            sim.user(t, "unlabel", 1, "docs")
        elif action == "push":
            sim.user(t, "push", 1)
        elif action == "force_back":
            sim.user(t, "force_back", 1)
        elif action == "draft_on":
            sim.user(t, "draft", 1, True)
        elif action == "draft_off":
            sim.user(t, "draft", 1, False)
        elif action == "bot_label":
            sim.user(t, "label", 1, LABEL, "dependabot[bot]", "Bot")
        elif action == "twin" and not twin_open:
            twin_open = True
            sim.at(t, lambda: (sim.repo.open_pr(2, sim.repo.head(1)), sim.trigger(2)))
        elif action == "close_twin" and twin_open:
            twin_open = False
            sim.at(t, lambda: (sim.repo.close(2), sim.trigger(1)))
        if writers and rng.random() < 0.3:
            kind = rng.choice(("schedule", "workflow_dispatch", "rerun"))
            run = {"kind": kind}
            if kind == "rerun":
                run["payload"] = payload(sim.repo.history[1][0])
            sim.at(t + rng.randint(0, 3), sim.enqueue, run)
    sim.run_until_idle()
    return sim


class PropertyTests(unittest.TestCase):
    def test_s5_random_sequences_safety_and_liveness(self):
        for seed in range(5000):
            sim = random_scenario(seed)
            with self.subTest(seed=seed):
                sim.check_safety(self)
                sim.check_liveness(self)

    def test_s16_all_writer_kinds_with_failures(self):
        for seed in range(5000):
            sim = random_scenario(seed, drop=0.15, fail_before=0.1, fail_after=0.1, writers=True)
            with self.subTest(seed=seed):
                sim.check_serialised(self)
                self.assertEqual(sim.violations, [])
                sim.recover()
                sim.check_safety(self)
                sim.check_liveness(self)


if __name__ == "__main__":
    unittest.main()
