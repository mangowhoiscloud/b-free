"""도구 계층: rehearsal.exec가 받는 구조화한 작업을 검사하고, 셸 해석 없는 명령으로 바꿉니다(설계 문서 v0.1 8.3, 제안 D-06).

자유 문자열 명령은 받지 않습니다. 문자열 차단 목록은 `rm -r -f`, `kubectl -n x delete ns x` 같은 변형에 뚫렸기 때문입니다.
목록에 없는 작업은 거부하고, 파괴적 동사는 이름을 붙여 거부합니다(콘솔의 차단 기록).

이 계층은 첫 경계입니다. `write_file`로 쓴 코드를 `run_check`가 실행하므로 Pod 안의 임의 코드 실행은 막지 못합니다.
매니페스트 필드 정책은 기본값이 적용된 최종 객체에 대해 클러스터 admission으로도 강제할 계획입니다(T3, 아직 없음).
"""
import base64
import json
import posixpath
import shlex
from dataclasses import dataclass

WORKSPACE = "/app"
SERVICE_ACCOUNT = "rehearsal-app"       # 러너가 만드는 전용 서비스 계정. 권한 없음
TEST_SECRET_PREFIX = "rehearsal-test-"  # 러너가 넣는 시험용 비밀만 참조 가능
READ_VERBS = {"get", "describe"}
WRITE_VERBS = {"apply", "diff"}
DESTRUCTIVE_VERBS = {"delete", "replace", "patch", "scale", "drain", "cordon", "exec", "cp", "port-forward"}
# 정확한 (apiVersion, kind)만 허용합니다. 모두 네임스페이스 범위 자원입니다. secret 읽기는 없습니다.
GVK = {("v1", "ConfigMap"), ("v1", "Service"), ("v1", "Pod"), ("apps/v1", "Deployment"), ("batch/v1", "Job")}
KINDS = {k.lower() for _, k in GVK}
# P0 (05 2, T11): the agent's apply/diff accepts exactly one (apiVersion, kind). Workloads are
# applied by the rehearsal provisioner from fixed templates, never by the agent.
P0_GVK = frozenset({("v1", "ConfigMap")})
LOG_KINDS = {"pod", "deployment", "job"}
CHECKS = {"app_import": ["python3", "-I", "-c", "import sys; sys.path.insert(0, '/app'); import config"]}


@dataclass(frozen=True)
class Decision:
    allowed: bool
    rule: str            # 허용이면 "ok", 거부면 차단 사유
    command: str = ""    # 허용일 때만. 모든 인자를 셸 인용한 한 줄
    timeout_s: int = 60  # run_check는 허용 목록의 검사별 상한


def _deny(rule: str) -> Decision:
    return Decision(False, rule)


def _ok(argv: list[str]) -> Decision:
    return Decision(True, "ok", shlex.join(argv))


def _workspace_path(path) -> str | None:
    if not isinstance(path, str) or "\0" in path:
        return None
    norm = posixpath.normpath(posixpath.join(WORKSPACE, path))
    return norm if norm == WORKSPACE or norm.startswith(WORKSPACE + "/") else None


def _name(value) -> bool:
    return isinstance(value, str) and value != "" and not value.startswith("-") and "/" not in value


def _pod_spec_problem(spec) -> str | None:
    """Pod 정의에서 권한을 넓히는 필드를 찾습니다."""
    if not isinstance(spec, dict):
        return "bad_pod_spec"
    # 생략하면 K8s가 default를 넣으므로 명시를 요구합니다
    if spec.get("serviceAccountName") != SERVICE_ACCOUNT or "serviceAccount" in spec:
        return "service_account_not_allowed"
    if spec.get("imagePullSecrets"):
        return "image_pull_secret_not_allowed"  # 데모 이미지는 공개 패키지(D-14)
    containers = spec.get("containers")
    if not isinstance(containers, list) or not containers:
        return "bad_container"
    for field in ("volumes", "initContainers"):
        if field in spec and not isinstance(spec[field], list):
            return "bad_pod_spec"
    if spec.get("automountServiceAccountToken") is not False:
        return "token_automount_not_disabled"
    if any(spec.get(f) for f in ("hostNetwork", "hostPID", "hostIPC", "nodeName")):
        return "host_access_not_allowed"
    for vol in spec.get("volumes") or []:
        if not isinstance(vol, dict) or set(vol) - {"name", "emptyDir", "configMap", "secret"}:
            return "volume_type_not_allowed"
        if "secret" in vol and not str(vol["secret"].get("secretName", "")).startswith(TEST_SECRET_PREFIX):
            return "secret_not_allowed"
    for c in containers + spec.get("initContainers", []):
        if not isinstance(c, dict):
            return "bad_container"
        sc = c.get("securityContext") or {}
        if sc.get("privileged") or sc.get("allowPrivilegeEscalation") is not False:
            return "privilege_not_allowed"
        if (sc.get("capabilities") or {}).get("add"):
            return "capability_not_allowed"
        refs = [e.get("valueFrom", {}).get("secretKeyRef", {}).get("name") for e in c.get("env") or []]
        refs += [e.get("secretRef", {}).get("name") for e in c.get("envFrom") or []]
        if any(r is not None and not str(r).startswith(TEST_SECRET_PREFIX) for r in refs):
            return "secret_not_allowed"
    return None


def _object_problem(m, namespace: str, gvk=GVK) -> str | None:
    if not isinstance(m, dict) or (m.get("apiVersion"), m.get("kind")) not in gvk:
        return "kind_not_allowed"
    if "items" in m:
        return "unexpected_items"
    meta = m.get("metadata")
    if not isinstance(meta, dict) or not _name(meta.get("name")):
        return "bad_metadata"
    if meta.get("namespace", namespace) != namespace:
        return "namespace_override"
    kind = m["kind"]
    if kind == "Pod":
        return _pod_spec_problem(m.get("spec"))
    if kind in ("Deployment", "Job"):
        return _pod_spec_problem(((m.get("spec") or {}).get("template") or {}).get("spec"))
    return None


