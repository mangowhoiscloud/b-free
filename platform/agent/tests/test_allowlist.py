import pytest
import yaml

from bravo_agents.allowlist import ROLES, Allowlist, load_allowlist
from bravo_agents.errors import AgentRunError
from conftest import AGENT_DIR


@pytest.mark.parametrize("role", ROLES)
def test_every_role_loads(role):
    al = load_allowlist(role)
    assert al.role == role
    assert not set(al.states) & {"SHIFT", "CUTOVER", "FAILBACK", "SAFE_HOLD"}
    assert al.sandbox.user == 65532


@pytest.mark.parametrize("role", ROLES)
def test_allowed_binaries_exist_in_image(role):
    """허용 목록의 바이너리는 실제 이미지 감사 기준선(expected-binaries.txt)에 있어야 합니다."""
    al = load_allowlist(role)
    image = al.sandbox.image.split("agent-sandbox-", 1)[1]
    present = set((AGENT_DIR / "images" / image / "expected-binaries.txt").read_text().split())
    assert set(al.sandbox.binaries) <= present, set(al.sandbox.binaries) - present


def _raw(role="governance"):
    return yaml.safe_load((AGENT_DIR / "allowlists" / f"{role}.yaml").read_text())


@pytest.mark.parametrize("mutate,needle", [
    (lambda d: d.update(states=["CUTOVER"]), "may not run"),
    (lambda d: d["mcp_tools"].append("plan_execute"), "unknown platform tools"),
    (lambda d: d["mcp_tools"].append("plan_propose"), "never granted to the model"),
    (lambda d: d["sandbox"].update(kubectl={"verbs": ["apply"], "kinds": ["ConfigMap"]}), "no kubectl"),
    (lambda d: d["sandbox"]["checks"]["json_syntax"].update(argv=["curl", "{path}"]), "not in sandbox binaries"),
    (lambda d: d.update(extra_field=1), "extra"),
    (lambda d: d["sandbox"]["checks"]["json_syntax"].update(env={"PGPASSWORD": "x"}), "secret-like env"),
    (lambda d: d["sandbox"].update(egress=["llm-gateway", "rehearsal-db"]), "egress"),
])
def test_rejects_widening(mutate, needle):
    d = _raw()
    mutate(d)
    with pytest.raises(ValueError, match=needle):
        Allowlist.model_validate(d)


def test_check_argv_substitutes_and_confines_paths():
    al = load_allowlist("governance")
    assert al.check_argv("json_syntax", {"path": "/out/./rbac.json"}) == ["jq", "empty", "/out/rbac.json"]
    for bad in ("/out/../etc/passwd", "/etc/passwd", "out/rbac.json", "/out/a\nb"):
        with pytest.raises(ValueError):
            al.check_argv("json_syntax", {"path": bad})
    with pytest.raises(ValueError, match="check_not_allowed"):
        al.check_argv("delete_bucket", {})
    with pytest.raises(ValueError, match="bad_check_args"):
        al.check_argv("json_syntax", {"path": "/out/x.json", "extra": "1"})


def test_pattern_param_rejects_flag_injection():
    al = load_allowlist("discovery")
    assert al.check_argv("search", {"pattern": "boto3.client"})[-2] == "boto3.client"
    with pytest.raises(ValueError):
        al.check_argv("search", {"pattern": "--pre=/bin/sh"})


def test_unknown_role_fails_closed():
    with pytest.raises(AgentRunError) as e:
        load_allowlist("cutover-agent")
    assert e.value.code == "allowlist_invalid"


@pytest.mark.parametrize("role", ROLES)
def test_no_secret_material_in_allowlists_and_tasks(role):
    """D-26: 허용 목록과 Harbor task에 비밀 값 · 자격 경로가 없음."""
    import re
    texts = [(AGENT_DIR / "allowlists" / f"{role}.yaml").read_text()]
    task = AGENT_DIR / "harbor" / role
    texts += [(task / "task.toml").read_text(), (task / "instruction.md").read_text()]
    for t in texts:
        assert not re.search(r"(PGPASS|PASSWORD\s*=|AWS_SECRET|BEGIN [A-Z ]*PRIVATE KEY|sk-ant-|AKIA[0-9A-Z]{16})", t)
