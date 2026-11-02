"""Assemble one snapshot (collector/v1) for one environment. Read-only; never raises for source errors."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

from . import __version__
from .environments import Environment
from .model import Snapshot, SourceResult, source_status

SOURCES: tuple[str, ...] = ("aws", "kubernetes", "postgres")
TTL_ENV = "BRAVO_EVIDENCE_TTL_S"


def evidence_ttl_s() -> int:
    raw = os.environ.get(TTL_ENV, "3600")
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{TTL_ENV} must be an integer number of seconds") from exc
    if value <= 0:
        raise RuntimeError(f"{TTL_ENV} must be positive")
    return value


def configured_sources(env: Environment) -> list[str]:
    return [s for s in SOURCES if getattr(env, s) is not None]


def _collect_source(env: Environment, name: str) -> SourceResult:
    section = getattr(env, name)
    if section is None:
        return SourceResult(status="not_configured")
    try:
        if name == "aws":
            from .aws import AwsCollector

            data = AwsCollector(section).collect()
        elif name == "kubernetes":
            from .kubernetes import KubernetesCollector

            data = KubernetesCollector(section).collect()
        else:
            from .postgres import PostgresCollector

            data = PostgresCollector(section).collect()
    except Exception as exc:  # noqa: BLE001 - a broken source must not hide the others
        return SourceResult(status="error", error=f"{type(exc).__name__}: {exc}"[:500])
    return SourceResult(status=source_status(data), data=data)


def collect(env: Environment, sources: set[str] | None = None) -> Snapshot:
    wanted = set(SOURCES) if not sources else sources
    unknown = wanted - set(SOURCES)
    if unknown:
        raise ValueError(f"unknown sources: {sorted(unknown)}")
    ttl = evidence_ttl_s()
    started = datetime.now(UTC).replace(microsecond=0)
    results = {name: _collect_source(env, name) for name in SOURCES if name in wanted}
    return Snapshot(
        environment_id=env.environment_id,
        collector_version=__version__,
        collected_at=started.isoformat(),
        expires_at=(started + timedelta(seconds=ttl)).isoformat(),
        sources=results,
    )
