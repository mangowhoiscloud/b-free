"""Integrity receipt hash rule v1.1 (§8.6, platform/executor/README.md).

v1.1 = v1 plus non-integer primary keys (D-04 ``attachments`` has a uuid PK). For integer-PK
tables v1.1 produces exactly the v1 hashes.

Rule, per table:

* Rows are read ordered by primary key ascending:
  - integer PK: numeric order (``ORDER BY id``);
  - uuid PK: byte order of the canonical lower-case text (``ORDER BY id::text COLLATE "C"``).
  The digest rejects any row whose key is not strictly greater than the previous one
  (in that order), so a wrong ORDER BY cannot produce a plausible hash.
* ``rows``          number of rows.
* ``pk_hash``       sha256 over ``<pk>\\n`` for every row (integer as base-10 ASCII,
                    uuid as canonical 8-4-4-4-12 lower-case text).
* ``content_hash``  sha256 over one line per row: the columns in declared order
  joined by ``|`` and terminated by ``\\n``. For ``messages`` the columns are
  ``id|room|author|body|idem_key|created_at``.

Column serialisation:

* NULL                -> ``\\N``
* int                 -> base-10 ASCII
* text                -> UTF-8, with ``\\`` -> ``\\\\``, ``|`` -> ``\\|``, LF -> ``\\n``, CR -> ``\\r``
  (escaping keeps the row boundary and column boundary unambiguous)
* uuid                -> lower-case canonical 8-4-4-4-12 form
* timestamptz         -> UTC, ``YYYY-MM-DDTHH:MM:SS.ffffffZ`` (always 6 fraction digits)
* bool                -> ``t`` / ``f``

Sequences are compared by their last issued value (``last_value`` when ``is_called``,
otherwise ``last_value - 1``).
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

HASH_RULE = "v1.1"
INT_PK = "int"
TEXT_PK = "text"


class HashOrderError(ValueError):
    """Rows were not delivered in strictly ascending primary-key order."""


def serialize_value(value: object) -> str:
    if value is None:
        return "\\N"
    if isinstance(value, bool):
        return "t" if value else "f"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("hash rule v1 requires timezone-aware timestamps")
        return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    if isinstance(value, str):
        return (
            value.replace("\\", "\\\\")
            .replace("|", "\\|")
            .replace("\n", "\\n")
            .replace("\r", "\\r")
        )
    raise TypeError(f"hash rule v1 has no serialisation for {type(value).__name__}")


def serialize_row(values: Sequence[object]) -> bytes:
    return ("|".join(serialize_value(v) for v in values) + "\n").encode("utf-8")


@dataclass(slots=True)
class TableDigest:
    """Single-pass digest of one table. The first column is the PK (integer or uuid, see pk_kind)."""

    name: str
    pk_kind: str = INT_PK
    rows: int = 0
    _last_pk: int | str | None = None
    _pk: Any = field(default_factory=hashlib.sha256)
    _content: Any = field(default_factory=hashlib.sha256)

    def _key(self, pk: object) -> int | str:
        if self.pk_kind == INT_PK:
            if not isinstance(pk, int) or isinstance(pk, bool):
                raise TypeError(f"{self.name}: primary key must be an integer, got {type(pk).__name__}")
            return pk
        if not isinstance(pk, UUID):
            raise TypeError(f"{self.name}: primary key must be a uuid, got {type(pk).__name__}")
        return str(pk)

    def add(self, row: Sequence[object]) -> None:
        key = self._key(row[0])
        if self._last_pk is not None and key <= self._last_pk:  # type: ignore[operator]
            raise HashOrderError(f"{self.name}: key {key} after {self._last_pk}")
        self._last_pk = key
        self.rows += 1
        self._pk.update(f"{key}\n".encode("ascii"))
        self._content.update(serialize_row(row))

    def add_all(self, rows: Iterable[Sequence[object]]) -> TableDigest:
        for row in rows:
            self.add(row)
        return self

    @property
    def pk_hash(self) -> str:
        return self._pk.hexdigest()

    @property
    def content_hash(self) -> str:
        return self._content.hexdigest()

    def as_dict(self) -> dict[str, object]:
        return {"rows": self.rows, "pk_hash": self.pk_hash, "content_hash": self.content_hash}


def sequence_last_issued(last_value: int, is_called: bool) -> int:
    return last_value if is_called else last_value - 1
