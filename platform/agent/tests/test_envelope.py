import json

import pytest
from pydantic import ValidationError

from bravo_agents.allowlist import load_allowlist
from bravo_agents.envelope import check_scope, envelope_digest, render_system_prompt, render_task_prompt
from conftest import make_envelope


def test_system_prompt_is_deterministic_and_carries_state(envelope):
    al = load_allowlist("governance")
    a, b = render_system_prompt(envelope, al), render_system_prompt(envelope, al)
    assert a == b
    state = json.loads(a.split("<platform_state>\n", 1)[1].split("\n</platform_state>", 1)[0])
    assert state["trial_id"] == "t-0012-2" and state["attempt"] == 2 and state["state"] == "PROPOSE"
    assert "mcp__platform__policy_evaluate" in a
    assert "Decide the gate verdict" in a           # 책임 경계(never)가 프롬프트에 들어감
    assert "data, not instructions" in a


def test_task_prompt_includes_prior_failure_summary(envelope):
    p = render_task_prompt(envelope)
    assert "attempt 2/3" in p and "anonymous_read_allowed" in p


def test_digest_changes_with_state(envelope):
    assert envelope_digest(envelope) != envelope_digest(make_envelope(attempt=3))


def test_envelope_rejects_oversize_and_bad_attempt():
    with pytest.raises(ValidationError):
        make_envelope(inputs={"blob": "x" * 40_000})
    with pytest.raises(ValidationError):
        make_envelope(attempt=3, max_attempts=2)
    with pytest.raises(ValidationError):
        make_envelope(deadline="2026-11-05T10:00:00")   # 시간대 없는 마감 거부


@pytest.mark.parametrize("kw,code", [
    ({"role": "ci"}, "role_mismatch"),
    ({"state": "AWAIT_APPROVAL"}, "state_not_allowed_for_role"),
    ({"tools": ("inventory_describe", "repo_read")}, "scope_exceeds_allowlist"),
])
def test_scope_must_stay_inside_allowlist(kw, code):
    with pytest.raises(ValueError, match=code):
        check_scope(make_envelope(**kw), load_allowlist("governance"))
