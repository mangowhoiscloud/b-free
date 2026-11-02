"""Run kube-bench as a Job on a real cluster (pinned image, explicit benchmark) and return its JSON report.

Objects created, all labelled ``app.kubernetes.io/managed-by=bravo-gate`` and deleted afterwards unless
``keep`` is set:
    Namespace        bravo-gate-scan (Pod Security ``privileged``: kube-bench needs hostPID and host paths,
                     read-only. This is the scanner's own namespace, recorded as an exception, not the app's)
    ServiceAccount   kube-bench (token mounted: the "policies" checks 5.1.x run kubectl in the Pod; without a token they
                     fail on "localhost:8080 connection refused", which is a tool error, not a finding)
    ClusterRole      bravo-gate-kube-bench-read (get/list only, for the "policies" target)
    ClusterRoleBinding
    Job              kube-bench-<platform>
Nothing in the app namespaces is touched.
"""

from __future__ import annotations

import json
import time
from typing import Any

from . import load_mapping

NAMESPACE = "bravo-gate-scan"
LABELS = {"app.kubernetes.io/managed-by": "bravo-gate", "app.kubernetes.io/name": "kube-bench",
          "component": "platform"}
ROLE = "bravo-gate-kube-bench-read"

# Targets and host paths per platform (kube-bench job-*.yaml at the pinned commit, adapted).
PLATFORMS: dict[str, dict[str, Any]] = {
    "eks": {"targets": "node,policies,managedservices,controlplane",
            "paths": ["/var/lib/kubelet", "/etc/systemd", "/etc/kubernetes"]},
    "k3s": {"targets": "master,node,controlplane,etcd,policies",
            "paths": ["/var/lib/rancher", "/etc/rancher", "/var/lib/kubelet", "/etc/systemd", "/lib/systemd",
                      "/etc/kubernetes"],
            "control_plane": True},
    # /etc/passwd and /etc/group: file-owner checks (etcd:etcd, 1.1.x) print names, not uids. /var/log: audit log.
    "rke2": {"targets": "master,node,controlplane,etcd,policies",
             "paths": ["/var/lib/rancher", "/etc/rancher", "/var/lib/kubelet", "/etc/systemd", "/lib/systemd",
                       "/etc/kubernetes", "/etc/passwd", "/etc/group", "/var/log"],
             "control_plane": True},
    "gke": {"targets": "node,policies,managedservices,controlplane",
            "paths": ["/var/lib/kubelet", "/etc/systemd", "/etc/kubernetes", "/home/kubernetes"]},
}


def job_manifest(platform: str, mapping: dict[str, Any] | None = None, *, node: str | None = None,
                 targets: str | None = None) -> dict[str, Any]:
    """``node`` pins the Job to one node (e.g. an agent node, with ``targets="node"``); default: a control-plane node
    for distributions with one, any node otherwise."""
    m = mapping or load_mapping()
    if platform not in PLATFORMS or platform not in m["benchmarks"]:
        raise ValueError(f"unknown platform {platform!r}")
    p = PLATFORMS[platform]
    vols = [{"name": f"h{i}", "hostPath": {"path": path}} for i, path in enumerate(p["paths"])]
    mounts = [{"name": f"h{i}", "mountPath": path, "readOnly": True} for i, path in enumerate(p["paths"])]
    spec: dict[str, Any] = {
        "hostPID": True,
        "serviceAccountName": "kube-bench",
        "automountServiceAccountToken": True,  # kubectl for the "policies" checks (read-only ClusterRole)
        "restartPolicy": "Never",
        "containers": [{
            "name": "kube-bench",
            "image": m["kube_bench"]["image"],
            "command": ["kube-bench", "run", "--targets", targets or p["targets"],
                        "--benchmark", m["benchmarks"][platform], "--json"],
            "volumeMounts": mounts,
            "resources": {"requests": {"cpu": "50m", "memory": "64Mi"}, "limits": {"memory": "256Mi"}},
        }],
        "volumes": vols,
        "tolerations": [{"operator": "Exists"}],
    }
    if node:
        spec["nodeSelector"] = {"kubernetes.io/hostname": node}
    elif p.get("control_plane"):
        spec["nodeSelector"] = {"node-role.kubernetes.io/control-plane": "true"}
    return {
        "apiVersion": "batch/v1", "kind": "Job",
        "metadata": {"name": f"kube-bench-{platform}", "namespace": NAMESPACE, "labels": LABELS},
        "spec": {"backoffLimit": 0, "ttlSecondsAfterFinished": 600, "activeDeadlineSeconds": 600,
                 "template": {"metadata": {"labels": LABELS}, "spec": spec}},
    }


def _ignore_conflict(fn, *a, **kw) -> None:
    from kubernetes.client.exceptions import ApiException

    try:
        fn(*a, **kw)
    except ApiException as exc:
        if exc.status != 409:
            raise


