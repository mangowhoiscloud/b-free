# Same logic as `python -m bravo_executor.attachment_copy --from src --to dst` in topology mode (next image);
# piped into executor-0 because image 98f57c71's CLI reads only the BRAVO_ATT_* env form.
import asyncio, json, os, sys, time
from pathlib import Path
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool
from bravo_executor.attachment_copy import AttachmentCopier, ObjectStore
from bravo_executor.topology import FileSecrets, settings_from_topology

async def main() -> int:
    secrets = FileSecrets(Path(os.environ["BRAVO_SECRETS_DIR"]))
    s = await settings_from_topology(Path(os.environ["BRAVO_TOPOLOGY_FILE"]), secrets.field,
                                     os.environ["PLATFORM_DB_DSN"], require_entries=False)
    stores = dict(s.object_stores)
    pool = AsyncConnectionPool(s.platform_dsn, connection_class=AsyncConnection, min_size=1, max_size=2, open=False)
    await pool.open(wait=True, timeout=30)
    try:
        copier = AttachmentCopier(pool, ObjectStore(stores["src"]), ObjectStore(stores["dst"]))
        t0 = time.monotonic()
        async with await AsyncConnection.connect(s.src_dsn, autocommit=True) as chat:
            report = await copier.run(chat)
        print(json.dumps({"ms": round((time.monotonic() - t0) * 1000), **report.as_dict()}))
        return 0 if report.complete else 1
    finally:
        await pool.close()

sys.exit(asyncio.run(main()))
