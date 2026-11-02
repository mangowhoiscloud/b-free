"""Cluster live view for the console (``GET /v1/clusters``). Read-only: get/list calls only.

Secret objects are never read, not even their metadata. The response carries no env vars, annotations,
tokens or kubeconfig content. A cluster that fails comes back as ``status: unreachable`` with a short
reason; the other clusters still answer.

EKS without the ``aws`` CLI: when the registry entry sets ``provider: eks`` + ``eks_cluster_name`` +
``aws_region``, the collector reads endpoint and CA with ``eks:DescribeCluster`` and builds the bearer token
from a presigned ``sts:GetCallerIdentity`` URL (the same token ``aws eks get-token`` prints). With
``api_server`` the connection goes to that link address (D-16 ``src-k8s.bravo-link.svc``) and TLS checks the EKS
CA and the real endpoint host name; only the network path changes.
"""

from __future__ import annotations

import base64
import os
import re
import tempfile
import threading
import time
import urllib.parse
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from botocore.signers import RequestSigner
from kubernetes import client, config
from kubernetes.client import ApiClient
from kubernetes.client.rest import ApiException

from .environments import Environment, KubernetesSection, Registry
from .kubernetes import load_api_client
from .redact import redact

CALL_TIMEOUT_S = 5  # per Kubernetes / AWS call
CLUSTER_DEADLINE_S = 12  # whole cluster, then "unreachable"
CACHE_TTL_S = 4.0
EKS_DESCRIBE_TTL_S = 600.0
TOKEN_PREFIX = "k8s-aws-v1."  # noqa: S105 - token format prefix, not a secret
TOKEN_EXPIRES_S = 60

_BOTO_CONFIG = Config(retries={"max_attempts": 1, "mode": "standard"}, connect_timeout=3, read_timeout=CALL_TIMEOUT_S)
_REDACT = re.compile(r"k8s-aws-v1\.[A-Za-z0-9_\-]+|Bearer\s+\S+|X-Amz-[A-Za-z\-]+=[^&\s]+", re.IGNORECASE)

# Get/list calls this module makes (README, ClusterRole). No secrets.
READ_ONLY_RULES: tuple[tuple[str, str, str], ...] = (
    ("", "nodes", "list"),
    ("", "namespaces", "list"),
    ("", "pods", "list"),
    ("apps", "deployments,statefulsets,daemonsets", "list"),
    ("storage.k8s.io", "storageclasses", "list"),
)


class NotConfigured(Exception):
    """The registry names a cluster but this process has no way to reach it (missing kubeconfig etc.)."""


def short_error(exc: BaseException) -> str:
    """Short reason for the console. No response headers, no tokens."""
    if isinstance(exc, ApiException):
        msg = f"HTTP {exc.status} {exc.reason or ''}".strip()
    elif isinstance(exc, ClientError):
        msg = f"AWS {exc.response.get('Error', {}).get('Code', 'error')}"
    else:
        text = redact(str(exc)).strip().splitlines()
        msg = f"{type(exc).__name__}: {text[0]}" if text else type(exc).__name__
    return _REDACT.sub("[redacted]", msg)[:200]


# --- EKS auth (boto3, no aws CLI) ---------------------------------------------------------------------------


def eks_token(session: boto3.Session, cluster_name: str, region: str) -> str:
    """Bearer token for EKS: ``k8s-aws-v1.`` + base64url(presigned STS GetCallerIdentity URL), no padding."""
    creds = session.get_credentials()
    if creds is None:
        raise NotConfigured("no AWS credentials in the default chain")
    sts = session.client("sts", region_name=region, config=_BOTO_CONFIG)
    signer = RequestSigner(sts.meta.service_model.service_id, region, "sts", "v4", creds, session.events)
    params = {
        "method": "GET",
        "url": f"https://sts.{region}.amazonaws.com/?Action=GetCallerIdentity&Version=2011-06-15",
        "body": {},
        "headers": {"x-k8s-aws-id": cluster_name},
        "context": {},
    }
    url = signer.generate_presigned_url(params, region_name=region, expires_in=TOKEN_EXPIRES_S, operation_name="")
    return TOKEN_PREFIX + base64.urlsafe_b64encode(url.encode("utf-8")).decode("ascii").rstrip("=")