def _ignore_missing(fn, *a, **kw) -> None:
    from kubernetes.client.exceptions import ApiException

    try:
        fn(*a, **kw)
    except ApiException as exc:
        if exc.status != 404:
            raise


def run_job(api: Any, platform: str, *, timeout_s: int = 420, keep: bool = False, node: str | None = None,
            targets: str | None = None) -> dict[str, Any]:
    """Create the Job, wait, return {"report": <kube-bench JSON>, "job": {...}}. Raises on failure."""
    from kubernetes import client

    core, batch, rbac = client.CoreV1Api(api), client.BatchV1Api(api), client.RbacAuthorizationV1Api(api)
    manifest = job_manifest(platform, node=node, targets=targets)
    ns_labels = {**LABELS, "pod-security.kubernetes.io/enforce": "privileged",
                 "pod-security.kubernetes.io/warn": "privileged"}
    _ignore_conflict(core.create_namespace, {"metadata": {"name": NAMESPACE, "labels": ns_labels}})
    _ignore_conflict(core.create_namespaced_service_account, NAMESPACE,
                     {"metadata": {"name": "kube-bench", "labels": LABELS}})
    _ignore_conflict(rbac.create_cluster_role, {"metadata": {"name": ROLE, "labels": LABELS}, "rules": [
        {"apiGroups": [""], "resources": ["pods", "serviceaccounts", "namespaces", "nodes", "services",
                                          "replicationcontrollers"], "verbs": ["get", "list"]},
        # 5.7.4 "kubectl get all -n default" lists these too.
        {"apiGroups": ["apps"], "resources": ["deployments", "replicasets", "statefulsets", "daemonsets"],
         "verbs": ["get", "list"]},
        {"apiGroups": ["batch"], "resources": ["jobs", "cronjobs"], "verbs": ["get", "list"]},
        {"apiGroups": ["autoscaling"], "resources": ["horizontalpodautoscalers"], "verbs": ["get", "list"]},
        {"apiGroups": ["rbac.authorization.k8s.io"],
         "resources": ["roles", "rolebindings", "clusterroles", "clusterrolebindings"], "verbs": ["get", "list"]},
        {"apiGroups": ["networking.k8s.io"], "resources": ["networkpolicies"], "verbs": ["get", "list"]},
    ]})
    _ignore_conflict(rbac.create_cluster_role_binding, {
        "metadata": {"name": ROLE, "labels": LABELS},
        "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole", "name": ROLE},
        "subjects": [{"kind": "ServiceAccount", "name": "kube-bench", "namespace": NAMESPACE}]})
    name = manifest["metadata"]["name"]
    _ignore_missing(batch.delete_namespaced_job, name, NAMESPACE, propagation_policy="Foreground")
    for _ in range(30):  # wait for a previous Job of the same name to go away
        try:
            batch.read_namespaced_job(name, NAMESPACE)
            time.sleep(1)
        except client.exceptions.ApiException as exc:
            if exc.status == 404:
                break
            raise
    started = time.monotonic()
    batch.create_namespaced_job(NAMESPACE, manifest)
    try:
        status = None
        while time.monotonic() - started < timeout_s:
            status = batch.read_namespaced_job_status(name, NAMESPACE).status
            if (status.succeeded or 0) >= 1 or (status.failed or 0) >= 1:
                break
            time.sleep(3)
        pods = core.list_namespaced_pod(NAMESPACE, label_selector=f"job-name={name}").items
        if not pods:
            raise RuntimeError(f"kube-bench Job {name} has no pod (status {status})")
        pod = pods[0]
        logs = core.read_namespaced_pod_log(pod.metadata.name, NAMESPACE)
        if not status or not (status.succeeded or 0):
            raise RuntimeError(f"kube-bench Job {name} did not succeed: {status}; log tail: {logs[-500:]}")
        report = parse_output(logs)
        return {"report": report, "job": {
            "name": name, "namespace": NAMESPACE, "node": pod.spec.node_name, "image": manifest["spec"]["template"][
                "spec"]["containers"][0]["image"], "command": manifest["spec"]["template"]["spec"]["containers"][0][
                "command"], "seconds": round(time.monotonic() - started, 1)}}
    finally:
        if not keep:
            _ignore_missing(batch.delete_namespaced_job, name, NAMESPACE, propagation_policy="Background")
            _ignore_missing(rbac.delete_cluster_role_binding, ROLE)
            _ignore_missing(rbac.delete_cluster_role, ROLE)
            _ignore_missing(core.delete_namespace, NAMESPACE)


def parse_output(text: str) -> dict[str, Any]:
    """kube-bench --json prints one object ({Controls, Totals}) or one object per target line."""
    text = text.strip()
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
        if isinstance(obj, list):
            return {"Controls": obj}
    except json.JSONDecodeError:
        pass
    controls: list[Any] = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        obj = json.loads(line)
        controls.extend(obj.get("Controls") or ([obj] if "tests" in obj else []))
    if not controls:
        raise ValueError("kube-bench output has no JSON controls")
    return {"Controls": controls}
