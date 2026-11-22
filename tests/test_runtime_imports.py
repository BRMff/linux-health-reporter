"""Runtime package contract, checked statically with ``ast``.

The package promises no shell, network, host-identifier collection or writes, and
no runtime dependencies. These tests keep that promise enforceable in CI:

* imports must come from a standard-library allowlist (adding a module is a
  conscious change to ALLOWED_IMPORTS, reviewed like any other);
* process, dynamic-code and file-writing APIs are rejected.

The local Claude Code guard (.claude/hooks/guard.py) applies the same rules
before an edit; tests/test_claude_hooks.py checks that both stay in step.
"""

from __future__ import annotations

import ast
from pathlib import Path
import unittest

PACKAGE = Path(__file__).resolve().parent.parent / "linux_health_reporter"

ALLOWED_IMPORTS = frozenset({
    "__future__", "argparse", "collections", "collections.abc", "datetime", "errno",
    "json", "math", "os", "pathlib", "re", "typing",
})
FORBIDDEN_NAMES = frozenset({"__import__", "eval", "exec", "compile", "breakpoint"})
FORBIDDEN_OS_ATTRS = frozenset({
    "system", "popen", "fork", "forkpty", "posix_spawn", "posix_spawnp", "startfile",
    "execl", "execle", "execlp", "execlpe", "execv", "execve", "execvp", "execvpe",
    "spawnl", "spawnle", "spawnlp", "spawnlpe", "spawnv", "spawnve", "spawnvp", "spawnvpe",
    "open", "write", "remove", "unlink", "rename", "renames", "replace", "rmdir", "removedirs",
    "mkdir", "makedirs", "truncate", "ftruncate", "chmod", "chown", "symlink", "link", "mkfifo",
    "kill", "killpg",
})
FORBIDDEN_METHODS = frozenset({
    "write_text", "write_bytes", "touch", "unlink", "rmdir", "mkdir", "symlink_to", "hardlink_to",
})

# Negative controls: each snippet must be rejected. Shared with tests/test_claude_hooks.py.
BAD_SNIPPETS = (
    "import subprocess",
    "import socket",
    "from socket import create_connection",
    "import urllib.request",
    "from urllib import request",
    "import http.client",
    "import ssl",
    "import shutil",
    "import importlib",
    "import ctypes",
    "import os\nos.system('true')",
    "import os\nos.popen('true')",
    "import os\nos.execv('/bin/true', ['true'])",
    "import os\nos.remove('x')",
    "__import__('socket')",
    "eval('1')",
    "open('report.txt', 'w')",
    "open('report.txt', mode='a')",
    "open('report.txt', 'r+')",
    "open('report.txt', mode)",
    "from pathlib import Path\nPath('x').write_text('y')",
    "def broken(:\n    pass",
)
# Positive controls: each snippet must be accepted.
GOOD_SNIPPETS = (
    "from __future__ import annotations",
    "import os\nos.statvfs('/')",
    "import os.path",
    "from collections.abc import Sequence",
    "from . import __version__",
    "from .collectors import collect_snapshot",
    "from pathlib import Path\nPath('/proc/uptime').read_text(encoding='ascii')",
    "open('/proc/loadavg')",
    "open('/proc/loadavg', 'r', encoding='ascii')",
    "text = 'a b'.replace(' ', '_')",
)


def _allowed(module: str) -> bool:
    """An allowlisted module, or a submodule of one (os.path)."""
    return module in ALLOWED_IMPORTS or module.split(".", 1)[0] in ALLOWED_IMPORTS


def violations(source: str) -> list[str]:
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [f"syntax error: {exc.msg}"]
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [f"import {a.name}" for a in node.names if not _allowed(a.name)]
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and not _allowed(node.module or ""):
                found.append(f"from {node.module} import")
        elif isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
            found.append(node.id)
        elif isinstance(node, ast.Attribute):
            if isinstance(node.value, ast.Name) and node.value.id == "os" and node.attr in FORBIDDEN_OS_ATTRS:
                found.append(f"os.{node.attr}")
            elif node.attr in FORBIDDEN_METHODS:
                found.append(f".{node.attr}()")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "open":
            mode = node.args[1] if len(node.args) > 1 else next(
                (k.value for k in node.keywords if k.arg == "mode"), None)
            if mode is not None and not (isinstance(mode, ast.Constant) and isinstance(mode.value, str)):
                found.append("open() with a non-literal mode")
            elif mode is not None and set("wax+") & set(mode.value):
                found.append(f"open() mode {mode.value!r}")
    return found


class RuntimeContractTest(unittest.TestCase):
    def test_every_package_module_respects_the_contract(self):
        modules = sorted(PACKAGE.glob("*.py"))
        self.assertGreaterEqual(len(modules), 5)
        for module in modules:
            with self.subTest(module=module.name):
                self.assertEqual([], violations(module.read_text(encoding="utf-8")))

    def test_package_has_no_subpackages_outside_the_scan(self):
        nested = [p for p in PACKAGE.rglob("*.py") if p.parent != PACKAGE]
        self.assertEqual([], nested, "extend the scan before adding subpackages")

    def test_rejects_every_negative_control(self):
        for snippet in BAD_SNIPPETS:
            with self.subTest(snippet=snippet):
                self.assertTrue(violations(snippet))

    def test_accepts_every_positive_control(self):
        for snippet in GOOD_SNIPPETS:
            with self.subTest(snippet=snippet):
                self.assertEqual([], violations(snippet))


if __name__ == "__main__":
    unittest.main()
