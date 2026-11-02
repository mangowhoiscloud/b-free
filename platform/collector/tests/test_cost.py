"""Cost receipt (T1, NFR-8): allocation arithmetic, caching of billed calls, read-only cost code, live price reads.

The arithmetic tests feed line items to the pure ``build_receipt``. Live tests read the real AWS account (Pricing,
Describe) and skip without credentials; they never call Cost Explorer (USD 0.01 per request).
"""

from __future__ import annotations

import json
import re
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import boto3
import pytest
from fastapi.testclient import TestClient

from bravo_collector.app import create_app
from bravo_collector.costing import actuals as actuals_mod
from bravo_collector.costing.aws_inventory import classify, env_of, monthly_segments
from bravo_collector.costing.llm import usage_lines
from bravo_collector.costing.model import CACHE, Line, Segment, TTLCache, overlap_hours
from bravo_collector.costing.receipt import CostRequestError, build_receipt

COSTING = Path(__file__).resolve().parents[1] / "src" / "bravo_collector" / "costing"
T0 = datetime(2026, 11, 2, 0, 0, tzinfo=UTC)


def h(n: float) -> datetime:
    return T0 + timedelta(hours=n)


def line(lid: str, comp: str, rate_usd_h: float, a: datetime, b: datetime, **kw) -> Line:
    base = dict(line_id=lid, provider="aws", service="EC2", resource_type="ec2_instance", resource_id=lid,
                resource=lid, environment=f"{comp}-env", component=comp, component_source="tag",
                basis_code="estimated", unit_price=rate_usd_h, unit="USD/hour", quantity_unit="hours",
                price_source={"name": "test input"}, segments=[Segment(a, b, 1.0)])
    base.update(kw)
    return Line(**base)


@pytest.fixture(autouse=True)
def _clear_cache():
    CACHE.clear()
    yield
    CACHE.clear()


# --- read-only by construction ----------------------------------------------------------------------------

def test_cost_code_makes_no_write_calls():
    for path in COSTING.glob("*.py"):
        src = path.read_text(encoding="utf-8")
        for verb in ("put_", "delete_", "create_", "update_", "tag_resource", "untag_", "attach_", "detach_",
                     "modify_", "terminate_", "start_", "stop_"):
            assert not re.search(rf"\.{verb}\w*\(", src), f"write-like call {verb} in {path.name}"
        assert "get_secret_value" not in src
        assert "requests.post" not in src and ".post(" not in src


# --- time arithmetic -------------------------------------------------------------------------------------

def test_overlap_and_month_split():
    assert overlap_hours(h(0), h(10), h(5), h(20)) == 5
    assert overlap_hours(h(0), h(1), h(2), h(3)) == 0
    a = datetime(2026, 10, 31, 23, tzinfo=UTC)
    segs = monthly_segments(a, a + timedelta(hours=2), 1.0)  # 1 unit per calendar month
    assert len(segs) == 2
    q = sum(s.quantity(a, a + timedelta(hours=2)) for s in segs)
    assert q == pytest.approx(1 / (31 * 24) + 1 / (30 * 24))


def test_classify_counts_only_this_project():
    assert classify({"component": "source"}, "anything") == ("source", "tag")
    assert classify({}, "bravo-platform-node") == ("platform", "inferred:name")
    assert classify({"eks:cluster-name": "bravo-source"}, None) == ("source", "inferred:eks-cluster")
    assert classify({}, "bravo-tfstate-1") == (None, "inferred:name (no component)")
    assert classify({}, "railshot-ci-poc") is None
    assert classify({"component": "other"}, "geode-verify") is None
    assert env_of({"env": "platform-aws"}, "platform") == "platform"
    assert env_of({}, "source") == "source-aws"


# --- receipt arithmetic ----------------------------------------------------------------------------------

