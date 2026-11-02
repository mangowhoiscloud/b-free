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
from .probes.platform import AwsProbe, KubernetesProbe, NetworkProbe
from .probes.postgres import PostgresProbe
from .result import Obs, error_text, unknown

TTL_ENV = "BRAVO_EVIDENCE_TTL_S"


SECTIONS = ("object_store", "postgres", "kubernetes", "platform", "network", "repository")
CIS_EVIDENCE_ENV = "BRAVO_GATE_CIS_EVIDENCE_FILE"


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
        return NetworkProbe(env.network, kube)
    if section == "repository" and env.repository:
        repo = env.repository

        class _Repo:
            def git_plaintext(self) -> Obs:
                from .probes.repo import git_plaintext

                return git_plaintext(repo)

        return _Repo()
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
    report = build_rows(SideResults(source.environment_id, src_obs, _kind(source)),
                        SideResults(target.environment_id, dst_obs, _kind(target)))
    ttl = int(os.environ.get(TTL_ENV, "3600"))
    report.update({
        "test_version": os.environ.get("BRAVO_GATE_TEST_VERSION", f"bravo-gate/{__version__}"),
        "collected_at": started.isoformat(),
        "expires_at": (started + timedelta(seconds=ttl)).isoformat(),
        "cis_benchmark": cis_evidence(source.environment_id, target.environment_id),
    })
    return report


def _kind(env: Environment) -> str | None:
    return env.platform.kind if env.platform else None


def cis_evidence(source: str, target: str, path: str | None = None) -> dict[str, Any]:
    """kube-bench summary per side, attached as evidence and NOT judged (approximate benchmarks, WARN items need a
    person; docs/architecture/16-gate-test-map.md). Read from the file an operator run left
    (``$BRAVO_GATE_CIS_EVIDENCE_FILE``, ConfigMap gate-cis-evidence); its own ``collected_at`` / ``expires_at``."""
    import json
    from pathlib import Path

    raw = path or os.environ.get(CIS_EVIDENCE_ENV)
    doc: dict[str, Any] = {}
    if raw and Path(raw).is_file():
        try:
            doc = json.loads(Path(raw).read_text(encoding="utf-8"))
        except ValueError:
            doc = {}
    envs = doc.get("environments") or {}
    now = datetime.now(UTC).isoformat()

    def side(env_id: str) -> dict[str, Any]:
        e = envs.get(env_id)
        if not e:
            return {"status": "not_run"}
        exp = e.get("expires_at")
        return {**e, "status": "expired" if exp and exp < now else "current"}

    return {"judged": False, "label": doc.get("label", "근사(approximate)"),
            "src": side(source), "dst": side(target)}
