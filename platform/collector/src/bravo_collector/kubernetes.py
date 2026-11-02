"""Kubernetes read-only collection (FR-1). Only get/list calls.

Secrets are listed as PartialObjectMetadataList, so the API server never sends secret values.
"""

from __future__ import annotations

import os
from typing import Any

from kubernetes import client, config
from kubernetes.client import ApiClient

from .environments import KubernetesSection
from .model import Item, failed, observe

SYSTEM_NAMESPACES = frozenset({"kube-system", "kube-public", "kube-node-lease"})
PSA_PREFIX = "pod-security.kubernetes.io/"
IRSA_ANNOTATION = "eks.amazonaws.com/role-arn"
PARTIAL_METADATA_LIST = "application/json;as=PartialObjectMetadataList;g=meta.k8s.io;v=v1"

# Verbs and resources this module needs (README, ClusterRole).
READ_ONLY_RULES: tuple[tuple[str, str, str], ...] = (
    ("", "namespaces", "get,list"),
    ("", "serviceaccounts", "list"),
    ("", "secrets", "list (metadata only)"),
    ("", "services", "list"),
    ("apps", "deployments", "list"),
    ("networking.k8s.io", "networkpolicies", "list"),
    ("rbac.authorization.k8s.io", "rolebindings", "list"),
)


def load_api_client(section: KubernetesSection) -> ApiClient:
    path = os.environ.get(section.kubeconfig_env) if section.kubeconfig_env else None
    if section.kubeconfig_env and not path:
        raise RuntimeError(f"environment variable {section.kubeconfig_env} is not set")
    cfg = client.Configuration()
    config.load_kube_config(config_file=path, context=section.context, client_configuration=cfg)
    return ApiClient(configuration=cfg)


def _default_deny(spec: Any, kind: str) -> bool:
    types = list(spec.policy_types or [])
    selector = spec.pod_selector
    empty_selector = selector is None or (not selector.match_labels and not selector.match_expressions)
    rules = spec.ingress if kind == "Ingress" else spec.egress
    return empty_selector and kind in types and not rules


def _selector(sel: Any) -> dict[str, Any]:
    if sel is None:
        return {}
    out: dict[str, Any] = {}
    if sel.match_labels:
        out["matchLabels"] = dict(sorted(sel.match_labels.items()))
    if sel.match_expressions:
        out["matchExpressions"] = [
            {"key": e.key, "operator": e.operator, "values": sorted(e.values or [])} for e in sel.match_expressions
        ]
    return out


