import asyncio, json, os
from pathlib import Path
from bravo_executor.front import Cloudflare
from bravo_executor.topology import FileSecrets


async def main():
    cf = await Cloudflare.from_broker(FileSecrets(Path(os.environ["BRAVO_SECRETS_DIR"])))
    try:
        pools = await cf._call("GET", f"/accounts/{cf.account_id}/load_balancers/pools")
        for p in pools:
            if not p["name"].startswith("bravo-b-free-chat"):
                continue
            h = await cf._call("GET", f"/accounts/{cf.account_id}/load_balancers/pools/{p['id']}/health")
            pop = (h or {}).get("pop_health") or {}
            print(p["name"], json.dumps({k: v for k, v in pop.items() if any(x in k for x in ("Seoul", "Incheon", "Tokyo", "Osaka", "Busan"))}))
            continue
            print(json.dumps({"pool": p["name"], "healthy": p.get("healthy"), "origins": p.get("origins") and [
                {"name": o.get("name"), "enabled": o.get("enabled"), "weight": o.get("weight")} for o in p["origins"]],
                "health": h})[:4000])
        lbs = await cf._call("GET", f"/zones/{cf.zone_id}/load_balancers")
        for lb in lbs:
            if lb["name"].startswith("b-free-chat"):
                print(json.dumps({k: lb.get(k) for k in ("name", "steering_policy", "session_affinity",
                                  "session_affinity_attributes", "random_steering", "adaptive_routing",
                                  "location_strategy", "proxied", "ttl", "fallback_pool", "default_pools")}))
    finally:
        await cf.close()


asyncio.run(main())
