"""Environment registry (FR-1). One YAML/JSON file names what to read per environment.

The file holds references only, never secret values:
    postgres.dsn_env        name of the environment variable that holds the libpq DSN
    kubernetes.kubeconfig_env  name of the environment variable that holds the kubeconfig path
                               (unset -> the standard KUBECONFIG / ~/.kube/config resolution)
    kubernetes.provider + eks_cluster_name + aws_region  EKS: endpoint and token from the AWS chain (boto3)
    aws                     region and resource names; credentials come from the standard AWS chain

The gate reads the same file and ignores the keys it does not need (and the other way round).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

ENVIRONMENTS_FILE_ENV = "BRAVO_ENVIRONMENTS_FILE"


class _Section(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class AwsSection(_Section):
    region: str
    profile: str | None = None  # named profile in the shared config; None -> default chain
    s3_buckets: tuple[str, ...] = ()
    secret_ids: tuple[str, ...] = ()
    kms_key_ids: tuple[str, ...] = ()
    iam_role_names: tuple[str, ...] = ()
    eks_clusters: tuple[str, ...] = ()
    # Namespaces whose EKS Pod Identity associations name the app's IAM roles.
    app_namespaces: tuple[str, ...] = ()
    # Empty -> every trail visible to the caller.
    cloudtrail_trails: tuple[str, ...] = ()
    account_public_access_block: bool = True


class KubernetesSection(_Section):
    kubeconfig_env: str | None = None
    context: str | None = None
    # eks | k3s | None (generic). provider=eks + eks_cluster_name + aws_region -> token from boto3, not kubeconfig.
    provider: str | None = None
    eks_cluster_name: str | None = None
    aws_region: str | None = None
    # Empty -> every namespace except kube-system, kube-public, kube-node-lease.
    namespaces: tuple[str, ...] = ()


class PostgresSection(_Section):
    dsn_env: str
    app_role: str | None = None
    app_tables: tuple[str, ...] = ()
    connect_timeout_s: int = 5


class Environment(_Section):
    environment_id: str = ""
    display_name: str | None = None
    aws: AwsSection | None = None
    kubernetes: KubernetesSection | None = None
    postgres: PostgresSection | None = None


class Registry(_Section):
    environments: dict[str, Environment] = Field(default_factory=dict)

    def get(self, environment_id: str) -> Environment | None:
        return self.environments.get(environment_id)


def parse_registry(data: Any) -> Registry:
    reg = Registry.model_validate(data or {})
    fixed = {k: v.model_copy(update={"environment_id": k}) for k, v in sorted(reg.environments.items())}
    return Registry(environments=fixed)


def load_registry(path: str | os.PathLike[str] | None = None) -> Registry:
    """Load the registry from ``path`` or from ``$BRAVO_ENVIRONMENTS_FILE``."""
    raw = path if path is not None else os.environ.get(ENVIRONMENTS_FILE_ENV)
    if not raw:
        raise RuntimeError(f"{ENVIRONMENTS_FILE_ENV} is not set and no path was given")
    text = Path(raw).read_text(encoding="utf-8")
    return parse_registry(yaml.safe_load(text))
