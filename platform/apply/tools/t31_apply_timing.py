"""Time the post-approval half of T31 (the S2 scene after the human approves), on a REHEARSAL target only.

Same step functions as ``pipeline.apply`` (image.locate -> destination.connect -> policies.scope -> staging.push ->
image.sign (cosign, OpenBao transit) -> signature.verify -> destination.import -> deploy.precheck -> deploy.rollout ->
deploy.verify -> policies.apply), each recorded by ``run_stage`` like the worker does, against:

- a verification namespace on the on-prem cluster (``--namespace``, default ``verify-t31timing``) holding a copy of the
  app: a synthetic DB (destination PostgreSQL image, the repo's schema) and Deployment ``chat-bravo-demo`` (2 replicas,
  the destination's current app image) whose DB password comes from the platform OpenBao through ESO;
- the rehearsal test bucket ``bravo-rehearsal-verify`` (not the attachment bucket);
- OpenBao policy names under ``app-target-onprem-t31timing-`` (deleted at the end).

What is NOT run: ``approval.verify`` (it reads the change approval row from the platform DB; this run has none and makes
none: approvals are human-only). The gate re-check is timed separately as the API does it (``POST /v1/controls/report``
from the API Pod). Nothing here touches namespace ``chat``, bucket ``bravo-demo-attachments`` or a real ``app-*`` policy.

    cd platform/apply && PYTHONPATH=src ../rehearsal/.venv/bin/python tools/t31_apply_timing.py --artifacts <run.jsonl> \
        --out <dir> --image-digest <prepared digest>   (the rehearsal venv has the kubernetes client the shared code needs)
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
import time
from pathlib import Path
from typing import Any

from bravo_apply import build, config, digests, policies, supply, targets, tools
from bravo_apply.destination import K3sContainerd
from bravo_apply.runtime import MemoryRecorder, Result, Step, run_stage

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "platform/rehearsal/src"))
from bravo_rehearsal import behavioral, behavioral_app  # noqa: E402  (render, eso objects, DB init: same code)

TEST_BUCKET, TEST_OWNER = "bravo-rehearsal-verify", "arn:aws:iam:::user/bravo-rehearsal-verify"
APP_PRINCIPAL = "arn:aws:iam:::user/obc-chat-bravo-demo-attachments-b292e551-66ce-442d-b51b-e5865a628e1b"


def setup_namespace(k: K3sContainerd, ns: str, app_image: str, db_image: str, bao: targets.CustodyBao) -> dict:
    """The app copy the rollout runs against: DB + ESO secret + Deployment at the destination's current image."""
    t0 = time.monotonic()
    labels = {"pod-security.kubernetes.io/enforce": "restricted", "bravo.dev/purpose": "t31-apply-timing"}
    k("apply", "-f", "-", input_bytes=json.dumps({"apiVersion": "v1", "kind": "Namespace",
                                                   "metadata": {"name": ns, "labels": labels}}).encode())
    from bravo_rehearsal.cluster import baseline

    objs = [b for kind, b in baseline(ns, "t31timing", "t31timing") if kind in ("np",)]
    # ESO: one secret_id for rehearsal-eso-target-onprem (the custody token mints it, 4 uses, 120 s).
    with bao.pf as port:
        _, r1 = bao.call(port, "GET", "auth/approle/role/rehearsal-eso-target-onprem/role-id")
        _, r2 = bao.call(port, "POST", "auth/approle/role/rehearsal-eso-target-onprem/secret-id",
                         {"ttl": "120s", "num_uses": 4})
    ca = (Path.home() / ".bravo/custody/platform-rke2/ca.crt").read_text()
    objs += [b for _, b in behavioral_app.eso_objects("t-t31timing00", "https://openbao:8200", ca, r1["data"]["role_id"],
                                                     r2["data"]["secret_id"], "envs/target-onprem/rehearsal-app-db")]
    schema = {f"schema--{n}": (REPO / "apps/demo/schema/sql" / n).read_text()
              for n in ("01_schema.sql", "02_roles.sql", "03_src.sql")}
    # Same init as the verification Job, but the DB is its own Pod here (the app reaches it through a Service).
    db_init = behavioral_app.DB_INIT.replace(
        'exec postgres -D "$PGDATA" -c listen_addresses=127.0.0.1',
        'echo "host all all 0.0.0.0/0 scram-sha-256" >> "$PGDATA/pg_hba.conf"\n'
        'exec postgres -D "$PGDATA" -c listen_addresses=0.0.0.0')
    assert db_init != behavioral_app.DB_INIT
    objs.append({"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "db-init"},
                 "data": {**schema, "db-init.sh": db_init}})
    sec = lambda uid: {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]},  # noqa: E731
                       "readOnlyRootFilesystem": True, "runAsNonRoot": True, "runAsUser": uid, "runAsGroup": uid}
    pw = {"name": "db-password", "secret": {"secretName": "app-db", "defaultMode": 0o440,
                                             "items": [{"key": "password", "path": "password"}]}}
    db = {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "db"},
          "spec": {"replicas": 1, "selector": {"matchLabels": {"app": "db"}}, "template": {
              "metadata": {"labels": {"app": "db"}},
              "spec": {"automountServiceAccountToken": False, "terminationGracePeriodSeconds": 1,
                       "securityContext": {"runAsNonRoot": True, "fsGroup": 65532,
                                           "seccompProfile": {"type": "RuntimeDefault"}},
                       "containers": [{"name": "db", "image": db_image, "command": ["bash", "/cfg/db-init.sh"],
                                       "securityContext": sec(26),
                                       "readinessProbe": {"exec": {"command": ["pg_isready", "-h", "127.0.0.1"]},
                                                          "periodSeconds": 2},
                                       "volumeMounts": [{"name": "db-password", "mountPath": "/var/run/bravo/db"},
                                                        {"name": "cfg", "mountPath": "/cfg"},
                                                        {"name": "pgdata", "mountPath": "/var/lib/pg"},
                                                        {"name": "pgrun", "mountPath": "/var/run/pg"},
                                                        {"name": "tmp", "mountPath": "/tmp"}]}],  # noqa: S108
                       "volumes": [pw, {"name": "cfg", "configMap": {"name": "db-init", "items": [
                           {"key": f"schema--{n}", "path": f"schema/{n}"} for n in
                           ("01_schema.sql", "02_roles.sql", "03_src.sql")] + [{"key": "db-init.sh",
                                                                                "path": "db-init.sh"}]}},
                                   {"name": "pgdata", "emptyDir": {}}, {"name": "pgrun", "emptyDir": {}},
                                   {"name": "tmp", "emptyDir": {}}]}}}}
    db_svc = {"apiVersion": "v1", "kind": "Service", "metadata": {"name": "db"},
              "spec": {"selector": {"app": "db"}, "ports": [{"port": 5432}]}}
    env = {"APP_ENV_ID": "verify", "APP_ENV_LABEL": "verify", "APP_PORT": "8000", "ADMIN_PORT": "8081",
           "DB_DSN": "host=db port=5432 dbname=chat user=app_writer sslmode=disable",
           "DB_PASSWORD_SOURCE": "env-file", "DB_PASSWORD_FILE": "/var/run/bravo/db/password"}
    app = {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "chat-bravo-demo"},
           "spec": {"replicas": 2, "selector": {"matchLabels": {"app": "chat"}}, "template": {
               "metadata": {"labels": {"app": "chat"}},
               "spec": {"automountServiceAccountToken": False,
                        "securityContext": {"runAsNonRoot": True, "fsGroup": 65532,
                                            "seccompProfile": {"type": "RuntimeDefault"}},
                        "containers": [{"name": "app", "image": app_image, "imagePullPolicy": "IfNotPresent",
                                        "env": [{"name": k2, "value": v} for k2, v in env.items()],
                                        "securityContext": sec(10001),
                                        "readinessProbe": {"httpGet": {"path": "/readyz", "port": 8000},
                                                           "periodSeconds": 2},
                                        "volumeMounts": [{"name": "db-password", "mountPath": "/var/run/bravo/db"},
                                                         {"name": "tmp", "mountPath": "/tmp"}]}],  # noqa: S108
                        "volumes": [pw, {"name": "tmp", "emptyDir": {}}]}}}}
    k("-n", ns, "apply", "-f", "-", input_bytes=json.dumps({"apiVersion": "v1", "kind": "List", "items": objs}).encode())
    k("-n", ns, "wait", "--for=condition=Ready", "externalsecret/app-db", "--timeout=90s", timeout=120)
    k("-n", ns, "delete", "secret", "eso-approle", "--ignore-not-found")  # the secret_id is spent
    k("-n", ns, "apply", "-f", "-", input_bytes=json.dumps({"apiVersion": "v1", "kind": "List",
                                                              "items": [db, db_svc, app]}).encode())
    k("-n", ns, "rollout", "status", "deploy/db", "--timeout=180s", timeout=200)
    k("-n", ns, "rollout", "status", "deploy/chat-bravo-demo", "--timeout=180s", timeout=200)
    return {"namespace": ns, "app_image": app_image, "setup_s": round(time.monotonic() - t0, 1)}


