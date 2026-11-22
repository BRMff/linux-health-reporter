#!/usr/bin/env python3
"""Claude Code PreToolUse guard for this repository. Standard library only.

Blocks a tool call (exit code 2, reason on stderr) when it would:

* edit a protected path: ``.github/``, ``ci/``, ``.claude/`` or ``LICENSE``
  (CI, governance and these guardrails change only with a separate owner
  authorisation, by hand);
* leave a module in ``linux_health_reporter/`` that imports outside the
  standard-library allowlist, or that calls process, dynamic-code or
  file-writing APIs (the package promises no shell, network or writes);
* run a Bash command that names a protected path together with a writing
  command (best effort: shell text cannot be parsed reliably).

Any internal error also blocks (fail closed). This is a guardrail against
mistakes, not a security boundary: the ``main`` ruleset and the owner review
gate remain the controls. ``tests/test_runtime_imports.py`` enforces the same
package contract in CI.
"""

from __future__ import annotations

import ast
import json
import os
import re
import sys

PROTECTED_DIRS = (".github/", "ci/", ".claude/")
PROTECTED_FILES = ("LICENSE",)
PACKAGE_DIR = "linux_health_reporter/"
FILE_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")

# Must equal ALLOWED_IMPORTS in tests/test_runtime_imports.py (checked by tests/test_claude_hooks.py).
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
WRITE_MODE_CHARS = frozenset("wax+")


def _allowed(module: str) -> bool:
    """An allowlisted module, or a submodule of one (os.path)."""
    return module in ALLOWED_IMPORTS or module.split(".", 1)[0] in ALLOWED_IMPORTS


def package_violations(source: str) -> list[str]:
    """Return the contract violations of one package module ([] if none)."""
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [f"not valid Python ({exc.msg}, line {exc.lineno}); keep each edit syntactically valid"]
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if not _allowed(alias.name):
                    found.append(f"line {node.lineno}: import {alias.name} is not in the allowlist")
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and not _allowed(node.module or ""):
                found.append(f"line {node.lineno}: from {node.module} import is not in the allowlist")
        elif isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
            found.append(f"line {node.lineno}: {node.id} is forbidden")
        elif isinstance(node, ast.Attribute):
            if isinstance(node.value, ast.Name) and node.value.id == "os" and node.attr in FORBIDDEN_OS_ATTRS:
                found.append(f"line {node.lineno}: os.{node.attr} is forbidden")
            elif node.attr in FORBIDDEN_METHODS:
                found.append(f"line {node.lineno}: .{node.attr}() is forbidden (no writes)")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "open":
            mode = node.args[1] if len(node.args) > 1 else next(
                (k.value for k in node.keywords if k.arg == "mode"), None)
            if mode is not None and not (isinstance(mode, ast.Constant) and isinstance(mode.value, str)):
                found.append(f"line {node.lineno}: open() with a non-literal mode")
            elif mode is not None and WRITE_MODE_CHARS & set(mode.value):
                found.append(f"line {node.lineno}: open() in write mode {mode.value!r}")
    return found


def _relative(path: str, project_dir: str) -> str | None:
    """Project-relative path with '/' separators, or None when outside the project."""
    root = os.path.realpath(project_dir)
    full = os.path.realpath(path if os.path.isabs(path) else os.path.join(root, path))
    rel = os.path.relpath(full, root)
    if rel == os.curdir or rel == os.pardir or rel.startswith(os.pardir + os.sep):
        return None
    return rel.replace(os.sep, "/")


def _is_protected(rel: str) -> bool:
    return rel in PROTECTED_FILES or any(rel.startswith(d) or rel == d.rstrip("/") for d in PROTECTED_DIRS)


