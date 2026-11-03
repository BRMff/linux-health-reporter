"""Static checks of the governance workflows (WF*). Text analysis only:
the standard library has no YAML parser, so the workflows use a plain,
predictable layout that these checks rely on."""

import re
import unittest
from pathlib import Path

from tests.governance_support import release_gate as rel

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"
NEW = ("review-gate-trigger.yml", "review-gate.yml", "release-gate.yml", "release-audit.yml")


def text(name):
    return (WORKFLOWS / name).read_text(encoding="utf-8")


def all_workflows():
    return sorted(WORKFLOWS.glob("*.yml"))


def jobs(source):
    """{job_id: [lines]} for the top-level 'jobs:' mapping."""
    lines = source.splitlines()
    start = lines.index("jobs:")
    result, current = {}, None
    for line in lines[start + 1:]:
        match = re.fullmatch(r"  ([A-Za-z0-9_-]+):", line)
        if match:
            current = match.group(1)
            result[current] = []
        elif current is not None:
            result[current].append(line)
    return result


def job_permissions(job_lines):
    perms, inside = {}, False
    for line in job_lines:
        if line == "    permissions:":
            inside = True
            continue
        if inside:
            match = re.fullmatch(r"      ([a-z-]+): ([a-z]+)", line)
            if not match:
                break
            perms[match.group(1)] = match.group(2)
    return perms


def run_blocks(source):
    lines, blocks = source.splitlines(), []
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("run:") or stripped.startswith("- run:"):
            indent = len(line) - len(line.lstrip())
            block = [stripped]
            for follow in lines[i + 1:]:
                if follow.strip() and len(follow) - len(follow.lstrip()) <= indent:
                    break
                block.append(follow)
            blocks.append("\n".join(block))
    return blocks


