"""App verification Job for ``app_migration`` proposals (S2 P0 code fix; spec 8.4; B05/P11).

Spec 8.4: the verification Job checks that "the app connects to its DB in the destination profile, and another service
account cannot read the Secret". Here, on the destination cluster, in namespace ``verify-<plan>-<role>-<n>-app``:

1. ESO (the landing zone's secret path) syncs the DB password from the platform OpenBao
   ``secret/envs/<target_env>/rehearsal-app-db`` into the Secret ``app-db``. The namespaced SecretStore authenticates
   with a short-lived AppRole secret_id (role ``rehearsal-eso-<target_env>``: read of that one path, 120 s, 4 uses since
   ESO logs in once to validate the store and once to sync) that the service mints per Trial; the Secret holding it is
   deleted as soon as ESO has synced (D-26: no key in files, argv or logs).
2. One Pod: the synthetic DB (the destination's PostgreSQL image, the app's own schema from the snapshot, the
   app_writer password = the ESO-synced value) as a native sidecar, the PATCHED app (the agent's diff applied to the
   snapshot by this module) and the ORIGINAL app (the snapshot unchanged) on the destination's current app image, and
   the probe (``verify_job_app.py``, verifier image). Both apps get only the landing-zone interface: ``DB_DSN`` and
   ``DB_PASSWORD_FILE`` (the ESO Secret mounted). No Secrets Manager selector, no AWS endpoint, no network out.
3. Checks: patched app ready (it read the DB with the injected password), a chat write committed and read back by its
   idempotency key, the Secrets Manager call gone from the patched code, ESO synced, and "another service account cannot
   read app-db" (LocalSubjectAccessReview for ``default``). Control: the original code must not become ready under the
   same interface, and must contain the Secrets Manager call; otherwise the test cannot tell the fix from no fix.

The namespace is deleted at the end; what is left is recorded. Nothing here applies anything to the real destination.
"""

from __future__ import annotations

import base64
import json
import re
import time
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from kubernetes import client as k8s
from kubernetes import config as k8s_config
from kubernetes.client.rest import ApiException

from .cluster import EGRESS_CANARIES, ROLE_LABEL, TRIAL_LABEL, Names, baseline

KIND = "behavioral:app_eso_write+static_rules"
ROLE = "app_migration"
ESO_KV_PATH = "envs/{env}/rehearsal-app-db"  # field ``password``; rehearsal-only value (keys.sh env-generate)
ESO_APPROLE = "rehearsal-eso-{env}"
ESO_SECRET_ID_USES = 4  # SecretStore validation + ExternalSecret sync each log in (single use failed the sync)
JOB_ROLE = "verify-app"
PROBE = Path(__file__).with_name("verify_job_app.py")
SDK_CALL = re.compile(r"secretsmanager|get_secret_value|GetSecretValue")
CODE_DIR = "src/bravo_demo/"  # the app package in the customer snapshot (apps/demo layout)
SCHEMA_FILES = ("schema/sql/01_schema.sql", "schema/sql/02_roles.sql", "schema/sql/03_src.sql")
MAX_CONFIGMAP_BYTES = 900_000
PG_UID, APP_UID, PROBE_UID = 26, 10001, 65532

