"""GET /v1/clusters without a cluster: a fake ApiClient answers the get/list paths and records every path."""

from __future__ import annotations

import base64
import urllib.parse
from types import SimpleNamespace as NS

import boto3
import pytest
from fastapi.testclient import TestClient
from kubernetes import client
from kubernetes.client import ApiClient, Configuration
from kubernetes.client.rest import ApiException

from bravo_collector import clusters as cl
from bravo_collector.app import create_app
from bravo_collector.environments import KubernetesSection, parse_registry

REGISTRY = parse_registry({
    "environments": {
        "bravo-source": {"display_name": "source EKS",
                         "kubernetes": {"provider": "eks", "eks_cluster_name": "bravo-source",
                                        "aws_region": "ap-northeast-2", "context": "bravo-source"}},
        "broken": {"kubernetes": {"context": "broken"}},
        "onprem-k3s": {"kubernetes": {"provider": "k3s", "kubeconfig_env": "BRAVO_TEST_UNSET_KUBECONFIG"}},
        "pg-only": {"postgres": {"dsn_env": "X"}},
    }
})


def _meta(name, namespace=None, labels=None):
    return NS(name=name, namespace=namespace, labels=labels or {}, annotations={"secret": "never-returned"})


def _deploy(ns, name, desired, ready):
    return NS(metadata=_meta(name, ns), spec=NS(replicas=desired), status=NS(ready_replicas=ready))


def _ds(ns, name, desired, ready):
    return NS(metadata=_meta(name, ns), spec=NS(), status=NS(desired_number_scheduled=desired, number_ready=ready))


def _pod(phase):
    return NS(metadata=_meta("p", "default"), status=NS(phase=phase))


RESPONSES = {
    "/version/": NS(git_version="v1.36.5-eks-6e7947a"),
    "/api/v1/nodes": NS(items=[NS(
        metadata=_meta("ip-10-10-11-107", labels={"node.kubernetes.io/instance-type": "t3.large",
                                                   "topology.kubernetes.io/zone": "ap-northeast-2a",
                                                   "node-role.kubernetes.io/worker": ""}),
        status=NS(conditions=[NS(type="Ready", status="True")], node_info=NS(kubelet_version="v1.36.5")),
    )]),
    "/api/v1/namespaces": NS(items=[
        NS(metadata=_meta("default"), status=NS(phase="Active")),
        NS(metadata=_meta("kube-system"), status=NS(phase="Active")),
        NS(metadata=_meta("external-secrets"), status=NS(phase="Active")),
        NS(metadata=_meta("chat", labels={"pod-security.kubernetes.io/enforce": "restricted"}),
           status=NS(phase="Active")),
    ]),
    "/apis/apps/v1/deployments": NS(items=[
        _deploy("kube-system", "coredns", 2, 2),
        _deploy("external-secrets", "external-secrets", 1, 0),
    ]),
    "/apis/apps/v1/statefulsets": NS(items=[]),
    "/apis/apps/v1/daemonsets": NS(items=[_ds("kube-system", "cilium", 1, 1)]),
    "/api/v1/pods": NS(items=[_pod("Running"), _pod("Running"), _pod("Pending")]),
    "/apis/storage.k8s.io/v1/storageclasses": NS(items=[NS(metadata=_meta("gp2")), NS(metadata=_meta("bravo-db"))]),
}


class FakeApi(ApiClient):
    """Answers every generated API method with canned objects (no HTTP). Records every path."""

    def __init__(self, fail: bool = False) -> None:
        super().__init__(configuration=Configuration())
        self.fail = fail
        self.paths: list[str] = []

    def _call_with_legacy_options(self, request, response_types_map, async_req, preload_content, request_timeout,
                                  return_http_data_only):
        method, url = request[0], request[1]
        path = urllib.parse.urlsplit(url).path
        self.paths.append(path)
        assert method == "GET", f"write call {method} {path}"
        assert request_timeout, "every call needs a timeout"
        if self.fail:
            raise ApiException(status=401, reason="Unauthorized")
        return RESPONSES[path]

    def call_api(self, *args, **kwargs):
        raise AssertionError("no real HTTP in unit tests")


