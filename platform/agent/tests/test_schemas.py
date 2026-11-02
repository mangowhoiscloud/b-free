import pytest

from bravo_agents.errors import AgentRunError
from bravo_agents.runner import digest, validate_output
from bravo_agents.schemas import OUTPUT_MODELS, output_schema
from conftest import make_envelope

BASE = {"plan_id": "p-0012", "trial_id": "t-0012-2", "attempt": 2, "rationale": "r", "risk": "low",
        "evidence_refs": ["gate:obj-01"]}
GOV = {**BASE, "role": "governance", "step": "translate", "audit_summary": "s", "translations": [{
    "source": {"type": "aws_s3_bucket_policy", "id": "attachments"},
    "target": {"type": "rgw_bucket_policy", "name": "attachments"},
    "document": {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Principal": {"AWS": ["arn:aws:iam:::user/app"]},
                 "Action": ["s3:GetObject", "s3:PutObject"], "Resource": ["arn:aws:s3:::attachments/*"]}]},
    "control_ids": ["IAM-01"]}]}


@pytest.mark.parametrize("role", OUTPUT_MODELS)
def test_output_schema_is_json_schema(role):
    s = output_schema(role)
    assert s["type"] == "object" and s["additionalProperties"] is False


def test_valid_governance_proposal():
    out = validate_output("governance", GOV, make_envelope())
    assert out.translations[0].target.type == "rgw_bucket_policy"
    assert digest(out) == digest(validate_output("governance", dict(GOV), make_envelope()))


@pytest.mark.parametrize("raw,code", [
    (None, "no_structured_output"),
    ({**GOV, "extra": 1}, "schema_invalid"),
    ({**GOV, "risk": "none"}, "schema_invalid"),
    ({**GOV, "attempt": 1}, "binding_mismatch"),
    ({**GOV, "evidence_refs": ["gate:made-up"]}, "ref_invalid"),
    ({**GOV, "role": "ci"}, "schema_invalid"),
])
def test_rejects_bad_output(raw, code):
    with pytest.raises(AgentRunError) as e:
        validate_output("governance", raw, make_envelope())
    assert e.value.code == code and not e.value.infra


def test_ci_values_and_relpaths_are_constrained():
    ci = {**BASE, "role": "ci", "step": "containerize", "helm_values": {"port": 8080},
          "build_params": {"template": "python-asgi", "python_version": "3.12", "entry": "app.main:app",
                           "port": 8080, "requirements": "../requirements.txt"}}
    with pytest.raises(AgentRunError) as e:
        validate_output("ci", ci, make_envelope(role="ci"))
    assert e.value.code == "schema_invalid"
