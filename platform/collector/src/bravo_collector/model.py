"""Snapshot contract (D-05, collector/v1). Every observed value is an ``Item`` with three states:

    ok      the value was read
    absent  the resource answered that the setting does not exist (e.g. NoSuchBucketPolicy)
    error   the value could not be read (access denied, timeout, not found). Readers treat it as UNKNOWN.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

SCHEMA_VERSION = "collector/v1"


class Item(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: Literal["ok", "absent", "error"]
    value: Any = None
    error: str | None = None


def ok(value: Any) -> Item:
    return Item(status="ok", value=value)


def absent(reason: str | None = None) -> Item:
    return Item(status="absent", error=reason)


def failed(exc: BaseException | str) -> Item:
    if isinstance(exc, BaseException):
        msg = f"{type(exc).__name__}: {exc}"
    else:
        msg = exc
    return Item(status="error", error=msg[:500])


def observe(fn: Callable[[], Any], *, absent_on: Callable[[BaseException], bool] | None = None) -> Item:
    """Run ``fn`` and wrap its result. ``absent_on(exc)`` marks known "not configured" answers."""
    try:
        return ok(fn())
    except Exception as exc:  # noqa: BLE001 - every failure becomes an explicit error item
        if absent_on is not None and absent_on(exc):
            return absent(_short(exc))
        return failed(exc)


def _short(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:300]


class SourceResult(BaseModel):
    status: Literal["ok", "partial", "error", "not_configured"]
    error: str | None = None
    data: dict[str, Any] = {}


class Snapshot(BaseModel):
    schema_version: str = SCHEMA_VERSION
    environment_id: str
    collector_version: str
    collected_at: str
    expires_at: str
    sources: dict[str, SourceResult]


def source_status(data: dict[str, Any]) -> Literal["ok", "partial"]:
    """``partial`` when any nested Item is an error."""

    def walk(v: Any) -> bool:
        if isinstance(v, Item):
            return v.status == "error"
        if isinstance(v, dict):
            return any(walk(x) for x in v.values())
        if isinstance(v, list | tuple):
            return any(walk(x) for x in v)
        return False

    return "partial" if walk(data) else "ok"