@dataclass(frozen=True)
class _EksEndpoint:
    host: str
    ca_file: str
    at: float


_eks_cache: dict[tuple[str, str], _EksEndpoint] = {}
_eks_lock = threading.Lock()


def _eks_endpoint(session: boto3.Session, cluster_name: str, region: str) -> _EksEndpoint:
    key = (cluster_name, region)
    with _eks_lock:
        hit = _eks_cache.get(key)
        if hit and time.monotonic() - hit.at < EKS_DESCRIBE_TTL_S:
            return hit
    eks = session.client("eks", region_name=region, config=_BOTO_CONFIG)
    cluster = eks.describe_cluster(name=cluster_name)["cluster"]
    ca = base64.b64decode(cluster["certificateAuthority"]["data"])
    fd, path = tempfile.mkstemp(prefix=f"eks-{cluster_name}-", suffix=".crt")
    with os.fdopen(fd, "wb") as f:
        f.write(ca)
    ep = _EksEndpoint(host=cluster["endpoint"], ca_file=path, at=time.monotonic())
    with _eks_lock:
        _eks_cache[key] = ep
    return ep


def eks_api_client(cluster_name: str, region: str, session: boto3.Session | None = None,
                   api_server: str | None = None, tls_server_name: str | None = None) -> ApiClient:
    """``api_server`` set: dial the link (D-16 ``src-k8s``) and check the certificate against the EKS CA and the
    real endpoint host (``tls_server_name`` or the DescribeCluster endpoint host). Verification stays on."""
    session = session or boto3.Session()
    ep = _eks_endpoint(session, cluster_name, region)
    cfg = client.Configuration()
    cfg.host = ep.host
    if api_server:
        cfg.host = api_server.rstrip("/")
        cfg.tls_server_name = tls_server_name or urllib.parse.urlsplit(ep.host).hostname
    cfg.ssl_ca_cert = ep.ca_file
    cfg.verify_ssl = True
    cfg.api_key = {"authorization": eks_token(session, cluster_name, region)}
    cfg.api_key_prefix = {"authorization": "Bearer"}
    return ApiClient(configuration=cfg)


def is_eks(section: KubernetesSection) -> bool:
    return (section.provider or "").lower() == "eks" and bool(section.eks_cluster_name and section.aws_region)


def eks_api_client_for(section: KubernetesSection) -> ApiClient:
    assert section.eks_cluster_name and section.aws_region
    return eks_api_client(section.eks_cluster_name, section.aws_region, api_server=section.api_server,
                          tls_server_name=section.tls_server_name)


def default_api_factory(section: KubernetesSection) -> ApiClient:
    if is_eks(section):
        return eks_api_client_for(section)
    if section.kubeconfig_env and not os.environ.get(section.kubeconfig_env):
        raise NotConfigured(f"environment variable {section.kubeconfig_env} (kubeconfig path) is not set")
    try:
        return load_api_client(section)
    except (config.ConfigException, FileNotFoundError) as exc:
        raise NotConfigured(f"kubeconfig not usable: {short_error(exc)}") from exc


# --- landing zone rules (docs/architecture/11-landing-zone.md §2) -------------------------------------------


@dataclass(frozen=True)
class LzRule:
    component: str
    namespace: str | None  # expected namespace; None -> any namespace
    kind: str | None  # Deployment / StatefulSet / DaemonSet; "StorageClass", "PodSecurity", "NamespacePrefix"
    names: tuple[str, ...] = ()  # workload name prefixes (or storage class names, namespace prefixes)
    missing: str | None = None  # detail when nothing is found (e.g. not detectable by get/list)


