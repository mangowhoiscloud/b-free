"""Destination-cluster pieces shared by the Harbor environment and the tool layer (spec §8.4, 05 3, 07 3).

Namespaces, Pods and their lifecycle belong to Harbor: ``harbor_env.RehearsalNamespaceEnvironment``
creates and deletes them with the provisioner identity. This module keeps what both sides need:

- the naming rule (``Names``) and the baseline every rehearsal/verify namespace gets: Pod Security
  ``restricted`` (enforce), NetworkPolicy default deny (ingress + egress) + same-namespace +
  kube-dns, ResourceQuota, LimitRange, token-less ServiceAccounts. Cluster-wide Kyverno policies
  (``policies/``) are installed by infra; the namespaces carry the labels they select on;
- the exec RBAC of the rehearsal namespace: ServiceAccount ``rehearsal-exec`` whose Role allows
  ConfigMaps, Pod read/logs and ``pods/exec`` on the one sandbox Pod name. No RoleBinding in the
  verify namespace (V1, V2);
- ``Cluster``: the tool layer's backend. It acts on one Trial's sandbox (``Sandbox``, filled in
  by the Harbor environment) with a short-lived TokenRequest token of ``rehearsal-exec`` and
  re-reads the Pod UID before every exec (V3b). All blocking client calls run in a worker thread.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kubernetes import client as k8s
from kubernetes import config as k8s_config
from kubernetes.client.rest import ApiException
from kubernetes.stream import stream

SANDBOX_CONTAINER = "workspace"
EXEC_SA = "rehearsal-exec"
APP_SA = "rehearsal-app"
TRIAL_LABEL = "bravo.dev/trial-id"
ROLE_LABEL = "bravo.dev/role"
EGRESS_CANARIES = (("1.1.1.1", 443), ("169.254.169.254", 80))  # internet, cloud metadata


class ClusterError(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class Names:
    rehearsal_ns: str
    verify_ns: str
    sandbox_pod: str

    @classmethod
    def of(cls, plan_id: str, attempt: int, trial_id: str) -> Names:
        short = plan_id.replace("-", "")[:8]
        # Sandbox Pod names carry the Trial ID and attempt and are never reused (05 3, V3).
        return cls(f"rehearsal-{short}-{attempt}", f"verify-{short}-{attempt}", f"rehearsal-sandbox-{trial_id[:12]}")


@dataclass
class Sandbox:
    names: Names
    pod_uid: str
    exec_api: k8s.ApiClient | None = None
    exec_token_exp: float = 0.0
    leftovers: list[str] = field(default_factory=list)


def _pod_security() -> dict[str, Any]:
    return {"runAsNonRoot": True, "runAsUser": 65532, "runAsGroup": 65532, "fsGroup": 65532,
            "seccompProfile": {"type": "RuntimeDefault"}}


def baseline(ns: str, trial_id: str, role: str) -> list[tuple[str, dict[str, Any]]]:
    """Objects every rehearsal/verify namespace gets, in creation order."""
    labels = {TRIAL_LABEL: trial_id, ROLE_LABEL: role}
    deny = {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
            "metadata": {"name": "default-deny", "labels": labels},
            "spec": {"podSelector": {}, "policyTypes": ["Ingress", "Egress"]}}
    same_ns = {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
               "metadata": {"name": "allow-same-namespace", "labels": labels},
               "spec": {"podSelector": {}, "policyTypes": ["Ingress", "Egress"],
                        "ingress": [{"from": [{"podSelector": {}}]}],
                        "egress": [{"to": [{"podSelector": {}}]}]}}
    dns = {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
           "metadata": {"name": "allow-kube-dns", "labels": labels},
           "spec": {"podSelector": {}, "policyTypes": ["Egress"],
                    "egress": [{"to": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}},
                                        "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}}}],
                                "ports": [{"protocol": "UDP", "port": 53}, {"protocol": "TCP", "port": 53}]}]}}
    quota = {"apiVersion": "v1", "kind": "ResourceQuota", "metadata": {"name": "trial-quota", "labels": labels},
             "spec": {"hard": {"pods": "4", "requests.cpu": "1", "requests.memory": "1Gi", "limits.cpu": "2",
                               "limits.memory": "2Gi", "configmaps": "16", "secrets": "4", "services": "2"}}}
    limits = {"apiVersion": "v1", "kind": "LimitRange", "metadata": {"name": "trial-limits", "labels": labels},
              "spec": {"limits": [{"type": "Container", "default": {"cpu": "500m", "memory": "256Mi"},
                                   "defaultRequest": {"cpu": "50m", "memory": "64Mi"},
                                   "max": {"cpu": "1", "memory": "512Mi"}}]}}
    app_sa = {"apiVersion": "v1", "kind": "ServiceAccount", "metadata": {"name": APP_SA, "labels": labels},
              "automountServiceAccountToken": False}
    objs: list[tuple[str, dict[str, Any]]] = [("np", deny), ("np", same_ns), ("np", dns), ("quota", quota),
                                              ("limits", limits), ("sa", app_sa)]
    return objs


def exec_rbac(ns: str, trial_id: str, sandbox_pod: str) -> list[tuple[str, dict[str, Any]]]:
    labels = {TRIAL_LABEL: trial_id, ROLE_LABEL: "rehearsal"}
    sa = {"apiVersion": "v1", "kind": "ServiceAccount", "metadata": {"name": EXEC_SA, "labels": labels},
          "automountServiceAccountToken": False}
    role = {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "Role",
            "metadata": {"name": EXEC_SA, "labels": labels},
            "rules": [
                {"apiGroups": [""], "resources": ["configmaps"], "verbs": ["get", "list", "create", "update", "patch"]},
                {"apiGroups": [""], "resources": ["pods"], "verbs": ["get", "list"]},
                {"apiGroups": [""], "resources": ["pods/log"], "verbs": ["get"]},
                {"apiGroups": [""], "resources": ["pods/exec"], "verbs": ["create", "get"], "resourceNames": [sandbox_pod]},
            ]}
    binding = {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "RoleBinding",
               "metadata": {"name": EXEC_SA, "labels": labels},
               "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": EXEC_SA},
               "subjects": [{"kind": "ServiceAccount", "name": EXEC_SA, "namespace": ns}]}
    return [("sa", sa), ("role", role), ("binding", binding)]


class Cluster:
    def __init__(self, kubeconfig: Path, context: str | None = None) -> None:
        self._kubeconfig = kubeconfig
        self._context = context
        self._api: k8s.ApiClient | None = None

    def _client(self) -> k8s.ApiClient:
        if self._api is None:
            if not self._kubeconfig.is_file():
                raise ClusterError("cluster_unconfigured")
            self._api = k8s_config.new_client_from_config(config_file=str(self._kubeconfig), context=self._context)
        return self._api

    async def _run(self, fn: Any, *args: Any, **kw: Any) -> Any:
        return await asyncio.to_thread(fn, *args, **kw)

    async def reachable(self) -> str | None:
        """Server version string, or raise ClusterError('cluster_unreachable')."""
        try:
            info = await self._run(lambda: k8s.VersionApi(self._client()).get_code())
            return str(info.git_version)
        except ClusterError:
            raise
        except Exception as exc:  # urllib3 / config errors: the cluster is not usable
            raise ClusterError("cluster_unreachable") from exc

    # Exec identity ------------------------------------------------------------------------------

    def _exec_api(self, box: Sandbox) -> k8s.ApiClient:
        if box.exec_api is not None and box.exec_token_exp - time.time() > 60:
            return box.exec_api
        core = k8s.CoreV1Api(self._client())
        req = core.create_namespaced_service_account_token(
            EXEC_SA, box.names.rehearsal_ns, {"spec": {"expirationSeconds": 600}}
        )
        base = self._client().configuration
        cfg = k8s.Configuration()
        cfg.host = base.host
        cfg.ssl_ca_cert = base.ssl_ca_cert
        cfg.verify_ssl = base.verify_ssl
        cfg.api_key = {"authorization": f"Bearer {req.status.token}"}
        box.exec_api = k8s.ApiClient(cfg)
        box.exec_token_exp = time.time() + 600
        return box.exec_api

    async def exec(
        self, box: Sandbox, argv: list[str], timeout_s: int = 60, as_provisioner: bool = False
    ) -> tuple[int, str, str]:
        """Run ``argv`` in the sandbox container. The Pod UID is re-read and compared first (V3b)."""

        def work() -> tuple[int, str, str]:
            api = self._client() if as_provisioner else self._exec_api(box)
            core = k8s.CoreV1Api(api)
            pod = core.read_namespaced_pod(box.names.sandbox_pod, box.names.rehearsal_ns)
            if str(pod.metadata.uid) != box.pod_uid:
                raise ClusterError("sandbox_uid_mismatch")
            ws = stream(
                k8s.CoreV1Api(api).connect_get_namespaced_pod_exec, box.names.sandbox_pod, box.names.rehearsal_ns,
                command=argv, container=SANDBOX_CONTAINER, stderr=True, stdin=False, stdout=True, tty=False,
                _preload_content=False,
            )
            out, err = [], []
            deadline = time.monotonic() + timeout_s
            while ws.is_open() and time.monotonic() < deadline:
                ws.update(timeout=1)
                if ws.peek_stdout():
                    out.append(ws.read_stdout())
                if ws.peek_stderr():
                    err.append(ws.read_stderr())
            timed_out = ws.is_open()
            ws.close()
            rc = 124 if timed_out else (ws.returncode if ws.returncode is not None else 1)
            return rc, "".join(out), "".join(err)

        try:
            return await self._run(work)
        except ApiException as exc:
            return 126, "", f"forbidden:{exc.status}"

    async def configmap(self, box: Sandbox, verb: str, manifest: dict[str, Any] | None = None,
                        name: str | None = None) -> dict[str, Any]:
        """get/describe/apply/diff on ConfigMaps with the exec identity only (namespace-bound)."""

        def work() -> dict[str, Any]:
            core = k8s.CoreV1Api(self._exec_api(box))
            ns = box.names.rehearsal_ns
            if verb in ("get", "describe"):
                if name:
                    cm = core.read_namespaced_config_map(name, ns)
                    return {"name": cm.metadata.name, "data": cm.data or {}}
                return {"items": [i.metadata.name for i in core.list_namespaced_config_map(ns).items]}
            assert manifest is not None
            target = manifest["metadata"]["name"]
            try:
                current = core.read_namespaced_config_map(target, ns).data or {}
            except ApiException as exc:
                if exc.status != 404:
                    raise
                current = None
            if verb == "diff":
                return {"name": target, "exists": current is not None, "changed": current != manifest.get("data", {})}
            body = {"apiVersion": "v1", "kind": "ConfigMap",
                    "metadata": {"name": target, "labels": {ROLE_LABEL: "agent-applied"}},
                    "data": manifest.get("data", {})}
            if current is None:
                core.create_namespaced_config_map(ns, body)
            else:
                core.replace_namespaced_config_map(target, ns, body)
            return {"name": target, "applied": True}

        try:
            return await self._run(work)
        except ApiException as exc:
            return {"error": f"forbidden:{exc.status}" if exc.status in (401, 403) else f"k8s_{exc.status}"}

    async def logs(self, box: Sandbox, kind: str, name: str) -> str:
        def work() -> str:
            core = k8s.CoreV1Api(self._exec_api(box))
            if kind != "pod":
                return "logs: only pods in P0"
            return str(core.read_namespaced_pod_log(name, box.names.rehearsal_ns, tail_lines=200))

        try:
            return await self._run(work)
        except ApiException as exc:
            return f"error: k8s_{exc.status}"
