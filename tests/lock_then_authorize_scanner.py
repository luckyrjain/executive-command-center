"""Finds writes whose only authorization is an authz SQL fragment evaluated
inside a row-locking statement.

`authz.visible_resource_filter_sql` (and the wrappers built on it) checks
`resource_grants` in a subquery. A grant revoke
(`authz_grants.revoke_grant_endpoint`) locks the resource row but only
updates `resource_grants`, never the row, so when a statement that waited
on that lock resumes, Postgres re-checks its `WHERE` only against the
re-fetched row (EvalPlanQual), not the grant subquery: the subquery keeps
the statement's pre-revoke snapshot. Ownership/visibility changes do update
the row and are re-checked; a revoked grant is not.

So a fragment in

- an `UPDATE ... SET` / `DELETE FROM` is always flagged: that `WHERE` is the
  authorization for the write;
- a `SELECT ... FOR UPDATE`/`FOR SHARE` is flagged unless the same function
  later calls `authz.authorize(...)` (lock-then-authorize: the fragment only
  narrows which rows get locked; each fresh READ COMMITTED `authorize`
  statement sees the committed revoke);
- a plain `SELECT` is not flagged: it never waits on a row lock, so it reads
  one consistent snapshot like any other read.

`authz.list_visible_resources` embeds the fragment itself, so a locking
clause passed through its `limit_clause`/`extra_clauses` is flagged too.

Static and heuristic (per function, names only): a fragment is any value
bound from a call whose name ends in `_filter_sql` or `visibility_sql`,
followed through assignments, `.append`/`.extend` and string building in
the same function.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

_FRAGMENT_CALL = re.compile(r"(_filter_sql|visibility_sql)$")
_WRITE = re.compile(r"\bUPDATE\s+\S+\s+SET\b|\bDELETE\s+FROM\b", re.IGNORECASE)
_LOCKING = re.compile(r"\bFOR\s+(NO\s+KEY\s+UPDATE|KEY\s+SHARE|UPDATE|SHARE)\b", re.IGNORECASE)


@dataclass(frozen=True, order=True)
class Finding:
    module: str
    function: str
    line: int
    kind: str  # "write" | "lock-without-authorize" | "list-visible-locking"


def _call_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


def _names(node: ast.AST) -> set[str]:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def _calls_fragment(node: ast.AST) -> bool:
    return any(
        isinstance(n, ast.Call) and _FRAGMENT_CALL.search(_call_name(n)) for n in ast.walk(node)
    )


def _own_nodes(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> Iterator[ast.AST]:
    """`fn`'s nodes, not descending into nested functions (scanned on
    their own)."""
    stack: list[ast.AST] = list(fn.body)
    while stack:
        node = stack.pop()
        yield node
        for child in ast.iter_child_nodes(node):
            if not isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
                stack.append(child)


def _bindings(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> list[tuple[set[str], ast.AST]]:
    """(target names, value) for every assignment-like statement in `fn`,
    including `x.append(v)`/`x.extend(v)` (binds into `x`)."""
    out: list[tuple[set[str], ast.AST]] = []
    for node in _own_nodes(fn):
        if isinstance(node, ast.Assign):
            out.append((set().union(*(_names(t) for t in node.targets)), node.value))
        elif isinstance(node, ast.AnnAssign | ast.AugAssign) and node.value is not None:
            out.append((_names(node.target), node.value))
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"append", "extend", "update"}
            and isinstance(node.func.value, ast.Name)
        ):
            for arg in node.args:
                out.append(({node.func.value.id}, arg))
    return out


def _tainted(bindings: list[tuple[set[str], ast.AST]]) -> set[str]:
    tainted: set[str] = set()
    changed = True
    while changed:
        changed = False
        for targets, value in bindings:
            if targets <= tainted:
                continue
            if _calls_fragment(value) or _names(value) & tainted:
                tainted |= targets
                changed = True
    return tainted


def _static_text(node: ast.AST, values: dict[str, list[ast.AST]], depth: int = 0) -> str:
    """Every string literal reachable from `node`, following names bound in
    the same function (a few levels: `suffix = " FOR UPDATE" if ... else ""`,
    `query = f"..."`)."""
    parts: list[str] = []
    for n in ast.walk(node):
        if isinstance(n, ast.Constant) and isinstance(n.value, str):
            parts.append(n.value)
        elif isinstance(n, ast.Name) and depth < 3:
            parts.extend(_static_text(v, values, depth + 1) for v in values.get(n.id, []))
    return " ".join(parts)


def _scan_function(module: str, fn: ast.FunctionDef | ast.AsyncFunctionDef) -> list[Finding]:
    bindings = _bindings(fn)
    tainted = _tainted(bindings)
    values: dict[str, list[ast.AST]] = {}
    for targets, value in bindings:
        for target in targets:
            values.setdefault(target, []).append(value)
    authorize_lines = [
        n.lineno
        for n in _own_nodes(fn)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "authorize"
        and ast.unparse(n.func.value) == "authz"
    ]
    findings: list[Finding] = []
    for node in _own_nodes(fn):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node)
        if name == "list_visible_resources":
            clauses = " ".join(
                _static_text(kw.value, values)
                for kw in node.keywords
                if kw.arg in {"limit_clause", "extra_clauses"}
            )
            if _LOCKING.search(clauses) or _WRITE.search(clauses):
                findings.append(Finding(module, fn.name, node.lineno, "list-visible-locking"))
            continue
        if name != "text" or not node.args:
            continue
        sql = node.args[0]
        if not (_calls_fragment(sql) or _names(sql) & tainted):
            # `text(query)` where `query` was built from a fragment.
            referenced = [v for n in _names(sql) for v in values.get(n, [])]
            if not any(_calls_fragment(v) or _names(v) & tainted for v in referenced):
                continue
        statement = _static_text(sql, values)
        if _WRITE.search(statement):
            findings.append(Finding(module, fn.name, node.lineno, "write"))
        elif _LOCKING.search(statement) and not any(line > node.lineno for line in authorize_lines):
            findings.append(Finding(module, fn.name, node.lineno, "lock-without-authorize"))
    return findings


def scan(paths: Iterable[Path], *, root: Path) -> list[Finding]:
    findings: list[Finding] = []
    for path in sorted(paths):
        tree = ast.parse(path.read_text(), filename=str(path))
        module = path.relative_to(root).as_posix()
        for fn in ast.walk(tree):
            if isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
                findings.extend(_scan_function(module, fn))
    return sorted(findings)


def fragment_statement_count(paths: Iterable[Path]) -> int:
    """How many `text(...)` statements embed a fragment at all -- so the
    coverage test can tell "nothing flagged" from "nothing recognized"."""
    count = 0
    for path in paths:
        tree = ast.parse(path.read_text(), filename=str(path))
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            tainted = _tainted(_bindings(fn))
            count += sum(
                1
                for n in _own_nodes(fn)
                if isinstance(n, ast.Call)
                and _call_name(n) == "text"
                and n.args
                and (_calls_fragment(n.args[0]) or _names(n.args[0]) & tainted)
            )
    return count