@pytest.fixture
def apis(monkeypatch):
    monkeypatch.delenv("BRAVO_TEST_UNSET_KUBECONFIG", raising=False)
    made: dict[str, FakeApi] = {}

    def factory(section: KubernetesSection) -> ApiClient:
        if section.kubeconfig_env and not __import__("os").environ.get(section.kubeconfig_env):
            raise cl.NotConfigured(f"environment variable {section.kubeconfig_env} (kubeconfig path) is not set")
        api = FakeApi(fail=section.context == "broken")
        made[section.context or ""] = api
        return api

    def boom(*_a, **_k):
        raise AssertionError("Secret API must never be called")

    for name in dir(client.CoreV1Api):
        if "secret" in name and not name.startswith("_"):
            monkeypatch.setattr(client.CoreV1Api, name, boom)
    return factory, made


def test_response_shape_failure_isolation_and_not_configured(apis):
    factory, made = apis
    with TestClient(create_app(REGISTRY, cl.ClusterView(factory))) as c:
        r = c.get("/v1/clusters")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"observed_at", "clusters", "not_configured", "credential_source"}
    assert body["observed_at"].endswith("Z")
    by_id = {x["cluster_id"]: x for x in body["clusters"]}
    assert set(by_id) == {"bravo-source", "broken"}

    src = by_id["bravo-source"]
    assert set(src) == {"cluster_id", "display_name", "provider", "context", "status", "error", "server_version",
                        "nodes", "namespaces", "workloads", "pods", "landing_zone"}
    assert (src["status"], src["error"], src["provider"]) == ("ok", None, "eks")
    assert src["server_version"] == "v1.36.5-eks-6e7947a"
    assert src["nodes"] == [{"name": "ip-10-10-11-107", "ready": True, "kubelet_version": "v1.36.5",
                             "roles": ["worker"], "instance_type": "t3.large", "zone": "ap-northeast-2a"}]
    assert {"name": "chat", "phase": "Active"} in src["namespaces"]
    assert {"namespace": "external-secrets", "kind": "Deployment", "name": "external-secrets", "desired": 1,
            "ready": 0} in src["workloads"]
    assert src["pods"] == {"Pending": 1, "Running": 2}

    bad = by_id["broken"]
    assert bad["status"] == "unreachable" and bad["error"] == "HTTP 401 Unauthorized"
    assert bad["provider"] == "kubernetes" and bad["nodes"] == []

    assert body["not_configured"] == [{"cluster_id": "onprem-k3s",
                                       "reason": "environment variable BRAVO_TEST_UNSET_KUBECONFIG "
                                                 "(kubeconfig path) is not set"}]
    # Annotations of the fake objects never reach the response.
    assert "never-returned" not in r.text


def test_never_calls_secret_api(apis):
    factory, made = apis
    cl.collect_clusters(REGISTRY, factory)
    paths = made["bravo-source"].paths
    assert paths and all("secret" not in p for p in paths)
    assert set(paths) == set(RESPONSES)


def test_landing_zone_detection(apis):
    factory, _ = apis
    src = next(x for x in cl.collect_clusters(REGISTRY, factory)["clusters"] if x["cluster_id"] == "bravo-source")
    lz = {row["component"]: row for row in src["landing_zone"]}
    assert len(lz) == len(cl.LANDING_ZONE_RULES)
    assert all(set(row) == {"component", "namespace", "present", "ready", "detail"} for row in lz.values())
    assert lz["cilium"]["present"] is True and lz["cilium"]["ready"] is True
    assert lz["external-secrets"]["present"] is True and lz["external-secrets"]["ready"] is False
    assert lz["storageclass bravo-db"]["present"] is True
    assert lz["storageclass bravo-general"]["present"] is False
    assert lz["pod-security restricted"]["present"] is True
    assert lz["argo-cd"] == {"component": "argo-cd", "namespace": "argocd", "present": False, "ready": None,
                             "detail": "no Deployment named argocd-server/argo-cd-argocd-server"}
    assert lz["object-storage (rook-ceph rgw)"]["present"] is False
    assert "S3" in lz["object-storage (rook-ceph rgw)"]["detail"]


