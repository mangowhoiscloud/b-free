"""Behavioural verification Job for proposed RGW bucket policies (B7; spec FR-5, 8.4 "별도 검증 Job").

Harbor's separate verifier grades the frozen proposal JSON with static rules. For every ``rgw_bucket_policy``
translation in that proposal the platform also asks RGW itself, in a separate Job on the destination cluster:
the policy is applied to the rehearsal test bucket, an unsigned GET must be refused with AccessDenied and a
signed GET with the test bucket owner's key must work. Before that, a control step shows that an open policy is
visible on that bucket. The test is ``verify_job_s3.py`` (platform-held, never shown to the agent).

Where it runs: namespace ``verify-<plan>-<n>-job`` (the provisioner may write only in ``verify-*``) with the
rehearsal baseline (Pod Security restricted, default deny, same namespace + kube-dns, quota, limits, token-less
service account) plus one NetworkPolicy: the Job's Pod may reach the RGW Pods on 443 and nothing else. The image is
the pinned verifier image. The namespace is deleted at the end; what is left is recorded.

Credentials (D-26): the test bucket owner's key is read per Trial from the platform OpenBao
``secret/envs/<target_env>/rehearsal-s3`` by the service and handed to the Job as a Secret in that namespace (deleted
right after the Job ends, and with the namespace). The record returned here holds no key.

Rendering (``render``, pure): the proposal is written for the destination attachment bucket. For the test, the bucket
in every ``arn:aws:s3:::<bucket>[/...]`` becomes the test bucket, and the destination app principal (the attachment
bucket's owner, ``target_app_arn``) becomes the test bucket's owner. Nothing else changes, so an anonymous or wildcard
grant is tested as written.
"""

from __future__ import annotations

import copy
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kubernetes import client as k8s
from kubernetes import config as k8s_config
from kubernetes.client.rest import ApiException

from .cluster import EGRESS_CANARIES, ROLE_LABEL, TRIAL_LABEL, Names, _pod_security, baseline

POLICY_TYPES = ("rgw_bucket_policy",)
KIND = "behavioral+static_rules"
SECRET_PATH = "envs/{env}/rehearsal-s3"
SECRET_FIELDS = ("access_key_id", "secret_access_key", "endpoint", "bucket", "ca_pem", "owner_arn")
JOB_ROLE = "verify-job"
CONTAINER = "verify"
RGW_NAMESPACE, RGW_POD_LABELS, RGW_PORT = "rook-ceph", {"app": "rook-ceph-rgw"}, 443
SCRIPT = Path(__file__).with_name("verify_job_s3.py")
S3_ARN = "arn:aws:s3:::"


