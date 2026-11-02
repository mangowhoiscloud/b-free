"""Rehearsal service: Trial token, role allowlists, proposal checks, SDK options, worker fail-closed,
service preconditions, and the MCP tool layer over Streamable HTTP (real HTTP, real MCP JSON-RPC).

Nothing here talks to a model. Tests that need one are marked ``llm`` and skip with
``llm_unconfigured``; tests that need the destination cluster are marked ``cluster``.
"""

from __future__ import annotations

import itertools
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from bravo_rehearsal import allowlist, proposal, sdk_options, trial_token
from bravo_rehearsal.cluster import Names
from bravo_rehearsal.service import ServiceSettings, create_app
from bravo_rehearsal.trials import Trial

KEY = b"k" * 32
TASKS = Path(__file__).resolve().parents[1] / "tasks" / "templates"
_ids = itertools.count(1)
AGENT_ALLOWLISTS = Path(__file__).resolve().parents[2] / "agent" / "allowlists"
PLAN = "11111111-2222-3333-4444-555555555555"
ENV_PLAN = f"p-{PLAN}"


def use_allowlists(directory: Path, *roles: str) -> None:
    """The real platform/agent allowlists, copied so each test owns its directory."""
    for role in roles:
        (directory / f"{role}.yaml").write_text((AGENT_ALLOWLISTS / f"{role}.yaml").read_text())


def bucket_policy(principal: Any = None, actions: Any = None) -> dict[str, Any]:
    return {"Version": "2012-10-17", "Statement": [{
        "Effect": "Allow", "Principal": principal or {"AWS": ["arn:aws:iam::111122223333:user/chat-app"]},
        "Action": actions or ["s3:GetObject", "s3:PutObject"], "Resource": ["arn:aws:s3:::chat-attachments/*"]}]}


def good_proposal(trial_id: str = "t-governance-1", attempt: int = 1) -> dict[str, Any]:
    """A GovernanceProposal (bravo_agents.schemas) bound to the test Trial."""
    return {
        "role": "governance", "step": "translate", "plan_id": ENV_PLAN, "trial_id": trial_id, "attempt": attempt,
        "rationale": "Same app-only read/write as the S3 policy; anonymous access stays denied.", "risk": "low",
        "evidence_refs": [],
        "translations": [{"source": {"type": "aws_s3_bucket_policy", "id": "chat-attachments"},
                          "target": {"type": "rgw_bucket_policy", "name": "chat-attachments"},
                          "document": bucket_policy(), "control_ids": ["IAM-01", "ISO-01"]}],
        "audit_summary": "One bucket policy translated; no exception requested.",
    }


def legacy_bucket_proposal() -> dict[str, Any]:
    """The pre-schema shape the legacy tasks/templates/fix-bucket-policy verifier still grades."""
    policy = {"anonymous": "deny", "actions": ["s3:GetObject", "s3:PutObject"], "principal": "chat-app"}
    return {
        "step": "translate",
        "source": {"type": "aws_s3_bucket_policy", "id": "chat-attachments"},
        "target": {"type": "rgw_bucket_policy", "name": "chat-attachments"},
        "proposal": policy,
        "rationale": "Same app-only read/write and anonymous deny as the S3 policy.",
        "risk": "low",
        "manifest": {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "chat-attachments-policy"},
                     "data": {"policy.json": json.dumps(policy)}},
    }


BINDING = proposal.Binding(plan_id=ENV_PLAN, trial_id="t-governance-1", attempt=1)


def llm_configured() -> bool:
    try:
        sdk_options.llm_env("opus")
        return True
    except sdk_options.LLMUnconfigured:
        return False


# Trial token ------------------------------------------------------------------------------


def claims(**kw: Any) -> trial_token.TrialClaims:
    base = dict(trial_id="t-1", tenant_id="t1", plan_id="p1", attempt=1, namespace="rehearsal-p1-1",
                ops=("kubectl",), budget_usd=2.0, exp=int(time.time()) + 60)
    return trial_token.TrialClaims(**{**base, **kw})


def test_token_roundtrip_and_tamper() -> None:
    tok = trial_token.issue(KEY, claims())
    assert trial_token.verify(KEY, tok).trial_id == "t-1"
    payload, sig = tok.split(".")
    with pytest.raises(trial_token.TokenError, match="token_bad_signature"):
        trial_token.verify(KEY, payload + "." + sig[::-1])
    with pytest.raises(trial_token.TokenError, match="token_bad_signature"):
        trial_token.verify(b"x" * 32, tok)
    with pytest.raises(trial_token.TokenError, match="token_expired"):
        trial_token.verify(KEY, trial_token.issue(KEY, claims(exp=int(time.time()) - 1)))
    with pytest.raises(ValueError):
        trial_token.issue(b"short", claims())


# Role allowlists (deny by default) ---------------------------------------------------------