class WorkflowTests(unittest.TestCase):
    def test_wf1_minimal_permissions(self):
        for name in NEW:
            with self.subTest(name=name):
                self.assertIn("\npermissions: {}\n", text(name))
        reconcile = jobs(text("review-gate.yml"))["reconcile"]
        self.assertEqual(job_permissions(reconcile), {
            "contents": "read", "pull-requests": "read", "issues": "read", "actions": "read", "statuses": "write",
        })
        self.assertEqual(job_permissions(jobs(text("review-gate-trigger.yml"))["trigger"]), {})

    def test_wf2_no_pull_request_target_anywhere(self):
        for path in all_workflows():
            with self.subTest(path=path.name):
                self.assertNotIn("pull_request_target", path.read_text(encoding="utf-8"))

    def test_wf3_trigger_has_no_code_and_reconciler_checks_out_main(self):
        trigger = text("review-gate-trigger.yml")
        self.assertNotRegex(trigger, r"(?m)^\s*(- )?uses:")
        code = "\n".join(line for line in trigger.splitlines() if not line.lstrip().startswith("#"))
        self.assertNotIn("checkout", code)
        self.assertEqual([b for b in run_blocks(trigger)], ['run: "true"'])
        for name in ("review-gate.yml", "release-audit.yml"):
            with self.subTest(name=name):
                source = text(name)
                self.assertIn("          ref: main\n", source)
                self.assertIn("          persist-credentials: false\n", source)
                self.assertIn("          sparse-checkout: ci\n", source)
        for source in (text("review-gate.yml"), text("release-audit.yml"), text("release-gate.yml")):
            self.assertNotIn("github.event.pull_request.head", source)
            self.assertNotIn("refs/pull/", source)

    def test_wf4_no_expressions_inside_run_blocks(self):
        for path in all_workflows():
            for block in run_blocks(path.read_text(encoding="utf-8")):
                with self.subTest(path=path.name, block=block[:40]):
                    self.assertNotIn("${{", block)

    def test_wf5_actions_pinned_by_full_sha(self):
        for path in all_workflows():
            for line in path.read_text(encoding="utf-8").splitlines():
                if re.match(r"\s*(- )?uses:", line):
                    with self.subTest(path=path.name, line=line.strip()):
                        self.assertRegex(line, r"uses: [\w.-]+/[\w.-]+@[0-9a-f]{40}( #.*)?$")

    def test_wf6_no_conditions_in_gate_workflows(self):
        for name in NEW:
            with self.subTest(name=name):
                self.assertNotRegex(text(name), r"(?m)^\s*if:")

    def test_wf7_single_global_concurrency_group(self):
        source = text("review-gate.yml")
        self.assertIn("\nconcurrency:\n  group: review-gate\n  cancel-in-progress: false\n", source)
        self.assertEqual(source.count("concurrency:"), 1)

    def test_wf8_release_permissions(self):
        gate = jobs(text("release-gate.yml"))
        self.assertTrue(all(v == "read" for v in job_permissions(gate["verify"]).values()))
        self.assertEqual(job_permissions(gate["record"]), {"contents": "read", "statuses": "write"})
        self.assertIn("    needs: verify", gate["record"])
        audit = jobs(text("release-audit.yml"))["audit"]
        self.assertTrue(job_permissions(audit))
        self.assertTrue(all(v == "read" for v in job_permissions(audit).values()))

    def test_wf9_trigger_event_types_exact(self):
        source = text("review-gate-trigger.yml")
        match = re.search(r"(?m)^    types: \[(.*)\]$", source)
        types = [t.strip() for t in match.group(1).split(",")]
        self.assertEqual(types, ["opened", "reopened", "synchronize", "edited", "labeled", "unlabeled",
                                 "converted_to_draft", "ready_for_review"])
        self.assertNotIn("branches", source)
        self.assertNotIn("concurrency", source)

    def test_wf10_triggers_and_run_name(self):
        gate = text("release-gate.yml")
        run_name = next(line for line in gate.splitlines() if line.startswith("run-name:"))
        for part in ("release-preflight {0} {1}", "inputs.tag", "inputs.sha"):
            self.assertIn(part, run_name)
        reconciler = text("review-gate.yml")
        for part in ("  workflow_run:\n", "    types: [completed]\n", "  schedule:\n", "  workflow_dispatch: {}\n"):
            self.assertIn(part, reconciler)

    def test_wf11_producer_name_matches_and_is_unique(self):
        producer = re.search(r"(?m)^name: (.+)$", text("review-gate-trigger.yml")).group(1)
        listed = re.search(r'(?m)^    workflows: \["([^"]+)"\]$', text("review-gate.yml")).group(1)
        self.assertEqual(producer, listed)
        self.assertEqual(producer, "review-gate-trigger")
        names = [re.search(r"(?m)^name: (.+)$", p.read_text(encoding="utf-8")).group(1) for p in all_workflows()]
        self.assertEqual(names.count(producer), 1)

    def test_wf12_only_gate_workflows_can_write_statuses(self):
        for path in all_workflows():
            source = path.read_text(encoding="utf-8")
            if path.name not in ("review-gate.yml", "release-gate.yml"):
                with self.subTest(path=path.name):
                    self.assertNotIn("statuses: write", source)
        self.assertEqual(text("release-gate.yml").count("statuses: write"), 1)

    def test_wf17_no_job_named_like_the_required_status(self):
        for path in all_workflows():
            for job_id, lines in jobs(path.read_text(encoding="utf-8")).items():
                with self.subTest(path=path.name, job=job_id):
                    self.assertNotEqual(job_id, "review-gate")
                    names = [line.split("name:", 1)[1].strip() for line in lines if line.startswith("    name:")]
                    self.assertNotIn("review-gate", names)

    def test_release_gate_checks_match_ci_matrix(self):
        tests = text("tests.yml")
        versions = re.search(r'python-version: \[([^\]]+)\]', tests).group(1)
        expected = [f"Python {v.strip().strip(chr(34))}" for v in versions.split(",")]
        expected.append(re.search(r"(?m)^    name: (Build.*)$", tests).group(1))
        self.assertEqual(list(rel.MAIN_REQUIRED_CHECKS), expected)


if __name__ == "__main__":
    unittest.main()