def scoped_translations(gov_artifact: dict[str, Any], ns: str) -> list[dict[str, Any]]:
    """The PASS proposal's translations, pointed at the rehearsal targets (bucket, owner, namespace)."""
    out = []
    for t in copy.deepcopy(gov_artifact["translations"]):
        ttype = t["target"]["type"]
        if ttype == "rgw_bucket_policy":
            t["document"] = behavioral.render(t["document"], test_bucket=TEST_BUCKET,
                                              principal_map={APP_PRINCIPAL: TEST_OWNER})
        elif ttype in ("k8s_rbac", "external_secret"):
            for obj in policies.objects(t["document"]):
                obj.setdefault("metadata", {})["namespace"] = ns
                if obj.get("apiVersion") == "external-secrets.io/v1beta1":
                    # Run 03's proposal asked for v1beta1, which ESO 2.x no longer serves (T31 now refuses it at
                    # policies.dryrun and the verifier at external_secret_api_version). Timing uses the served v1.
                    obj["apiVersion"] = "external-secrets.io/v1"
        out.append(t)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--artifacts", required=True, help="driver JSONL with the governance and app_migration results")
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--namespace", default="verify-t31timing")
    ap.add_argument("--app-image", default="bravo.local/bravo-demo@sha256:76e8f595d90267b3d740fdf9012cdf5c93d6337faf63e3601a1da3b2a65c262d")
    ap.add_argument("--db-image", default="ghcr.io/cloudnative-pg/postgresql:16.15-system-bookworm"
                                          "@sha256:639ef58e1ecca2ecb91ba6aa52f55a25fc59dd1792df99f2e99d81d85043a8d6")
    ap.add_argument("--image-digest", required=True, help="the prepared (approvable) image digest")
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    views = {}
    for line in Path(a.artifacts).read_text().splitlines():
        d = json.loads(line)
        if d.get("kind") == "result":
            views[d["view"]["role"]] = d["view"]
    cfg = config.load()
    lock = tools.load_lock(cfg.lock_path)
    real = cfg.destination("target-onprem")
    ns = a.namespace
    dest = {**real, "namespace": ns, "deployment": "chat-bravo-demo", "container": "app",
            "object_store": {"bucket": TEST_BUCKET, "secret": "envs/target-onprem/rehearsal-s3",
                             "rgw_service": "rook-ceph/rook-ceph-rgw-bravo"},
            "secret_store": {**real["secret_store"], "policy_prefix": "app-target-onprem-t31timing-"}}
    bao = targets.CustodyBao(lock["kubectl"].path)
    drv0 = K3sContainerd(dest, lock["kubectl"].path, lock["limactl"].path)
    setup = setup_namespace(drv0.k, ns, a.app_image, a.db_image, bao)
    rec = MemoryRecorder(a.out)
    A = "apply"
    t_all = time.monotonic()
    tar = build.image_path(cfg.cache_dir, a.image_digest)
    info = run_stage(rec, A, "image.locate", lambda: Step(value=build.oci_manifest_digest(tar),
                                                          detail={"digest": a.image_digest})).value
    drv = run_stage(rec, A, "destination.connect", lambda: Step(detail=drv0.reachable(), value=drv0)).value
    items = scoped_translations(views["governance"]["artifact"], ns)
    plan = run_stage(rec, A, "policies.scope", lambda: Step(
        value=policies.scoped(items, digests.policy_diff_digest({"translations": items}), dest),
        detail={"translations": len(items)})).value
    run_stage(rec, A, "policies.dryrun", lambda: policies.dry_run(drv.k, plan["k8s"], ns))
    reg = cfg.registry
    repo = "bravo/bravo-demo"
    staged = run_stage(rec, A, "staging.push", lambda: Step(value={**supply.ensure_registry(reg), **supply.push(
        lock["skopeo"].path, tar, reg["address"], repo, "t31timing", a.image_digest)})).value
    notes = {"bravo.dev/change-id": "t31-apply-timing", "bravo.dev/approval-id": "none-timing-run",
             "bravo.dev/target": f"{ns} (rehearsal copy)"}
    run_stage(rec, A, "image.sign", lambda: Step(value=supply.sign(
        cfg.path(cfg.signer["keys_sh"]), lock["cosign"].path, cfg.signer["key_ref"], staged["ref"], notes,
        cfg.path(cfg.signer["signing_config"]))))
    run_stage(rec, A, "signature.verify", lambda: Step(value=supply.verify(
        lock["cosign"].path, cfg.path(cfg.signer["public_key"]), staged["ref"], a.image_digest, notes)))
    run_stage(rec, A, "destination.import", lambda: drv.import_image(tar, a.image_digest, info["name"]))
    run_stage(rec, A, "deploy.precheck", lambda: drv.precheck(a.image_digest))
    run_stage(rec, A, "deploy.rollout", lambda: drv.rollout(a.image_digest, "bravo T31 apply timing (no approval)"))
    run_stage(rec, A, "deploy.verify", lambda: drv.verify(a.image_digest))
    applied = run_stage(rec, A, "policies.apply", lambda: policies.apply_plan(
        drv.k, plan, dest, lock["kubectl"].path, drv.ctx))
    total = round(time.monotonic() - t_all, 1)
    # Clean the rehearsal targets: test bucket policy, the timing OpenBao policies.
    cleanup = {}
    with bao.pf as port:
        rec_s3 = bao.call(port, "GET", "secret/data/envs/target-onprem/rehearsal-s3")[1]["data"]["data"]
    with targets.PortForward(lock["kubectl"].path, drv.ctx, "rook-ceph", "svc/rook-ceph-rgw-bravo", 443) as p443:
        rgw = targets.Rgw(p443, TEST_BUCKET, rec_s3["ca_pem"], rec_s3["access_key_id"], rec_s3["secret_access_key"])
        cleanup["test_bucket_policy_delete"] = rgw.policy_call("DELETE")[0]
        rgw.close()
    for item in (applied.detail.get("external") or {}).get("applied") or []:
        if item["type"] == "openbao_policy":
            with bao.pf as port:
                cleanup[item["policy"]] = bao.call(port, "DELETE", f"sys/policies/acl/{item['policy']}")[0]
    summary = {"setup": setup, "apply_total_s": total, "image_digest": a.image_digest,
               "stages": [{k: r[k] for k in ("seq", "name", "ms", "result")} for r in rec.rows],
               "cleanup_openbao": cleanup, "result": Result.OK.value}
    (a.out / "summary.json").write_text(json.dumps(summary, indent=1))
    print(json.dumps(summary, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