def _resulting_content(tool: str, tool_input: dict, path: str) -> str:
    if tool == "Write":
        content = tool_input.get("content")
        if not isinstance(content, str):
            raise ValueError("Write without string content")
        return content
    try:
        with open(path, encoding="utf-8") as handle:
            current = handle.read()
    except FileNotFoundError:
        current = ""
    edits = tool_input.get("edits") if tool == "MultiEdit" else [tool_input]
    if not isinstance(edits, list):
        raise ValueError("MultiEdit without an edits list")
    for edit in edits:
        old, new = edit.get("old_string"), edit.get("new_string")
        if not isinstance(old, str) or not isinstance(new, str):
            raise ValueError("edit without old_string/new_string")
        if old == "":
            current = new if current == "" else current
        elif edit.get("replace_all"):
            current = current.replace(old, new)
        else:
            current = current.replace(old, new, 1)
    return current


_DEVNULL = re.compile(r"\d?>&\d|\d?>\s*/dev/null")
_PROTECTED_IN_SHELL = re.compile(r"(?:^|[\s'\"=:(/])(?:\./)?(?:\.github/|ci/|\.claude/|\.github\b|\.claude\b|LICENSE\b)")
_WRITING_SHELL = re.compile(
    r">|\btee\b|\bsed\b[^|;&]*\s-i|\bperl\b[^|;&]*\s-i|\b(?:mv|cp|rm|rmdir|mkdir|touch|truncate|install|ln|"
    r"chmod|chown|dd|patch|unzip|tar|rsync|shred)\b|\bgit\s+(?:checkout|restore|apply|rm|mv|reset|stash|clean|am)\b|"
    r"\bopen\(|write_text|write_bytes"
)


def decide(payload: dict, project_dir: str) -> tuple[int, str]:
    """Return (exit_code, reason): 0 lets the call proceed, 2 blocks it."""
    tool = payload.get("tool_name")
    tool_input = payload.get("tool_input")
    if not isinstance(tool, str) or not isinstance(tool_input, dict):
        return 2, "guard: hook input without tool_name/tool_input (blocking, fail closed)"
    if tool == "Bash":
        command = tool_input.get("command")
        if not isinstance(command, str):
            return 2, "guard: Bash call without a command string"
        text = _DEVNULL.sub(" ", command)
        if _PROTECTED_IN_SHELL.search(text) and _WRITING_SHELL.search(text):
            return 2, ("guard: this Bash command names a protected path (.github/, ci/, .claude/, LICENSE) "
                       "together with a writing command. Changes there need a separate owner authorisation; "
                       "use Read/Grep to inspect them.")
        return 0, ""
    if tool not in FILE_TOOLS:
        return 0, ""
    path = tool_input.get("file_path") or tool_input.get("notebook_path")
    if not isinstance(path, str) or not path:
        return 2, f"guard: {tool} call without a file path"
    rel = _relative(path, project_dir)
    if rel is None:
        return 0, ""
    if _is_protected(rel):
        return 2, (f"guard: {rel} is protected (.github/, ci/, .claude/, LICENSE). CI, governance and these "
                   "guardrails change only with a separate owner authorisation, edited by hand.")
    if rel.startswith(PACKAGE_DIR) and rel.endswith(".py") and tool != "NotebookEdit":
        problems = package_violations(_resulting_content(tool, tool_input, os.path.join(project_dir, rel)))
        if problems:
            return 2, ("guard: the package must stay standard-library only, with no shell, network, dynamic "
                       f"code or writes. In {rel}: " + "; ".join(problems))
    return 0, ""


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read())
        if not isinstance(payload, dict):
            raise ValueError("hook input is not a JSON object")
        project_dir = os.environ.get("CLAUDE_PROJECT_DIR") or payload.get("cwd")
        if not isinstance(project_dir, str) or not project_dir:
            code, reason = 2, "guard: no project directory (blocking, fail closed)"
        else:
            code, reason = decide(payload, project_dir)
    except Exception as exc:  # fail closed on anything unexpected
        code, reason = 2, f"guard: internal error {type(exc).__name__} (blocking, fail closed)"
    if reason:
        print(reason, file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