class KubernetesCollector:
    def __init__(self, section: KubernetesSection, api: ApiClient | None = None) -> None:
        self.section = section
        self._api = api

    def api(self) -> ApiClient:
        if self._api is None:
            self._api = load_api_client(self.section)
        return self._api

    def server_version(self) -> Item:
        def call() -> dict[str, Any]:
            v = client.VersionApi(self.api()).get_code()
            return {"git_version": v.git_version, "platform": v.platform}

        return observe(call)

    def namespaces(self) -> Item:
        def call() -> list[dict[str, Any]]:
            core = client.CoreV1Api(self.api())
            wanted = set(self.section.namespaces)
            out = []
            for ns in core.list_namespace().items:
                name = ns.metadata.name
                if wanted and name not in wanted:
                    continue
                if not wanted and name in SYSTEM_NAMESPACES:
                    continue
                labels = ns.metadata.labels or {}
                out.append({
                    "name": name,
                    "pod_security": {k[len(PSA_PREFIX):]: v for k, v in sorted(labels.items())
                                     if k.startswith(PSA_PREFIX)},
                })
            missing = sorted(wanted - {n["name"] for n in out})
            for name in missing:
                out.append({"name": name, "missing": True})
            return sorted(out, key=lambda x: x["name"])

        return observe(call)

    def network_policies(self, ns: str) -> Item:
        def call() -> list[dict[str, Any]]:
            net = client.NetworkingV1Api(self.api())
            out = []
            for p in net.list_namespaced_network_policy(ns).items:
                s = p.spec
                out.append({
                    "name": p.metadata.name,
                    "pod_selector": _selector(s.pod_selector),
                    "policy_types": sorted(s.policy_types or []),
                    "ingress_rules": len(s.ingress or []),
                    "egress_rules": len(s.egress or []),
                    "default_deny_ingress": _default_deny(s, "Ingress"),
                    "default_deny_egress": _default_deny(s, "Egress"),
                })
            return sorted(out, key=lambda x: x["name"])

        return observe(call)

    def service_accounts(self, ns: str) -> Item:
        def call() -> list[dict[str, Any]]:
            core = client.CoreV1Api(self.api())
            out = []
            for sa in core.list_namespaced_service_account(ns).items:
                ann = sa.metadata.annotations or {}
                out.append({
                    "name": sa.metadata.name,
                    "automount_token": sa.automount_service_account_token,
                    "irsa_role_arn": ann.get(IRSA_ANNOTATION),
                })
            return sorted(out, key=lambda x: x["name"])

        return observe(call)

    def secrets_presence(self, ns: str) -> Item:
        """Names and metadata only. The request asks for PartialObjectMetadataList; values never arrive."""

        def call() -> list[dict[str, Any]]:
            api = self.api()
            resp = api.call_api(
                f"/api/v1/namespaces/{ns}/secrets", "GET",
                header_params={"Accept": PARTIAL_METADATA_LIST},
                auth_settings=["BearerToken"], response_type="object", _return_http_data_only=True,
            )
            if not isinstance(resp, dict) or resp.get("kind") != "PartialObjectMetadataList":
                raise RuntimeError("server did not return PartialObjectMetadataList; refusing to read secrets")
            out = []
            for item in resp.get("items", []):
                md = item.get("metadata", {})
                labels = md.get("labels") or {}
                out.append({
                    "name": md.get("name"),
                    "created_at": md.get("creationTimestamp"),
                    "owner_kinds": sorted({o.get("kind") for o in md.get("ownerReferences") or []}),
                    "managed_by": labels.get("app.kubernetes.io/managed-by"),
                })
            return sorted(out, key=lambda x: x["name"] or "")

        return observe(call)

    def deployments(self, ns: str) -> Item:
        def call() -> list[dict[str, Any]]:
            apps = client.AppsV1Api(self.api())
            out = []
            for d in apps.list_namespaced_deployment(ns).items:
                pod = d.spec.template.spec
                sc = pod.security_context
                out.append({
                    "name": d.metadata.name,
                    "service_account": pod.service_account_name or "default",
                    "automount_token": pod.automount_service_account_token,
                    "run_as_non_root": sc.run_as_non_root if sc else None,
                })
            return sorted(out, key=lambda x: x["name"])

        return observe(call)

    def services(self, ns: str) -> Item:
        def call() -> list[dict[str, Any]]:
            core = client.CoreV1Api(self.api())
            out = []
            for s in core.list_namespaced_service(ns).items:
                out.append({
                    "name": s.metadata.name,
                    "type": s.spec.type,
                    "ports": sorted(
                        [{"port": p.port, "protocol": p.protocol, "node_port": p.node_port}
                         for p in s.spec.ports or []],
                        key=lambda x: (x["port"], x["protocol"] or ""),
                    ),
                })
            return sorted(out, key=lambda x: x["name"])

        return observe(call)

    def role_bindings(self, ns: str) -> Item:
        def call() -> list[dict[str, Any]]:
            rbac = client.RbacAuthorizationV1Api(self.api())
            out = []
            for rb in rbac.list_namespaced_role_binding(ns).items:
                out.append({
                    "name": rb.metadata.name,
                    "role_ref": {"kind": rb.role_ref.kind, "name": rb.role_ref.name},
                    "subjects": sorted(
                        [{"kind": s.kind, "name": s.name, "namespace": s.namespace} for s in rb.subjects or []],
                        key=lambda x: (x["kind"], x["name"], x["namespace"] or ""),
                    ),
                })
            return sorted(out, key=lambda x: x["name"])

        return observe(call)

    def collect(self) -> dict[str, Any]:
        try:
            self.api()
        except Exception as exc:  # noqa: BLE001 - kubeconfig missing or invalid
            item = failed(exc)
            return {"server_version": item, "namespaces": item, "by_namespace": {}}
        data: dict[str, Any] = {"server_version": self.server_version()}
        nss = self.namespaces()
        data["namespaces"] = nss
        by_ns: dict[str, Any] = {}
        if nss.status == "ok":
            for ns in nss.value:
                if ns.get("missing"):
                    continue
                name = ns["name"]
                by_ns[name] = {
                    "network_policies": self.network_policies(name),
                    "service_accounts": self.service_accounts(name),
                    "secrets": self.secrets_presence(name),
                    "deployments": self.deployments(name),
                    "services": self.services(name),
                    "role_bindings": self.role_bindings(name),
                }
        data["by_namespace"] = by_ns
        return data
