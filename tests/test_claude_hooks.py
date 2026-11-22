"""Tests for the local Claude Code guard (.claude/hooks/guard.py).

Synthetic hook payloads only; no Claude Code installation is needed. Skipped
when .claude/ is absent (for example in an sdist, which does not ship it).
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from tests import test_runtime_imports as contract

ROOT = Path(__file__).resolve().parent.parent
GUARD_PATH = ROOT / ".claude" / "hooks" / "guard.py"
SETTINGS_PATH = ROOT / ".claude" / "settings.json"


def _load_guard():
    spec = importlib.util.spec_from_file_location("lhr_claude_guard", GUARD_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@unittest.skipUnless(GUARD_PATH.is_file(), ".claude/ is not shipped in the sdist")
class ClaudeGuardTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.guard = _load_guard()

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.project = Path(self._tmp.name) / "project"
        (self.project / "linux_health_reporter").mkdir(parents=True)
        (self.project / "tests").mkdir()
        (self.project / "linux_health_reporter" / "units.py").write_text(
            "from __future__ import annotations\nimport math\n\nVALUE = 1\n", encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def decide(self, tool, **tool_input):
        return self.guard.decide({"tool_name": tool, "tool_input": tool_input}, str(self.project))

    def assertBlocked(self, result, fragment=""):
        code, reason = result
        self.assertEqual(2, code, reason)
        self.assertIn(fragment, reason)

    def assertAllowed(self, result):
        self.assertEqual((0, ""), result)

    # Protected paths -----------------------------------------------------------------------
    def test_blocks_every_protected_path_for_every_file_tool(self):
        paths = (".github/workflows/tests.yml", ".github/dependabot.yml", "ci/review_gate.py",
                 "ci/requirements-build.txt", ".claude/settings.json", ".claude/hooks/guard.py",
                 ".claude/agents/privacy-adversarial-reviewer.md", ".claude/settings.local.json", "LICENSE")
        for rel in paths:
            for tool in ("Write", "Edit", "MultiEdit"):
                with self.subTest(path=rel, tool=tool):
                    self.assertBlocked(self.decide(tool, file_path=str(self.project / rel), content="x",
                                                   old_string="a", new_string="b", edits=[]), "protected")
            with self.subTest(path=rel, tool="NotebookEdit"):
                self.assertBlocked(self.decide("NotebookEdit", notebook_path=str(self.project / rel),
                                               new_source="x"), "protected")

    def test_blocks_protected_paths_reached_by_dot_dot_relative_or_symlink(self):
        (self.project / "ci").mkdir()
        (self.project / "docs").mkdir()
        os.symlink(self.project / "ci", self.project / "docs" / "alias")
        for path in (str(self.project / "docs" / ".." / "ci" / "x.py"), "ci/x.py", "./LICENSE",
                     str(self.project / "docs" / "alias" / "x.py")):
            with self.subTest(path=path):
                self.assertBlocked(self.decide("Write", file_path=path, content="x"), "protected")

    def test_allows_ordinary_paths_and_files_outside_the_project(self):
        for path in (str(self.project / "README.md"), str(self.project / "docs" / "governance.md"),
                     str(self.project / "tests" / "test_new.py"), str(self.project / "circle.md"),
                     "/tmp/elsewhere/.github/x.yml"):
            with self.subTest(path=path):
                self.assertAllowed(self.decide("Write", file_path=path, content="import subprocess\n"))

    # Package contract -----------------------------------------------------------------------
    def test_write_rejects_every_negative_control_in_the_package(self):
        target = str(self.project / "linux_health_reporter" / "new.py")
        for snippet in contract.BAD_SNIPPETS:
            with self.subTest(snippet=snippet):
                self.assertBlocked(self.decide("Write", file_path=target, content=snippet), "linux_health_reporter/new.py")

    def test_write_accepts_every_positive_control_in_the_package(self):
        target = str(self.project / "linux_health_reporter" / "new.py")
        for snippet in contract.GOOD_SNIPPETS:
            with self.subTest(snippet=snippet):
                self.assertAllowed(self.decide("Write", file_path=target, content=snippet))

    def test_edit_is_checked_on_the_resulting_file(self):
        target = str(self.project / "linux_health_reporter" / "units.py")
        self.assertBlocked(self.decide("Edit", file_path=target, old_string="import math",
                                       new_string="import math\nimport socket"), "import socket")
        self.assertAllowed(self.decide("Edit", file_path=target, old_string="VALUE = 1", new_string="VALUE = 2"))

    def test_edit_judges_the_whole_resulting_file_not_the_fragment(self):
        package = self.project / "linux_health_reporter"
        (package / "nested.py").write_text("def f():\n    return 1\n", encoding="utf-8")
        self.assertAllowed(self.decide("Edit", file_path=str(package / "nested.py"),
                                       old_string="    return 1", new_string="    return 2"))
        (package / "legacy.py").write_text("import socket\nVALUE = 1\n", encoding="utf-8")
        self.assertBlocked(self.decide("Edit", file_path=str(package / "legacy.py"),
                                       old_string="VALUE = 1", new_string="VALUE = 2"), "import socket")

    def test_edit_replace_all_and_multiedit_are_applied_in_order(self):
        target = str(self.project / "linux_health_reporter" / "units.py")
        self.assertBlocked(self.decide("MultiEdit", file_path=target, edits=[
            {"old_string": "VALUE = 1", "new_string": "VALUE = 2"},
            {"old_string": "VALUE = 2", "new_string": "import os\nVALUE = os.popen('id')"},
        ]), "os.popen")
        self.assertAllowed(self.decide("MultiEdit", file_path=target, edits=[
            {"old_string": "VALUE = 1", "new_string": "VALUE = 2"}]))
        self.assertBlocked(self.decide("Edit", file_path=target, old_string="math", new_string="shutil",
                                       replace_all=True), "import shutil")

    def test_package_files_are_not_checked_outside_python(self):
        self.assertAllowed(self.decide("Write", file_path=str(self.project / "linux_health_reporter" / "notes.txt"),
                                       content="import subprocess"))

    def test_tests_and_ci_style_code_outside_the_package_may_use_subprocess(self):
        self.assertAllowed(self.decide("Write", file_path=str(self.project / "tests" / "test_x.py"),
                                       content="import subprocess\n"))

    # Bash (best effort) -------------------------------------------------------------------
    def test_bash_blocks_writing_commands_on_protected_paths(self):
        for command in ("sed -i 's/a/b/' .github/workflows/tests.yml", "echo x > ci/review_gate.py",
                        "rm -rf .claude", "git checkout -- LICENSE", "cp /tmp/x .github/dependabot.yml",
                        "cat x | tee .claude/settings.json", "mv ci/release_gate.py /tmp/",
                        'python3 -c "open(\'ci/x\', \'w\')"', "printf x >> $CLAUDE_PROJECT_DIR/.claude/settings.json"):
            with self.subTest(command=command):
                self.assertBlocked(self.decide("Bash", command=command), "protected path")

    def test_bash_allows_reading_protected_paths_and_other_writes(self):
        for command in ("cat .github/workflows/tests.yml", "grep -rn uses: .github/workflows 2>/dev/null",
                        "git diff -- ci/", "ls -la .claude 2>&1 | head", "echo ok > /tmp/out.txt",
                        "PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest discover -s tests -v"):
            with self.subTest(command=command):
                self.assertAllowed(self.decide("Bash", command=command))

    # Fail closed ---------------------------------------------------------------------------
    def test_malformed_payloads_are_blocked(self):
        for payload in ({}, {"tool_name": "Write"}, {"tool_name": "Write", "tool_input": {}},
                        {"tool_name": "Bash", "tool_input": {"command": 3}},
                        {"tool_name": "Write", "tool_input": {"file_path": str(self.project / "linux_health_reporter" / "a.py")}}):
            with self.subTest(payload=payload):
                try:
                    code, _ = self.guard.decide(payload, str(self.project))
                except ValueError:
                    code = 2  # main() turns exceptions into exit code 2
                self.assertEqual(2, code)

    def test_other_tools_pass_through(self):
        self.assertAllowed(self.decide("Read", file_path=str(self.project / "LICENSE")))
        self.assertAllowed(self.decide("Grep", pattern="x"))

    def test_script_exit_codes_end_to_end(self):
        def run(stdin):
            env = dict(os.environ, CLAUDE_PROJECT_DIR=str(self.project), PYTHONDONTWRITEBYTECODE="1")
            return subprocess.run([sys.executable, "-B", str(GUARD_PATH)], input=stdin, capture_output=True,
                                  text=True, env=env, timeout=30)
        ok = run(json.dumps({"tool_name": "Write", "tool_input": {
            "file_path": str(self.project / "README.md"), "content": "x"}}))
        self.assertEqual((0, ""), (ok.returncode, ok.stderr))
        blocked = run(json.dumps({"tool_name": "Edit", "tool_input": {
            "file_path": str(self.project / ".github" / "workflows" / "tests.yml"), "old_string": "a", "new_string": "b"}}))
        self.assertEqual(2, blocked.returncode)
        self.assertIn("protected", blocked.stderr)
        for bad in ("not json", "[]", json.dumps({"tool_name": "Write", "tool_input": {
                "file_path": str(self.project / "linux_health_reporter" / "a.py")}})):
            with self.subTest(stdin=bad):
                result = run(bad)
                self.assertEqual(2, result.returncode)
                self.assertIn("fail closed", result.stderr)

    # Consistency with the CI contract ----------------------------------------------------------
    def test_rules_match_tests_test_runtime_imports(self):
        for name in ("ALLOWED_IMPORTS", "FORBIDDEN_NAMES", "FORBIDDEN_OS_ATTRS", "FORBIDDEN_METHODS"):
            with self.subTest(rule=name):
                self.assertEqual(getattr(contract, name), getattr(self.guard, name))

    def test_settings_wire_the_guard_for_file_tools_and_bash(self):
        settings = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        entries = settings["hooks"]["PreToolUse"]
        matchers = {tool for entry in entries for tool in entry["matcher"].split("|")}
        self.assertTrue({"Edit", "Write", "MultiEdit", "NotebookEdit", "Bash"} <= matchers)
        commands = [hook["command"] for entry in entries for hook in entry["hooks"]]
        self.assertTrue(all(".claude/hooks/guard.py" in c and "$CLAUDE_PROJECT_DIR" in c for c in commands))


if __name__ == "__main__":
    unittest.main()