DB_INIT = r"""set -euo pipefail
export PGDATA=/var/lib/pg/data
initdb -D "$PGDATA" -U postgres -E UTF8 --auth-local=trust --auth-host=scram-sha-256 >/dev/null
pg_ctl -D "$PGDATA" -w -o "-c listen_addresses='' -c unix_socket_directories=/var/run/pg" start >/dev/null
q() { psql -h /var/run/pg -U postgres -v ON_ERROR_STOP=1 --no-psqlrc -q "$@"; }
q -d postgres -c "CREATE DATABASE chat"
q -d chat -f /cfg/schema/01_schema.sql
rnd() { head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n'; }
# app_writer's password is the ESO-synced value (the server hashes it; statement logging is off). Others are random.
BRAVO_APP_WRITER_VERIFIER="$(cat /var/run/bravo/db/password)" BRAVO_REPLICATOR_VERIFIER="$(rnd)" \
  BRAVO_EXECUTOR_CTL_VERIFIER="$(rnd)" BRAVO_EXECUTOR_OBSERVE_VERIFIER="$(rnd)" q -d chat -f /cfg/schema/02_roles.sql
q -d chat -v attachment_store_id=verify -f /cfg/schema/03_src.sql
pg_ctl -D "$PGDATA" -w stop >/dev/null
echo "bravo-verify-db: schema from the snapshot loaded"
exec postgres -D "$PGDATA" -c listen_addresses=127.0.0.1 -c unix_socket_directories=/var/run/pg
"""


