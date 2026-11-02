"""Bound-value recomputation (migration 0004 contract) and policy scope rules. Pure functions, no I/O."""

from __future__ import annotations

import hashlib
import json

import pytest

from bravo_apply import digests, policies
from bravo_apply.runtime import Result, Stop

DIFF = "--- a/src/app.py\n+++ b/src/app.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n"
ROLE = {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "Role", "metadata": {"name": "chat-app-secrets"},
        "rules": [{"apiGroups": [""], "resources": ["secrets"], "resourceNames": ["chat-db"], "verbs": ["get"]}]}
BINDING = {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "RoleBinding", "metadata": {"name": "chat-app-secrets-bind"},
           "subjects": [{"kind": "ServiceAccount", "name": "chat-app"}],
           "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": "chat-app-secrets"}}
TRANSLATIONS = [
    {"source": {"type": "aws_iam_role", "id": "chat-app"}, "target": {"type": "k8s_rbac", "name": "chat-app-secrets"},
     "document": {"items": [ROLE, BINDING]}, "control_ids": ["IAM-01"]},
]
DEST = {"namespace": "chat", "policy_types": ["k8s_rbac", "external_secret"]}


def _expected(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def test_diff_digest_is_sha256_of_utf8_text() -> None:
    assert digests.diff_digest({"diff": DIFF}) == _expected(DIFF.encode("utf-8"))
    assert digests.diff_digest({"diff": ""}) is None
    assert digests.diff_digest(None) is None


def test_policy_diff_digest_is_canonical_json_of_translations() -> None:
    canon = json.dumps(TRANSLATIONS, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    assert digests.policy_diff_digest({"translations": TRANSLATIONS}) == _expected(canon)
    # Key order in the artifact does not change the digest.
    reordered = [{k: t[k] for k in reversed(list(t))} for t in TRANSLATIONS]
    assert digests.policy_diff_digest({"translations": reordered}) == _expected(canon)


def test_policy_targets_sorted_by_type_then_name() -> None:
    items = [{"target": {"type": "k8s_rbac", "name": "b"}}, {"target": {"type": "external_secret", "name": "z"}},
             {"target": {"type": "k8s_rbac", "name": "a"}}]
    assert digests.policy_targets({"translations": items}) == [
        {"type": "external_secret", "name": "z"}, {"type": "k8s_rbac", "name": "a"}, {"type": "k8s_rbac", "name": "b"}]


def test_mismatches_names_every_value_that_differs() -> None:
    art = {"diff": DIFF, "translations": TRANSLATIONS}
    good = {**digests.bound_values(art), "artifact_digest": digests.artifact_digest(art)}
    assert digests.mismatches(art, good) == []
    bad = {**good, "diff_digest": _expected(b"other"), "policy_targets": [{"type": "k8s_rbac", "name": "x"}]}
    assert digests.mismatches(art, bad) == ["diff_digest", "policy_targets"]
    assert digests.mismatches({**art, "rationale": "changed"}, good) == ["artifact_digest"]


def test_scope_accepts_namespaced_rbac_in_destination_namespace() -> None:
    approved = digests.policy_diff_digest({"translations": TRANSLATIONS})
    objs = policies.check_scope(TRANSLATIONS, approved, DEST)
    assert [(o["kind"], o["metadata"]["name"], o["metadata"]["namespace"]) for o in objs] == [
        ("Role", "chat-app-secrets", "chat"), ("RoleBinding", "chat-app-secrets-bind", "chat")]


def test_scope_refuses_when_translations_differ_from_the_approved_policy_diff() -> None:
    with pytest.raises(Stop) as e:
        policies.check_scope(TRANSLATIONS, _expected(b"something else"), DEST)
    assert (e.value.status, e.value.reason, e.value.result) == ("refused", "policy_diff_mismatch", Result.REFUSED)


def test_scope_reports_unwired_target_types_as_not_connected() -> None:
    items = [{**TRANSLATIONS[0], "target": {"type": "rgw_bucket_policy", "name": "chat-attachments"}}]
    with pytest.raises(Stop) as e:
        policies.check_scope(items, digests.policy_diff_digest({"translations": items}), DEST)
    assert e.value.status == "failed" and e.value.result is Result.NOT_CONNECTED
    assert e.value.reason == "policy_target_not_connected:rgw_bucket_policy"


@pytest.mark.parametrize(
    ("obj", "problem"),
    [
        ({**ROLE, "kind": "ClusterRole"}, "kind_not_allowed:ClusterRole"),
        ({**ROLE, "metadata": {"name": "chat-app-secrets", "namespace": "kube-system"}}, "namespace_out_of_scope"),
        ({**ROLE, "metadata": {"name": "other-role"}}, "name_out_of_scope"),
        ({**ROLE, "rules": [{"apiGroups": [""], "resources": ["*"], "verbs": ["get"]}]}, "wildcard_rule"),
    ],
)
def test_scope_refuses_out_of_scope_documents(obj: dict, problem: str) -> None:
    items = [{**TRANSLATIONS[0], "document": obj}]
    with pytest.raises(Stop) as e:
        policies.check_scope(items, digests.policy_diff_digest({"translations": items}), DEST)
    assert e.value.reason == "policy_out_of_scope"
    assert any(problem in p for p in e.value.detail["problems"])


def test_no_policy_diff_means_nothing_to_apply() -> None:
    assert policies.check_scope(None, None, DEST) == []
    with pytest.raises(Stop):  # an approval that names a policy diff the artifact does not have
        policies.check_scope(None, _expected(b"x"), DEST)