def test_allowlist_missing_dir_denies_everything(tmp_path: Path) -> None:
    al = allowlist.load("governance", tmp_path / "nope")
    assert al.problem == "role_allowlist_missing"
    assert al.tool_denied("inventory_describe") == "role_allowlist_missing"
    assert allowlist.load("root", tmp_path).problem == "role_unknown"


def test_allowlist_loads_and_narrows(tmp_path: Path) -> None:
    use_allowlists(tmp_path, "governance", "discovery")
    gov = allowlist.load("governance", tmp_path)
    assert gov.problem is None and gov.kinds == frozenset() and gov.kubectl_verbs == frozenset()
    assert gov.tool_denied("controls_describe") is None and gov.tool_denied("plan_propose") is None  # worker-only
    assert gov.tool_denied("db_inspect") == "tool_not_in_role_allowlist"
    assert gov.op_denied({"op": "kubectl", "verb": "apply"}) == "kubectl_not_allowed"  # 13 4: no role
    assert gov.path_denied("/work/../etc/passwd", write=False)[0] == "path_outside_workspace"
    assert gov.path_denied("/out/policy.json", write=True) == (None, "/out/policy.json")
    assert gov.path_denied("/src/x", write=True)[0] == "path_outside_workspace"  # /src is read-only
    assert gov.check("json_syntax", {"path": "/work/a.json"})[:2] == (None, ["jq", "empty", "/work/a.json"])
    assert gov.check("json_syntax", {"path": "-n"})[0] == "path_outside_workspace"  # flag injection
    assert gov.check("rm", {})[0] == "check_not_allowed"
    disc = allowlist.load("discovery", tmp_path)
    assert disc.tool_denied("policy_evaluate") == "tool_not_in_role_allowlist"
    assert disc.op_denied({"op": "write_file", "path": "/src/x"}) == "op_not_in_role_allowlist"


@pytest.mark.parametrize("body", [
    "role: governance\ntools: [inventory_describe]\n",  # the old flat format is refused
    "role: db\ntools: [inventory_describe]\n",
    ":::",
])
def test_allowlist_invalid_files_deny(tmp_path: Path, body: str) -> None:
    (tmp_path / "governance.yaml").write_text(body)
    assert allowlist.load("governance", tmp_path).problem == "role_allowlist_invalid"


@pytest.mark.parametrize(("old", "new"), [
    ("mcp_tools: [inventory_describe, controls_describe, policy_evaluate, rehearsal_exec]",
     "mcp_tools: [inventory_describe, controls_describe, policy_evaluate, rehearsal_exec, plan_propose]"),
    ("states: [ANALYZE, PROPOSE, REHEARSE]", "states: [ANALYZE, CUTOVER]"),
    ("role: governance", "role: db"),
])
def test_allowlist_refuses_widening_edits(tmp_path: Path, old: str, new: str) -> None:
    text = (AGENT_ALLOWLISTS / "governance.yaml").read_text()
    assert old in text
    (tmp_path / "governance.yaml").write_text(text.replace(old, new))
    assert allowlist.load("governance", tmp_path).problem == "role_allowlist_invalid"


# Proposal: per-role schema (bravo_agents.schemas) + binding + lint -------------------------------


def test_good_proposal_passes_and_digest_is_canonical() -> None:
    p, problems = proposal.check(good_proposal(), "governance", BINDING)
    assert p is not None and problems == []
    artifact = p.model_dump(mode="json")
    d = proposal.digest(artifact)
    assert d == proposal.digest(json.loads(json.dumps(artifact)))  # key order does not matter
    assert d.startswith("sha256:") and len(d) == 71


def _rbac(kind: str) -> dict[str, Any]:
    return {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": kind, "metadata": {"name": "chat-app"},
            "rules": [{"apiGroups": [""], "resources": ["secrets"], "verbs": ["get", "list"]}]}


@pytest.mark.parametrize(("mutate", "problem"), [
    (lambda p: p["translations"][0].update(document=bucket_policy(principal="*")), "anonymous_allow"),
    (lambda p: p["translations"][0].update(document=bucket_policy(actions=["s3:*"])), "wildcard_action"),
    (lambda p: p["translations"][0].update(target={"type": "k8s_rbac", "name": "chat-app"},
                                           document=_rbac("ClusterRole")), "cluster_scoped_rbac"),
    (lambda p: p["translations"][0].update(target={"type": "k8s_rbac", "name": "chat-app"},
                                           document=_rbac("Role")), "secret_list_watch"),
    (lambda p: p["translations"][0]["target"].update(name="rehearsal-sandbox-x"), "reserved_name"),
    (lambda p: p.update(rationale="use password = 'hunter22secret' for the app"), "secret_like_value"),
    (lambda p: p.update(trial_id="t-other-trial"), "binding_trial_id"),
    (lambda p: p.update(evidence_refs=["inv:not-in-envelope"]), "unknown_evidence_ref"),
])
def test_policy_lint_and_binding(mutate: Any, problem: str) -> None:
    raw = good_proposal()
    mutate(raw)
    p, problems = proposal.check(raw, "governance", BINDING)
    assert p is not None and problem in problems


