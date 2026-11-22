"""Runtime package contract, checked statically with ``ast``.

The package promises no shell, network, host-identifier collection or writes, and
no runtime dependencies. These tests keep that promise enforceable in CI:

* imports must come from a standard-library allowlist (adding a module is a
  conscious change to ALLOWED_IMPORTS, reviewed like any other);
* process, dynamic-code, host-identifier and file-writing APIs are rejected,
  including through aliases, ``from os import ...``, getattr() and Path.open().

This is a static check, not a proof of behaviour; the privacy contract itself
is tested by tests/test_privacy.py and the collector tests. The local Claude
Code guard (.claude/hooks/guard.py) carries the identical checker text;
tests/test_claude_hooks.py fails if the two copies differ.
"""

from __future__ import annotations

import ast
from pathlib import Path
import unittest

PACKAGE = Path(__file__).resolve().parent.parent / "linux_health_reporter"

# --- runtime contract checker: identical text in .claude/hooks/guard.py and tests/test_runtime_imports.py ---
# Static (ast) check of one package module. It rejects imports outside the standard-library allowlist and
# the process, dynamic-code, host-identifier and file-writing APIs below, including through `import os as x`,
# `from os import ...`, getattr() and Path.open(). It is not a proof: code can still misbehave in ways a
# syntax check cannot see; the privacy contract itself is tested by tests/test_privacy.py and the
# collector tests.
ALLOWED_IMPORTS = frozenset({
    "__future__", "argparse", "collections", "collections.abc", "datetime", "errno",
    "json", "math", "os", "pathlib", "re", "typing",
})
FORBIDDEN_NAMES = frozenset({
    "__import__", "eval", "exec", "compile", "breakpoint", "globals", "__builtins__",
})
FORBIDDEN_OS_ATTRS = frozenset({
    "system", "popen", "fork", "forkpty", "posix_spawn", "posix_spawnp", "startfile",
    "execl", "execle", "execlp", "execlpe", "execv", "execve", "execvp", "execvpe",
    "spawnl", "spawnle", "spawnlp", "spawnlpe", "spawnv", "spawnve", "spawnvp", "spawnvpe",
    "open", "fdopen", "write", "remove", "unlink", "rename", "renames", "replace", "rmdir",
    "removedirs", "mkdir", "makedirs", "truncate", "ftruncate", "chmod", "chown", "symlink",
    "link", "mkfifo", "kill", "killpg",
    "uname", "getlogin", "environ", "environb", "getenv", "putenv", "unsetenv",
})
FORBIDDEN_METHODS = frozenset({
    "write_text", "write_bytes", "touch", "unlink", "rmdir", "mkdir", "symlink_to",
    "hardlink_to", "chmod",
})
FORBIDDEN_DUNDERS = frozenset({"__dict__", "__globals__", "__builtins__", "__subclasses__"})


def _allowed_module(module: str) -> bool:
    """An allowlisted module, or a submodule of one (os.path)."""
    return module in ALLOWED_IMPORTS or module.split(".", 1)[0] in ALLOWED_IMPORTS


def _mode_problem(mode) -> str | None:
    if mode is None:
        return None
    if not (isinstance(mode, ast.Constant) and isinstance(mode.value, str)):
        return "open() with a non-literal mode"
    if set("wax+") & set(mode.value):
        return f"open() in write mode {mode.value!r}"
    return None


def _keyword(call: ast.Call, name: str):
    return next((k.value for k in call.keywords if k.arg == name), None)