def test_phases_and_migration_extra():
    lines = [
        line("src-node", "source", 1.0, h(0), h(30)),   # existed before the plan: the customer's own cost
        line("dst-node", "target", 2.0, h(12), h(30)),  # created for the migration
        line("plat-node", "platform", 0.5, h(10), h(30)),
    ]
    plan = {"plan_id": "p1", "created_at": h(12), "done_at": h(24)}
    r = build_receipt(lines, start=h(0), end=h(30), plan=plan, generated_at=h(30))
    s = r["summary"]
    assert s["before"]["hours"] == 12 and s["during"]["hours"] == 12 and s["after"]["hours"] == 6
    assert s["before"]["total"] == pytest.approx(12 * 1.0 + 2 * 0.5)
    assert s["during"]["total"] == pytest.approx(12 * (1.0 + 2.0 + 0.5))
    assert s["during"]["daily"] == pytest.approx(3.5 * 24)
    m = r["migration_extra"]
    assert m["value"] == pytest.approx(12 * 2.5)          # during total minus the source baseline
    assert m["inputs"]["during_source"] == pytest.approx(12.0)
    assert r["totals"]["migration_extra"] == m["value"]
    assert r["totals"]["total"] == pytest.approx(30 * 1.0 + 18 * 2.0 + 20 * 0.5)
    assert r["totals"]["by_component"]["target"] == pytest.approx(36.0)
    row = next(x for x in r["lines"] if x["line_id"] == "dst-node")
    assert row["phases"]["before"]["subtotal"] == 0 and row["phases"]["during"]["subtotal"] == pytest.approx(24.0)
    assert row["basis"] == "추정"


def test_plan_not_done_runs_to_now_and_daily_buckets_are_kst():
    lines = [line("a", "source", 1.0, h(0), h(20))]
    r = build_receipt(lines, start=h(0), end=h(20), plan={"plan_id": "p", "created_at": h(5)}, generated_at=h(20))
    assert r["phases"]["during"]["end"] == r["period"]["end"]
    assert r["summary"]["after"]["hours"] == 0
    # 00:00Z = 09:00 KST: the first KST day ends at 15:00Z.
    assert [d["date"] for d in r["daily"]] == ["2026-11-02", "2026-11-03"]
    assert r["daily"][0]["total"] == pytest.approx(15.0) and r["daily"][1]["total"] == pytest.approx(5.0)
    assert r["day_timezone"] == "+09:00"


def test_unpriced_lines_make_the_total_a_lower_bound():
    lines = [line("priced", "source", 1.0, h(0), h(10)),
             line("unpriced", "target", None, h(0), h(10), status="not_connected", reason="no price")]
    r = build_receipt(lines, start=h(0), end=h(10), plan={"plan_id": "p", "created_at": h(2)})
    assert r["totals"]["total"] == pytest.approx(10.0)
    assert r["totals"]["unpriced_lines"] == 1
    assert r["migration_extra"]["lower_bound"] is True
    assert any(n["reason"] == "no price" for n in r["not_connected"])
    row = next(x for x in r["lines"] if x["line_id"] == "unpriced")
    assert row["subtotal"] is None and row["quantity"] == 10


def test_api_equivalent_is_never_in_the_total_and_runs_follow_their_plan():
    run = Line(line_id="llm:run:t1", provider="llm", service="claude", resource_type="agent_run", resource_id="t1",
               resource="run", environment="llm", component="llm", component_source="inferred:llm",
               basis_code="api_equivalent", unit_price=None, unit=None, quantity_unit="tokens", price_source=None,
               running={"_at": h(3)}, in_total=False, fixed_quantity=1000.0, fixed_subtotal=0.25, plan_id="p1")
    other = Line(**{**run.__dict__, "line_id": "llm:run:t2", "plan_id": "p2", "segments": []})
    lines = [line("a", "source", 1.0, h(0), h(10)), run, other]
    r = build_receipt(lines, start=h(0), end=h(10), plan={"plan_id": "p1", "created_at": h(1)})
    ids = {x["line_id"] for x in r["lines"]}
    assert "llm:run:t1" in ids and "llm:run:t2" not in ids
    assert r["totals"]["total"] == pytest.approx(10.0)
    assert r["totals"]["api_equivalent_not_billed"] == pytest.approx(0.25)
    assert next(x for x in r["lines"] if x["line_id"] == "llm:run:t1")["basis"] == "API 가격 환산(청구 아님)"


def test_billed_lines_replace_aws_estimates_in_the_total():
    est = line("aws-est", "source", 1.0, h(0), h(24))
    billed = Line(line_id="aws:billed:source", provider="aws", service="Cost Explorer",
                  resource_type="billed_component", resource_id="component=source",
                  resource="AWS 청구 component=source", environment="source-aws",
                  component="source", component_source="tag", basis_code="billed", unit_price=1.0, unit="USD/USD",
                  quantity_unit="USD", price_source=None, segments=[Segment(h(0), h(24), 30.0 / 24)])
    r = build_receipt([est, billed], start=h(0), end=h(24))
    assert r["totals"]["total"] == pytest.approx(30.0)
    assert r["totals"]["billed"] == pytest.approx(30.0)
    assert next(x for x in r["lines"] if x["line_id"] == "aws-est")["in_total"] is False