def test_schema_rejects_free_text_other_roles_and_extra_fields() -> None:
    p, problems = proposal.check("please delete the bucket", "governance", BINDING)
    assert p is None and problems
    p, problems = proposal.check(good_proposal(), "discovery", BINDING)  # role literal must match the Trial
    assert p is None and any(x.startswith("schema:") for x in problems)
    raw = good_proposal()
    raw["manifest"] = {"apiVersion": "v1", "kind": "Pod"}
    p, problems = proposal.check(raw, "governance", BINDING)
    assert p is None and any("manifest" in x for x in problems)
    assert proposal.check(good_proposal(), "root", BINDING) == (None, ["role_unknown"])


# SDK options (07 7) and fail-closed model configuration --------------------------------------


def test_llm_env_fails_closed_and_passes_route_settings_only() -> None:
    for env in ({}, {"ANTHROPIC_BASE_URL": "http://somewhere"}, {"ANTHROPIC_API_KEY": "k"},
                {"BRAVO_LLM_PROVIDER": "bedrock_gateway", "BRAVO_LLM_GATEWAY_URL": "https://gw.internal",
                 "BRAVO_MODEL_OPUS": "jp.anthropic.claude-opus-5-5"}):  # bedrock without a region
        with pytest.raises(sdk_options.LLMUnconfigured):
            sdk_options.llm_env("opus", env)
    env = {"BRAVO_LLM_PROVIDER": "bedrock_gateway", "BRAVO_LLM_GATEWAY_URL": "https://gw.internal",
           "BRAVO_MODEL_OPUS": "jp.anthropic.claude-opus-5-5", "BRAVO_MODEL_HAIKU": "jp.anthropic.claude-haiku-5-5",
           "BRAVO_BEDROCK_REGION": "ap-northeast-1", "BRAVO_LLM_GATEWAY_TOKEN": "gw-secret",
           "ANTHROPIC_API_KEY": "k", "AWS_SECRET_ACCESS_KEY": "s"}
    out = sdk_options.llm_env("opus", env)
    assert out == {k: env[k] for k in ("BRAVO_BEDROCK_REGION", "BRAVO_LLM_GATEWAY_URL", "BRAVO_LLM_PROVIDER",
                                       "BRAVO_MODEL_HAIKU", "BRAVO_MODEL_OPUS")}  # no key, no gateway token
    env2 = {**env, "BRAVO_WORKER_ENV_PASSTHROUGH": "CODEX_HOME,HOME", "CODEX_HOME": "/srv/codex", "HOME": "/h"}
    assert sdk_options.llm_env("opus", env2) == out                      # no passthrough of any other name
    sub = {"BRAVO_MODEL_OPUS": "claude-opus-5-5", "BRAVO_MODEL_HAIKU": "claude-haiku-5-5",
           "CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat-should-not-pass", "ANTHROPIC_API_KEY": "k"}
    assert sdk_options.llm_env("opus", sub) == {"BRAVO_MODEL_HAIKU": "claude-haiku-5-5",
                                                "BRAVO_MODEL_OPUS": "claude-opus-5-5"}  # default claude_subscription
    assert sdk_options.provider(sub) == "claude_subscription" and sdk_options.vault_approle(sub) == "llm-claude"
    assert sdk_options.vault_approle({"BRAVO_LLM_PROVIDER": "openai_agents"}) == "llm-openai"
    assert sdk_options.vault_approle(env) is None                        # gateway: no model credential
    with pytest.raises(sdk_options.LLMUnconfigured):
        sdk_options.llm_env("opus", {"BRAVO_LLM_PROVIDER": "codex_chatgpt", "BRAVO_MODEL_OPUS": "m"})  # removed


