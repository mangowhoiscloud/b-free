"""Front switch rules (pure functions): step plan, judgement and revert decision, propagation time."""

from __future__ import annotations

import pytest

from bravo_executor.front import (
    LB_REQUIRED,
    FrontConfig,
    PlannedStep,
    Sample,
    judge,
    lb_pool_weights,
    measure_propagation,
    p95,
    plan_steps,
)
from bravo_executor.vault import VaultConfig

CFG = FrontConfig.from_env(
    {
        "BRAVO_FRONT_APP": "b-free-chat",
        "BRAVO_FRONT_ENV_IDS": "src=aws,dst=onprem",
        "BRAVO_FRONT_MIN_SAMPLES": "10",
        "BRAVO_FRONT_MAX_ERROR_RATE": "0.05",
        "BRAVO_FRONT_MAX_P95_MS": "500",
    }
)


def test_config_names_follow_the_edge_module() -> None:
    assert CFG.tunnel_name("dst") == "bravo-b-free-chat-dst"
    assert CFG.other_env("dst") == "src" and CFG.env_ids == {"src": "aws", "dst": "onprem"}
    assert CFG.lb_enabled is False


def test_cname_mode_records_partial_weights_as_not_applied() -> None:
    assert plan_steps(False) == [
        PlannedStep(10, False, LB_REQUIRED),
        PlannedStep(50, False, LB_REQUIRED),
        PlannedStep(100, True),
    ]


def test_lb_mode_applies_every_step() -> None:
    assert [s.weight for s in plan_steps(True)] == [10, 50, 100]
    assert all(s.applied for s in plan_steps(True))


@pytest.mark.parametrize("steps", [[], [50, 10], [10, 10], [10, 120], [-1]])
def test_invalid_step_lists_are_rejected(steps: list[int]) -> None:
    with pytest.raises(ValueError):
        plan_steps(True, steps)


def test_pool_weights_split_and_revert() -> None:
    assert lb_pool_weights("pd", "ps", 10) == {"pd": 0.1, "ps": 0.9}
    assert lb_pool_weights("pd", "ps", 0) == {"pd": 0.0, "ps": 1.0}


def _ok(n: int, latency: float = 100.0) -> list[Sample]:
    return [Sample(i * 0.5, True, latency, "onprem", 200) for i in range(n)]


def test_judge_healthy_step() -> None:
    j = judge(_ok(20), CFG)
    assert j.healthy and j.error_rate == 0.0 and j.p95_ms == 100.0 and j.reasons == ()


def test_judge_reverts_on_error_rate() -> None:
    samples = _ok(18) + [Sample(10.0, False, 30.0, None, 502), Sample(10.5, False, 30.0, None, None)]
    j = judge(samples, CFG)
    assert not j.healthy and any(r.startswith("ERROR_RATE") for r in j.reasons)


def test_judge_reverts_on_p95() -> None:
    samples = _ok(18) + _ok(2, latency=2000.0)
    j = judge(samples, CFG)
    assert j.p95_ms == 2000.0 and not j.healthy and any(r.startswith("P95") for r in j.reasons)


def test_judge_does_not_pass_a_step_without_enough_samples() -> None:
    j = judge(_ok(3), CFG)
    assert not j.healthy and j.reasons[0].startswith("INSUFFICIENT_SAMPLES")


def test_p95_nearest_rank() -> None:
    assert p95([float(v) for v in range(1, 101)]) == 95.0
    assert p95([]) is None


def test_propagation_is_the_start_of_the_final_consecutive_run() -> None:
    samples = [
        Sample(0.1, True, 50, "aws", 200),
        Sample(0.6, True, 50, "onprem", 200),   # first hit, then a stale edge answers again
        Sample(1.1, True, 50, "aws", 200),
        Sample(1.6, True, 50, "onprem", 200),   # final run starts here
        Sample(2.1, True, 50, "onprem", 200),
        Sample(2.6, True, 50, "onprem", 200),
    ]
    r = measure_propagation(samples, "onprem", consecutive=3)
    assert r.converged and r.propagation_ms == 1600.0 and r.first_target_ms == 600.0 and r.probes == 6


def test_propagation_not_converged_is_reported_as_such() -> None:
    samples = [Sample(0.1, False, 5, None, None), Sample(0.6, True, 50, "onprem", 200)]
    r = measure_propagation(samples, "onprem", consecutive=3)
    assert not r.converged and r.propagation_ms is None and r.first_target_ms == 600.0


def test_vault_config_never_shows_credentials() -> None:
    cfg = VaultConfig.from_env(
        {"BRAVO_BAO_ADDR": "https://127.0.0.1:8200", "BRAVO_BAO_ROLE_ID": "ROLE-XYZ", "BRAVO_BAO_SECRET_ID": "SID-XYZ"}
    )
    assert "ROLE-XYZ" not in repr(cfg) and "SID-XYZ" not in repr(cfg)


def test_platform_front_is_never_switched() -> None:
    import pytest

    from bravo_executor.front import FrontConfig

    with pytest.raises(ValueError):
        FrontConfig.from_env({"BRAVO_FRONT_APP": "b-free", "BRAVO_FRONT_ENV_IDS": "src=aws,dst=onprem"})
    cfg = FrontConfig.from_env({"BRAVO_FRONT_APP": "b-free-chat", "BRAVO_FRONT_ENV_IDS": "src=aws,dst=onprem"})
    assert cfg.tunnel_name("dst") == "bravo-b-free-chat-dst"
