"""kube-bench normalisation: explicit benchmark, control-ID mapping, 근사 label, one-sided topics are 미검증.

Input is kube-bench's documented ``--json`` layout (Controls[].tests[].results[]); no cluster is needed.
"""

from __future__ import annotations

import pytest

from bravo_gate.kube_bench import compare, load_mapping, normalize


def _report(results: dict[str, str]) -> dict:
    return {"Controls": [{"id": "x", "tests": [{"section": "s", "results": [
        {"test_number": k, "status": v} for k, v in results.items()]}]}]}


def test_mapping_is_pinned_and_explicit() -> None:
    m = load_mapping()
    assert len(m["kube_bench"]["commit"]) == 40 and "@sha256:" in m["kube_bench"]["image"]
    assert m["benchmarks"] == {"k3s": "k3s-cis-1.9", "rke2": "rke2-cis-1.9", "eks": "eks-1.8.0", "gke": "gke-1.9.0"}
    assert m["label"] == "근사(approximate)"


def test_eks_vs_k3s_normalised_with_one_sided_topics() -> None:
    eks = normalize(_report({"2.1.1": "PASS", "2.1.2": "WARN", "5.4.1": "PASS", "5.4.2": "PASS", "5.4.3": "PASS",
                             "5.3.1": "PASS"}), "eks")
    k3s = normalize(_report({"1.2.16": "FAIL", "1.2.27": "PASS", "1.2.28": "PASS"}), "k3s")
    rows = {r["topic"]: r for r in compare(eks, k3s)}
    assert rows["audit-log"]["src"] == "UNKNOWN"  # WARN needs a manual check
    assert rows["audit-log"]["dst"] == "FAIL" and rows["audit-log"]["dst_status"] == "보완 필요"
    cpe = rows["control-plane-endpoint"]
    assert cpe["src"] == "PASS" and cpe["dst"] == "UNKNOWN" and cpe["note"] == "한쪽에만 있는 항목: 미검증"
    assert rows["secrets-encryption-at-rest"]["src"] == "PASS" and rows["secrets-encryption-at-rest"]["dst"] == "PASS"
    assert all(r["label"] == "근사(approximate)" for r in rows.values())
    assert rows["rbac-least-privilege"]["src"] == "UNKNOWN"  # items missing from the report are not a pass


def test_unknown_platform_is_rejected() -> None:
    with pytest.raises(ValueError):
        normalize(_report({}), "openshift")
