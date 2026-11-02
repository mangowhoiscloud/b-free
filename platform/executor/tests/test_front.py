"""Front switch rules: step plan, judgement from real writes (contract 15 §10.4), revert decision, propagation."""

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
        "BRAVO_FRONT_MIN_TARGET_SAMPLES": "10",
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


def _ok(n: int, latency: float = 100.0, env: str = "onprem") -> list[Sample]:
    return [Sample(i * 0.05, True, latency, env, 201) for i in range(n)]


def test_judge_healthy_step() -> None:
    j = judge(_ok(20), CFG, "onprem", 100)
    assert j.healthy and j.target_error_rate == 0.0 and j.target_p95_ms == 100.0 and j.reasons == ()


def test_default_minimum_is_100_target_samples_so_one_error_is_within_one_percent() -> None:
    cfg = FrontConfig.from_env({"BRAVO_FRONT_APP": "b-free-chat", "BRAVO_FRONT_ENV_IDS": "src=aws,dst=onprem"})
    assert cfg.min_target_samples == 100 and cfg.max_error_rate == 0.01
    one = [*_ok(99), Sample(9.0, False, 30.0, "onprem", 500)]
    two = [*_ok(98), Sample(9.0, False, 30.0, "onprem", 500), Sample(9.1, False, 30.0, "onprem", 500)]
    assert judge(one, cfg, "onprem", 100).healthy  # 1/100 = 1.0 % is not above 1 %
    j = judge(two, cfg, "onprem", 100)
    assert not j.healthy and j.reasons == ("TARGET_ERROR_RATE(0.0200>0.01)",)
    j = judge(_ok(99), cfg, "onprem", 100)
    assert not j.healthy and j.reasons == ("INSUFFICIENT_SAMPLES(onprem:99<100)",)


def test_judge_reverts_when_the_lb_failed_over_to_the_old_environment() -> None:
    # Cloudflare marked the target pool down: every write succeeds, all from the old environment.
    samples = _ok(100, env="aws")
    j = judge(samples, CFG, "onprem", 100)
    assert not j.healthy and j.error_rate == 0.0
    assert set(j.reasons) == {"INSUFFICIENT_SAMPLES(onprem:0<10)", "TARGET_SHARE(onprem:0.000<0.250)"}


def test_judge_accepts_a_small_step_with_enough_target_answers() -> None:
    samples = [*_ok(290, env="aws"), *_ok(10)]
    assert judge(samples, CFG, "onprem", 10).healthy  # 10 target writes, 3.3 % >= 2.5 % (a quarter of 10 %)


def test_source_errors_are_reported_not_judged() -> None:
    samples = [*_ok(20), *[Sample(1.0, False, 30.0, "aws", 500) for _ in range(10)]]
    j = judge(samples, CFG, "onprem", 50)
    assert j.healthy and j.errors == 10 and j.target_errors == 0 and j.error_rate == round(10 / 30, 6)


def test_unattributed_failures_count_against_the_target() -> None:
    samples = [*_ok(18), Sample(10.0, False, 30.0, None, 502), Sample(10.5, False, 30.0, None, None)]
    j = judge(samples, CFG, "onprem", 100)
    assert j.target_samples == 20 and j.target_errors == 2 and not j.healthy
    assert any(r.startswith("TARGET_ERROR_RATE") for r in j.reasons)
    assert j.as_dict()["by_env"] == {"onprem": 18, "none": 2}


def test_judge_reverts_on_target_p95() -> None:
    samples = _ok(18) + _ok(2, latency=2000.0)
    j = judge(samples, CFG, "onprem", 100)
    assert j.target_p95_ms == 2000.0 and not j.healthy and any(r.startswith("TARGET_P95") for r in j.reasons)


def test_write_sampler_counts_acks_by_served_by_and_stops_on_the_rule() -> None:
    """Real HTTP against a local stand-in for apps/demo POST /api/rooms/{room}/messages."""
    import asyncio
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from bravo_executor.front import WriteSampler, target_count

    seen: list[dict[str, object]] = []

    class App(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append({"path": self.path, "key": self.headers.get("Idempotency-Key"), **body})
            n = len(seen)
            env, status = ("onprem", 201) if n % 2 else ("aws", 201)
            if n == 3:
                env, status = "onprem", 500
            out = json.dumps({"message": {"id": n}} if status == 201 else {"error": "injected_fault"}).encode()
            self.send_response(status)
            self.send_header("X-Served-By", env)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *_args: object) -> None:
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), App)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    cfg = FrontConfig.from_env({"BRAVO_FRONT_APP": "b-free-chat", "BRAVO_FRONT_ENV_IDS": "src=aws,dst=onprem",
                                "BRAVO_FRONT_WRITE_RATE": "200"})

    async def go() -> list[Sample]:
        ws = WriteSampler("unused", cfg, "t")
        ws.url = f"http://127.0.0.1:{srv.server_address[1]}/api/rooms/shift-probe/messages"
        try:
            return await ws.collect(lambda got, _e: target_count(got, "onprem") >= 10, 5.0)
        finally:
            await ws.close()

    try:
        samples = asyncio.run(go())
    finally:
        srv.shutdown()
    assert target_count(samples, "onprem") >= 10
    assert all(str(r["path"]).split("?")[0] == "/api/rooms/shift-probe/messages" for r in seen)
    assert all(r["author"] == "bravo-shift" for r in seen)
    assert len({r["path"] for r in seen}) == len(seen)  # a distinct query per write (Cloudflare steering)
    assert len({r["key"] for r in seen}) == len(seen)  # a new Idempotency-Key per write
    bad = [s for s in samples if not s.ok]
    assert len(bad) == 1 and bad[0].status == 500 and bad[0].env_id == "onprem"


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