def test_cache_reuses_result(apis):
    factory, made = apis
    view = cl.ClusterView(factory, ttl_s=60)
    first = view.get(REGISTRY)
    assert view.get(REGISTRY) is first


def test_eks_token_format_offline():
    session = boto3.Session(aws_access_key_id="AKIDEXAMPLE", aws_secret_access_key="example-secret",  # noqa: S106
                            region_name="ap-northeast-2")
    token = cl.eks_token(session, "bravo-source", "ap-northeast-2")
    assert token.startswith("k8s-aws-v1.")
    payload = token[len("k8s-aws-v1."):]
    assert "=" not in payload and "+" not in payload and "/" not in payload
    url = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)).decode()
    parts = urllib.parse.urlsplit(url)
    q = urllib.parse.parse_qs(parts.query)
    assert parts.netloc == "sts.ap-northeast-2.amazonaws.com"
    assert q["Action"] == ["GetCallerIdentity"] and q["X-Amz-Expires"] == ["60"]
    assert "x-k8s-aws-id" in q["X-Amz-SignedHeaders"][0].split(";")


def test_eks_over_link_keeps_tls_name_and_ca(monkeypatch, tmp_path):
    """D-16: api_server changes only where we dial; TLS still checks the EKS CA and the real endpoint host."""
    ca = tmp_path / "eks.crt"
    ca.write_text("ca")
    ep = cl._EksEndpoint(host="https://ABC123.gr7.ap-northeast-2.eks.amazonaws.com", ca_file=str(ca), at=0.0)
    monkeypatch.setattr(cl, "_eks_endpoint", lambda *a: ep)
    session = boto3.Session(aws_access_key_id="AKIDEXAMPLE", aws_secret_access_key="example-secret",  # noqa: S106
                            region_name="ap-northeast-2")
    section = KubernetesSection(provider="eks", eks_cluster_name="bravo-source", aws_region="ap-northeast-2",
                                api_server="https://src-k8s.bravo-link.svc.cluster.local:443/")
    monkeypatch.setattr(cl.boto3, "Session", lambda: session)
    cfg = cl.default_api_factory(section).configuration
    assert cfg.host == "https://src-k8s.bravo-link.svc.cluster.local:443"
    assert cfg.tls_server_name == "abc123.gr7.ap-northeast-2.eks.amazonaws.com"
    assert cfg.verify_ssl is True and cfg.ssl_ca_cert == str(ca)
    assert cfg.api_key["authorization"].startswith("k8s-aws-v1.")
    # The snapshot route (kubernetes.py) takes the same path.
    from bravo_collector.kubernetes import load_api_client

    assert load_api_client(section).configuration.host == cfg.host
    # Without api_server: the public endpoint, no override.
    direct = cl.default_api_factory(section.model_copy(update={"api_server": None})).configuration
    assert direct.host == ep.host and not direct.tls_server_name


def test_short_error_redacts_tokens():
    msg = cl.short_error(RuntimeError("Bearer k8s-aws-v1.abcDEF_123 rejected"))
    assert "abcDEF" not in msg and "redacted" in msg


def test_default_factory_not_configured(monkeypatch):
    monkeypatch.delenv("BRAVO_TEST_UNSET_KUBECONFIG", raising=False)
    with pytest.raises(cl.NotConfigured):
        cl.default_api_factory(KubernetesSection(provider="k3s", kubeconfig_env="BRAVO_TEST_UNSET_KUBECONFIG"))


def test_failed_read_is_unknown_not_absent():
    """Audit 36: when the workloads list fails, workload rows are present=None, not False."""
    rows = cl.detect_landing_zone([{"name": "kube-system"}], [], ["bravo-db"], [], frozenset({"workloads"}))
    by = {r["component"]: r for r in rows}
    assert by["cloudnative-pg"]["present"] is None and "could not be read" in by["cloudnative-pg"]["detail"]
    assert by["storageclass bravo-db"]["present"] is True  # storage classes were read
    rows2 = cl.detect_landing_zone([{"name": "kube-system"}], [], ["bravo-db"], [])
    assert {r["component"]: r for r in rows2}["cloudnative-pg"]["present"] is False