LANDING_ZONE_RULES: tuple[LzRule, ...] = (
    LzRule("cloudnative-pg", "cnpg-system", "Deployment", ("cnpg-controller-manager", "cnpg-cloudnative-pg")),
    LzRule("pgbouncer", None, "StatefulSet", ("pgbouncer",)),
    LzRule("object-storage (rook-ceph rgw)", "rook-ceph", "Deployment", ("rook-ceph-rgw",),
           "on-prem engine; on EKS/GKE the engine is S3/Cloud Storage outside the cluster (not visible by get/list)"),
    LzRule("storageclass bravo-db", None, "StorageClass", ("bravo-db",)),
    LzRule("storageclass bravo-general", None, "StorageClass", ("bravo-general",)),
    LzRule("external-secrets", "external-secrets", "Deployment", ("external-secrets",)),
    LzRule("cilium", "kube-system", "DaemonSet", ("cilium",)),
    LzRule("hubble-relay", "kube-system", "Deployment", ("hubble-relay",)),
    LzRule("gateway-api (cilium envoy)", "kube-system", "DaemonSet", ("cilium-envoy",),
           "GatewayClass/Gateway objects are not checked; only the cilium-envoy DaemonSet"),
    LzRule("kyverno", "kyverno", "Deployment", ("kyverno-admission-controller", "kyverno")),
    LzRule("opa-gatekeeper", "gatekeeper-system", "Deployment", ("gatekeeper-controller-manager",)),
    LzRule("pod-security restricted", None, "PodSecurity", ("restricted",)),
    LzRule("argo-cd", "argocd", "Deployment", ("argocd-server", "argo-cd-argocd-server")),
    LzRule("cf-edge (cloudflare tunnel)", None, "Deployment", ("cf-edge", "cloudflared")),
    LzRule("bravo-link", None, "Deployment", ("bravo-link",)),
    LzRule("otel-collector agent", None, "DaemonSet", ("otel-collector", "opentelemetry-collector", "otel-agent")),
    LzRule("rehearsal namespaces", None, "NamespacePrefix", ("rehearsal-", "verify-"),
           "created per trial; none exist now"),
)


def _ready(w: dict[str, Any]) -> bool:
    return w["ready"] >= w["desired"]


# Which list read each rule depends on. A failed read makes the row unknown (present: null), not absent.
def _depends_on(r: LzRule) -> str:
    if r.kind == "StorageClass":
        return "storageclasses"
    if r.kind in ("PodSecurity", "NamespacePrefix"):
        return "namespaces"
    return "workloads"


def detect_landing_zone(
    namespaces: list[dict[str, Any]],
    workloads: list[dict[str, Any]],
    storage_classes: list[str],
    restricted_namespaces: list[str],
    failed_reads: frozenset[str] = frozenset(),
) -> list[dict[str, Any]]:
    ns_names = {n["name"] for n in namespaces}
    out = []
    for r in LANDING_ZONE_RULES:
        row: dict[str, Any] = {"component": r.component, "namespace": r.namespace, "present": False,
                               "ready": None, "detail": None}
        dep = _depends_on(r)
        if dep in failed_reads:
            # Audit 36: a read that failed is not evidence of absence.
            row.update(present=None, detail=f"unknown: {dep} could not be read")
            out.append(row)
            continue
        if r.kind == "StorageClass":
            row["present"] = any(n in storage_classes for n in r.names)
            row["detail"] = None if row["present"] else "StorageClass not found"
        elif r.kind == "PodSecurity":
            row["present"] = bool(restricted_namespaces)
            row["detail"] = (f"{len(restricted_namespaces)} namespace(s) enforce restricted" if restricted_namespaces
                             else "no namespace has pod-security.kubernetes.io/enforce=restricted")
        elif r.kind == "NamespacePrefix":
            hits = sorted(n for n in ns_names if n.startswith(r.names))
            row["present"] = bool(hits)
            row["detail"] = f"{len(hits)} namespace(s)" if hits else r.missing
        else:
            hits = [w for w in workloads if w["kind"] == r.kind and w["name"].startswith(r.names)
                    and (r.namespace is None or w["namespace"] == r.namespace)]
            if hits:
                row.update(present=True, ready=all(_ready(w) for w in hits), namespace=hits[0]["namespace"],
                           detail=", ".join(f"{w['kind']}/{w['name']} {w['ready']}/{w['desired']}" for w in hits)[:200])
            elif r.namespace and r.namespace in ns_names:
                row["detail"] = f"namespace {r.namespace} exists, no {r.kind} {'/'.join(r.names)}"
            else:
                row["detail"] = r.missing or f"no {r.kind} named {'/'.join(r.names)}"
        out.append(row)
    return out


