"""Gate view of the environment registry (same file as the collector, ``$BRAVO_ENVIRONMENTS_FILE``).

The file holds references only. Secrets stay in environment variables or the standard credential chains:
    postgres.dsn_env                 env var with the probe DSN (read-only role, e.g. collector_ro)
    postgres.tls.sslrootcert_env     env var with the CA file path for the verify-full probe
    object_store.app_credential      how to get the app credential (profile, env var names, default chain)
    kubernetes.kubeconfig_env        env var with the kubeconfig path
    kubernetes.provider eks          EKS endpoint + CA from eks:DescribeCluster, bearer token from the gate's own AWS
                                     identity (standard chain, web identity on the platform); ``api_server`` dials a
                                     D-16 link and TLS still checks the real endpoint host
    kubernetes.provider gke          GKE DNS endpoint ``api_server``, bearer token from Google application default
                                     credentials (Workload Identity Federation on the platform), public CA
Keys the gate does not use are ignored, so the collector and the gate share one file. Each process brings its own
credentials: the same entry gives the collector its read identity and the gate its own, separate one.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

ENVIRONMENTS_FILE_ENV = "BRAVO_ENVIRONMENTS_FILE"


class _Section(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class Credential(_Section):
    """Exactly one source. ``default_chain`` uses the standard AWS chain of the gate process."""

    profile: str | None = None
    access_key_env: str | None = None
    secret_key_env: str | None = None
    default_chain: bool = False


class ObjectStoreSection(_Section):
    bucket: str
    region: str = "us-east-1"
    endpoint_url: str | None = None  # None -> AWS S3 regional endpoint
    addressing_style: Literal["auto", "path", "virtual"] = "auto"
    canary_key: str = "_gate/canary"
    app_credential: Credential | None = None
    other_credential: Credential | None = None
    # Read-only fact checks (canary presence, GetBucketEncryption) when the app credential is not available
    # to the gate (e.g. EKS Pod Identity). Never used for the access questions OBJ-01..06.
    read_credential: Credential | None = None
    # PUT probes (OBJ-03 anonymous PUT, OBJ-04 app PUT) write to the bucket. Off unless approved for this store.
    write_probes: bool = False
    timeout_s: float = 5.0
    # CA bundle file for a private CA (e.g. the on-prem RGW). A public file path, not a secret. None = system CAs.
    ca_file: str | None = None
    # OBJ-06 sends its plain-HTTP request here instead of the https endpoint with the scheme swapped. For a store the
    # gate reaches only through a TLS link, this is the store's own plain listener behind its own link.
    plain_http_endpoint_url: str | None = None


class PostgresTls(_Section):
    sslrootcert_env: str | None = None


class PostgresSection(_Section):
    dsn_env: str
    # Dial this host (a D-16 link Service) while libpq still checks the server certificate against the DSN ``host``
    # (libpq ``hostaddr``). Resolved on every probe run.
    dial_host: str | None = None
    app_role: str | None = None
    app_tables: tuple[str, ...] = ()
    tls: PostgresTls = PostgresTls()
    connect_timeout_s: int = 5


class KubernetesSection(_Section):
    kubeconfig_env: str | None = None
    context: str | None = None
    provider: str | None = None  # eks | gke | None (kubeconfig)
    eks_cluster_name: str | None = None
    aws_region: str | None = None
    api_server: str | None = None  # eks: D-16 link address; gke: the DNS endpoint
    tls_server_name: str | None = None
    app_namespace: str | None = None
    app_secret: str | None = None
    # "namespace/name" of service accounts that must not read the app secret.
    other_service_accounts: tuple[str, ...] = ()
    restore_cluster: str | None = None  # "namespace/name" of a CNPG Cluster bootstrapped by recovery


class AwsSection(_Section):
    region: str
    profile: str | None = None
    secret_ids: tuple[str, ...] = ()
    # Principals that must not read the app secrets (iam:SimulatePrincipalPolicy). Role names are resolved
    # to ARNs with iam:GetRole at run time, so the registry needs no account id.
    other_principal_arns: tuple[str, ...] = ()
    other_principal_roles: tuple[str, ...] = ()
    cloudtrail_trails: tuple[str, ...] = ()
    eks_clusters: tuple[str, ...] = ()


class NetworkSection(_Section):
    control_paths: tuple[str, ...] = ()  # "host:port" that must connect (proves the prober works)
    denied_paths: tuple[str, ...] = ()  # "host:port" that must not connect
    timeout_s: float = 2.0


class Environment(_Section):
    environment_id: str = ""
    display_name: str | None = None
    postgres: PostgresSection | None = None
    object_store: ObjectStoreSection | None = None
    kubernetes: KubernetesSection | None = None
    aws: AwsSection | None = None
    network: NetworkSection | None = None


class Registry(_Section):
    environments: dict[str, Environment] = Field(default_factory=dict)

    def get(self, environment_id: str) -> Environment | None:
        return self.environments.get(environment_id)


def parse_registry(data: Any) -> Registry:
    reg = Registry.model_validate(data or {})
    return Registry(environments={k: v.model_copy(update={"environment_id": k})
                                  for k, v in sorted(reg.environments.items())})


def load_registry(path: str | os.PathLike[str] | None = None) -> Registry:
    raw = path if path is not None else os.environ.get(ENVIRONMENTS_FILE_ENV)
    if not raw:
        raise RuntimeError(f"{ENVIRONMENTS_FILE_ENV} is not set and no path was given")
    return parse_registry(yaml.safe_load(Path(raw).read_text(encoding="utf-8")))
