"""FR-1 Kubernetes collection against the cluster in the kubeconfig. Skips when none is reachable."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from bravo_collector.environments import KubernetesSection
from bravo_collector.kubernetes import KubernetesCollector

pytestmark = pytest.mark.k8s


@pytest.fixture(scope="module")
def section(tmp_path_factory) -> KubernetesSection:
    path = os.environ.get("BRAVO_TEST_KUBECONFIG") or str(Path.home() / ".kube" / "config")
    if not Path(path).is_file():
        pytest.skip(f"no kubeconfig at {path} (set BRAVO_TEST_KUBECONFIG)")
    os.environ["BRAVO_TEST_KUBECONFIG_PATH"] = path
    sec = KubernetesSection(kubeconfig_env="BRAVO_TEST_KUBECONFIG_PATH",
                            context=os.environ.get("BRAVO_TEST_KUBE_CONTEXT") or None)
    version = KubernetesCollector(sec).server_version()
    if version.status != "ok":
        pytest.skip(f"cluster in {path} not reachable: {version.error}")
    return sec


def test_namespaces_and_no_secret_values(section):
    data = KubernetesCollector(section).collect()
    assert data["namespaces"].status == "ok"
    assert data["by_namespace"], "at least one non-system namespace expected"
    for ns in data["by_namespace"].values():
        secrets = ns["secrets"]
        assert secrets.status == "ok", secrets.error  # an error here used to hide the call_api break
        for s in secrets.value:
            assert set(s) == {"name", "created_at", "owner_kinds", "managed_by"}
    dumped = json.dumps({k: str(v) for k, v in data.items()})
    assert '"data"' not in dumped and "stringData" not in dumped


def test_secrets_presence_uses_metadata_only_request(section):
    """Regression: kubernetes client 37 changed call_api; the metadata-only list must still succeed."""
    from bravo_collector.kubernetes import list_secret_metadata

    col = KubernetesCollector(section)
    raw = list_secret_metadata(col.api(), "kube-system")
    assert raw["kind"] == "PartialObjectMetadataList"
    for item in raw.get("items") or []:
        assert "data" not in item and "stringData" not in item
    item = col.secrets_presence("kube-system")
    assert item.status == "ok", item.error
    for s in item.value:
        assert set(s) == {"name", "created_at", "owner_kinds", "managed_by"}
