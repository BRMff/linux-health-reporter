#!/usr/bin/env python3
"""Claude Code PreToolUse guard for this repository. Standard library only.

Blocks a tool call (exit code 2, reason on stderr) when it would:

* edit a protected path: ``.github/``, ``ci/``, ``.claude/`` or ``LICENSE``
  (CI, governance and these guardrails change only with a separate owner
  authorisation, by hand);
* leave a module in ``linux_health_reporter/`` that fails the runtime
  contract checker below (standard-library allowlist; no process,
  dynamic-code, host-identifier or file-writing APIs);
* run a Bash command that names a protected path together with a writing
  command (best effort: shell text cannot be parsed reliably).

Paths are checked both as written and with symlinks resolved. Any error inside
this script exits 2 (fail closed); a hook that cannot start or times out is
blocked by ``"onFailure": "block"`` in .claude/settings.json. This is a guardrail against
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


def _inside(full: str, root: str) -> str | None:
    rel = os.path.relpath(full, root)
    if rel == os.curdir or rel == os.pardir or rel.startswith(os.pardir + os.sep):
        return None
    return rel.replace(os.sep, "/")


def _relative_forms(path: str, project_dir: str) -> set[str]:
    """Project-relative forms of a path: lexical and fully resolved (symlinks followed).

    Both are checked, so a protected path that is a symlink to elsewhere, and a path
    elsewhere that is a symlink into a protected area, are both caught.
    """
    lexical_root = os.path.normpath(os.path.abspath(project_dir))
    real_root = os.path.realpath(project_dir)
    lexical = os.path.normpath(path if os.path.isabs(path) else os.path.join(lexical_root, path))
    resolved = os.path.realpath(lexical)
    forms = {_inside(lexical, lexical_root), _inside(lexical, real_root), _inside(resolved, real_root)}
    forms.discard(None)
    return forms


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
    forms = _relative_forms(path, project_dir)
    protected = sorted(rel for rel in forms if _is_protected(rel))
    if protected:
        return 2, (f"guard: {protected[0]} is protected (.github/, ci/, .claude/, LICENSE). CI, governance and "
                   "these guardrails change only with a separate owner authorisation, edited by hand.")
    package = sorted(rel for rel in forms if rel.startswith(PACKAGE_DIR) and rel.endswith(".py"))
    if package and tool != "NotebookEdit":
        rel = package[0]
        absolute = path if os.path.isabs(path) else os.path.join(project_dir, path)
        problems = contract_violations(_resulting_content(tool, tool_input, absolute))
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