def _kubectl(op: dict, namespace: str, gvk=GVK) -> Decision:
    verb = op.get("verb")
    if not isinstance(verb, str):
        return _deny("malformed_op")
    if verb in DESTRUCTIVE_VERBS:
        return _deny("destructive_verb")
    if verb == "logs":
        kind, name = str(op.get("kind", "")).lower(), op.get("name")
        if kind not in LOG_KINDS or not _name(name):
            return _deny("bad_logs_target")
        return _ok(["kubectl", "logs", f"{kind}/{name}", "-n", namespace])
    if verb in READ_VERBS:
        kind, name = str(op.get("kind", "")).lower(), op.get("name")
        if kind not in {k.lower() for _, k in gvk}:
            return _deny("kind_not_allowed")
        if name is not None and not _name(name):
            return _deny("bad_name")
        return _ok(["kubectl", verb, kind] + ([name] if name else []) + ["-n", namespace])
    if verb not in WRITE_VERBS:
        return _deny("verb_not_allowed")
    manifest = op.get("manifest")
    if isinstance(manifest, dict) and (manifest.get("apiVersion"), manifest.get("kind")) == ("v1", "List"):
        items = manifest.get("items")
        if not isinstance(items, list) or not items:
            return _deny("bad_list")
    else:
        items = [manifest]
    for m in items:
        problem = _object_problem(m, namespace, gvk)
        if problem:
            return _deny(problem)
    payload = base64.b64encode(json.dumps(manifest, separators=(",", ":")).encode()).decode()
    # ponytail: 매니페스트는 base64로 넘겨 셸 해석을 피함. 네임스페이스는 도구 계층이 고정
    return Decision(True, "ok", f"printf %s {payload} | base64 -d | kubectl {verb} -n {shlex.quote(namespace)} -f -")


def _authorize(op, namespace: str, gvk=GVK) -> Decision:
    if not isinstance(op, dict):
        return _deny("free_text_command")
    name = op.get("op")
    if name == "kubectl":
        return _kubectl(op, namespace, gvk)
    if name in ("read_file", "list_dir", "write_file"):
        path = _workspace_path(op.get("path"))
        if path is None:
            return _deny("path_outside_workspace")
        if name == "read_file":
            return _ok(["cat", "--", path])
        if name == "list_dir":
            return _ok(["ls", "-la", "--", path])
        content = op.get("content")
        if not isinstance(content, str):
            return _deny("bad_content")
        payload = base64.b64encode(content.encode()).decode()
        return Decision(True, "ok", f"printf %s {payload} | base64 -d > {shlex.quote(path)}")
    if name == "run_check":
        check = op.get("name")
        argv = CHECKS.get(check) if isinstance(check, str) else None
        return _ok(argv) if argv else _deny("check_not_allowed")
    return _deny("op_not_allowed")


def authorize(op, namespace: str, gvk=GVK) -> Decision:
    """에이전트가 낸 작업 하나를 검사합니다. 네임스페이스는 에이전트가 아니라 러너가 넘깁니다.

    ``gvk``: 받는 (apiVersion, kind) 집합. 리허설 서비스는 P0_GVK(ConfigMap 하나)를 넘깁니다.
    """
    try:
        return _authorize(op, namespace, gvk)
    except (TypeError, AttributeError, ValueError):
        return _deny("malformed_op")


def authorize_sandbox(op, al) -> Decision:
    """역할 허용 목록(``allowlist.Allowlist``, bravo_agents 형식)으로 sandbox 작업 하나를 검사합니다(13 4).

    작업은 read_file, list_dir, write_file, run_check뿐이고 kubectl은 어떤 역할도 받지 않습니다. 경로는 정규화 뒤
    역할의 읽기 · 쓰기 루트 안이어야 합니다. run_check의 argv는 ``check_argv``(선언한 자리에만 인자, 플래그 주입
    거부)로 만들고, 검사의 env · cwd · timeout_s를 그대로 씁니다. 셸 해석은 인용한 한 줄로만 합니다.
    """
    try:
        if not isinstance(op, dict):
            return _deny("free_text_command")
        rule = al.op_denied(op)
        if rule:
            return _deny(rule)
        name = op.get("op")
        if name in ("read_file", "list_dir"):
            rule, path = al.path_denied(op.get("path"), write=False)
            if rule:
                return _deny(rule)
            return _ok(["cat", "--", path] if name == "read_file" else ["ls", "-la", "--", path])
        if name == "write_file":
            rule, path = al.path_denied(op.get("path"), write=True)
            if rule:
                return _deny(rule)
            content = op.get("content")
            if not isinstance(content, str):
                return _deny("bad_content")
            raw = content.encode()
            if len(raw) > al.write_max_bytes:
                return _deny("write_too_large")
            payload = base64.b64encode(raw).decode()
            return Decision(True, "ok", f"printf %s {payload} | base64 -d > {shlex.quote(path)}")
        if name == "run_check":
            rule, argv, chk = al.check(op.get("name"), op.get("args"))
            if rule:
                return _deny(rule)
            env = [f"{k}={v}" for k, v in sorted(chk.env.items())]
            line = f"cd {shlex.quote(chk.cwd)} && exec " + shlex.join(["env", *env, *argv])
            return Decision(True, "ok", line, timeout_s=int(chk.timeout_s))
        return _deny("op_not_allowed")
    except (TypeError, AttributeError, ValueError):
        return _deny("malformed_op")
