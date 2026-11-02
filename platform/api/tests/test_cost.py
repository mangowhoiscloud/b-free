"""``GET /v1/cost`` (T1, NFR-8) through the API against the real platform/collector process.

The collector runs with a cost config that names no cloud provider, so it answers without network: every cloud
section is "연결 안 됨" with its reason and the subscription line has no price. The API adds the plan window.
"""

from __future__ import annotations

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

from .conftest import COLLECTOR_DIR, ExecutorStub, Keys, free_port, make_settings
from .test_api_flow import Console

COLLECTOR_PY = COLLECTOR_DIR / ".venv" / "bin" / "python"


@pytest.fixture(scope="module")
def cost_collector(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    if not COLLECTOR_PY.is_file():
        pytest.skip("collector_venv_missing")
    d = tmp_path_factory.mktemp("cost")
    (d / "environments.yaml").write_text("environments: {}\n")
    (d / "cost.yaml").write_text("llm:\n  subscriptions:\n    - {id: s1, name: test sub, provider: x, monthly_usd: null}\n")
    port = free_port()
    proc = subprocess.Popen(
        [str(COLLECTOR_PY), "-m", "bravo_collector", "serve"],
        env={
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(tmp_path_factory.mktemp("home")),
            "BRAVO_ENVIRONMENTS_FILE": str(d / "environments.yaml"),
            "BRAVO_COST_CONFIG": str(d / "cost.yaml"),
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


def test_cost_with_and_without_a_plan(keys: Keys, pg: dict[str, str], executor: ExecutorStub,
                                      cost_collector: str) -> None:
    executor.healthy()
    settings = replace(make_settings(keys, pg, executor.url, Mode.API), collector_url=cost_collector)
    with TestClient(create_app(settings)) as client:
        console = Console(client, keys)
        r = console.get("viewer", "/cost")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["schema_version"] == "cost/v1" and body["plan"] is None and body["summary"] is None
        assert {"AWS 인벤토리", "GCP 인벤토리"} <= {n["what"] for n in body["not_connected"]}

        pid = console.plan()
        with psycopg.connect(pg["owner"], autocommit=True) as c:
            created = c.execute("SELECT created_at FROM plans WHERE plan_id = %s", (pid,)).fetchone()[0]
        body = console.get("viewer", f"/cost?plan_id={pid}").json()
        assert body["plan"]["plan_id"] == str(pid)
        assert body["plan"]["created_at"].startswith(created.astimezone().strftime("%Y")) and body["plan"]["done_at"] is None
        assert body["summary"]["before"]["hours"] == pytest.approx(24, abs=0.01)  # one day of "before"
        assert body["phases"]["during"]["end"] == body["period"]["end"]           # not DONE: runs to now
        assert body["migration_extra"]["formula"].startswith("이전 중 전체")

        assert console.get("viewer", "/cost?plan_id=00000000-0000-0000-0000-000000000000").status_code == 404
        r = console.get("viewer", "/cost?start=not-a-date")
        assert r.status_code == 422 and r.json()["error"] == "cost_bad_request"


def test_cost_without_collector_is_not_connected(keys: Keys, pg: dict[str, str], executor: ExecutorStub) -> None:
    executor.healthy()
    settings = replace(make_settings(keys, pg, executor.url, Mode.API), collector_url=None)
    with TestClient(create_app(settings)) as client:
        r = Console(client, keys).get("viewer", "/cost")
        assert r.status_code == 503 and r.json()["error"] == "collector_unconfigured"
