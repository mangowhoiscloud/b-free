"""Collector ``inventory_root_empty`` (503: nothing mounted to scan) against the real platform/collector.

It is "inventory unavailable": the plan stays in ANALYZE with that reason (shown in ``plan.analysis``), it is
not "collector unreachable", and a rehearsal start stops before any Trial without counting an attempt.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import time
from collections.abc import Iterator
from dataclasses import replace

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient

from bravo_api.app import create_app
from bravo_api.config import Mode
from bravo_api.upstreams import CollectorClient, UpstreamError

from .conftest import COLLECTOR_DIR, ExecutorStub, Keys, free_port, make_settings
from .test_api_flow import Console

COLLECTOR_PY = COLLECTOR_DIR / ".venv" / "bin" / "python"


@pytest.fixture(scope="module")
def empty_collector(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    if not COLLECTOR_PY.is_file():
        pytest.skip("collector_venv_missing")
    root = tmp_path_factory.mktemp("nothing-mounted")  # an empty emptyDir / failed checkout
    envs = tmp_path_factory.mktemp("envs") / "environments.yaml"
    envs.write_text("environments: {}\n")
    port = free_port()
    proc = subprocess.Popen(
        [str(COLLECTOR_PY), "-m", "bravo_collector", "serve"],
        env={
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(tmp_path_factory.mktemp("home")),
            "BRAVO_ENVIRONMENTS_FILE": str(envs),
            "BRAVO_INVENTORY_ROOT": str(root),
            "BRAVO_COLLECTOR_HOST": "127.0.0.1",
            "BRAVO_COLLECTOR_PORT": str(port),
            "LOG_LEVEL": "warning",
        },
        cwd=COLLECTOR_DIR,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.time() + 30
        while True:
            try:
                if httpx.get(f"{url}/healthz", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            if time.time() > deadline or proc.poll() is not None:
                raise RuntimeError("collector did not start")
            time.sleep(0.2)
        yield url
    finally:
        proc.terminate()
        proc.wait(10)


def test_client_maps_the_collector_503_to_inventory_root_empty(empty_collector: str) -> None:
    raw = httpx.get(f"{empty_collector}/v1/inventory", timeout=5)
    assert raw.status_code == 503 and "inventory_root_empty" in raw.text  # the real collector's answer

    async def go() -> list[str]:
        c = CollectorClient(empty_collector, 5.0)
        out = []
        for call in (c.inventory, lambda: c.snapshot("0" * 64)):
            try:
                await call()
            except UpstreamError as exc:
                out.append(f"{exc.reason}:{exc.status}")
        await c.aclose()
        return out

    assert asyncio.run(go()) == ["inventory_root_empty:503", "inventory_root_empty:503"]


def test_plan_holds_in_analyze_and_a_rehearsal_start_is_not_counted(
    keys: Keys, pg: dict[str, str], executor: ExecutorStub, empty_collector: str
) -> None:
    executor.healthy()
    settings = replace(
        make_settings(keys, pg, executor.url, Mode.API), collector_url=empty_collector, rehearsal_url="http://127.0.0.1:9"
    )  # never reached: the inventory stops the start first
    with TestClient(create_app(settings)) as client:
        console = Console(client, keys)
        pid = console.plan()
        plan = console.get("viewer", f"/plans/{pid}").json()
        assert plan["state"] == "ANALYZE" and plan["state_reason"] == "inventory_root_empty"
        inv = plan["analysis"]["inventory"]
        assert (inv["status"], inv["reason"]) == ("unavailable", "inventory_root_empty")
        assert plan["analysis"]["gaps"] == []  # recorded as unknown, the plan does not move on it
        with psycopg.connect(pg["owner"], autocommit=True) as c:
            c.execute("UPDATE plans SET state = 'PROPOSE' WHERE plan_id = %s", (pid,))
        r = console.post("operator", f"/plans/{pid}/rehearsals", {"task": "fix-secrets-manager", "role": "app_migration"})
        assert r.status_code == 202, r.text
        body = r.json()
        assert (body["status"], body["reason"]) == ("INFRA_ERROR", "inventory_root_empty")
        plan = console.get("viewer", f"/plans/{pid}").json()
    assert plan["rehearsal_attempts"]["failed"] == 0 and plan["rehearsal_attempts"]["infra_errors"] == 0
