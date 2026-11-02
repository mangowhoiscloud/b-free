"""Kubernetes API clients for the gate's own read identity, without kubeconfig exec plugins.

eks  endpoint and CA from ``eks:DescribeCluster``; bearer token = ``k8s-aws-v1.`` + base64url of a presigned
     ``sts:GetCallerIdentity`` URL (what ``aws eks get-token`` prints), signed with the gate's AWS identity (standard
     chain; web identity on the platform). With ``api_server`` the client dials that address (a D-16 link such as
     ``src-k8s.bravo-link.svc``) and TLS still checks the EKS CA and the real endpoint host (``tls_server_name``).
gke  ``api_server`` = the D-16 link gke-k8s with ``gke_cluster`` set (cluster CA from the container API, TLS name
     kubernetes.default.svc), or the cluster's public DNS endpoint without it. Bearer token: Google application default
     credentials (Workload Identity Federation on the platform); RBAC sees the impersonated service account's email.
Same approach as platform/collector clusters.py; tokens and kubeconfigs never reach logs or evidence.
"""

from __future__ import annotations

import base64
import os
import tempfile
import urllib.parse
from typing import Any

from ..environments import KubernetesSection

TOKEN_PREFIX = "k8s-aws-v1."  # noqa: S105 - token format prefix, not a secret
TOKEN_EXPIRES_S = 60
# userinfo.email: GKE then names the caller by its email (the RBAC subject), not by the numeric unique id.
GKE_SCOPES = ("https://www.googleapis.com/auth/cloud-platform", "https://www.googleapis.com/auth/userinfo.email")


class NotConfigured(Exception):
    """The registry names a cluster but this process has no way to reach it."""


def eks_token(session: Any, cluster_name: str, region: str) -> str:
    from botocore.signers import RequestSigner

    creds = session.get_credentials()
    if creds is None:
        raise NotConfigured("no AWS credentials in the gate's default chain")
    sts = session.client("sts", region_name=region)
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


def _ca_file(pem: bytes, name: str) -> str:
    fd, path = tempfile.mkstemp(prefix=f"ca-{name}-", suffix=".crt")
    with os.fdopen(fd, "wb") as f:
        f.write(pem)
    return path


def eks_client(s: KubernetesSection) -> Any:
    import boto3
    from kubernetes import client

    if not (s.eks_cluster_name and s.aws_region):
        raise NotConfigured("provider eks needs eks_cluster_name and aws_region")
    session = boto3.session.Session(region_name=s.aws_region)
    cluster = session.client("eks", region_name=s.aws_region).describe_cluster(name=s.eks_cluster_name)["cluster"]
    cfg = client.Configuration()
    cfg.host = cluster["endpoint"]
    if s.api_server:
        cfg.host = s.api_server.rstrip("/")
        cfg.tls_server_name = s.tls_server_name or urllib.parse.urlsplit(cluster["endpoint"]).hostname
    cfg.ssl_ca_cert = _ca_file(base64.b64decode(cluster["certificateAuthority"]["data"]), s.eks_cluster_name)
    cfg.verify_ssl = True
    cfg.api_key = {"authorization": eks_token(session, s.eks_cluster_name, s.aws_region)}
    cfg.api_key_prefix = {"authorization": "Bearer"}
    return client.ApiClient(cfg)


def gke_client(s: KubernetesSection) -> Any:
    import google.auth
    import google.auth.transport.requests
    from kubernetes import client

    if not s.api_server:
        raise NotConfigured("provider gke needs api_server (the D-16 link gke-k8s or the cluster DNS endpoint)")
    creds, _ = google.auth.default(scopes=list(GKE_SCOPES))
    cfg = client.Configuration()
    cfg.host = s.api_server.rstrip("/")
    cfg.verify_ssl = True
    if s.gke_cluster:  # through the D-16 link: cluster CA from the container API, TLS name kubernetes.default.svc
        from google.auth.transport.requests import AuthorizedSession

        r = AuthorizedSession(creds).get(f"https://container.googleapis.com/v1/{s.gke_cluster}",
                                         params={"fields": "masterAuth.clusterCaCertificate"}, timeout=15)
        r.raise_for_status()
        cfg.ssl_ca_cert = _ca_file(base64.b64decode(r.json()["masterAuth"]["clusterCaCertificate"]), "gke")
        cfg.tls_server_name = s.tls_server_name or "kubernetes.default.svc"
    creds.refresh(google.auth.transport.requests.Request())
    cfg.api_key = {"authorization": creds.token}
    cfg.api_key_prefix = {"authorization": "Bearer"}
    return client.ApiClient(cfg)


def kubeconfig_client(s: KubernetesSection) -> Any:
    from kubernetes import client, config

    path = os.environ.get(s.kubeconfig_env) if s.kubeconfig_env else None
    if s.kubeconfig_env and not path:
        raise NotConfigured(f"kubeconfig_env {s.kubeconfig_env} is not set")
    cfg = client.Configuration()
    config.load_kube_config(config_file=path, context=s.context, client_configuration=cfg)
    return client.ApiClient(cfg)


def api_client(s: KubernetesSection) -> Any:
    provider = (s.provider or "").lower()
    if provider == "eks":
        return eks_client(s)
    if provider == "gke":
        return gke_client(s)
    return kubeconfig_client(s)