def test_bad_window_is_refused():
    with pytest.raises(CostRequestError):
        build_receipt([], start=h(5), end=h(5))


# --- LLM usage from recorded runs ------------------------------------------------------------------------

def test_usage_lines_read_recorded_runs(tmp_path):
    run = tmp_path / "t-abc"
    run.mkdir()
    (run / "result.json").write_text(json.dumps({
        "started_at": "2026-11-02T12:47:02Z", "finished_at": "2026-11-02T12:49:14Z",
        "config": {"environment": {"kwargs": {"plan_id": "p-1"}}},
        "agent_result": {"metadata": {"worker": {
            "model": "claude-opus-5-5", "provider": "claude_subscription", "num_turns": 10,
            "tokens": {"input_tokens": 14, "output_tokens": 2386, "cache_creation_input_tokens": 7328,
                       "cache_read_input_tokens": 32426, "api_price_equivalent_usd": 0.1128852,
                       "cost_basis": "subscription (flat plan; USD not billed)"}}}}}), encoding="utf-8")
    (tmp_path / "t-noagent").mkdir()
    (tmp_path / "t-noagent" / "result.json").write_text(json.dumps({"agent_result": None}), encoding="utf-8")
    lines, src = usage_lines(str(tmp_path))
    assert src == {"status": "ok", "runs": 1, "unreadable": 0}
    (x,) = lines
    assert x.fixed_quantity == 14 + 2386 + 7328 + 32426 and x.fixed_subtotal == pytest.approx(0.1128852)
    assert x.plan_id == "p-1" and x.in_total is False
    assert usage_lines(None)[1]["status"] == "not_connected"


def test_real_recorded_runs_if_present():
    root = Path(__file__).resolve().parents[2] / "rehearsal" / "runs"
    if not root.is_dir():
        pytest.skip("no local rehearsal runs")
    lines, src = usage_lines(str(root))
    assert src["status"] == "ok"
    for x in lines:
        assert x.fixed_quantity and x.fixed_quantity > 0


# --- caching: Cost Explorer is billed per request ---------------------------------------------------------

def test_ttl_cache_fills_once_under_concurrency():
    cache = TTLCache()
    calls = []
    barrier = threading.Barrier(8)

    def fill():
        calls.append(1)
        return len(calls)

    def worker():
        barrier.wait()
        cache.get_or_fill("k", 3600, fill)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(calls) == 1
    assert cache.get_or_fill("k", 3600, fill)[0] == 1
    assert cache.get_or_fill("k", 0, fill)[0] == 2  # expired -> refilled


def test_stale_value_is_served_while_one_background_refill_runs():
    cache = TTLCache()
    release = threading.Event()
    calls = []

    def slow_fill():
        calls.append(1)
        if len(calls) > 1:
            release.wait(5)
        return len(calls)

    assert cache.get_or_fill("k", 3600, slow_fill)[0] == 1
    # Expired: both calls return the old value at once; only one refill starts.
    assert cache.get_or_fill("k", 0, slow_fill, stale_ok=True)[0] == 1
    assert cache.get_or_fill("k", 0, slow_fill, stale_ok=True)[0] == 1
    release.set()
    for _ in range(50):
        if cache.get_or_fill("k", 3600, slow_fill)[0] == 2:
            break
        threading.Event().wait(0.05)
    assert len(calls) == 2 and cache.get_or_fill("k", 3600, slow_fill)[0] == 2


class _RecordingCe:
    """Counts Cost Explorer requests; answers with the shapes the real API returns for an inactive tag."""

    def __init__(self):
        self.calls: list[str] = []

    def list_cost_allocation_tags(self, **kw):
        self.calls.append("ListCostAllocationTags")
        return {"CostAllocationTags": []}

    def get_cost_and_usage(self, **kw):
        self.calls.append("GetCostAndUsage:" + kw["GroupBy"][0]["Key"])
        return {"ResultsByTime": [{"TimePeriod": {"Start": kw["TimePeriod"]["Start"]}, "Estimated": True,
                                   "Groups": [{"Keys": ["Amazon Elastic Compute Cloud - Compute"],
                                               "Metrics": {"UnblendedCost": {"Amount": "1.5", "Unit": "USD"}}}]}]}


