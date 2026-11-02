"""Platform-held probe of the app verification Job (S2 P0 code fix, B05/P11). Runs in the Job Pod, standard library only.

The Pod holds the synthetic DB, the PATCHED app (agent's diff applied to the customer snapshot) and the ORIGINAL app
(the snapshot as uploaded), both on the destination's current app image and both given only the landing-zone
interface: the DB password as a file at ``DB_PASSWORD_FILE``, synced by ESO from the platform OpenBao. This probe:

0. egress: TCP to the internet and the metadata address must fail (default-deny namespace);
1. waits for the patched app's ``/readyz`` (it read the write DB with the injected password);
2. sends one chat message over HTTP (``POST /api/rooms/<room>/messages``, new idempotency key): 201 ``created``;
   sends the same key again: 200 ``created: false`` with the same message id (the row is read back from the DB);
3. control: the original app (Secrets Manager mode selector, no landing-zone support of its own) must NOT become
   ready under the same interface. If it did, the test could not tell the fix from no fix.

The last stdout line is one JSON object; the service reads it from the container log. No secret is printed.
"""

from __future__ import annotations

import json
import socket
import sys
import time
import urllib.error
import urllib.request
import uuid

PATCHED, ORIGINAL = "http://127.0.0.1:8000", "http://127.0.0.1:8001"


def egress(targets: list) -> list[dict]:
    out = []
    for host, port in targets:
        try:
            socket.create_connection((host, int(port)), 3).close()
            out.append({"target": f"{host}:{port}", "blocked": False})
        except OSError as exc:
            out.append({"target": f"{host}:{port}", "blocked": True, "error": type(exc).__name__})
    return out


def http(method: str, url: str, body: dict | None = None, headers: dict | None = None) -> tuple[int, dict | str]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            raw = r.read(65536).decode("utf-8", "replace")
            status = r.status
    except urllib.error.HTTPError as exc:
        raw, status = exc.read(4096).decode("utf-8", "replace"), exc.code
    except (OSError, urllib.error.URLError) as exc:
        return 0, type(getattr(exc, "reason", exc)).__name__
    try:
        return status, json.loads(raw)
    except ValueError:
        return status, raw[:200]


def wait_ready(base: str, seconds: float) -> dict:
    end, last = time.monotonic() + seconds, (0, "not_tried")
    t0 = time.monotonic()
    while time.monotonic() < end:
        last = http("GET", base + "/readyz")
        if last[0] == 200:
            return {"ready": True, "after_s": round(time.monotonic() - t0, 1), "readyz": last[1]}
        time.sleep(1)
    return {"ready": False, "after_s": round(time.monotonic() - t0, 1), "last": [last[0], last[1]]}


def run(p: dict) -> dict:
    out: dict = {"run": p["run"], "egress": egress(p.get("egress_canaries") or [])}
    patched = wait_ready(PATCHED, float(p.get("ready_s", 120)))
    out["patched_ready"] = patched
    checks = [{"name": "app_ready_with_injected_password", "ok": patched["ready"],
               "detail": f"readyz after {patched['after_s']} s" if patched["ready"] else f"not ready: {patched['last']}"}]
    if patched["ready"]:
        room, key = p.get("room", "verify-1"), str(uuid.uuid4())
        msg = {"author": "bravo-verify", "body": f"rehearsal {p['run']} write check"}
        s1, b1 = http("POST", f"{PATCHED}/api/rooms/{room}/messages", msg, {"Idempotency-Key": key})
        s2, b2 = http("POST", f"{PATCHED}/api/rooms/{room}/messages", msg, {"Idempotency-Key": key})
        id1 = (b1.get("message") or {}).get("id") if isinstance(b1, dict) else None
        id2 = (b2.get("message") or {}).get("id") if isinstance(b2, dict) else None
        out["write"] = {"first": {"status": s1, "created": b1.get("created") if isinstance(b1, dict) else None,
                                  "message_id": id1},
                        "repeat": {"status": s2, "created": b2.get("created") if isinstance(b2, dict) else None,
                                   "message_id": id2}}
        checks.append({"name": "chat_write_committed", "ok": s1 == 201 and isinstance(b1, dict) and b1.get("created") is True
                       and id1 is not None, "detail": f"POST {s1} message_id={id1}"})
        checks.append({"name": "chat_write_read_back", "ok": s2 == 200 and isinstance(b2, dict)
                       and b2.get("created") is False and id2 == id1, "detail": f"same key POST {s2} message_id={id2}"})
    # Control: the original code under the same interface. Given the patched app the same time budget first.
    original = wait_ready(ORIGINAL, float(p.get("control_s", 20)))
    out["control"] = {"original_ready": original["ready"], "detail": original, "ok": not original["ready"]}
    out["checks"] = checks
    return out


def main() -> int:
    with open("/cfg/params.json", encoding="utf-8") as f:
        p = json.load(f)
    try:
        result = run(p)
    except Exception as exc:  # noqa: BLE001 - recorded as an error, never as a pass
        result = {"run": p.get("run"), "error": f"probe_exception:{type(exc).__name__}"}
    print(json.dumps(result, sort_keys=True, default=str), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
