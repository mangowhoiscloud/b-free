"""S2 rehearsal path through the API against a real postgres:16, the real platform/rehearsal service and
the real platform/collector service (each started from its own virtualenv in a child process with a clean
environment).

Before a Trial the API reads the collector inventory (``inventory/v1``), fetches the customer repo snapshot
bound to its ``input_digest`` and uploads it to the rehearsal service (``PUT /v1/snapshots``); the Trial
request then carries ``source_env``, ``target_env``, ``evidence`` and ``inventory_digest``. No model key is
configured here, so a Trial that gets that far fails closed with ``llm_unconfigured``; every "nothing ran"
reason is recorded without counting an attempt (FR-5). Nothing here produces a PASS or FAIL verdict.

The collector does not serve the snapshot yet (proposed ``GET /v1/inventory/snapshot``). One test proves that
against the real collector (``snapshot_unavailable``); one uses a thin front for the real collector that adds
the proposed endpoint, so the snapshot and Trial contracts are exercised against the real rehearsal service.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
import uuid
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient

from bravo_api.app import create_app
from bravo_api.config import Mode, Settings
from bravo_api.db import Database
from bravo_api.rehearsal import (
    PRECONDITIONS,
    _row,
    artifact_digest,
    bound_problem,
    evidence_from,
    prior_failures,
    settle,
    trial_request,
)
from bravo_api.upstreams import CollectorClient, RehearsalClient, UpstreamError

from .conftest import ExecutorStub, Keys, _serve, free_port, make_settings
from .test_api_flow import Console

REHEARSAL_DIR = Path(__file__).resolve().parents[2] / "rehearsal"
REHEARSAL_PY = REHEARSAL_DIR / ".venv" / "bin" / "python"
COLLECTOR_DIR = Path(__file__).resolve().parents[2] / "collector"
COLLECTOR_PY = COLLECTOR_DIR / ".venv" / "bin" / "python"
SERVICE_TOKEN = "svc-" + uuid.uuid4().hex
NOTHING_RAN_FROM_ENVELOPE = ("envelope_invalid", "state_not_allowed_for_role", "agent_runtime_unavailable")


@pytest.fixture(scope="module")
def snapshot_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("rehearsal-snapshots")


@pytest.fixture(scope="module")
def rehearsal_service(tmp_path_factory: pytest.TempPathFactory, snapshot_dir: Path) -> Iterator[str]:
    if not REHEARSAL_PY.is_file():
        pytest.skip(f"rehearsal_venv_missing: {REHEARSAL_PY.relative_to(REHEARSAL_DIR.parent)}")
    port = free_port()
    env = {  # built from scratch: no model key, no kubeconfig, nothing inherited
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(tmp_path_factory.mktemp("rehearsal-home")),
        "BRAVO_REHEARSAL_SNAPSHOT_DIR": str(snapshot_dir),
        "BRAVO_REHEARSAL_API_TOKEN": SERVICE_TOKEN,
        "BRAVO_REHEARSAL_TOKEN_KEY": uuid.uuid4().hex + uuid.uuid4().hex,
        "BRAVO_REHEARSAL_TOOL_LAYER_URL": f"http://127.0.0.1:{port}",
    }
    proc = subprocess.Popen(
        [
            str(REHEARSAL_PY),
            "-m",
            "uvicorn",
            "bravo_rehearsal.service:create_app",
            "--factory",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        env=env,
        cwd=REHEARSAL_DIR,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
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
                out = proc.stdout.read().decode() if proc.stdout and proc.poll() is not None else ""
                raise RuntimeError(f"rehearsal service did not start: {out[-2000:]}")
            time.sleep(0.2)
        yield url
    finally:
        proc.terminate()
        proc.wait(10)


@pytest.fixture(scope="module")
def api_settings(keys: Keys, pg: dict[str, str], executor: ExecutorStub, rehearsal_service: str) -> Settings:
    return replace(
        make_settings(keys, pg, executor.url, Mode.API),
        rehearsal_url=rehearsal_service,
        rehearsal_poll_interval_s=0.2,
        collector_url=None,  # these tests opt in to a collector explicitly
    )


@pytest.fixture(scope="module")
def api(api_settings: Settings) -> Iterator[TestClient]:
    os.environ["BRAVO_REHEARSAL_API_TOKEN"] = SERVICE_TOKEN
    try:
        with TestClient(create_app(api_settings)) as client:
            yield client
    finally:
        os.environ.pop("BRAVO_REHEARSAL_API_TOKEN", None)


@pytest.fixture()
def console(api: TestClient, keys: Keys, executor: ExecutorStub) -> Console:
    executor.healthy()
    return Console(api, keys)


def test_service_itself_reports_llm_unconfigured(rehearsal_service: str) -> None:
    body = {
        "plan_id": str(uuid.uuid4()),
        "tenant_id": "t1",
        "rehearsal_id": "r",
        "attempt": 1,
        "max_attempts": 3,
        "task": "fix-bucket-policy",
        "role": "governance",
    }
    r = httpx.post(
        f"{rehearsal_service}/v1/trials",
        json=body,
        headers={"Idempotency-Key": "direct-1", "Authorization": f"Bearer {SERVICE_TOKEN}"},
    )
    assert r.status_code == 503 and r.json()["error"] == "llm_unconfigured"


def _to_state(pg: dict[str, str], pid: str, state: str) -> None:
    """Put a plan in ``state`` directly (the path to it is tested elsewhere; ANALYZE needs a collector)."""
    with psycopg.connect(pg["owner"], autocommit=True) as c:
        c.execute("UPDATE plans SET state = %s, state_reason = NULL WHERE plan_id = %s", (state, pid))


def test_gate_block_to_rehearsal_fails_closed_without_inventory(
    console: Console, executor: ExecutorStub, pg: dict[str, str]
) -> None:
    """No collector configured: the Trial is not requested at all (``inventory_unavailable``), not counted."""
    executor.controls[0]["dst"] = "FAIL"  # S1: target attachments readable without login
    pid = console.plan()
    _to_state(pg, pid, "GATE")
    assert console.post("operator", f"/plans/{pid}/gate").json()["verdict"] == "BLOCK"
    r = console.post("operator", f"/plans/{pid}/rehearsals", {"task": "fix-bucket-policy", "role": "governance"})
    assert r.status_code == 202, r.text
    out = r.json()
    assert out["status"] == "INFRA_ERROR" and out["reason"] == "inventory_unavailable" and out["state"] == "PROPOSE"
    plan = console.get("viewer", f"/plans/{pid}").json()
    assert plan["state"] == "PROPOSE" and plan["state_reason"] == "inventory_unavailable"
    assert plan["rehearsal"] == "infra_error"
    assert plan["rehearsal_attempts"] == {"failed": 0, "infra_errors": 0, "max": 3}  # precondition: not counted
    assert plan["last_rehearsal"]["status"] == "INFRA_ERROR" and plan["last_rehearsal"]["trial_id"] is None
    with psycopg.connect(pg["owner"]) as c:
        rows = c.execute("SELECT kind, data FROM events WHERE plan_id = %s ORDER BY seq", (pid,)).fetchall()
    kinds = [k for k, _ in rows]
    assert kinds[-4:] == ["state.changed", "trial.started", "trial.verdict", "state.changed"]
    assert rows[-2][1]["reason"] == "inventory_unavailable" and rows[-2][1]["verdict"] == "INFRA_ERROR"
    # The gate cannot be passed from PROPOSE: a proposal must go through the rehearsal first.
    executor.healthy()
    assert console.post("operator", f"/plans/{pid}/gate").json()["error"] == "state_mismatch"


def test_role_must_belong_to_the_stage(console: Console) -> None:
    pid = console.plan()
    r = console.post("operator", f"/plans/{pid}/rehearsals", {"task": "fix-bucket-policy", "role": "discovery"})
    assert r.status_code == 422


def test_rehearsal_refused_after_cutover(console: Console, pg: dict[str, str]) -> None:
    pid = console.plan()
    with psycopg.connect(pg["owner"], autocommit=True) as c:  # a plan whose cutover is committed
        c.execute("UPDATE plans SET state = 'WATCH' WHERE plan_id = %s", (pid,))
    r = console.post("operator", f"/plans/{pid}/rehearsals", {"task": "fix-bucket-policy", "role": "governance"})
    assert r.status_code == 409 and r.json()["error"] == "rehearsal_not_allowed"  # S2-AC5


def test_verdict_binding_rejects_unbound_results() -> None:
    row: dict[str, Any] = {"trial_id": "t-1", "plan_id": uuid.UUID(int=1), "attempt": 2}
    artifact = {"a": 1}
    good_meta = {
        "trial_id": "t-1",
        "plan_id": str(uuid.UUID(int=1)),
        "attempt": 2,
        "verdict": "FAIL",
        "artifact": artifact,
        "artifact_digest": artifact_digest(artifact),
    }
    assert bound_problem({**good_meta, "attempt": 1}, row) == "verdict_unbound:attempt"  # previous attempt (V5)
    assert bound_problem({**good_meta, "trial_id": "t-2"}, row) == "verdict_unbound:trial"  # other Trial (V5)
    assert bound_problem({**good_meta, "artifact": {"a": 2}}, row) == "verdict_unbound:artifact"  # tampered (V5)
    assert bound_problem(good_meta, row) == "verdict_unbound:verifier"  # no verifier record -> not accepted


def test_infra_error_cap_sends_plan_to_needs_human(console: Console, pg: dict[str, str], api_settings: Settings) -> None:
    """The real failure mode: the rehearsal service restarted and no longer knows the Trial (404)."""
    pid = console.plan()
    with psycopg.connect(pg["owner"], autocommit=True) as c:
        c.execute("UPDATE plans SET state = 'PROPOSE' WHERE plan_id = %s", (pid,))
        row0 = c.execute("SELECT id FROM principals WHERE email = 'operator@bravo.test'").fetchone()
        assert row0 is not None
        principal = row0[0]

    async def run() -> list[str]:
        db = Database(pg["bravo_api"], 1, 2)
        await db.open()
        states = []
        try:
            for n in range(1, 4):
                rid = uuid.uuid4()
                async with db.tenant_tx("t1", principal) as conn:
                    await conn.execute(
                        """INSERT INTO rehearsals (rehearsal_id, tenant_id, plan_id, attempt, task, role, idem_key,
                                                   status, trial_id, requested_by, channel)
                           VALUES (%s, 't1', %s, %s, 'fix-bucket-policy', 'governance', %s, 'running', %s, %s,
                                   'console')""",
                        (rid, pid, n, f"rehearsal-{rid}", f"t-lost-{n}", principal),
                    )
                    await conn.execute("UPDATE plans SET active_rehearsal_id = %s WHERE plan_id = %s", (rid, pid))
                    row = await _row(conn, rid)
                    plan = await settle(conn, api_settings, row, {"verdict": "INFRA_ERROR", "reason": "trial_lost"})
                    states.append(plan["state"])
        finally:
            await db.close()
        return states

    assert asyncio.run(run()) == ["PROPOSE", "PROPOSE", "NEEDS_HUMAN"]  # cap 2 (07 3)
    plan = console.get("viewer", f"/plans/{pid}").json()
    assert plan["state_reason"] == "rehearsal_infra_errors_exhausted" and plan["rehearsal"] == "needs_human"
    assert plan["rehearsal_attempts"]["infra_errors"] == 3 and plan["rehearsal_attempts"]["failed"] == 0


def test_rehearsal_start_over_operator_mcp_uses_mcp_role(
    api_settings: Settings, console: Console, keys: Keys, executor: ExecutorStub, pg: dict[str, str]
) -> None:
    """Same handler through operator-MCP (DB role ``bravo_mcp``): grants, RLS and idempotency policy."""
    from .conftest import MCP_AUD, email
    from .test_mcp import call, rpc

    executor.controls[0]["dst"] = "FAIL"
    pid = console.plan()
    _to_state(pg, pid, "GATE")
    console.post("operator", f"/plans/{pid}/gate")
    mcp_settings = replace(api_settings, mode=Mode.MCP, database_url=pg["bravo_mcp"])
    os.environ["BRAVO_REHEARSAL_API_TOKEN"] = SERVICE_TOKEN
    try:
        with TestClient(create_app(mcp_settings)) as mcp:
            op = keys.token(email("operator"), MCP_AUD)
            args = {"plan_id": pid, "task": "fix-bucket-policy", "role": "governance", "idempotency_key": f"m-r-{pid}"}
            out = call(mcp, op, "rehearsal_start", args)["result"]
            assert out["isError"] is False
            sc = out["structuredContent"]
            assert sc["status"] == "INFRA_ERROR" and sc["reason"] == "inventory_unavailable" and sc["state"] == "PROPOSE"
            again = call(mcp, op, "rehearsal_start", args)["result"]["structuredContent"]
            assert again == sc  # idempotent replay
            viewer = keys.token(email("viewer"), MCP_AUD)
            denied = rpc(mcp, viewer, "tools/call", {"name": "rehearsal_start", "arguments": args})
            assert denied.status_code == 403 and denied.json()["error"] == "insufficient_scope"
    finally:
        os.environ.pop("BRAVO_REHEARSAL_API_TOKEN", None)


# Collector inventory and repo snapshot (S2 envelope contract) --------------------------------------------

REPO_FILES = {
    # A direct Secrets Manager call: the collector grades it code_change (the S2 material, §8.10).
    "src/chat/secrets.py": (
        "import boto3\n\n"
        'client = boto3.client("secretsmanager")\n\n\n'
        "def db_password() -> str:\n"
        '    return client.get_secret_value(SecretId="chat/db")["SecretString"]\n'
    ),
    "src/chat/storage.py": (
        'import os\n\nimport boto3\n\ns3 = boto3.client("s3", endpoint_url=os.environ.get("S3_ENDPOINT_URL"))\n'
    ),
    "deploy/values.yaml": "image:\n  repository: chat\nservice:\n  port: 8000\n",
}


def _walk(root: Path) -> list[Path]:
    """platform/rehearsal ``snapshot.walk`` order: top-down, sorted directories and files, no symlinks."""
    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(dirnames)
        out.extend(Path(dirpath) / n for n in sorted(filenames))
    return out


def _tree_digest(root: Path) -> str:
    """platform/rehearsal ``snapshot.tree_digest`` (= the collector's ``input_digest`` for the same files)."""
    import hashlib

    h = hashlib.sha256()
    for path in _walk(root):
        h.update(f"{path.relative_to(root).as_posix()}\0{hashlib.sha256(path.read_bytes()).hexdigest()}\n".encode())
    return h.hexdigest()


def _tar_gz(root: Path) -> bytes:
    import io
    import tarfile

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for path in _walk(root):
            info = tarfile.TarInfo(path.relative_to(root).as_posix())
            data = path.read_bytes()
            info.size, info.mode = len(data), 0o644
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


@pytest.fixture(scope="module")
def customer_repo(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("customer-repo")
    for rel, text in REPO_FILES.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    return root


@pytest.fixture(scope="module")
def collector_service(tmp_path_factory: pytest.TempPathFactory, customer_repo: Path) -> Iterator[str]:
    """The real platform/collector (``python -m bravo_collector serve``) scanning ``customer_repo``."""
    if not COLLECTOR_PY.is_file():
        pytest.skip(f"collector_venv_missing: {COLLECTOR_PY.relative_to(COLLECTOR_DIR.parent)}")
    port = free_port()
    envs = tmp_path_factory.mktemp("collector") / "environments.yaml"
    envs.write_text("environments: {}\n")
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(tmp_path_factory.mktemp("collector-home")),
        "BRAVO_ENVIRONMENTS_FILE": str(envs),
        "BRAVO_INVENTORY_ROOT": str(customer_repo),
        "BRAVO_COLLECTOR_HOST": "127.0.0.1",
        "BRAVO_COLLECTOR_PORT": str(port),
        "LOG_LEVEL": "warning",
    }
    proc = subprocess.Popen(
        [str(COLLECTOR_PY), "-m", "bravo_collector", "serve"],
        env=env,
        cwd=COLLECTOR_DIR,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
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
                out = proc.stdout.read().decode() if proc.stdout and proc.poll() is not None else ""
                raise RuntimeError(f"collector did not start: {out[-2000:]}")
            time.sleep(0.2)
        yield url
    finally:
        proc.terminate()
        proc.wait(10)


@pytest.fixture(scope="module")
def collector_with_snapshot(collector_service: str, customer_repo: Path) -> str:
    """The real collector behind a thin front that adds the PROPOSED ``GET /v1/inventory/snapshot`` (the
    collector does not serve it yet). ``/v1/inventory`` is the real collector's answer, unchanged; the snapshot
    is the scanned files as tar.gz with the inventory's own ``input_digest`` in ``X-Bravo-Inventory-Digest``."""
    from fastapi import FastAPI, Response

    front = FastAPI()

    @front.get("/v1/inventory")
    def inventory() -> Response:
        r = httpx.get(f"{collector_service}/v1/inventory", timeout=10)
        return Response(r.content, status_code=r.status_code, media_type="application/json")

    @front.get("/v1/inventory/snapshot")
    def snapshot() -> Response:
        digest = httpx.get(f"{collector_service}/v1/inventory", timeout=10).json()["input_digest"]
        assert _tree_digest(customer_repo) == digest  # the front ships exactly the files the collector scanned
        return Response(_tar_gz(customer_repo), media_type="application/gzip", headers={"X-Bravo-Inventory-Digest": digest})

    return _serve(front)


def _client_call(coro_fn: Any) -> Any:
    return asyncio.run(coro_fn())


def test_coordinator_reasons_are_nothing_ran_preconditions() -> None:
    for reason in (
        *NOTHING_RAN_FROM_ENVELOPE,
        "inventory_unavailable",
        "snapshot_unavailable",
        "snapshot_digest_mismatch",
        "snapshot_inventory_mismatch",
    ):
        assert reason in PRECONDITIONS


def test_collector_client_accepts_the_real_inventory_v1(collector_service: str) -> None:
    async def run() -> dict[str, Any]:
        c = CollectorClient(collector_service, 10)
        try:
            return await c.inventory()
        finally:
            await c.aclose()

    inv = _client_call(run)
    assert inv["schema_version"] == "inventory/v1" and len(inv["input_digest"]) == 64
    grades = {f["tier_code"] for f in inv["findings"]}
    assert "code_change" in grades  # secretsmanager get_secret_value in src/chat/secrets.py
    assert any(f["file"] == "src/chat/secrets.py" for f in inv["findings"])


def test_collector_client_rejects_the_old_dependencies_shape() -> None:
    old = {"collected_at": "x", "dependencies": [{"id": "d1", "kind": "secret", "grade": "code_change"}]}

    async def run() -> str:
        transport = httpx.MockTransport(lambda _r: httpx.Response(200, json=old))
        c = CollectorClient("http://collector.test", 2, transport=transport)
        try:
            await c.inventory()
        except UpstreamError as exc:
            return exc.reason
        finally:
            await c.aclose()
        return "accepted"

    assert _client_call(run) == "collector_bad_response"


def test_real_collector_snapshot_reaches_the_real_rehearsal_service(
    api_settings: Settings, keys: Keys, executor: ExecutorStub, pg: dict[str, str], collector_service: str
) -> None:
    """The collector now serves ``GET /v1/inventory/snapshot`` (2026-11-02 evening): the real snapshot is
    uploaded and the Trial request reaches the real rehearsal service, which refuses before running anything
    (no model key on this machine). Nothing is counted."""
    os.environ["BRAVO_REHEARSAL_API_TOKEN"] = SERVICE_TOKEN
    try:
        with TestClient(create_app(replace(api_settings, collector_url=collector_service))) as client:
            executor.healthy()
            console = Console(client, keys)
            pid = console.plan()
            _to_state(pg, pid, "PROPOSE")
            r = console.post("operator", f"/plans/{pid}/rehearsals", {"task": "fix-bucket-policy", "role": "governance"})
            assert r.status_code == 202, r.text
            status, reason = r.json()["status"], r.json()["reason"]
            assert status == "INFRA_ERROR" and reason in {
                "llm_unconfigured",
                "cluster_unconfigured",
                "verifier_unconfigured",
            }
            plan = console.get("viewer", f"/plans/{pid}").json()
            assert plan["rehearsal_attempts"] == {"failed": 0, "infra_errors": 0, "max": 3}  # not counted
    finally:
        os.environ.pop("BRAVO_REHEARSAL_API_TOKEN", None)


def test_snapshot_upload_bound_to_the_inventory_digest(rehearsal_service: str, customer_repo: Path) -> None:
    archive, digest = _tar_gz(customer_repo), _tree_digest(customer_repo)
    plan = str(uuid.uuid4())

    async def run() -> tuple[dict[str, Any], str]:
        c = RehearsalClient(rehearsal_service, 10, SERVICE_TOKEN)
        try:
            manifest = await c.put_snapshot("t1", plan, archive, digest)
            try:
                await c.put_snapshot("t1", plan, archive, "0" * 64)  # not the files the collector scanned
                refused = "accepted"
            except UpstreamError as exc:
                refused = exc.reason
            return manifest, refused
        finally:
            await c.aclose()

    manifest, refused = _client_call(run)
    assert manifest["input_digest"] == digest and manifest["files"] == len(REPO_FILES)
    assert refused == "snapshot_digest_mismatch" and refused in PRECONDITIONS


def test_trial_request_envelope_passes_the_real_rehearsal_validation(rehearsal_service: str, collector_service: str) -> None:
    """The exact body the API sends (``trial_request``) against the real ``TrialRequest`` + ``StateEnvelope``
    checks. The service still refuses (no model key, no cluster), but never for the envelope itself."""

    async def run() -> dict[str, Any]:
        col = CollectorClient(collector_service, 10)
        reh = RehearsalClient(rehearsal_service, 10, SERVICE_TOKEN)
        try:
            inv = await col.inventory()
            gate = {
                "verdict_id": uuid.uuid4(),
                "controls": [
                    {"control_id": "DB-ANON-READ", "src": "PASS", "dst": "FAIL", "verdict": "BLOCK", "reason": "regression"},
                    {"control_id": "DB-AUTH", "src": "PASS", "dst": "PASS", "verdict": "PASS", "reason": None},
                ],
            }
            body = trial_request(
                plan_id=uuid.uuid4(),
                tenant_id="t1",
                rehearsal_id=uuid.uuid4(),
                attempt=2,
                max_attempts=3,
                task="fix-bucket-policy",
                role="governance",
                state="PROPOSE",
                prior=prior_failures([{"attempt": 1, "reason": "verifier: anonymous GET allowed"}]),
                evidence=evidence_from(inv, gate),
                source_env="src-aws",
                target_env="target-onprem",
                analysis={"inventory": {"status": "ok"}, "gaps": []},
                inventory_digest=inv["input_digest"],
            )
            try:
                await reh.start(body, f"contract-{uuid.uuid4().hex[:8]}")
            except UpstreamError as exc:
                return {"body": body, "status": exc.status, "error": exc.reason, "missing": (exc.body or {}).get("missing")}
            return {"body": body, "status": 202, "error": None, "missing": []}
        finally:
            await col.aclose()
            await reh.aclose()

    out = _client_call(run)
    body = out["body"]
    assert set(body) == {
        "plan_id",
        "tenant_id",
        "rehearsal_id",
        "attempt",
        "max_attempts",
        "task",
        "role",
        "state",
        "prior_failures",
        "evidence",
        "source_env",
        "target_env",
        "analysis",
        "inventory_digest",
    }
    ids = [e["id"] for e in body["evidence"]]
    assert ids[0].startswith("gate:") and ids[0].endswith(":db-anon-read") and len(ids) == len(set(ids))
    assert any(i.startswith("inv:") for i in ids)
    assert body["prior_failures"] == [
        {
            "attempt": 1,
            "verdict": "FAIL",
            "reason": "verifier_anonymous_get_allowed",
            "summary": "attempt 1: verifier verdict FAIL (verifier: anonymous GET allowed)",
        }
    ]
    # Accepted shape: a 503 precondition refusal (no model key here), never 422 bad_request or an envelope reason.
    assert out["status"] == 503 and out["error"] in PRECONDITIONS, out
    assert not set(out["missing"]) & set(NOTHING_RAN_FROM_ENVELOPE), out["missing"]


def test_rehearsal_start_uploads_the_snapshot_then_requests_the_trial(
    api_settings: Settings,
    keys: Keys,
    executor: ExecutorStub,
    pg: dict[str, str],
    collector_with_snapshot: str,
    snapshot_dir: Path,
    customer_repo: Path,
) -> None:
    """End to end through the API handler: inventory -> snapshot PUT (verified by the real rehearsal service) ->
    Trial request with the envelope fields -> the service's precondition (no model key), not counted."""
    os.environ["BRAVO_REHEARSAL_API_TOKEN"] = SERVICE_TOKEN
    try:
        with TestClient(create_app(replace(api_settings, collector_url=collector_with_snapshot))) as client:
            executor.healthy()
            console = Console(client, keys)
            pid = console.plan()
            _to_state(pg, pid, "PROPOSE")
            r = console.post("operator", f"/plans/{pid}/rehearsals", {"task": "fix-bucket-policy", "role": "governance"})
            assert r.status_code == 202, r.text
            out = r.json()
            assert out["status"] == "INFRA_ERROR" and out["reason"] in PRECONDITIONS, out
            assert out["reason"] not in ("inventory_unavailable", "snapshot_unavailable", *NOTHING_RAN_FROM_ENVELOPE)
            assert out["attempt"] == 1
            plan = console.get("viewer", f"/plans/{pid}").json()
            assert plan["rehearsal_attempts"] == {"failed": 0, "infra_errors": 0, "max": 3}
    finally:
        os.environ.pop("BRAVO_REHEARSAL_API_TOKEN", None)
    manifest = json.loads((snapshot_dir / "t1" / pid / "manifest.json").read_text())
    assert manifest["input_digest"] == _tree_digest(customer_repo) and manifest["files"] == len(REPO_FILES)


def test_prior_failure_names_the_failed_checks_and_what_was_observed() -> None:
    """S2 4: the next attempt sees which verifier check failed and what the store answered, not just a reason code."""
    verifier = {"checks": [{"name": "behavioral:gate_OBJ-01", "ok": True, "detail": "PASS"},
                           {"name": "behavioral:gate_OBJ-06", "ok": False,
                            "detail": "UNKNOWN plain_http_inconclusive error 400 InvalidRequest"}]}
    [p] = prior_failures([{"attempt": 1, "reason": "verifier:behavioral:gate_OBJ-06", "verifier": verifier}])
    assert p["reason"] == "verifier_behavioral_gate_obj_06"
    assert p["summary"].endswith("failed checks: behavioral:gate_OBJ-06: UNKNOWN plain_http_inconclusive error "
                                 "400 InvalidRequest")
    assert len(p["summary"]) <= 500
