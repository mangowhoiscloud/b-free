"""Run the questions against two environments and build the comparison (FR-12) and gate input (FR-6)."""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any

from . import __version__
from .controls import QUESTIONS, SideResults, build_rows
from .environments import Environment
from .probes.objstore import ObjectStoreProbe
from .probes.platform import AwsProbe, KubernetesProbe, network_paths
from .probes.postgres import PostgresProbe
from .result import Obs, error_text, unknown

TTL_ENV = "BRAVO_EVIDENCE_TTL_S"


def _probe_for(env: Environment, section: str) -> Any:
    if section == "object_store" and env.object_store:
        return ObjectStoreProbe(env.object_store)
    if section == "postgres" and env.postgres:
        return PostgresProbe(env.postgres)
    if section == "kubernetes" and env.kubernetes:
        return KubernetesProbe(env.kubernetes)
    if section == "aws" and env.aws:
        return AwsProbe(env.aws)
    if section == "network" and env.network:
        net = env.network

        class _Net:
            def paths(self) -> Obs:
                return network_paths(net)

        return _Net()
    return None


def observe(env: Environment, sections: set[str]) -> dict[str, Obs]:
    """Run every question whose section is in ``sections``. Questions run in a fixed order per section."""
    out: dict[str, Obs] = {}
    for section in sorted(sections):
        probe = None
        try:
            probe = _probe_for(env, section)
        except Exception as exc:  # noqa: BLE001
            err = unknown("probe_setup_error", error=error_text(exc))
            for q in QUESTIONS:
                if q.needs == section:
                    out[q.qid] = err
            continue
        for q in QUESTIONS:
            if q.needs != section:
                continue
            if probe is None:
                out[q.qid] = unknown("not_configured", section=section)
                continue
            try:
                out[q.qid] = getattr(probe, q.probe)()
            except Exception as exc:  # noqa: BLE001 - a probe crash is UNKNOWN, never PASS
                out[q.qid] = unknown("probe_crashed", error=error_text(exc))
    return out


def _sections(env: Environment) -> set[str]:
    return {s for s in ("object_store", "postgres", "kubernetes", "aws", "network") if getattr(env, s) is not None}


def compare(source: Environment, target: Environment) -> dict[str, Any]:
    sections = _sections(source) | _sections(target)
    started = datetime.now(UTC)
    with ThreadPoolExecutor(max_workers=2) as pool:
        fs = pool.submit(observe, source, sections)
        ft = pool.submit(observe, target, sections)
        src_obs, dst_obs = fs.result(), ft.result()
    report = build_rows(SideResults(source.environment_id, src_obs), SideResults(target.environment_id, dst_obs))
    ttl = int(os.environ.get(TTL_ENV, "3600"))
    report.update({
        "test_version": os.environ.get("BRAVO_GATE_TEST_VERSION", f"bravo-gate/{__version__}"),
        "collected_at": started.isoformat(),
        "expires_at": (started + timedelta(seconds=ttl)).isoformat(),
    })
    return report