def proposed_policies(artifact: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Bucket policy documents of the frozen proposal that this Job can test, in proposal order."""
    out = []
    for t in (artifact or {}).get("translations") or []:
        if isinstance(t, dict) and (t.get("target") or {}).get("type") in POLICY_TYPES and \
                isinstance(t.get("document"), dict):
            out.append(t["document"])
    return out


def _bucket_arn(value: str, bucket: str) -> str:
    if not value.startswith(S3_ARN):
        return value
    rest = value[len(S3_ARN):]
    return S3_ARN + bucket + (rest[rest.index("/"):] if "/" in rest else "")


def render(doc: dict[str, Any], *, test_bucket: str, principal_map: dict[str, str]) -> dict[str, Any]:
    """The proposal's policy, pointed at the test bucket and the test owner. Pure."""

    def walk(v: Any, in_resource: bool) -> Any:
        if isinstance(v, dict):
            return {k: walk(x, in_resource or k in ("Resource", "NotResource")) for k, x in v.items()}
        if isinstance(v, list):
            return [walk(x, in_resource) for x in v]
        if isinstance(v, str):
            v = principal_map.get(v, v)
            return _bucket_arn(v, test_bucket) if in_resource else v
        return v

    return walk(copy.deepcopy(doc), False)


def control_policy(bucket: str, key: str) -> dict[str, Any]:
    """Known-open policy: an unsigned GET of the canary must work under it (the control step)."""
    return {"Version": "2012-10-17", "Statement": [{
        "Sid": "BravoVerifyControlAnonymousRead", "Effect": "Allow", "Principal": "*", "Action": "s3:GetObject",
        "Resource": f"{S3_ARN}{bucket}/{key}"}]}


def params(trial_id: str, record: dict[str, str], docs: list[dict[str, Any]]) -> dict[str, Any]:
    bucket, key = record["bucket"], f"_verify/{trial_id}"
    pmap = {record["target_app_arn"]: record["owner_arn"]} if record.get("target_app_arn") else {}
    return {"run": trial_id, "endpoint": record["endpoint"], "bucket": bucket, "canary_key": key,
            "egress_canaries": [list(x) for x in EGRESS_CANARIES],
            "control_policy": control_policy(bucket, key),
            "policies": [{"index": i, "policy": render(d, test_bucket=bucket, principal_map=pmap)}
                         for i, d in enumerate(docs)]}


def namespace_of(names: Names) -> str:
    return f"{names.verify_ns}-job"


def job_objects(ns: str, trial_id: str, image: str, prm: dict[str, Any], ca_pem: str, access_key: str,
                secret_key: str, deadline_s: int) -> list[tuple[str, dict[str, Any]]]:
    labels = {TRIAL_LABEL: trial_id, ROLE_LABEL: JOB_ROLE}
    egress = {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
              "metadata": {"name": "allow-rgw-egress", "labels": labels},
              "spec": {"podSelector": {"matchLabels": {ROLE_LABEL: JOB_ROLE}}, "policyTypes": ["Egress"],
                       "egress": [{"to": [{"namespaceSelector": {"matchLabels": {
                           "kubernetes.io/metadata.name": RGW_NAMESPACE}}, "podSelector": {"matchLabels": RGW_POD_LABELS}}],
                           "ports": [{"protocol": "TCP", "port": RGW_PORT}]}]}}
    cm = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "verify-job", "labels": labels},
          "data": {"job.py": SCRIPT.read_text(encoding="utf-8"), "params.json": json.dumps(prm, sort_keys=True),
                   "ca.crt": ca_pem}}
    secret = {"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
              "metadata": {"name": "verify-job-s3", "labels": labels},
              "stringData": {"access_key_id": access_key, "secret_access_key": secret_key}}
    env = [{"name": f"S3_{k.upper()}", "valueFrom": {"secretKeyRef": {"name": "verify-job-s3", "key": k}}}
           for k in ("access_key_id", "secret_access_key")]
    job = {"apiVersion": "batch/v1", "kind": "Job", "metadata": {"name": f"verify-job-{trial_id[:12]}", "labels": labels},
           "spec": {"backoffLimit": 0, "activeDeadlineSeconds": deadline_s, "template": {
               "metadata": {"labels": labels},
               "spec": {"serviceAccountName": "rehearsal-app", "automountServiceAccountToken": False,
                        "enableServiceLinks": False, "restartPolicy": "Never", "securityContext": _pod_security(),
                        "containers": [{
                            "name": CONTAINER, "image": image, "imagePullPolicy": "IfNotPresent",
                            "command": ["python3", "-I", "-B", "/cfg/job.py"], "env": env,
                            "securityContext": {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]},
                                                "readOnlyRootFilesystem": True, "runAsNonRoot": True},
                            "resources": {"requests": {"cpu": "50m", "memory": "64Mi"},
                                          "limits": {"cpu": "500m", "memory": "128Mi"}},
                            "volumeMounts": [{"name": "cfg", "mountPath": "/cfg", "readOnly": True}]}],
                        "volumes": [{"name": "cfg", "configMap": {"name": "verify-job"}}]}}}}
    return [("np", egress), ("configmap", cm), ("secret", secret), ("job", job)]


def parse_result(log: str) -> dict[str, Any] | None:
    for line in reversed([ln for ln in log.splitlines() if ln.strip()]):
        try:
            out = json.loads(line)
        except ValueError:
            continue
        return out if isinstance(out, dict) else None
    return None


def checks_of(result: dict[str, Any]) -> list[dict[str, Any]]:
    """Flat check list for the verdict: one entry per (policy, check)."""
    multi = len(result.get("policies") or []) > 1
    out = []
    for pol in result.get("policies") or []:
        for c in pol.get("checks") or []:
            name = f"behavioral:{c['name']}" + (f"#{pol['index']}" if multi else "")
            out.append({"name": name, "ok": bool(c.get("ok")), "detail": str(c.get("detail", ""))})
    return out


