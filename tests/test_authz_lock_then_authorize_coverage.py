"""No write is authorized only by an authz SQL fragment inside a row-locking
statement: a grant revoked while that statement waits on the row lock is
not re-checked (see `lock_then_authorize_scanner`). Lock first, then
`authz.authorize(...)` each locked row in its own statement.
"""

from __future__ import annotations

from pathlib import Path

from lock_then_authorize_scanner import Finding, fragment_statement_count, scan

_REPO = Path(__file__).resolve().parents[1]
_BACKEND = _REPO / "backend" / "ecc"

# Known violations being fixed elsewhere: tolerated while present, not
# required. `create_recommendation`'s supersede UPDATE carries its read and
# write fragments in the WHERE; luckyrjain/executive-command-center#383
# converts it to lock-then-authorize. Remove this entry once that lands.
_PENDING: frozenset[tuple[str, str]] = frozenset(
    {("backend/ecc/domains/governance/recommendation_mutations.py", "create_recommendation")}
)


def test_no_write_is_authorized_only_inside_a_row_locking_statement() -> None:
    findings = [
        finding
        for finding in scan(_BACKEND.rglob("*.py"), root=_REPO)
        if (finding.module, finding.function) not in _PENDING
    ]

    assert findings == []


def test_the_scan_recognizes_the_backend_fragment_statements() -> None:
    """Guards the guard: a scanner that silently stopped recognizing
    fragments would report nothing and pass the test above."""
    assert fragment_statement_count(_BACKEND.rglob("*.py")) >= 20


_SOURCE = """
def update_in_where(session, auth):
    sql, params = authz.visible_resource_filter_sql(session, auth, resource_type="t")
    session.execute(text(f"UPDATE t SET x = 1 WHERE id = :id AND {sql}"), params)


def delete_in_where(session, auth):
    sql, params = run_visibility.visible_runs_filter_sql(session, auth)
    session.execute(text(f"DELETE FROM t WHERE {sql}"), params)


def lock_without_authorize(session, auth):
    sql, params = authz.visible_resource_filter_sql(session, auth, resource_type="t")
    ids = session.execute(text(f"SELECT id FROM t WHERE {sql} FOR UPDATE"), params).all()
    session.execute(text("UPDATE t SET x = 1 WHERE id = ANY(:ids)"), {"ids": ids})


def lock_then_authorize(session, auth):
    sql, params = authz.visible_resource_filter_sql(session, auth, resource_type="t")
    ids = session.execute(text(f"SELECT id FROM t WHERE {sql} FOR UPDATE"), params).all()
    ok = [i for i in ids if authz.authorize(session, auth, resource_type="t", resource_id=i)]
    session.execute(text("UPDATE t SET x = 1 WHERE id = ANY(:ids)"), {"ids": ok})


def authorize_only_before_the_lock(session, auth):
    authz.authorize(session, auth, resource_type="t", resource_id=x)
    sql, params = authz.visible_resource_filter_sql(session, auth, resource_type="t")
    session.execute(text(f"SELECT id FROM t WHERE {sql} FOR SHARE"), params)


def plain_select(session, auth):
    sql, params = authz.visible_resource_filter_sql(session, auth, resource_type="t")
    return session.execute(text(f"SELECT id FROM t WHERE {sql}"), params).all()


def through_a_clause_list(session, auth):
    left, left_params = authz.evidence_visibility_filter_sql(session, auth, table_alias="e")
    clauses = ["workspace_id = :ws"]
    clauses.append(f"({left})")
    session.execute(text(f"UPDATE e SET x = 1 WHERE {' AND '.join(clauses)}"), left_params)


def through_a_wrapper_and_suffix(session, auth, for_update):
    scoped, params = _scope_clause(*authz.evidence_visibility_filter_sql(session, auth))
    suffix = " FOR UPDATE" if for_update else ""
    session.execute(text(f"SELECT id FROM e WHERE {scoped}{suffix}"), params)


def through_a_query_variable(session, auth):
    sql, params = authz.visible_resource_filter_sql(session, auth, resource_type="t")
    query = f"SELECT id FROM t WHERE {sql} FOR NO KEY UPDATE"
    session.execute(text(query), params)


def unrelated_write(session, auth):
    sql, params = authz.visible_resource_filter_sql(session, auth, resource_type="t")
    rows = session.execute(text(f"SELECT id FROM t WHERE {sql}"), params).all()
    session.execute(text("UPDATE t SET seen = true WHERE id = :id"), {"id": rows[0]})


def list_visible_locking(session, auth):
    return authz.list_visible_resources(
        session, auth, resource_type="t", columns="id", order_by="id",
        limit_clause="LIMIT 1 FOR UPDATE",
    )


def list_visible_plain(session, auth):
    return authz.list_visible_resources(
        session, auth, resource_type="t", columns="id", order_by="id", limit_clause="LIMIT 1",
    )
"""


def test_scanner_flags_fragment_writes_and_locks_not_followed_by_authorize(
    tmp_path: Path,
) -> None:
    module = tmp_path / "sample.py"
    module.write_text(_SOURCE)

    flagged = {(f.function, f.kind) for f in scan([module], root=tmp_path)}

    assert flagged == {
        ("update_in_where", "write"),
        ("delete_in_where", "write"),
        ("lock_without_authorize", "lock-without-authorize"),
        ("authorize_only_before_the_lock", "lock-without-authorize"),
        ("through_a_clause_list", "write"),
        ("through_a_wrapper_and_suffix", "lock-without-authorize"),
        ("through_a_query_variable", "lock-without-authorize"),
        ("list_visible_locking", "list-visible-locking"),
    }
    assert all(isinstance(f, Finding) for f in scan([module], root=tmp_path))
