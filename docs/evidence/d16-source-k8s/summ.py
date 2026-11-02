import json, sys
kind = sys.argv[1]
d = json.load(sys.stdin)
if kind == "clusters":
    print("observed_at", d.get("observed_at"), "not_configured", d.get("not_configured"))
    for c in d["clusters"]:
        lz = [r["component"] for r in c.get("landing_zone", []) if r.get("present")]
        ns = [n["name"] for n in c.get("namespaces", [])]
        print("cluster=%s status=%s error=%s server=%s nodes=%s namespaces=%d chat_ns=%s workloads=%d pods=%s lz_present=%d"
              % (c["cluster_id"], c["status"], c["error"], c["server_version"],
                 [(n["name"], n["ready"]) for n in c["nodes"]], len(ns), "chat" in ns, len(c["workloads"]), c["pods"], len(lz)))
elif kind == "snapshot":
    k = d["sources"]["kubernetes"]
    print("kubernetes", k["status"], "collected_at", d["collected_at"])
    for name, item in (k.get("data") or {}).items():
        if isinstance(item, dict) and "status" in item:
            v = item.get("value")
            if isinstance(v, dict) and "git_version" in v:
                brief = v["git_version"]
            elif isinstance(v, (list, dict)):
                brief = "%d entries" % len(v)
            else:
                brief = v
            print("  %s: %s %s %s" % (name, item["status"], str(item.get("error") or "")[:120], brief))
elif kind == "inventory":
    print("files_scanned", d.get("files_scanned"), "rules_version", d.get("rules_version"))
elif kind == "gate":
    print("collected_at", d["collected_at"], "gate_verdict", d["gate_verdict"])
    for q in d["questions"]:
        if q["control_id"] in ("SEC-SECRET-READ-K8S", "NET-DEFAULT-DENY", "DR-RESTORE"):
            print("  %s: src=%s reason=%s" % (q["control_id"], q["src"], q["src_evidence"].get("reason")))
