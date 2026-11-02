"""All traffic to one environment in one step without revert (front_api.move_front, the failback step 8 code),
recorded as a shift run in the platform ledger. Used to put the LB back on the source after measurements."""
import asyncio, json, os, sys, time, uuid
from pathlib import Path
from types import SimpleNamespace

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from bravo_executor.front_api import move_front
from bravo_executor.ledger import PostgresLedger, RunRequest, RunStatus
from bravo_executor.stages import EffectRunner, StageRecorder
from bravo_executor.topology import FileSecrets


async def main(target: str) -> None:
    pool = AsyncConnectionPool(os.environ["PLATFORM_DB_DSN"], connection_class=AsyncConnection, min_size=1,
                               max_size=3, open=False)
    await pool.open(wait=True, timeout=30)
    try:
        ledger = PostgresLedger(pool)
        app = SimpleNamespace(state=SimpleNamespace(secret_broker=FileSecrets(Path(os.environ["BRAVO_SECRETS_DIR"]))))
        key = f"shift-move-{target}-{uuid.uuid4()}"
        plan_id = uuid.uuid5(uuid.NAMESPACE_URL, f"bravo:front-shift:{key}")
        run_id, _ = await ledger.create_or_get_run(RunRequest(plan_id=plan_id, epoch=0, idem_key=key, kind="shift"))
        rec, fx = StageRecorder(ledger, run_id), EffectRunner(ledger, run_id, 0)
        t0 = time.monotonic()
        status = RunStatus.DONE
        try:
            async with rec.stage("front.preflight") as st:
                st.detail.update({"origin": "tool", "approval_id": None, "note": "move without revert"})
            async with rec.stage("front.summary") as st:
                st.detail.update(await move_front(app, target, fx, rec))
        except BaseException:
            status = RunStatus.SAFE_HOLD
            raise
        finally:
            await ledger.finish_run(run_id, status, None)
        print(json.dumps({"run_id": str(run_id), "target_env": target, "wall_ms": int((time.monotonic() - t0) * 1000)}))
    finally:
        await pool.close()


asyncio.run(main(sys.argv[1]))