class DiffError(ValueError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _strip1(path: str) -> str:
    """``-p1`` like ``git apply`` / T31: drop the first path component (``src/`` or ``app/``)."""
    path = path.split("\t", 1)[0].strip()
    parts = PurePosixPath(path).parts
    if len(parts) < 2 or path.startswith("/") or ".." in parts:
        raise DiffError("diff_path_rejected")
    return "/".join(parts[1:])


def apply_diff(files: dict[str, str], diff: str) -> tuple[dict[str, str], list[str]]:
    """Apply a unified diff (``diff -ruN src app`` from /, strip 1) to ``files`` exactly: every context and removed line
    must match, no fuzz, no offset; hunk line counts decide where a hunk ends. Returns (new files, changed paths).
    Raises ``DiffError``. Pure."""
    out = dict(files)
    changed: list[str] = []
    lines = diff.splitlines(keepends=True)
    i = 0
    while i < len(lines):
        if not lines[i].startswith("--- "):
            i += 1
            continue
        if i + 1 >= len(lines) or not lines[i + 1].startswith("+++ "):
            raise DiffError("diff_malformed")
        old_path, new_path = _strip1(lines[i][4:]), _strip1(lines[i + 1][4:])
        if old_path != new_path:
            raise DiffError("diff_rename_unsupported")
        i += 2
        src = out.get(new_path, "").splitlines(keepends=True)
        result: list[str] = []
        pos = 0
        while i < len(lines) and lines[i].startswith("@@"):
            m = _HUNK.match(lines[i])
            if not m:
                raise DiffError("diff_malformed")
            old_n, new_n = int(m.group(2) or 1), int(m.group(4) or 1)
            start = int(m.group(1)) - (0 if old_n == 0 else 1)
            if start < pos or start > len(src):
                raise DiffError("diff_hunk_out_of_range")
            result.extend(src[pos:start])
            pos = start
            i += 1
            prev = ""
            while old_n > 0 or new_n > 0 or (i < len(lines) and lines[i].startswith("\\")):
                if i >= len(lines):
                    raise DiffError("diff_truncated")
                ln, tag = lines[i], lines[i][:1]
                if tag == "\\":  # "\ No newline at end of file": the previous line ends without a newline
                    if prev in (" ", "+") and result and result[-1].endswith("\n"):
                        result[-1] = result[-1][:-1]
                    i += 1
                    continue
                if tag in (" ", "-"):
                    have = src[pos] if pos < len(src) else None
                    if have is None or have.rstrip("\n") != ln[1:].rstrip("\n"):
                        raise DiffError("diff_context_mismatch")
                    pos += 1
                    old_n -= 1
                    if tag == " ":
                        result.append(have if have.endswith("\n") else have + "\n")
                        new_n -= 1
                elif tag == "+":
                    body = ln[1:]
                    result.append(body if body.endswith("\n") else body + "\n")
                    new_n -= 1
                else:
                    raise DiffError("diff_malformed")
                if old_n < 0 or new_n < 0:
                    raise DiffError("diff_malformed")
                prev = tag
                i += 1
        result.extend(src[pos:])
        text = "".join(result)
        if text:
            out[new_path] = text
        else:
            out.pop(new_path, None)
        changed.append(new_path)
    if not changed:
        raise DiffError("empty_diff")
    return out, changed


def read_tree(root: Path) -> dict[str, str]:
    return {p.relative_to(root).as_posix(): p.read_text(encoding="utf-8", errors="replace")
            for p in sorted(root.rglob("*")) if p.is_file() and not p.is_symlink()}


def sdk_calls(tree: dict[str, str]) -> list[str]:
    """Files under the app package that still call Secrets Manager (same pattern as the static verifier)."""
    return sorted(f for f, text in tree.items() if f.startswith(CODE_DIR) and f.endswith(".py") and SDK_CALL.search(text))


def _key(path: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", path)


def configmap_items(original: dict[str, str], patched: dict[str, str]) -> tuple[dict[str, str], dict[str, list[dict[str, str]]]]:
    """ConfigMap data and the per-volume item lists: patched/original packages, the patched schema, scripts."""
    data: dict[str, str] = {}
    items: dict[str, list[dict[str, str]]] = {"patched": [], "original": [], "cfg": []}
    for vol, tree in (("patched", patched), ("original", original)):
        for f, text in tree.items():
            if f.startswith(CODE_DIR):
                k = f"{vol}--{_key(f)}"
                data[k] = text
                items[vol].append({"key": k, "path": "bravo_demo/" + f[len(CODE_DIR):]})
    for f in SCHEMA_FILES:
        if f not in patched:
            raise DiffError(f"schema_missing:{f}")
        k = f"schema--{_key(f)}"
        data[k] = patched[f]
        items["cfg"].append({"key": k, "path": "schema/" + PurePosixPath(f).name})
    data["db-init.sh"] = DB_INIT
    data["probe.py"] = PROBE.read_text(encoding="utf-8")
    items["cfg"] += [{"key": "db-init.sh", "path": "db-init.sh"}, {"key": "probe.py", "path": "probe.py"}]
    return data, items


def app_env(port: int, admin_port: int) -> list[dict[str, str]]:
    """The landing-zone interface only: DSN without a password and the password FILE (ESO Secret mount)."""
    return [{"name": "APP_ENV_ID", "value": "verify"}, {"name": "APP_ENV_LABEL", "value": "verify"},
            {"name": "APP_PORT", "value": str(port)}, {"name": "ADMIN_PORT", "value": str(admin_port)},
            {"name": "DB_DSN", "value": "host=127.0.0.1 port=5432 dbname=chat user=app_writer sslmode=disable"},
            {"name": "DB_PASSWORD_FILE", "value": "/var/run/bravo/db/password"},
            {"name": "PYTHONPATH", "value": "/opt/bravo/code"}, {"name": "LOG_LEVEL", "value": "INFO"}]


def _sec(uid: int) -> dict[str, Any]:
    return {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}, "readOnlyRootFilesystem": True,
            "runAsNonRoot": True, "runAsUser": uid, "runAsGroup": uid}


def pod(trial_id: str, images: dict[str, str], items: dict[str, list[dict[str, str]]], prm: dict[str, Any],
        deadline_s: int) -> dict[str, Any]:
    labels = {TRIAL_LABEL: trial_id, ROLE_LABEL: JOB_ROLE}
    pw = {"name": "db-password", "mountPath": "/var/run/bravo/db", "readOnly": True}
    res = lambda c, m, lc, lm: {"requests": {"cpu": c, "memory": m}, "limits": {"cpu": lc, "memory": lm}}  # noqa: E731

    def app(name: str, vol: str, port: int, admin: int) -> dict[str, Any]:
        return {"name": name, "image": images["app"], "imagePullPolicy": "IfNotPresent", "env": app_env(port, admin),
                "securityContext": _sec(APP_UID), "resources": res("50m", "96Mi", "500m", "256Mi"),
                "volumeMounts": [pw, {"name": vol, "mountPath": "/opt/bravo/code", "readOnly": True},
                                 {"name": "tmp", "mountPath": "/tmp"}]}

    db = {"name": "db", "image": images["db"], "imagePullPolicy": "IfNotPresent", "restartPolicy": "Always",
          "command": ["bash", "/cfg/db-init.sh"], "securityContext": _sec(PG_UID),
          "resources": res("100m", "128Mi", "500m", "384Mi"),
          "startupProbe": {"exec": {"command": ["pg_isready", "-h", "127.0.0.1", "-p", "5432"]},
                           "periodSeconds": 2, "failureThreshold": 60},
          "volumeMounts": [pw, {"name": "cfg", "mountPath": "/cfg", "readOnly": True},
                           {"name": "pgdata", "mountPath": "/var/lib/pg"}, {"name": "pgrun", "mountPath": "/var/run/pg"},
                           {"name": "tmp", "mountPath": "/tmp"}]}
    probe = {"name": "probe", "image": images["probe"], "imagePullPolicy": "IfNotPresent",
             "command": ["python3", "-I", "-B", "/cfg/probe.py"], "securityContext": _sec(PROBE_UID),
             "resources": res("20m", "32Mi", "200m", "64Mi"),
             "volumeMounts": [{"name": "cfg", "mountPath": "/cfg", "readOnly": True}]}
    cm = lambda key: {"name": "verify-app", "items": items[key]}  # noqa: E731
    return {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": "app-verify", "labels": labels},
            "spec": {"serviceAccountName": "rehearsal-app", "automountServiceAccountToken": False,
                     "enableServiceLinks": False, "restartPolicy": "Never", "activeDeadlineSeconds": deadline_s,
                     "securityContext": {"runAsNonRoot": True, "fsGroup": PROBE_UID,
                                         "seccompProfile": {"type": "RuntimeDefault"}},
                     "initContainers": [db],
                     "containers": [app("app-patched", "patched", 8000, 8081),
                                    app("app-original", "original", 8001, 8082), probe],
                     "volumes": [{"name": "db-password", "secret": {"secretName": "app-db", "defaultMode": 0o440,
                                                                     "items": [{"key": "password", "path": "password"}]}},
                                 {"name": "patched", "configMap": cm("patched")},
                                 {"name": "original", "configMap": cm("original")},
                                 {"name": "cfg", "configMap": {**cm("cfg"), "items": items["cfg"] + [
                                     {"key": "params.json", "path": "params.json"}]}},
                                 {"name": "pgdata", "emptyDir": {"sizeLimit": "256Mi"}},
                                 {"name": "pgrun", "emptyDir": {"sizeLimit": "1Mi"}},
                                 {"name": "tmp", "emptyDir": {"sizeLimit": "64Mi"}}]}}


def store_name(trial_id: str) -> str:
    """One SecretStore name per Trial. ESO caches provider clients by store namespace/name; a rerun of the same plan
    and attempt reuses the namespace name, and a cached client with a revoked token made the AppRole login 403
    "permission denied" (measured 2026-11-03 03:28 KST)."""
    return f"openbao-{trial_id.removeprefix('t-')[:12]}"


def eso_objects(trial_id: str, eso_server: str, ca_pem: str, role_id: str, secret_id: str,
                kv_key: str) -> list[tuple[str, dict[str, Any]]]:
    labels = {TRIAL_LABEL: trial_id, ROLE_LABEL: JOB_ROLE}
    store_ref = store_name(trial_id)
    secret = {"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
              "metadata": {"name": "eso-approle", "labels": labels}, "stringData": {"secret_id": secret_id}}
    store = {"apiVersion": "external-secrets.io/v1", "kind": "SecretStore",
             "metadata": {"name": store_ref, "labels": labels},
             "spec": {"provider": {"vault": {
                 "server": eso_server, "path": "secret", "version": "v2",
                 "caBundle": base64.b64encode(ca_pem.encode()).decode(),
                 "auth": {"appRole": {"path": "approle", "roleId": role_id,
                                      "secretRef": {"name": "eso-approle", "key": "secret_id"}}}}}}}
    es = {"apiVersion": "external-secrets.io/v1", "kind": "ExternalSecret", "metadata": {"name": "app-db", "labels": labels},
          "spec": {"refreshInterval": "0", "secretStoreRef": {"kind": "SecretStore", "name": store_ref},
                   "target": {"name": "app-db", "creationPolicy": "Owner"},
                   "data": [{"secretKey": "password", "remoteRef": {"key": kv_key, "property": "password"}}]}}
    return [("secret", secret), ("secretstore", store), ("externalsecret", es)]


def namespace_of(names: Names) -> str:
    return f"{names.verify_ns}-app"


class AppJobRun:
    """Creates the namespace, ESO objects, the Pod; waits; reads the result; asks the access review; deletes it all."""

    def __init__(self, kubeconfig: Path, context: str | None) -> None:
        self.api = k8s_config.new_client_from_config(config_file=str(kubeconfig), context=context)
        self.core, self.net = k8s.CoreV1Api(self.api), k8s.NetworkingV1Api(self.api)
        self.custom, self.authz = k8s.CustomObjectsApi(self.api), k8s.AuthorizationV1Api(self.api)

    def _create(self, ns: str, kind: str, body: dict[str, Any]) -> None:
        if kind in ("secretstore", "externalsecret"):
            self.custom.create_namespaced_custom_object("external-secrets.io", "v1", ns, kind + "s", body)
            return
        {"np": lambda: self.net.create_namespaced_network_policy(ns, body),
         "quota": lambda: self.core.create_namespaced_resource_quota(ns, body),
         "limits": lambda: self.core.create_namespaced_limit_range(ns, body),
         "sa": lambda: self.core.create_namespaced_service_account(ns, body),
         "configmap": lambda: self.core.create_namespaced_config_map(ns, body),
         "secret": lambda: self.core.create_namespaced_secret(ns, body),
         "pod": lambda: self.core.create_namespaced_pod(ns, body)}[kind]()

    def _baseline(self, ns: str, trial_id: str) -> list[str]:
        notes = []
        for kind, body in baseline(ns, trial_id, JOB_ROLE):
            try:
                self._create(ns, kind, body)
            except ApiException as exc:  # Kyverno bravo-default-deny may have generated default-deny first
                if exc.status != 409 or (kind, body["metadata"]["name"]) != ("np", "default-deny"):
                    raise
                notes.append("np/default-deny: generated by cluster policy")
        return notes

    def _eso_ready(self, ns: str, timeout_s: float) -> dict[str, Any]:
        end, last = time.monotonic() + timeout_s, {}
        t0 = time.monotonic()
        while time.monotonic() < end:
            obj = self.custom.get_namespaced_custom_object("external-secrets.io", "v1", ns, "externalsecrets", "app-db")
            conds = (obj.get("status") or {}).get("conditions") or []
            last = next((c for c in conds if c.get("type") == "Ready"), {})
            if last.get("status") == "True":
                return {"ready": True, "after_s": round(time.monotonic() - t0, 1), "reason": last.get("reason"),
                        "synced_secret": ((obj.get("status") or {}).get("binding") or {}).get("name")}
            time.sleep(1)
        return {"ready": False, "after_s": round(time.monotonic() - t0, 1), "reason": last.get("reason"),
                "message": str(last.get("message", ""))[:300]}

    def _other_sa_can_read(self, ns: str) -> dict[str, Any]:
        user = f"system:serviceaccount:{ns}:default"
        body = {"apiVersion": "authorization.k8s.io/v1", "kind": "LocalSubjectAccessReview",
                "metadata": {"namespace": ns},
                "spec": {"user": user, "groups": ["system:serviceaccounts", f"system:serviceaccounts:{ns}",
                                                  "system:authenticated"],
                         "resourceAttributes": {"namespace": ns, "verb": "get", "resource": "secrets", "name": "app-db"}}}
        r = self.authz.create_namespaced_local_subject_access_review(ns, body)
        return {"user": user, "allowed": bool(r.status.allowed), "reason": r.status.reason or "",
                "evaluation_error": r.status.evaluation_error or ""}

    def _image_names(self, node: str | None, image_id: str) -> list[str]:
        if not node or not image_id:
            return []
        try:
            for img in self.core.read_node(node).status.images or []:
                if image_id in (img.names or []):
                    return [str(x) for x in img.names]
        except ApiException:
            pass
        return []

    def _delete_namespace(self, ns: str, timeout_s: int = 120) -> list[str]:
        try:
            self.core.delete_namespace(ns, propagation_policy="Foreground")
        except ApiException as exc:
            if exc.status == 404:
                return []
        end = time.monotonic() + timeout_s
        while time.monotonic() < end:
            try:
                self.core.read_namespace(ns)
            except ApiException as exc:
                if exc.status == 404:
                    return []
            time.sleep(2)
        return [ns]

    def _log(self, ns: str, container: str, lines: int = 20) -> str:
        try:
            return str(self.core.read_namespaced_pod_log("app-verify", ns, container=container, tail_lines=lines))
        except ApiException as exc:
            return f"k8s_{exc.status}"

    def run(self, *, names: Names, trial_id: str, images: dict[str, str], eso: dict[str, str],
            original: dict[str, str], patched: dict[str, str], changed: list[str], deadline_s: int) -> dict[str, Any]:
        ns = namespace_of(names)
        out: dict[str, Any] = {"ran": False, "reason": None, "namespace": ns, "kind": KIND,
                               "pinned_image": images["probe"], "pinned_image_digest": images["probe"].rsplit("@", 1)[-1],
                               "images": images, "changed_files": changed, "eso_kv_key": eso["kv_key"],
                               "started_at": datetime.now(UTC).isoformat()}
        before, after = sdk_calls(original), sdk_calls(patched)
        static_checks = [
            {"name": "app:secrets_manager_call_removed", "ok": not after,
             "detail": "no Secrets Manager call left in " + CODE_DIR if not after else "still in " + ",".join(after)}]
        out["sdk_calls"] = {"original": before, "patched": after}
        data, items = configmap_items(original, patched)
        prm = {"run": trial_id, "egress_canaries": [list(x) for x in EGRESS_CANARIES], "ready_s": 120, "control_s": 20}
        data["params.json"] = json.dumps(prm, sort_keys=True)
        size = sum(len(v.encode()) for v in data.values())
        if size > MAX_CONFIGMAP_BYTES:
            out["reason"] = "configmap_too_large"
            return out
        labels = {TRIAL_LABEL: trial_id, ROLE_LABEL: JOB_ROLE, "pod-security.kubernetes.io/enforce": "restricted",
                  "pod-security.kubernetes.io/enforce-version": "latest",
                  "pod-security.kubernetes.io/warn": "restricted", "bravo.dev/kyverno-baseline": "rehearsal"}
        try:
            self.core.create_namespace({"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": ns, "labels": labels}})
        except ApiException as exc:
            out["reason"] = "namespace_exists" if exc.status == 409 else f"k8s_{exc.status}"
            return out
        try:
            out["baseline_notes"] = self._baseline(ns, trial_id)
            for kind, body in eso_objects(trial_id, eso["server"], eso["ca_pem"], eso["role_id"], eso["secret_id"],
                                          eso["kv_key"]):
                self._create(ns, kind, body)
            out["eso"] = self._eso_ready(ns, 60)
            try:  # the secret_id is spent by the first login; remove it either way
                self.core.delete_namespaced_secret("eso-approle", ns)
                out["approle_secret_deleted"] = True
            except ApiException as exc:
                out["approle_secret_deleted"] = f"k8s_{exc.status}"
            if not out["eso"]["ready"]:
                out["reason"] = "eso_not_synced"
                return out
            self._create(ns, "configmap", {"apiVersion": "v1", "kind": "ConfigMap",
                                           "metadata": {"name": "verify-app", "labels": {TRIAL_LABEL: trial_id}},
                                           "data": data})
            self._create(ns, "pod", pod(trial_id, images, items, prm, deadline_s))
            end, probe_state, statuses = time.monotonic() + deadline_s + 30, None, []
            p = None
            while time.monotonic() < end:
                p = self.core.read_namespaced_pod("app-verify", ns)
                statuses = list(p.status.container_statuses or [])
                probe_state = next((c for c in statuses if c.name == "probe"), None)
                if probe_state is not None and probe_state.state and probe_state.state.terminated:
                    break
                if p.status.phase in ("Failed", "Succeeded"):
                    break
                time.sleep(2)
            if p is None or probe_state is None or not (probe_state.state and probe_state.state.terminated):
                waiting = [f"{c.name}:{c.state.waiting.reason}" for c in statuses if c.state and c.state.waiting]
                out["reason"] = "probe_timeout" + (f":{','.join(waiting)}" if waiting else "")
                out["logs"] = {c: self._log(ns, c) for c in ("db", "app-patched", "app-original")}
                return out
            by = {c.name: c for c in statuses}
            orig = by.get("app-original")
            out.update({"pod": p.metadata.name, "pod_uid": str(p.metadata.uid), "node": p.spec.node_name,
                        "image_id": str(probe_state.image_id), "image": str(probe_state.image)})
            out["image_names"] = self._image_names(p.spec.node_name, out["image_id"])
            out["containers"] = {n: {"image_id": str(c.image_id), "ready": bool(c.ready),
                                     "terminated": ({"exit_code": c.state.terminated.exit_code,
                                                     "reason": c.state.terminated.reason}
                                                    if c.state and c.state.terminated else None)}
                                 for n, c in by.items()}
            out["logs"] = {"app-original": self._log(ns, "app-original", 6), "app-patched": self._log(ns, "app-patched", 8),
                           "db": self._log(ns, "db", 4)}
            result = None
            for line in reversed([ln for ln in self._log(ns, "probe", 50).splitlines() if ln.strip()]):
                try:
                    result = json.loads(line)
                    break
                except ValueError:
                    continue
            if not isinstance(result, dict):
                out["reason"] = "probe_no_result"
                return out
            out["result"] = result
            if result.get("error"):
                out["reason"] = f"probe_{result['error']}"
                return out
            access = self._other_sa_can_read(ns)
            out["other_sa_access"] = access
            orig_exit = ((out["containers"].get("app-original") or {}).get("terminated") or {}).get("exit_code")
            out["ran"] = True
            # Control: the unpatched code fails under the landing-zone interface and contains the SDK call.
            out["control_ok"] = bool((result.get("control") or {}).get("ok")) and bool(before) and \
                orig is not None and orig_exit not in (None, 0)
            out["egress_blocked"] = all(e.get("blocked") for e in result.get("egress") or [])
            out["checks"] = static_checks + [
                {"name": "app:eso_synced_from_openbao", "ok": True,
                 "detail": f"ExternalSecret app-db Ready after {out['eso']['after_s']} s ({eso['kv_key']})"},
                *({"name": "app:" + c["name"], "ok": bool(c.get("ok")), "detail": str(c.get("detail", ""))}
                  for c in result.get("checks") or []),
                {"name": "app:other_service_account_cannot_read_secret", "ok": not access["allowed"],
                 "detail": f"{access['user']} get secrets/app-db allowed={access['allowed']}"}]
            return out
        except ApiException as exc:
            out["reason"] = f"k8s_{exc.status}"
            return out
        finally:
            out["finished_at"] = datetime.now(UTC).isoformat()
            out["leftovers"] = self._delete_namespace(ns)
