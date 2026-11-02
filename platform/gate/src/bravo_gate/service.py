"""Run the questions against two environments and build the comparison (FR-12) and gate input (FR-6)."""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any

from . import __version__
from .controls import QUESTIONS, SideResults, build_rows
from .environments import Environment
from .probes.neutral import PlatformProbe
from .probes.objstore import ObjectStoreProbe
from .probes.platform import AwsProbe, KubernetesProbe, network_paths
from .probes.postgres import PostgresProbe
from .result import Obs, error_text, unknown

TTL_ENV = "BRAVO_EVIDENCE_TTL_S"


SECTIONS = ("object_store", "postgres", "kubernetes", "platform", "network")


def _probe_for(env: Environment, section: str, allow_writes: bool = False,
               kube: KubernetesProbe | None = None) -> Any:
    if section == "object_store" and env.object_store:
        return ObjectStoreProbe(env.object_store, allow_writes=allow_writes)
    if section == "postgres" and env.postgres:
        return PostgresProbe(env.postgres)
    if section == "kubernetes" and env.kubernetes:
        return kube
    if section == "platform" and env.platform:
        return PlatformProbe(env, kube=kube, aws=AwsProbe(env.aws) if env.aws else None)
    if section == "network" and env.network:
        net = env.network

        class _Net:
            def paths(self) -> Obs:
                return network_paths(net, kube)

        return _Net()
    return None


def _observe_section(env: Environment, section: str, allow_writes: bool,
                     kube: KubernetesProbe | None) -> dict[str, Obs]:
    out: dict[str, Obs] = {}
    try:
        probe = _probe_for(env, section, allow_writes, kube)
    except Exception as exc:  # noqa: BLE001
        err = unknown("probe_setup_error", error=error_text(exc))
        return {q.qid: err for q in QUESTIONS if q.needs == section}
    for q in QUESTIONS:  # fixed order within a section
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


def observe(env: Environment, sections: set[str] | None = None, *, allow_writes: bool = False) -> dict[str, Obs]:
    """Run every question (all five sections by default). Sections run in parallel (the NET-PATHS probe Pod and
    the audit window take seconds); questions keep a fixed order within a section. The result does not depend on
    the order. ``allow_writes`` (POST runs only) lets OBJ-03/OBJ-04 PUT their probe keys (``write_probes``)."""
    kube = KubernetesProbe(env.kubernetes) if env.kubernetes else None  # one API client per environment
    if kube is not None:
        kube.api_client()  # build once before the threads share it
    names = sorted(sections or SECTIONS)
    out: dict[str, Obs] = {}
    with ThreadPoolExecutor(max_workers=len(names) or 1) as pool:
        for part in pool.map(lambda sec: _observe_section(env, sec, allow_writes, kube), names):
            out.update(part)
    return out


def compare(source: Environment, target: Environment, *, allow_writes: bool = False) -> dict[str, Any]:
    """Every question of every section on both sides (audit B16): an unconfigured section is answered
    UNKNOWN ``not_configured`` and blocks, it never disappears from the report."""
    sections = set(SECTIONS)
    started = datetime.now(UTC)
    with ThreadPoolExecutor(max_workers=2) as pool:
        fs = pool.submit(observe, source, sections, allow_writes=allow_writes)
        ft = pool.submit(observe, target, sections, allow_writes=allow_writes)
        src_obs, dst_obs = fs.result(), ft.result()
    report = build_rows(SideResults(source.environment_id, src_obs), SideResults(target.environment_id, dst_obs))
    ttl = int(os.environ.get(TTL_ENV, "3600"))
    report.update({
        "test_version": os.environ.get("BRAVO_GATE_TEST_VERSION", f"bravo-gate/{__version__}"),
        "collected_at": started.isoformat(),
        "expires_at": (started + timedelta(seconds=ttl)).isoformat(),
    })
    return report