# --- per-cluster collection ---------------------------------------------------------------------------------


def _provider(section: KubernetesSection) -> str:
    p = (section.provider or "").lower()
    return p if p in ("eks", "k3s") else "kubernetes"


def _nodes(api: ApiClient) -> list[dict[str, Any]]:
    out = []
    for n in client.CoreV1Api(api).list_node(_request_timeout=CALL_TIMEOUT_S).items:
        labels = n.metadata.labels or {}
        conds = (n.status.conditions or []) if n.status else []
        info = n.status.node_info if n.status else None
        roles = {k.split("/", 1)[1] for k in labels if k.startswith("node-role.kubernetes.io/")}
        if labels.get("kubernetes.io/role"):
            roles.add(labels["kubernetes.io/role"])
        out.append({
            "name": n.metadata.name,
            "ready": any(c.type == "Ready" and c.status == "True" for c in conds),
            "kubelet_version": info.kubelet_version if info else None,
            "roles": sorted(r for r in roles if r),
            "instance_type": labels.get("node.kubernetes.io/instance-type"),
            "zone": labels.get("topology.kubernetes.io/zone"),
        })
    return sorted(out, key=lambda x: x["name"])


def _namespaces(api: ApiClient) -> tuple[list[dict[str, Any]], list[str]]:
    out, restricted = [], []
    for ns in client.CoreV1Api(api).list_namespace(_request_timeout=CALL_TIMEOUT_S).items:
        out.append({"name": ns.metadata.name, "phase": ns.status.phase if ns.status else None})
        if (ns.metadata.labels or {}).get("pod-security.kubernetes.io/enforce") == "restricted":
            restricted.append(ns.metadata.name)
    return sorted(out, key=lambda x: x["name"]), sorted(restricted)


def _workloads(api: ApiClient) -> list[dict[str, Any]]:
    apps = client.AppsV1Api(api)
    out = []
    for d in apps.list_deployment_for_all_namespaces(_request_timeout=CALL_TIMEOUT_S).items:
        out.append({"namespace": d.metadata.namespace, "kind": "Deployment", "name": d.metadata.name,
                    "desired": (d.spec.replicas if d.spec else 0) or 0,
                    "ready": (d.status.ready_replicas if d.status else 0) or 0})
    for s in apps.list_stateful_set_for_all_namespaces(_request_timeout=CALL_TIMEOUT_S).items:
        out.append({"namespace": s.metadata.namespace, "kind": "StatefulSet", "name": s.metadata.name,
                    "desired": (s.spec.replicas if s.spec else 0) or 0,
                    "ready": (s.status.ready_replicas if s.status else 0) or 0})
    for ds in apps.list_daemon_set_for_all_namespaces(_request_timeout=CALL_TIMEOUT_S).items:
        out.append({"namespace": ds.metadata.namespace, "kind": "DaemonSet", "name": ds.metadata.name,
                    "desired": (ds.status.desired_number_scheduled if ds.status else 0) or 0,
                    "ready": (ds.status.number_ready if ds.status else 0) or 0})
    return sorted(out, key=lambda x: (x["namespace"], x["kind"], x["name"]))


def _pods(api: ApiClient) -> dict[str, int]:
    counts: dict[str, int] = {}
    for p in client.CoreV1Api(api).list_pod_for_all_namespaces(_request_timeout=CALL_TIMEOUT_S).items:
        phase = (p.status.phase if p.status else None) or "Unknown"
        counts[phase] = counts.get(phase, 0) + 1
    return dict(sorted(counts.items()))


def _storage_classes(api: ApiClient) -> list[str]:
    items = client.StorageV1Api(api).list_storage_class(_request_timeout=CALL_TIMEOUT_S).items
    return sorted(sc.metadata.name for sc in items)