def contract_violations(source: str) -> list[str]:
    """Return the runtime-contract violations of one package module ([] if none)."""
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [f"line {exc.lineno}: not valid Python ({exc.msg})"]
    os_names = {"os"}
    parents = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
        if isinstance(node, ast.Import):
            os_names.update(a.asname for a in node.names if a.name == "os" and a.asname)
    found: list[str] = []
    for node in ast.walk(tree):
        line = f"line {getattr(node, 'lineno', '?')}"
        parent = parents.get(node)
        if isinstance(node, ast.Import):
            found += [f"{line}: import {a.name} is not in the allowlist" for a in node.names
                      if not _allowed_module(a.name)]
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            module = node.module or ""
            if not _allowed_module(module):
                found.append(f"{line}: from {module} import is not in the allowlist")
            elif module.split(".", 1)[0] == "os":
                found += [f"{line}: from {module} import {a.name} is forbidden" for a in node.names
                          if a.name == "*" or a.name in FORBIDDEN_OS_ATTRS]
        elif isinstance(node, ast.Name):
            if node.id in FORBIDDEN_NAMES:
                found.append(f"{line}: {node.id} is forbidden")
            elif node.id in os_names and not (isinstance(parent, ast.Attribute) and parent.value is node):
                found.append(f"{line}: the os module may only be used as os.<attribute>")
            elif node.id == "open" and not (isinstance(parent, ast.Call) and parent.func is node):
                found.append(f"{line}: open may only be called directly")
        elif isinstance(node, ast.Attribute):
            base = node.value
            on_os = (isinstance(base, ast.Name) and base.id in os_names) or (
                isinstance(base, ast.Attribute) and base.attr == "os")
            if on_os and node.attr in FORBIDDEN_OS_ATTRS:
                found.append(f"{line}: os.{node.attr} is forbidden")
            elif node.attr in FORBIDDEN_METHODS:
                found.append(f"{line}: .{node.attr}() is forbidden (no writes)")
            elif node.attr in FORBIDDEN_DUNDERS:
                found.append(f"{line}: .{node.attr} is forbidden")
        elif isinstance(node, ast.Call):
            func = node.func
            problem = None
            if isinstance(func, ast.Name) and func.id == "open":
                problem = _mode_problem(node.args[1] if len(node.args) > 1 else _keyword(node, "mode"))
            elif isinstance(func, ast.Attribute) and func.attr == "open":
                problem = _mode_problem(node.args[0] if node.args else _keyword(node, "mode"))
            elif isinstance(func, ast.Name) and func.id in ("getattr", "setattr", "delattr", "hasattr"):
                name = node.args[1] if len(node.args) > 1 else None
                if not (isinstance(name, ast.Constant) and isinstance(name.value, str)):
                    problem = f"{func.id}() with a non-literal attribute name"
                elif name.value in FORBIDDEN_OS_ATTRS | FORBIDDEN_METHODS | FORBIDDEN_DUNDERS:
                    problem = f"{func.id}(..., {name.value!r}) is forbidden"
            if problem:
                found.append(f"{line}: {problem}")
    return found
# --- end of runtime contract checker ---


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
    # C1 (CC-AB-R1-F2): aliases, direct imports, Path.open, fdopen, dynamic access, identifiers
    "import os as runner\nrunner.system('true')",
    "from os import system",
    "from os import popen",
    "from os import fork",
    "from os import system as run",
    "from os import *",
    "from os import fdopen",
    "import os\nos.fdopen(3, 'w')",
    "from pathlib import Path\nPath('x').open('w')",
    "from pathlib import Path\nPath('x').open(mode='a')",
    "from pathlib import Path\nPath('x').open(mode)",
    "import os\nrun = os",
    "writer = open\nwriter('x', 'w')",
    "import os\ngetattr(os, 'system')('true')",
    "getattr(object, name)",
    "import pathlib\npathlib.os.system('true')",
    "import os\nos.uname()",
    "import os\nos.environ['HOME']",
    "from os import environ",
    "import os\nos.getlogin()",
    "globals()['__builtins__']",
    "().__class__.__subclasses__()",
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
    "from pathlib import Path\nPath('/proc/uptime').open()",
    "from pathlib import Path\nPath('/proc/uptime').open('r', encoding='ascii')",
    "import errno\ngetattr(errno, 'ENOTSUP', errno.EOPNOTSUPP)",
    "from os import statvfs",
    "import os.path\nos.path.join('a', 'b')",
    "import os as _os\n_os.statvfs('/')",
    "import os\nresult: os.statvfs_result",
)


violations = contract_violations


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
