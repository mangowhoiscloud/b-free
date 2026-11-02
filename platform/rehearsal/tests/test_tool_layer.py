import shlex

import pytest

from bravo_rehearsal.tool_layer import authorize

NS = "rehearsal-p1-1"
SAFE_POD_SPEC = {
    "serviceAccountName": "rehearsal-app", "automountServiceAccountToken": False,
    "containers": [{"name": "app", "image": "ghcr.io/x/app@sha256:aa", "securityContext": {"allowPrivilegeEscalation": False}}],
}


def apply(manifest):
    return authorize({"op": "kubectl", "verb": "apply", "manifest": manifest}, NS)


def pod(**spec):
    return {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": "app"}, "spec": {**SAFE_POD_SPEC, **spec}}


# 전문가 리뷰가 옛 정규식 차단 목록에서 재현한 우회 문자열. 자유 문자열은 모두 거부돼야 합니다.
@pytest.mark.parametrize("cmd", [
    "rm -r -f /app", "rm --recursive --force /app", "kubectl -n ns delete ns ns", "k delete ns x",
    "kubectl delete -f ns.yaml", "find /app -delete", "mc rm --recursive --force local/chat-attachments",
    "curl -X DELETE http://seaweedfs:8333/chat-attachments", "echo cm0gLXJmIC9hcHA= | base64 -d | sh",
])
def test_free_text_is_denied(cmd):
    assert authorize(cmd, NS).rule == "free_text_command"


@pytest.mark.parametrize("op,rule", [
    ({"op": "kubectl", "verb": "delete", "kind": "namespace", "name": NS}, "destructive_verb"),
    ({"op": "kubectl", "verb": "exec", "kind": "pod", "name": "app"}, "destructive_verb"),
    ({"op": "kubectl", "verb": "get", "kind": "secret"}, "kind_not_allowed"),
    ({"op": "kubectl", "verb": "get", "kind": "pod", "name": "--all-namespaces"}, "bad_name"),
    ({"op": "kubectl", "verb": "logs", "kind": "pod"}, "bad_logs_target"),
    ({"op": "read_file", "path": "/etc/passwd"}, "path_outside_workspace"),
    ({"op": "write_file", "path": "../etc/cron.d/x", "content": "x"}, "path_outside_workspace"),
    ({"op": "run_check", "name": "sh"}, "check_not_allowed"),
    ({"op": "shell", "cmd": "ls"}, "op_not_allowed"),
    # 형식이 틀린 입력은 예외가 아니라 거부
    ({"op": "kubectl", "verb": []}, "malformed_op"),
    ({"op": "run_check", "name": []}, "check_not_allowed"),
])
def test_structured_denials(op, rule):
    d = authorize(op, NS)
    assert not d.allowed and d.rule == rule


@pytest.mark.parametrize("manifest,rule", [
    ({"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "x", "namespace": "prod"}}, "namespace_override"),
    ({"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "ClusterRoleBinding", "metadata": {"name": "x"}}, "kind_not_allowed"),
    # 허용 kind와 이름이 같은 다른 API group
    ({"apiVersion": "evil.example.com/v1", "kind": "ConfigMap", "metadata": {"name": "x"}}, "kind_not_allowed"),
    # ConfigMap으로 위장한 목록
    ({"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "x"},
      "items": [{"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "y"}}]}, "unexpected_items"),
    ({"apiVersion": "v1", "kind": "List", "items": {}}, "bad_list"),
    ({"apiVersion": "v1", "kind": "List", "items": [{"apiVersion": "v1", "kind": "List", "items": []}]}, "kind_not_allowed"),
    ({"apiVersion": "v1", "kind": "ConfigMap", "metadata": None}, "bad_metadata"),
    # 워크로드로 권한 넓히기
    (pod(serviceAccountName="default"), "service_account_not_allowed"),
    ({"apiVersion": "v1", "kind": "Pod", "metadata": {"name": "app"},
      "spec": {k: v for k, v in SAFE_POD_SPEC.items() if k != "serviceAccountName"}}, "service_account_not_allowed"),
    (pod(imagePullSecrets=[{"name": "production-registry"}]), "image_pull_secret_not_allowed"),
    (pod(containers={}), "bad_container"),
    (pod(volumes={}), "bad_pod_spec"),
    (pod(containers=[{"name": "a", "securityContext": {"allowPrivilegeEscalation": False,
                                                     "capabilities": {"add": ["SYS_PTRACE"]}}}]), "capability_not_allowed"),
    (pod(automountServiceAccountToken=True), "token_automount_not_disabled"),
    (pod(hostNetwork=True), "host_access_not_allowed"),
    (pod(volumes=[{"name": "t", "projected": {"sources": [{"serviceAccountToken": {"path": "t"}}]}}]), "volume_type_not_allowed"),
    (pod(volumes=[{"name": "s", "secret": {"secretName": "chat-db"}}]), "secret_not_allowed"),
    (pod(containers=[{"name": "a", "securityContext": {"allowPrivilegeEscalation": False},
                      "env": [{"name": "P", "valueFrom": {"secretKeyRef": {"name": "chat-db", "key": "p"}}}]}]), "secret_not_allowed"),
    (pod(containers=[{"name": "a", "securityContext": {"privileged": True}}]), "privilege_not_allowed"),
    ({"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "d"},
      "spec": {"template": {"spec": {**SAFE_POD_SPEC, "serviceAccountName": "default"}}}}, "service_account_not_allowed"),
])
def test_manifest_denials(manifest, rule):
    d = apply(manifest)
    assert not d.allowed and d.rule == rule


def test_allowed_ops_pin_namespace_and_quote_args():
    d = authorize({"op": "kubectl", "verb": "get", "kind": "Pod", "name": "app"}, NS)
    assert d.allowed and shlex.split(d.command) == ["kubectl", "get", "pod", "app", "-n", NS]
    d = authorize({"op": "kubectl", "verb": "logs", "kind": "pod", "name": "app"}, NS)
    assert d.allowed and shlex.split(d.command) == ["kubectl", "logs", "pod/app", "-n", NS]
    d = authorize({"op": "write_file", "path": "/app/a;rm -rf x", "content": "$(reboot)"}, NS)
    assert d.allowed and "'/app/a;rm -rf x'" in d.command and "$(reboot)" not in d.command
    assert apply({"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "x"}}).command.endswith(f"kubectl apply -n {NS} -f -")
    assert apply(pod(volumes=[{"name": "s", "secret": {"secretName": "rehearsal-test-db"}}])).allowed