class _Session:
    def __init__(self, ce):
        self.ce = ce

    def client(self, name, **kw):
        assert name == "ce"
        return self.ce


def test_cost_explorer_is_called_once_per_hour():
    ce = _RecordingCe()
    first = actuals_mod.cached_actuals(session=_Session(ce))
    second = actuals_mod.cached_actuals(session=_Session(ce))
    assert ce.calls == ["ListCostAllocationTags", "GetCostAndUsage:SERVICE"]  # tag inactive: no TAG query
    assert first["requests_this_response"] == 2 and first["from_cache"] is False
    assert second["requests_this_response"] == 0 and second["from_cache"] is True
    assert first["tag"]["label"] == "태그 활성화 대기" and first["by_component_daily"] is None
    assert first["by_service"]["total"] == 1.5 and first["ttl_s"] == 3600


# --- HTTP ------------------------------------------------------------------------------------------------

def test_http_cost_without_providers_reports_not_connected(tmp_path, monkeypatch):
    cfg = tmp_path / "cost.yaml"
    cfg.write_text("llm:\n  subscriptions:\n    - {id: s1, name: test sub, provider: x, monthly_usd: null}\n",
                   encoding="utf-8")
    monkeypatch.setenv("BRAVO_COST_CONFIG", str(cfg))
    monkeypatch.delenv("BRAVO_LLM_USAGE_DIR", raising=False)
    reg = tmp_path / "environments.yaml"
    reg.write_text("environments: {}\n", encoding="utf-8")
    monkeypatch.setenv("BRAVO_ENVIRONMENTS_FILE", str(reg))
    with TestClient(create_app()) as c:
        r = c.get("/v1/cost")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["schema_version"] == "cost/v1"
        whats = {n["what"] for n in body["not_connected"]}
        assert {"AWS 인벤토리", "GCP 인벤토리", "에이전트 실행별 토큰 사용량"} <= whats
        assert body["totals"]["total"] == 0 and body["totals"]["unpriced_lines"] == 1
        assert c.get("/v1/cost", params={"start": "not-a-date"}).status_code == 422
        assert c.get("/v1/cost", params={"plan_created_at": "2026-11-02T10:00:00Z",
                                         "plan_done_at": "2026-11-02T09:00:00Z"}).status_code == 422
        r = c.get("/v1/cost", params={"plan_id": "p", "plan_created_at": "2026-11-02T10:00:00Z"})
        assert r.status_code == 200 and r.json()["summary"]["before"]["hours"] == 24


# --- live reads (no Cost Explorer) -----------------------------------------------------------------------

@pytest.fixture(scope="module")
def aws_session():
    s = boto3.session.Session(region_name="ap-northeast-2")
    if s.get_credentials() is None:
        pytest.skip("no AWS credentials in the default chain")
    try:
        s.client("sts").get_caller_identity()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"sts:GetCallerIdentity failed: {type(exc).__name__}")
    return s


@pytest.mark.aws
def test_live_pricing_api(aws_session):
    from bravo_collector.costing.prices import AwsPrices

    p = AwsPrices(aws_session).lookup("AmazonEC2", {
        "regionCode": "ap-northeast-2", "instanceType": "t4g.large", "operatingSystem": "Linux", "tenancy": "Shared",
        "preInstalledSw": "NA", "capacitystatus": "Used", "licenseModel": "No License required"})
    assert p.usd > 0 and p.unit == "Hrs"
    assert p.source["name"] == "AWS Pricing API" and p.source["ref"].endswith("BoxUsage:t4g.large")


@pytest.mark.aws
def test_live_inventory_finds_only_this_project(aws_session):
    from bravo_collector.costing.aws_inventory import AwsInventory

    inv = AwsInventory(["ap-northeast-2"], session=aws_session).collect()
    assert inv.lines, inv.not_connected
    for x in inv.lines:
        assert x.component_source == "tag" or x.component_source.startswith("inferred:")
        assert "railshot" not in x.resource and "geode" not in x.resource
        assert x.basis_code == "estimated"
        assert x.status == "ok" or x.reason
