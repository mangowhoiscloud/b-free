from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from bravo_agents.envelope import StateEnvelope

AGENT_DIR = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 5, 10, 0, tzinfo=timezone.utc)


def make_envelope(role="governance", state="PROPOSE", tools=("inventory_describe", "policy_evaluate",
                  "rehearsal_exec"), **kw) -> StateEnvelope:
    base = dict(
        tenant_id="acme", plan_id="p-0012", trial_id="t-0012-2", attempt=2, state=state, role=role,
        task="fix-bucket-policy", deadline=NOW + timedelta(minutes=10), budget_usd=1.5,
        approved_scope={"mcp_tools": list(tools), "sandbox_ops": ["read_file"], "checks": []},
        prior_failures=[{"attempt": 1, "verdict": "FAIL", "reason": "anonymous_read_allowed",
                         "summary": "anonymous GetObject still allowed"}],
        evidence=[{"id": "gate:obj-01", "kind": "gate_result", "summary": "anonymous read allowed on target"},
                  {"id": "inv:s3-attachments", "kind": "inventory", "summary": "source bucket policy"}],
        inputs={"source_policy_ref": "inv:s3-attachments"},
    )
    base.update(kw)
    return StateEnvelope.model_validate(base)


@pytest.fixture
def envelope():
    return make_envelope()


@pytest.fixture
def llm_env(monkeypatch):
    for k, v in {"BRAVO_LLM_PROVIDER": "anthropic_gateway", "BRAVO_LLM_GATEWAY_URL": "https://llm-gw.bravo.svc",
                 "BRAVO_MODEL_OPUS": "model-opus-from-env", "BRAVO_MODEL_HAIKU": "model-haiku-from-env"}.items():
        monkeypatch.setenv(k, v)