def collect_cluster(env: Environment, api: ApiClient) -> dict[str, Any]:
    section = env.kubernetes
    assert section is not None
    row: dict[str, Any] = {
        "cluster_id": env.environment_id,
        "display_name": env.display_name,
        "provider": _provider(section),
        "context": section.context,
        "status": "ok",
        "error": None,
        "server_version": None,
        "nodes": [],
        "namespaces": [],
        "workloads": [],
        "pods": {},
        "landing_zone": [],
    }
    try:
        row["server_version"] = client.VersionApi(api).get_code(_request_timeout=CALL_TIMEOUT_S).git_version
    except Exception as exc:  # noqa: BLE001 - first call decides reachability
        row.update(status="unreachable", error=short_error(exc))
        return row
    errors: list[str] = []
    failed: set[str] = set()

    def take(label: str, fn: Callable[[], Any], default: Any) -> Any:
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - partial view, reason in "error"
            errors.append(f"{label}: {short_error(exc)}")
            failed.add(label)
            return default

    row["nodes"] = take("nodes", lambda: _nodes(api), [])
    row["namespaces"], restricted = take("namespaces", lambda: _namespaces(api), ([], []))
    row["workloads"] = take("workloads", lambda: _workloads(api), [])
    row["pods"] = take("pods", lambda: _pods(api), {})
    storage_classes = take("storageclasses", lambda: _storage_classes(api), [])
    row["landing_zone"] = detect_landing_zone(row["namespaces"], row["workloads"], storage_classes, restricted,
                                              frozenset(failed))
    if errors:
        row["status"] = "partial"
        row["failed_reads"] = sorted(failed)
        row["error"] = ("partial: " + "; ".join(errors))[:300]
    return row


def _unreachable(env: Environment, error: str) -> dict[str, Any]:
    section = env.kubernetes
    assert section is not None
    return {"cluster_id": env.environment_id, "display_name": env.display_name, "provider": _provider(section),
            "context": section.context, "status": "unreachable", "error": error, "server_version": None,
            "nodes": [], "namespaces": [], "workloads": [], "pods": {}, "landing_zone": []}


def _one(env: Environment, factory: Callable[[KubernetesSection], ApiClient]) -> dict[str, Any]:
    assert env.kubernetes is not None
    try:
        api = factory(env.kubernetes)
    except NotConfigured as exc:
        return {"not_configured": {"cluster_id": env.environment_id, "reason": short_error_text(exc)}}
    except Exception as exc:  # noqa: BLE001 - auth / describe failures
        return _unreachable(env, short_error(exc))
    try:
        return collect_cluster(env, api)
    finally:
        try:
            api.close()
        except Exception:  # noqa: BLE001,S110 - best effort
            pass


def short_error_text(exc: BaseException) -> str:
    return _REDACT.sub("[redacted]", str(exc))[:200]


def collect_clusters(
    registry: Registry, factory: Callable[[KubernetesSection], ApiClient] = default_api_factory,
) -> dict[str, Any]:
    envs = [e for _, e in sorted(registry.environments.items()) if e.kubernetes is not None]
    clusters: list[dict[str, Any]] = []
    not_configured: list[dict[str, Any]] = []
    if envs:
        pool = ThreadPoolExecutor(max_workers=min(8, len(envs)), thread_name_prefix="clusters")
        futs = {pool.submit(_one, e, factory): e for e in envs}
        wait(futs, timeout=CLUSTER_DEADLINE_S)
        for fut, env in futs.items():
            if not fut.done():
                clusters.append(_unreachable(env, f"timed out after {CLUSTER_DEADLINE_S}s"))
                continue
            res = fut.result()
            if "not_configured" in res:
                not_configured.append(res["not_configured"])
            else:
                clusters.append(res)
        pool.shutdown(wait=False, cancel_futures=True)
    return {
        "observed_at": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "clusters": clusters,
        "not_configured": not_configured,
    }


class ClusterView:
    """Small in-process cache so a polling console does not hit every API server on each request."""

    def __init__(self, factory: Callable[[KubernetesSection], ApiClient] = default_api_factory,
                 ttl_s: float = CACHE_TTL_S) -> None:
        self.factory = factory
        self.ttl_s = ttl_s
        self._lock = threading.Lock()
        self._cached: tuple[float, dict[str, Any]] | None = None

    def get(self, registry: Registry) -> dict[str, Any]:
        with self._lock:
            if self._cached and time.monotonic() - self._cached[0] < self.ttl_s:
                return self._cached[1]
            body = collect_clusters(registry, self.factory)
            self._cached = (time.monotonic(), body)
            return body
