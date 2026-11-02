"""Pilot controls for the gate (CONTRACT.md 6). Each value is PASS, FAIL or UNKNOWN.

DB-ANON-READ  "Can PUBLIC read messages?"
    PASS    has_table_privilege('public', 'messages', 'SELECT') is false
    FAIL    it is true
    UNKNOWN the database cannot be queried

DB-AUTH  "Can the app role connect without a password?"  (deterministic rule)
    UNKNOWN any input cannot be read, or pg_hba_file_rules reports a parse error
    FAIL    password_encryption is not scram-sha-256
    FAIL    the app role has no password (bravo_catalog.role_has_password)
    FAIL    any pg_hba rule that can match the app role on the chat database uses a
            method that needs no password: trust, peer, ident. Rule order is ignored on
            purpose (any reachable no-password rule counts), so the rule never under-reports.
    PASS    otherwise
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

PASS = "PASS"  # noqa: S105 - control verdict, not a secret
FAIL = "FAIL"
UNKNOWN = "UNKNOWN"

NO_PASSWORD_METHODS = frozenset({"trust", "peer", "ident"})


@dataclass(frozen=True, slots=True)
class ControlResult:
    control_id: str
    question: str
    src: str
    dst: str

    def as_dict(self) -> dict[str, str]:
        return {"control_id": self.control_id, "question": self.question, "src": self.src, "dst": self.dst}


ANON_READ = ("DB-ANON-READ", "Can PUBLIC read the messages table?")
AUTH = ("DB-AUTH", "Can the app role connect without a password?")


def evaluate_anon_read(public_can_select: bool | None) -> str:
    if public_can_select is None:
        return UNKNOWN
    return FAIL if public_can_select else PASS


def _covers(values: Iterable[str] | None, wanted: str, wildcards: frozenset[str]) -> bool:
    if values is None:
        return False
    for v in values:
        if v in wildcards or v == wanted or v.startswith("+"):
            # "+group" is treated as a match: membership is not resolved here (conservative).
            return True
    return False


def evaluate_auth(
    *,
    password_encryption: str | None,
    role_has_password: bool | None,
    hba_rules: Sequence[Mapping[str, Any]] | None,
    app_role: str,
    database: str,
) -> tuple[str, dict[str, Any]]:
    """Return (verdict, evidence). Evidence holds no secrets."""
    if password_encryption is None or role_has_password is None or hba_rules is None:
        return UNKNOWN, {"reason": "INPUT_UNAVAILABLE"}
    errors = [r.get("line_number") for r in hba_rules if r.get("error")]
    if errors:
        return UNKNOWN, {"reason": "HBA_PARSE_ERROR", "lines": errors}
    if password_encryption != "scram-sha-256":  # noqa: S105 - setting name, not a secret
        return FAIL, {"reason": "PASSWORD_ENCRYPTION", "value": password_encryption}
    if not role_has_password:
        return FAIL, {"reason": "ROLE_HAS_NO_PASSWORD", "role": app_role}
    offending = [
        {"line": r.get("line_number"), "type": r.get("type"), "method": r.get("auth_method")}
        for r in hba_rules
        if str(r.get("auth_method", "")).lower() in NO_PASSWORD_METHODS
        and _covers(r.get("database"), database, frozenset({"all", "sameuser", "samerole"}))
        and _covers(r.get("user_name"), app_role, frozenset({"all"}))
    ]
    if offending:
        return FAIL, {"reason": "NO_PASSWORD_HBA_RULE", "rules": offending}
    return PASS, {"reason": "OK", "hba_rules": len(hba_rules)}