class JobRun:
    """Creates the namespace, its baseline, the Job and its inputs; waits; reads the result; deletes it all."""

    def __init__(self, kubeconfig: Path, context: str | None) -> None:
        self.api = k8s_config.new_client_from_config(config_file=str(kubeconfig), context=context)
        self.core, self.net, self.batch = k8s.CoreV1Api(self.api), k8s.NetworkingV1Api(self.api), k8s.BatchV1Api(self.api)

    def _create(self, ns: str, kind: str, body: dict[str, Any]) -> None:
        {"np": lambda: self.net.create_namespaced_network_policy(ns, body),
         "quota": lambda: self.core.create_namespaced_resource_quota(ns, body),
         "limits": lambda: self.core.create_namespaced_limit_range(ns, body),
         "sa": lambda: self.core.create_namespaced_service_account(ns, body),
         "configmap": lambda: self.core.create_namespaced_config_map(ns, body),
         "secret": lambda: self.core.create_namespaced_secret(ns, body),
         "job": lambda: self.batch.create_namespaced_job(ns, body)}[kind]()

    def _baseline(self, ns: str, trial_id: str) -> list[str]:
        notes = []
        for kind, body in baseline(ns, trial_id, JOB_ROLE):
            try:
                self._create(ns, kind, body)
            except ApiException as exc:
                # Kyverno bravo-default-deny may have generated the same default-deny first: same as harbor_env.
                if exc.status != 409 or (kind, body["metadata"]["name"]) != ("np", "default-deny"):
                    raise
                spec = self.net.read_namespaced_network_policy("default-deny", ns).spec
                if spec is None or spec.pod_selector.match_labels or spec.pod_selector.match_expressions or \
                        set(spec.policy_types or []) != {"Ingress", "Egress"} or spec.ingress or spec.egress:
                    raise
                notes.append("np/default-deny: generated by cluster policy, verified equivalent")
        return notes

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

    def run(self, *, names: Names, trial_id: str, image: str, record: dict[str, str], docs: list[dict[str, Any]],
            deadline_s: int) -> dict[str, Any]:
        ns = namespace_of(names)
        prm = params(trial_id, record, docs)
        out: dict[str, Any] = {"ran": False, "reason": None, "namespace": ns, "test_bucket": record["bucket"],
                               "endpoint": record["endpoint"], "policies": len(docs),
                               "rendered": [p["policy"] for p in prm["policies"]],
                               "started_at": datetime.now(UTC).isoformat()}
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
            objs = job_objects(ns, trial_id, image, prm, record["ca_pem"], record["access_key_id"],
                               record["secret_access_key"], deadline_s)
            for kind, body in objs:
                self._create(ns, kind, body)
            job = objs[-1][1]["metadata"]["name"]
            out["job"] = job
            end = time.monotonic() + deadline_s + 30
            status = None
            while time.monotonic() < end:
                status = self.batch.read_namespaced_job_status(job, ns).status
                if status.succeeded or status.failed:
                    break
                time.sleep(1)
            try:
                self.core.delete_namespaced_secret("verify-job-s3", ns)  # the key leaves the cluster first
                out["secret_deleted"] = True
            except ApiException as exc:
                out["secret_deleted"] = f"k8s_{exc.status}"
            pods = self.core.list_namespaced_pod(ns, label_selector=f"job-name={job}").items
            if not pods:
                out["reason"] = "job_pod_missing"
                return out
            pod = pods[0]
            st = (pod.status.container_statuses or [None])[0]
            out.update({"pod": pod.metadata.name, "pod_uid": str(pod.metadata.uid), "node": pod.spec.node_name,
                        "image": str(st.image) if st else "", "image_id": str(st.image_id) if st else "",
                        "job_succeeded": bool(status and status.succeeded)})
            out["image_names"] = self._image_names(pod.spec.node_name, out["image_id"])
            if not (status and (status.succeeded or status.failed)):
                out["reason"] = "job_timeout"
                return out
            result = parse_result(str(self.core.read_namespaced_pod_log(pod.metadata.name, ns, container=CONTAINER,
                                                                         tail_lines=50)))
            if result is None:
                out["reason"] = "job_no_result"
                return out
            out["result"] = result
            if result.get("error"):
                out["reason"] = f"job_{result['error']}"
                return out
            out["ran"] = True
            out["control_ok"] = bool((result.get("control") or {}).get("ok"))
            out["egress_blocked"] = all(e.get("blocked") for e in result.get("egress") or [])
            out["checks"] = checks_of(result)
            return out
        except ApiException as exc:
            out["reason"] = f"k8s_{exc.status}"
            return out
        finally:
            out["finished_at"] = datetime.now(UTC).isoformat()
            out["leftovers"] = self._delete_namespace(ns)
