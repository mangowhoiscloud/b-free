import asyncio, json, sys, time, uuid, collections
import psycopg
port, sc, retry = int(sys.argv[1]), sys.argv[2], sys.argv[3]
APP = f"host=127.0.0.1 port={port} dbname=chat user=postgres password=<가림>"
ADM = f"host=127.0.0.1 port={port} dbname=pgbouncer user=pgb_admin password=<가림>"
stats = collections.Counter(); errors = collections.Counter(); lat = []
stop = asyncio.Event()
async def worker():
    conn = None
    while not stop.is_set():
        k = uuid.uuid4(); t = time.monotonic()
        for attempt in range(50):
            try:
                if conn is None or conn.closed:
                    conn = await psycopg.AsyncConnection.connect(APP, autocommit=True, prepare_threshold=None)
                await conn.execute("INSERT INTO m (k, body) VALUES (%s, 'x') ON CONFLICT (k) DO NOTHING", (k,))
                stats["ok"] += 1; lat.append((time.monotonic()-t)*1000); break
            except psycopg.Error as e:
                errors[str(e).splitlines()[0][:90]] += 1; stats["err_attempts"] += 1
                try: await conn.close()
                except Exception: pass
                conn = None; await asyncio.sleep(0.05)
        else:
            stats["failed_requests"] += 1
        await asyncio.sleep(0.02)
GUARD = __import__("os").environ.get("GUARD") == "1"
states_seen = collections.Counter(); guard_waits = []
async def no_logins():
    t = time.monotonic()
    async with await psycopg.AsyncConnection.connect(ADM, autocommit=True, prepare_threshold=None) as c:
        while True:
            cur = await c.execute(b"SHOW SERVERS")
            cols = [d.name for d in cur.description]
            rows = [dict(zip(cols, r)) for r in await cur.fetchall()]
            for r in rows: states_seen[r.get("state")] += 1
            if not any(r.get("state") in ("login", "new") and r.get("database") == "chat" for r in rows) or time.monotonic() - t > 5:
                guard_waits.append(round((time.monotonic()-t)*1000,1)); return
            await asyncio.sleep(0.02)
async def admin(cmd):
    async with await psycopg.AsyncConnection.connect(ADM, autocommit=True, prepare_threshold=None) as c:
        await c.execute(cmd.encode())
async def main():
    ws = [asyncio.create_task(worker()) for _ in range(4 if sc == 'spike' else 16)]
    await asyncio.sleep(4)
    pauses = []
    rounds, gap, hold = {"rapid": (10, 1.5, 0.3), "midlogin": (10, 0.4, 0.3), "single": (4, 5.0, 0.3),
                         "spike": (20, 0.3, 0.0)}[sc]
    for _ in range(rounds):
        if GUARD: await no_logins()
        t = time.monotonic(); await admin("PAUSE chat"); pauses.append(round((time.monotonic()-t)*1000,1))
        if hold: await asyncio.sleep(hold)
        await admin("RESUME chat"); await asyncio.sleep(gap)
    stop.set(); await asyncio.gather(*ws)
    lat.sort()
    print(json.dumps({"server_login_retry": retry, "scenario": sc, "pause_ms": pauses, **stats,
                      "max_latency_ms": round(lat[-1],1) if lat else None,
                      "p99_ms": round(lat[int(len(lat)*0.99)-1],1) if lat else None, "errors": dict(errors), "guard": GUARD, "guard_wait_ms_max": max(guard_waits) if guard_waits else None,
                      "server_states_seen": dict(states_seen)}))
asyncio.run(main())
