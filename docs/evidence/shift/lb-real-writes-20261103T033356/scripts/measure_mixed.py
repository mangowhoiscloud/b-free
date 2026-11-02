"""Steering experiment: one tool-path shift whose samples alternate POST (chat write) and GET /api/status, plus
variants of the POST (keep-alive or a new connection each). Prints which pool answered each kind."""
import json, os, sys, time, uuid
from collections import Counter

import httpx
import bravo_executor.front as front
from bravo_executor.front import Sample

SEEN: list[tuple[str, str | None, int | None]] = []
_orig_collect = front.WriteSampler.collect

async def once(self, t0):
    self.sent += 1
    kind = ("post-ka", "get-ka", "post-new", "get-new")[self.sent % 4]
    start = time.monotonic()
    try:
        if kind.endswith("new"):
            async with httpx.AsyncClient(timeout=10) as c:
                resp = await (c.post(self.url, params={"_s": f"m-{self.sent}"}, json={"author": "bravo-shift", "body": f"shift sample mixed {self.sent}"},
                                     headers={"Idempotency-Key": str(uuid.uuid4())}) if kind.startswith("post")
                              else c.get(self.url.split("/api/rooms")[0] + "/api/status", params={"_s": f"m-{self.sent}"}))
        else:
            resp = await (self._http.post(self.url, params={"_s": f"m-{self.sent}"}, json={"author": "bravo-shift", "body": f"shift sample mixed {self.sent}"},
                                          headers={"Idempotency-Key": str(uuid.uuid4())}) if kind.startswith("post")
                          else self._http.get(self.url.split("/api/rooms")[0] + "/api/status", params={"_s": f"m-{self.sent}"}))
        env = resp.headers.get("x-served-by") or None
        SEEN.append((kind, env, resp.status_code))
        return Sample(start - t0, resp.status_code in (200, 201) and env is not None, (time.monotonic() - start) * 1000, env, resp.status_code)
    except Exception:  # noqa: BLE001
        SEEN.append((kind, None, None))
        return Sample(start - t0, False, (time.monotonic() - start) * 1000, None, None)

front.WriteSampler.once = once
import atexit
atexit.register(lambda: print(json.dumps({"by_kind": {k: dict(Counter(str(e) for kk, e, _ in SEEN if kk == k)) for k in ("post-ka", "get-ka", "post-new", "get-new")}})))
exec(open("/tmp/shift-src/measure_shift.py").read())