def test_worker_sdk_options_match_13_section_6(tmp_path: Path) -> None:
    """The options bravo_agents.run_agent builds for our worker: MCP tool layer only, no submit tool."""
    from datetime import UTC, datetime, timedelta

    from bravo_agents import StateEnvelope, load_allowlist
    from bravo_agents.llm import resolve_llm
    from bravo_agents.options import build_options

    al = load_allowlist("governance", AGENT_ALLOWLISTS)
    env = StateEnvelope.model_validate({
        "tenant_id": "t1", "plan_id": ENV_PLAN, "trial_id": "t-governance-1", "attempt": 1, "state": "REHEARSE",
        "role": "governance", "task": "governance", "deadline": (datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
        "budget_usd": 1.5, "approved_scope": {"mcp_tools": list(al.mcp_tools)}})
    route = resolve_llm("opus", {"BRAVO_LLM_PROVIDER": "bedrock_gateway",
                                 "BRAVO_LLM_GATEWAY_URL": "https://gw.internal",
                                 "BRAVO_MODEL_OPUS": "jp.anthropic.claude-opus-5-5",
                                 "BRAVO_BEDROCK_REGION": "ap-northeast-1"})
    hooks = {k: None for k in ("PreToolUse", "PostToolUse", "UserPromptSubmit")}
    o = build_options(al, env, "http://tools/mcp", "TOKEN", route, str(tmp_path), hooks)  # type: ignore[arg-type]
    assert o.tools == [] and o.setting_sources == [] and o.strict_mcp_config is True and o.permission_mode == "dontAsk"
    assert "mcp__platform__plan_propose" not in o.allowed_tools and "mcp__platform__plan_propose" in o.disallowed_tools
    assert set(o.allowed_tools) == {f"mcp__platform__{t}" for t in al.mcp_tools}
    assert o.max_budget_usd == min(al.limits.max_budget_usd, 1.5) and o.model == "jp.anthropic.claude-opus-5-5"
    assert o.env["ANTHROPIC_CUSTOM_HEADERS"] == "Authorization: Bearer TOKEN" and o.env["AWS_SECRET_ACCESS_KEY"] == ""


def test_worker_exits_llm_unconfigured_without_model_credentials(tmp_path: Path) -> None:
    clean = {"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path)}
    r = subprocess.run([sys.executable, "-m", "bravo_rehearsal.worker"], env=clean, capture_output=True,
                       text=True, timeout=60, check=False)
    assert r.returncode == 20 and "llm_unconfigured" in r.stderr


@pytest.mark.llm
@pytest.mark.skipif(not llm_configured(), reason="llm_unconfigured")
def test_live_agent_proposes_bucket_policy() -> None:  # pragma: no cover - needs a model and a cluster
    pytest.skip("cluster_unconfigured" if not os.environ.get("BRAVO_REHEARSAL_KUBECONFIG") else "run via service")


# Service: preconditions and the tool layer over HTTP ------------------------------------------


def settings(tmp_path: Path, **kw: Any) -> ServiceSettings:
    base = dict(api_token="svc-token", token_key=KEY, kubeconfig=None, kube_context=None,
                sandbox_image=None, verifier_image=None, tool_layer_url="http://127.0.0.1:1",
                tasks_dir=TASKS, allowlists_dir=tmp_path, trial_deadline_s=60, verifier_deadline_s=30,
                budget_usd=1.0, idle_s=60)
    return ServiceSettings(**{**base, **kw})


def trial_body(**kw: Any) -> dict[str, Any]:
    return {"plan_id": "11111111-2222-3333-4444-555555555555", "tenant_id": "t1", "rehearsal_id": "r1",
            "attempt": 1, "max_attempts": 3, "task": "fix-bucket-policy", "role": "governance", **kw}


@pytest.mark.skipif(llm_configured(), reason="asserts the unconfigured path")
def test_start_fails_closed_with_llm_unconfigured(tmp_path: Path) -> None:
    use_allowlists(tmp_path, "governance")
    with TestClient(create_app(settings(tmp_path))) as c:
        assert c.post("/v1/trials", json=trial_body(), headers={"Idempotency-Key": "k1"}).status_code == 401
        r = c.post("/v1/trials", json=trial_body(),
                   headers={"Idempotency-Key": "k1", "Authorization": "Bearer svc-token"})
        assert r.status_code == 503
        body = r.json()
        assert body["error"] == "llm_unconfigured"
        assert body["missing"] == ["llm_unconfigured", "cluster_unconfigured", "verifier_unconfigured"]
        assert c.app.state.store.get("anything") is None  # nothing was created
        r = c.post("/v1/trials", json=trial_body(task="nope", role="ci"),
                   headers={"Idempotency-Key": "k2", "Authorization": "Bearer svc-token"})
        assert {"task_unknown", "role_allowlist_missing"} <= set(r.json()["missing"])


def rpc(c: TestClient, token: str | None, method: str, params: dict[str, Any] | None = None) -> Any:
    h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
         "MCP-Protocol-Version": "2025-11-25"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return c.post("/mcp", json={"jsonrpc": "2.0", "id": next(_ids), "method": method, "params": params or {}},
                  headers=h)


def call(c: TestClient, token: str, name: str, args: dict[str, Any]) -> dict[str, Any]:
    r = rpc(c, token, "tools/call", {"name": name, "arguments": args})
    assert r.status_code == 200, r.text
    return json.loads(r.json()["result"]["content"][0]["text"])


@pytest.fixture()
def layer(tmp_path: Path) -> Any:
    use_allowlists(tmp_path, "governance", "discovery")
    s = settings(tmp_path)
    app = create_app(s)
    with TestClient(app) as c:
        store = app.state.store

        def add(role: str, attempt: int = 1) -> tuple[Trial, str]:
            plan = PLAN
            t = Trial(trial_id=f"t-{role}-{attempt}", tenant_id="t1", plan_id=plan, rehearsal_id="r", attempt=attempt,
                      max_attempts=3, task="fix-bucket-policy", role=role, idem_key=f"k-{role}-{attempt}",
                      request_digest="d", deadline=time.time() + 60, allowlist=allowlist.load(role, tmp_path),
                      envelope={"plan_id": ENV_PLAN, "prior_failures": [], "inputs": {},
                                "evidence": [{"id": "inv:bucket-1", "kind": "inventory", "summary": "s3 policy"}]})
            t.names = Names.of(plan, attempt, t.trial_id)
            t.phase = "agent_running"
            store.add(t)
            tok = trial_token.issue(KEY, claims(trial_id=t.trial_id, plan_id=plan, attempt=attempt,
                                                namespace=t.names.rehearsal_ns))
            return t, tok

        yield c, add


def test_tool_layer_identity_failures(layer: Any) -> None:
    c, add = layer
    trial, tok = add("governance")
    assert rpc(c, None, "tools/list").status_code == 401
    assert rpc(c, "garbage", "tools/list").status_code == 401
    forged = trial_token.issue(b"z" * 32, claims(trial_id=trial.trial_id))
    assert rpc(c, forged, "tools/list").json()["error"] == "token_bad_signature"
    other_ns = trial_token.issue(KEY, claims(trial_id=trial.trial_id, plan_id=trial.plan_id, namespace="prod"))
    assert rpc(c, other_ns, "tools/list").status_code == 403  # token bound to a different namespace


def test_tool_layer_enforces_role_allowlist_and_sandbox_rules(layer: Any) -> None:
    c, add = layer
    trial, tok = add("governance")
    names = {t["name"] for t in rpc(c, tok, "tools/list").json()["result"]["tools"]}
    assert names == {"inventory_describe", "controls_describe", "policy_evaluate", "rehearsal_exec"}  # no submit tool
    ns = trial.names.rehearsal_ns
    assert call(c, tok, "rehearsal_exec", {"op": "rm -rf /app"})["blocked"] == "free_text_command"
    assert call(c, tok, "rehearsal_exec", {"op": {"op": "kubectl", "verb": "delete", "kind": "namespace", "name": ns}}
                )["blocked"] == "kubectl_not_allowed"  # V4: no role applies or deletes anything
    assert call(c, tok, "rehearsal_exec", {"op": {"op": "write_file", "path": "/src/app.py", "content": "x"}}
                )["blocked"] == "path_outside_workspace"  # /src is read-only for governance
    assert call(c, tok, "rehearsal_exec", {"op": {"op": "write_file", "path": "/out/p.json", "content": "x" * 300_000}}
                )["blocked"] == "write_too_large"
    assert call(c, tok, "rehearsal_exec", {"op": {"op": "run_check", "name": "json_syntax", "args": {"path": "-n"}}}
                )["blocked"] == "path_outside_workspace"
    r = call(c, tok, "repo_read", {"path": "source/s3-bucket-policy.json"})
    assert r["blocked"] == "tool_not_in_role_allowlist"
    assert call(c, tok, "db_inspect", {"query": "extensions"})["blocked"] == "tool_not_in_role_allowlist"
    assert call(c, tok, "controls_describe", {})["blocked"] == "gate_unconfigured"  # server tool, fails closed
    # Allowed op, but no sandbox in this test: refused, never simulated.
    check = {"op": "run_check", "name": "json_syntax", "args": {"path": "/work/a.json"}}
    assert call(c, tok, "rehearsal_exec", {"op": check})["blocked"] == "sandbox_unavailable"
    rules = [(e["tool"], e["allowed"], e["rule"]) for e in trial.tool_events]
    assert ("rehearsal_exec", False, "free_text_command") in rules
    assert ("rehearsal_exec", False, "sandbox_unavailable") in rules and ("db_inspect", False,
                                                                          "tool_not_in_role_allowlist") in rules


def test_role_allowlist_hides_and_blocks_tools(layer: Any) -> None:
    c, add = layer
    trial, tok = add("discovery")
    names = {t["name"] for t in rpc(c, tok, "tools/list").json()["result"]["tools"]}
    assert names == {"inventory_describe", "repo_read", "rehearsal_exec"}
    assert call(c, tok, "policy_evaluate", {"proposal": good_proposal()})["blocked"] == "tool_not_in_role_allowlist"
    r = call(c, tok, "plan_propose", {"proposal": good_proposal(trial.trial_id)})  # a governance shape
    assert r["blocked"] == "proposal_rejected" and trial.artifact is None
    # B9: no uploaded snapshot of the customer repo, so nothing to read (never the task template).
    assert call(c, tok, "repo_read", {"path": "source/s3-bucket-policy.json"})["blocked"] == "repo_unconfigured"


def test_plan_propose_freezes_artifact_and_locks_trial(layer: Any) -> None:
    c, add = layer
    trial, tok = add("governance", attempt=2)
    bad = good_proposal(trial.trial_id, attempt=2)
    bad["translations"][0]["document"] = bucket_policy(principal="*")
    r = call(c, tok, "plan_propose", {"proposal": bad})
    assert r["accepted"] is False and "anonymous_allow" in r["problems"] and trial.artifact is None
    assert call(c, tok, "plan_propose", {"proposal": good_proposal(trial.trial_id, attempt=1)}
                )["problems"] == ["binding_attempt"]
    r = call(c, tok, "plan_propose", {"proposal": good_proposal(trial.trial_id, attempt=2)})
    assert r["accepted"] is True and r["artifact_digest"] == proposal.digest(trial.artifact)
    assert trial.locked and trial.phase == "proposed"
    assert rpc(c, tok, "tools/call", {"name": "inventory_describe", "arguments": {}}).status_code == 403


def test_evidence_ingest_takes_trial_token_and_dedups(layer: Any) -> None:
    c, add = layer
    trial, tok = add("governance")
    ev = {"trial_id": trial.trial_id, "seq": 1, "kind": "tool_request", "tool_name": "mcp__platform__x",
          "tool_input": {"a": 1}, "at": "2026-11-02T00:00:00Z"}
    assert c.post("/v1/evidence", json=ev).status_code == 401
    assert c.post("/v1/evidence", json=ev, headers={"Authorization": f"Bearer {tok}"}).status_code == 202
    assert c.post("/v1/evidence", json=ev, headers={"Authorization": f"Bearer {tok}"}).status_code == 409
    assert c.post("/v1/evidence", json={**ev, "trial_id": "t-other"},
                  headers={"Authorization": f"Bearer {tok}"}).status_code == 422
    assert trial.hook_events[0]["authoritative"] is False and "tool_input" not in trial.hook_events[0]


# Destination cluster: the namespace, baseline, isolation and exec tests run through the Harbor
# environment in tests/test_harbor.py (test_every_environment_method_on_real_cluster).


class _FakeBao:
    """The service's OpenBao identity: mints one secret_id per Trial, reports presence from metadata."""

    def __init__(self, present: str = "ok") -> None:
        from bravo_rehearsal.server_tools import OpenBaoConfig

        self.cfg = OpenBaoConfig(addr="https://bao.internal:8200", kv_mount="secret", ca_cert="/etc/bravo/bao-ca.crt")
        self.minted: list[str] = []
        self.present, self.paths = present, []

    async def mint_secret_id(self, approle: str, ttl: str = "60s") -> tuple[str, str]:
        self.minted.append(approle)
        return "role-id-1", f"secret-id-{len(self.minted)}"

    async def secret_present(self, path: str) -> str:
        self.paths.append(path)
        return self.present


async def test_worker_env_carries_one_use_delivery_not_the_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from bravo_rehearsal.service import Orchestrator, TrialRequest, new_trial
    from bravo_rehearsal.trials import TrialStore

    use_allowlists(tmp_path, "governance")
    for k, v in {"BRAVO_LLM_PROVIDER": "claude_subscription", "BRAVO_MODEL_OPUS": "claude-opus-5-5",
                 "BRAVO_MODEL_HAIKU": "claude-haiku-5-5", "CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat-leak",
                 "ANTHROPIC_API_KEY": "sk-ant-leak", "BRAVO_WORKER_ENV_PASSTHROUGH": "BRAVO_TRIAL_TOKEN,HOME"}.items():
        monkeypatch.setenv(k, v)
    s = settings(tmp_path)
    trial = new_trial(TrialRequest.model_validate(trial_body(task="governance")), s, "k", "d")
    trial.names = Names.of(trial.plan_id, trial.attempt, trial.trial_id)
    bao = _FakeBao()
    orch = Orchestrator(s, TrialStore(), bao)  # type: ignore[arg-type]
    env = await orch._worker_env(trial, {}, str(tmp_path / "cfg"))
    assert bao.minted == ["llm-claude"]                                   # one secret_id per Trial
    assert env["BRAVO_LLM_BAO_ROLE_ID"] == "role-id-1" and env["BRAVO_LLM_BAO_SECRET_ID"] == "secret-id-1"
    assert env["BRAVO_BAO_ADDR"] == "https://bao.internal:8200" and env["BRAVO_BAO_CACERT"] == "/etc/bravo/bao-ca.crt"
    assert env["BRAVO_MODEL_OPUS"] == "claude-opus-5-5" and env["HOME"] == str(tmp_path / "cfg")
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env and "ANTHROPIC_API_KEY" not in env
    assert "sk-ant" not in json.dumps(env)                                # the token never transits the service
    assert trial_token.verify(KEY, env["BRAVO_TRIAL_TOKEN"]).trial_id == trial.trial_id  # not overridable
    assert env["BRAVO_EVIDENCE_URL"].endswith("/v1/evidence") and "BRAVO_REHEARSAL_TOKEN_KEY" not in env
    assert json.loads(env["BRAVO_TRIAL_ENVELOPE"])["plan_id"] == ENV_PLAN
    assert set(env) <= {"PATH", "HOME", "BRAVO_LLM_PROVIDER", "BRAVO_MODEL_OPUS", "BRAVO_MODEL_HAIKU",
                        "BRAVO_BAO_ADDR", "BRAVO_BAO_CACERT", "BRAVO_BAO_KV_MOUNT", "BRAVO_LLM_BAO_ROLE_ID",
                        "BRAVO_LLM_BAO_SECRET_ID", "BRAVO_TRIAL_TOKEN", "BRAVO_TOOL_LAYER_URL",
                        "BRAVO_EVIDENCE_URL", "BRAVO_TRIAL_ENVELOPE", "BRAVO_AGENT_ROLE",
                        "BRAVO_AGENT_ALLOWLIST_DIR"}                       # exact names only
    with pytest.raises(sdk_options.LLMUnconfigured):
        await Orchestrator(s, TrialStore(), None)._worker_env(trial, {}, str(tmp_path / "cfg"))  # no OpenBao
    monkeypatch.setenv("BRAVO_LLM_PROVIDER", "openai_agents")
    await orch._worker_env(trial, {}, str(tmp_path / "cfg"))
    assert bao.minted == ["llm-claude", "llm-openai"]


async def test_llm_credential_presence_from_metadata(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from bravo_rehearsal.service import Orchestrator
    from bravo_rehearsal.trials import TrialStore

    monkeypatch.delenv("BRAVO_LLM_PROVIDER", raising=False)
    bao = _FakeBao("llm_token_missing")
    orch = Orchestrator(settings(tmp_path), TrialStore(), bao)  # type: ignore[arg-type]
    assert await orch.llm_credential() == "llm_token_missing" and bao.paths == ["envs/platform/llm-claude"]
    bao.present = "ok"
    assert await orch.llm_credential() == "ok"
    monkeypatch.setenv("BRAVO_LLM_PROVIDER", "openai_agents")
    await orch.llm_credential()
    assert bao.paths[-1] == "envs/platform/llm-openai"
    monkeypatch.setenv("BRAVO_LLM_PROVIDER", "bedrock_gateway")
    assert await orch.llm_credential() == "ok"                            # gateway: nothing in the worker
    monkeypatch.setenv("BRAVO_LLM_PROVIDER", "claude_subscription")
    assert await Orchestrator(settings(tmp_path), TrialStore(), None).llm_credential() == "openbao_unconfigured"


def test_worker_exits_llm_unconfigured_without_openbao_delivery(tmp_path: Path) -> None:
    clean = {"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path), "BRAVO_MODEL_OPUS": "claude-opus-5-5",
             "BRAVO_MODEL_HAIKU": "claude-haiku-5-5", "BRAVO_AGENT_ROLE": "discovery"}
    r = subprocess.run([sys.executable, "-m", "bravo_rehearsal.worker"], env=clean, capture_output=True,
                       text=True, timeout=60, check=False)
    assert r.returncode == 20 and "llm_unconfigured" in r.stderr


async def test_bravo_agents_submit_proposal_against_the_live_tool_layer(tmp_path: Path) -> None:
    """The worker's real submit path (bravo_agents.runner.submit_proposal, MCP 2.x) against this server:
    accepted once, the Trial locks, and a rejected proposal surfaces as proposal_rejected (is_error)."""
    import asyncio
    import socket

    import uvicorn
    from bravo_agents.errors import AgentRunError
    from bravo_agents.runner import submit_proposal
    from bravo_agents.schemas import GovernanceProposal

    use_allowlists(tmp_path, "governance")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    app = create_app(settings(tmp_path, tool_layer_url=f"http://127.0.0.1:{port}"))
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on"))
    serving = asyncio.create_task(server.serve())
    try:
        while not server.started:
            await asyncio.sleep(0.05)
        store = app.state.store
        trials = {}
        for attempt in (1, 2):
            t = Trial(trial_id=f"t-governance-{attempt}", tenant_id="t1", plan_id=PLAN, rehearsal_id="r",
                      attempt=attempt, max_attempts=3, task="governance", role="governance",
                      idem_key=f"k{attempt}", request_digest="d", deadline=time.time() + 60,
                      allowlist=allowlist.load("governance", tmp_path),
                      envelope={"plan_id": ENV_PLAN, "evidence": [], "inputs": {}})
            t.names = Names.of(PLAN, attempt, t.trial_id)
            t.phase = "agent_running"
            store.add(t)
            trials[attempt] = (t, trial_token.issue(KEY, claims(trial_id=t.trial_id, plan_id=PLAN, attempt=attempt,
                                                                namespace=t.names.rehearsal_ns)))
        url = f"http://127.0.0.1:{port}/mcp"
        t1, tok1 = trials[1]
        await submit_proposal(url, tok1, GovernanceProposal.model_validate(good_proposal(t1.trial_id, 1)))
        assert t1.artifact_digest == proposal.digest(t1.artifact) and t1.locked
        t2, tok2 = trials[2]
        bad = good_proposal(t2.trial_id, 2)
        bad["translations"][0]["document"] = bucket_policy(principal="*")
        with pytest.raises(AgentRunError) as exc:
            await submit_proposal(url, tok2, GovernanceProposal.model_validate(bad))
        assert exc.value.code == "proposal_rejected" and "anonymous_allow" in exc.value.detail
        assert t2.artifact is None
    finally:
        server.should_exit = True
        await serving


COLLECTOR = Path(__file__).resolve().parents[2] / "collector"


def _demo_repo(root: Path) -> Path:
    (root / "app" / "storage").mkdir(parents=True)
    (root / "app" / "main.py").write_text("import boto3\ns3 = boto3.client('s3')\n")
    (root / "app" / "storage" / "policy.json").write_text('{"Statement": []}\n')
    (root / ".env").write_text("DB_PASSWORD=hunter2hunter2\n")
    (root / "README.md").write_text("not scanned by the collector\n")
    (root / "logo.png").write_bytes(b"\x89PNG\0\0binary")
    return root


def _pack_with_collector(repo: Path, out: Path) -> str:
    py = COLLECTOR / ".venv" / "bin" / "python"
    if not py.is_file():
        pytest.skip("collector_unconfigured: platform/collector/.venv not built")
    env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
    r = subprocess.run([str(py), "-m", "bravo_rehearsal.snapshot", "pack", "--repo", str(repo), "--out", str(out)],
                       capture_output=True, text=True, timeout=120, env=env, check=False)
    assert r.returncode == 0, r.stderr
    return dict(line.split("=", 1) for line in r.stdout.splitlines())["input_digest"]


def test_snapshot_upload_is_bound_to_collector_inventory(tmp_path: Path) -> None:
    """B9: repo_read serves only a customer repo snapshot whose digest equals the collector's inventory."""
    use_allowlists(tmp_path, "governance", "discovery")
    repo = _demo_repo(tmp_path / "repo")
    digest = _pack_with_collector(repo, tmp_path / "snap.tar.gz")
    s = settings(tmp_path)
    app = create_app(s)
    auth = {"Authorization": "Bearer svc-token"}
    url = f"/v1/snapshots/t1/{PLAN}"
    body = (tmp_path / "snap.tar.gz").read_bytes()
    with TestClient(app) as c:
        assert c.put(url, content=body, headers={"X-Bravo-Inventory-Digest": digest}).status_code == 401
        bad = c.put(url, content=body, headers={**auth, "X-Bravo-Inventory-Digest": "0" * 64})
        assert bad.status_code == 422 and bad.json()["error"] == "snapshot_digest_mismatch"
        ok = c.put(url, content=body, headers={**auth, "X-Bravo-Inventory-Digest": digest})
        assert ok.status_code == 200, ok.text
        assert ok.json()["withheld"] == [".env"] and ok.json()["input_digest"] == digest
        from bravo_rehearsal.service import TrialRequest, new_trial

        req = TrialRequest.model_validate(trial_body(task="governance", inventory_digest=digest))
        trial = new_trial(req, s, "k-snap", "d")
        assert trial.envelope["inputs"]["repo_snapshot"]["input_digest"] == digest
        trial.names = Names.of(PLAN, 1, trial.trial_id)
        trial.phase = "agent_running"
        app.state.store.add(trial)
        trial.allowlist = allowlist.load("discovery", tmp_path)  # a role that has repo_read
        tok = trial_token.issue(KEY, claims(trial_id=trial.trial_id, plan_id=PLAN, attempt=1,
                                            namespace=trial.names.rehearsal_ns))
        assert "boto3" in call(c, tok, "repo_read", {"path": "app/main.py"})["content"]
        assert call(c, tok, "repo_read", {"path": ".env"})["blocked"] == "path_outside_repo"  # withheld
        assert call(c, tok, "repo_read", {"path": "README.md"})["blocked"] == "path_outside_repo"  # not scanned
        other = TrialRequest.model_validate(trial_body(task="governance", inventory_digest="1" * 64))
        assert new_trial(other, s, "k-other", "d").repo_snapshot is None  # a different inventory: not bound


def test_readyz_reports_each_precondition(tmp_path: Path) -> None:
    use_allowlists(tmp_path, "governance")
    with TestClient(create_app(settings(tmp_path))) as c:
        r = c.get("/readyz")
    assert r.status_code == 503 and r.json()["ready"] is False
    checks = r.json()["checks"]
    assert checks["cluster"] == "cluster_unconfigured" and checks["images"] == "verifier_unconfigured"
    assert checks["tool_layer"] == "ok" and set(checks) == {"llm", "cluster", "images", "tool_layer", "openbao",
                                                            "gate", "llm_token"}
    assert checks["llm_token"] in ("openbao_unconfigured", "ok")          # presence only, never a value
