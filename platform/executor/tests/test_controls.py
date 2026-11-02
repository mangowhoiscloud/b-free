"""Control rules DB-ANON-READ and DB-AUTH (pure functions)."""

from __future__ import annotations

from typing import Any

import pytest

from bravo_executor.controls import FAIL, PASS, UNKNOWN, evaluate_anon_read, evaluate_auth


def _rule(method: str, db: str = "all", user: str = "all", typ: str = "host", err: str | None = None) -> dict[str, Any]:
    return {"line_number": 1, "type": typ, "database": [db], "user_name": [user], "auth_method": method, "error": err}


SCRAM_ONLY = [_rule("scram-sha-256", typ="local"), _rule("scram-sha-256")]


@pytest.mark.parametrize(("value", "verdict"), [(True, FAIL), (False, PASS), (None, UNKNOWN)])
def test_anon_read(value: bool | None, verdict: str) -> None:
    assert evaluate_anon_read(value) == verdict


def _auth(**kw: Any) -> str:
    args: dict[str, Any] = {
        "password_encryption": "scram-sha-256",
        "role_has_password": True,
        "hba_rules": SCRAM_ONLY,
        "app_role": "app_writer",
        "database": "chat",
    }
    args.update(kw)
    return evaluate_auth(**args)[0]


def test_auth_pass_with_scram_everywhere() -> None:
    assert _auth() == PASS


@pytest.mark.parametrize(
    "rules",
    [
        [_rule("trust", typ="local")],                       # docker default local trust
        [_rule("trust", db="chat", user="app_writer")],
        [_rule("peer", typ="local", user="+writers")],       # group membership treated as a match
        [_rule("ident", db="sameuser")],
    ],
)
def test_auth_fail_on_any_no_password_rule(rules: list[dict[str, Any]]) -> None:
    assert _auth(hba_rules=SCRAM_ONLY + rules) == FAIL


def test_auth_ignores_no_password_rules_for_other_roles_or_dbs() -> None:
    rules = SCRAM_ONLY + [_rule("trust", db="replication", user="replicator"), _rule("trust", user="postgres")]
    assert _auth(hba_rules=rules) == PASS


def test_auth_fail_without_password_or_with_md5_storage() -> None:
    assert _auth(role_has_password=False) == FAIL
    assert _auth(password_encryption="md5") == FAIL  # noqa: S106 - setting value, not a secret


def test_auth_unknown_on_missing_input_or_parse_error() -> None:
    assert _auth(hba_rules=None) == UNKNOWN
    assert _auth(role_has_password=None) == UNKNOWN
    assert _auth(hba_rules=[_rule("scram-sha-256", err="invalid line")]) == UNKNOWN
