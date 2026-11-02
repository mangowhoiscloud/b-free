"""Steering experiment (not a step judgement on writes): the same tool-path shift, but each sample is
GET https://<front>/api/status (no DB write, fast on both environments). Shows which share of requests
Cloudflare sends to each pool per weight. Recorded in the ledger as a normal shift run."""
import sys
import time

import bravo_executor.front as front
from bravo_executor.front import Sample

async def once(self, t0):  # replaces WriteSampler.once for this experiment only
    self.sent += 1
    start = time.monotonic()
    try:
        resp = await self._http.get(self.url.replace(f"/api/rooms/{self.cfg.write_room}/messages", "/api/status"),
                                    params={"_s": f"{start:.6f}"})
        env = resp.headers.get("x-served-by") or None
        return Sample(start - t0, resp.status_code == 200 and env is not None, (time.monotonic() - start) * 1000,
                      env, resp.status_code)
    except Exception:  # noqa: BLE001
        return Sample(start - t0, False, (time.monotonic() - start) * 1000, None, None)

front.WriteSampler.once = once
sys.argv = [sys.argv[0], *sys.argv[1:]]
exec(open("/tmp/shift-src/measure_shift.py").read())
