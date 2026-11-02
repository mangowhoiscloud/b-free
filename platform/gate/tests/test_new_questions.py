"""Rule version 2026-11-03.2: method labels on every row, OBJ-PLAIN-EGRESS, NET-EXTERNAL-SCAN, SEC-GIT-PLAINTEXT."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from bravo_gate.controls import QUESTIONS, SideResults, build_rows
from bravo_gate.environments import NetworkSection, PublicEndpoint, RepositorySection
from bravo_gate.probes.exposure import scan
from bravo_gate.probes.platform import judge_plain_egress
from bravo_gate.result import PASS, Obs


def test_every_row_says_how_it_was_measured() -> None:
    obs = {q.qid: Obs(PASS, "ok", {}) for q in QUESTIONS}
    rep = build_rows(SideResults("source-aws", obs, "eks"), SideResults("target-onprem", obs, "rke2"))
    rows = {r["control_id"]: r for r in rep["questions"]}
    assert all(r["method"] and r["src_method"] and r["dst_method"] and r["spec_test"] for r in rows.values())
    # AWS has no app credential in the gate: OBJ-04 is an IAM simulation there, a real PUT on-prem
    assert (rows["OBJ-04"]["src_method"], rows["OBJ-04"]["dst_method"], rows["OBJ-04"]["method"]) == (
        "simulation", "behavioural", "mixed")
    assert rows["SEC-SECRET-READ-K8S"]["method"] == "simulation"
    assert rows["OBJ-ENC"]["method"] == "config"
    assert {"SEC-GIT-PLAINTEXT", "OBJ-PLAIN-EGRESS", "NET-EXTERNAL-SCAN"} <= set(rows)


@pytest.mark.parametrize(("tls", "plain", "endpoint", "want"), [
    ("connected", "timeout", {"state": "set", "value": "https://storage.googleapis.com"},
     ("PASS", "plain_http_blocked_app_https")),
    ("connected", "timeout", {"state": "unset_sdk_default_https"}, ("PASS", "plain_http_blocked_app_https")),
    ("connected", "connected", {"state": "set", "value": "https://x"}, ("FAIL", "plain_http_port_reachable")),
    ("connected", "refused", {"state": "set", "value": "http://x"}, ("FAIL", "app_endpoint_plain_http")),
    ("timeout", "timeout", {"state": "set", "value": "https://x"}, ("UNKNOWN", "tls_control_path_failed")),
    ("connected", "dns_error", {"state": "set", "value": "https://x"}, ("UNKNOWN", "plain_path_inconclusive")),
    ("connected", "timeout", {"state": "from_reference"}, ("UNKNOWN", "app_endpoint_unreadable")),
])
def test_plain_egress_rule(tls: str, plain: str, endpoint: dict, want: tuple[str, str]) -> None:
    o = judge_plain_egress(tls, plain, endpoint, {})
    assert (o.verdict, o.reason) == want


def _net(*eps: PublicEndpoint) -> NetworkSection:
    return NetworkSection(public_endpoints=eps, scan_ports=(22, 80, 443, 6443))


def test_external_scan_rules() -> None:
    edge = PublicEndpoint(host="edge.example", allow=(80, 443), kind="edge")
    api = PublicEndpoint(host="api.example", allow=(443,), kind="control_plane")
    resolver = {"edge.example": ["198.51.100.1"], "api.example": ["198.51.100.2"]}.__getitem__

    def connect(open_ports: dict[str, set[int]]):
        return lambda a, p, t: "open" if p in open_ports.get(a, set()) else "filtered"

    ok = scan(_net(edge, api), resolver=resolver, connect=connect({"198.51.100.1": {80, 443}}))
    assert (ok.verdict, ok.reason) == ("PASS", "only_published_entry_points")
    bad = scan(_net(edge, api), resolver=resolver, connect=connect({"198.51.100.1": {443}, "198.51.100.2": {6443}}))
    assert (bad.verdict, bad.reason) == ("FAIL", "unexpected_open_port")
    assert bad.evidence["unexpected"] == ["api.example:6443"]
    silent = scan(_net(edge), resolver=resolver, connect=connect({}))
    assert (silent.verdict, silent.reason) == ("UNKNOWN", "nothing_answered")  # nothing proven
    assert scan(_net()).reason == "public_endpoints_not_configured"


@pytest.mark.skipif(shutil.which("gitleaks") is None, reason="gitleaks not installed")
def test_gitleaks_on_a_checkout(tmp_path: Path) -> None:
    from bravo_gate.probes.repo import git_plaintext

    root = tmp_path / "repo" / "apps" / "demo"
    root.mkdir(parents=True)
    (root / "app.py").write_text("print('hello')\n")
    sec = RepositorySection(root=str(root))
    clean = git_plaintext(sec)
    assert (clean.verdict, clean.reason) == ("PASS", "no_plaintext_secret")
    # a real-looking AWS key pair (generated shape, not a live key) must be found and reported without its value
    leak = "AKIA" + "QYLPMN5HHHFPZAM2"
    (root / "settings.py").write_text(f'aws_access_key_id = "{leak}"\n'
                                      'aws_secret_access_key = "' + "Zx9" * 13 + 'q"\n')
    bad = git_plaintext(sec)
    assert (bad.verdict, bad.reason) == ("FAIL", "plaintext_secret_in_repo")
    assert leak not in str(bad.evidence)
    assert any(f["file"] == "settings.py" for f in bad.evidence["findings"])
    assert git_plaintext(RepositorySection(root=str(tmp_path / "none"))).reason == "repository_checkout_missing"
