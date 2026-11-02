"""One tool-path shift (contract 15 §10.9) with the committed bravo_executor code, run inside executor-0.

Same code path as POST /v1/front/shift without a plan: the run, its stages and effects go to the platform
ledger (runs.kind = shift). Secrets stay in the pod (FileSecrets). Prints the run id and wall time only."""
import asyncio
import json
import os
import sys
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from bravo_executor.front import FrontConfig
from bravo_executor.front_api import ShiftBody, _run_shift
from bravo_executor.ledger import PostgresLedger, RunRequest
from bravo_executor.topology import FileSecrets


async def main(target: str, label: str) -> None:
    cfg = FrontConfig.from_env()
    pool = AsyncConnectionPool(os.environ["PLATFORM_DB_DSN"], connection_class=AsyncConnection, min_size=1,
                               max_size=3, open=False)
    await pool.open(wait=True, timeout=30)
    try:
        ledger = PostgresLedger(pool)
        running = await ledger.running_run_ids()
        if running:
            raise SystemExit(f"refused: run {running[0]} is RUNNING")
        app = SimpleNamespace(state=SimpleNamespace(
            service=SimpleNamespace(ledger=ledger, observe_only=False),
            secret_broker=FileSecrets(Path(os.environ["BRAVO_SECRETS_DIR"]))))
        key = f"shift-measure-{label}-{uuid.uuid4()}"
        plan_id = uuid.uuid5(uuid.NAMESPACE_URL, f"bravo:front-shift:{key}")
        run_id, _ = await ledger.create_or_get_run(RunRequest(plan_id=plan_id, epoch=0, idem_key=key, kind="shift"))
        t0 = time.monotonic()
        steps = [int(x) for x in os.environ.get("SHIFT_STEPS", "10,50,100").split(",")]
        await _run_shift(app, run_id, ShiftBody(target_env=target, steps=steps), cfg, None)
        print(json.dumps({"run_id": str(run_id), "idem_key": key, "target_env": target,
                          "wall_ms": int((time.monotonic() - t0) * 1000),
                          "config": {"write_rate": cfg.write_rate, "min_target_samples": cfg.min_target_samples,
                                     "min_hold_s": cfg.min_hold_s, "max_hold_s": cfg.max_hold_s,
                                     "max_error_rate": cfg.max_error_rate, "max_p95_ms": cfg.max_p95_ms}}))
    finally:
        await pool.close()


asyncio.run(main(sys.argv[1], sys.argv[2]))
