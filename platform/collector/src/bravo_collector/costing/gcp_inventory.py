"""GCP resources of the target project (read-only REST), priced from the Cloud Billing Catalog API.

Credentials: Application Default Credentials (workload identity federation on the platform, gcloud ADC on a
laptop). Scope cloud-platform, read calls only. The project is dedicated to this platform, so every resource
in it counts (component from the ``component`` label, else ``target``).
GCP list prices per month assume 730 hours (Google pricing pages); a monthly free tier is reported, not applied.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from ..redact import redact
from .model import GCP_MONTH_HOURS, Line, Segment, iso, now_utc
from .prices import GcpCatalog, PriceError

SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]
COMPUTE = "https://compute.googleapis.com/compute/v1/projects/{p}"
HYPERDISK_BALANCED_BASELINE = {"iops": 3000, "throughput": 140}  # included per disk (cloud.google.com/compute/disks-image-pricing)
DISK_PRICING_URL = "https://cloud.google.com/compute/disks-image-pricing"
GKE_PRICING_URL = "https://cloud.google.com/kubernetes-engine/pricing"


class GcpError(Exception):
    pass


def authed_session(quota_project: str | None):
    import google.auth
    from google.auth.transport.requests import AuthorizedSession

    creds, _ = google.auth.default(scopes=SCOPES, quota_project_id=quota_project)
    return AuthorizedSession(creds)


def _ts(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(UTC)


def _base(url: str) -> str:
    return url.rstrip("/").rsplit("/", 1)[-1]


class GcpInventory:
    def __init__(self, project: str, regions: list[str], session=None) -> None:
        self.project = project
        self.regions = regions
        self.session = session or authed_session(project)
        self.catalog = GcpCatalog(self.get, project)
        self.lines: list[Line] = []
        self.not_connected: list[dict[str, Any]] = []
        self.notes: list[str] = []
        self.now = now_utc()

    def get(self, url: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        r = self.session.get(url, params=params, timeout=30)
        if r.status_code != 200:
            try:
                msg = r.json().get("error", {}).get("message", "")
            except ValueError:
                msg = r.text[:200]
            raise GcpError(f"HTTP {r.status_code} {url.split('?')[0]}: {msg[:200]}")
        return r.json()

    def _fail(self, what: str, exc: Exception) -> None:
        self.not_connected.append({"provider": "gcp", "what": what, "reason": redact(str(exc))[:300]})

    def _line(self, price_q, **kw: Any) -> Line:
        price, err = price_q
        labels = kw.pop("labels", {}) or {}
        comp = labels.get("component") or "target"
        line = Line(provider="gcp", basis_code="estimated", unit_price=price.usd if price else None,
                    price_source=price.source if price else None, component=comp,
                    component_source="tag" if labels.get("component") else "inferred:target-project",
                    environment=labels.get("env") or "target-gcp", **kw)
        if price is None:
            line.status, line.reason = "not_connected", err
        return line

    def _price(self, service: str, region: str, must: list[str], exclude: list[str] = ()):
        try:
            return self.catalog.find(service, region=region, must=must, exclude=exclude), None
        except (PriceError, GcpError) as exc:
            return None, redact(str(exc))

    def collect(self) -> GcpInventory:
        for step in (self.gke, self.instances, self.disks, self.nat, self.load_balancers, self.artifact_registry,
                     self.kms):
            try:
                step()
            except Exception as exc:  # noqa: BLE001 - every failure becomes a reason, never a value
                self._fail(step.__name__, exc)
        self.not_connected.append({
            "provider": "gcp", "what": "GCP 청구(실제 금액)",
            "reason": "Cloud Billing 내보내기(BigQuery)가 없어 실제 청구액을 읽을 수 없음. GCP 줄은 모두 추정"})
        return self

    def _agg(self, kind: str) -> list[tuple[str, dict[str, Any]]]:
        out: list[tuple[str, dict[str, Any]]] = []
        url = f"{COMPUTE.format(p=self.project)}/aggregated/{kind}"
        token = None
        while True:
            body = self.get(url, {"pageToken": token} if token else None)
            for scope, v in (body.get("items") or {}).items():
                for item in v.get(kind, []) or []:
                    out.append((scope.split("/", 1)[-1], item))
            token = body.get("nextPageToken")
            if not token:
                return out

    def gke(self) -> None:
        body = self.get(f"https://container.googleapis.com/v1/projects/{self.project}/locations/-/clusters")
        for c in body.get("clusters", []):
            start = _ts(c.get("createTime"))
            zonal = c.get("location", "").count("-") == 2
            autopilot = bool((c.get("autopilot") or {}).get("enabled"))
            sku = "Autopilot Kubernetes Clusters" if autopilot else ("Zonal Kubernetes Clusters" if zonal else
                                                                     "Regional Kubernetes Clusters")
            line = self._line(self._price("gke", "global", [sku], ["Extended"]),
                              line_id=f"gcp:gke:{c['name']}", service="GKE", labels=c.get("resourceLabels"),
                              resource_type="gke_cluster", resource_id=c.get("selfLink", c["name"]),
                              resource=f"GKE {c['name']} ({'zonal' if zonal else 'regional'}, {c.get('currentMasterVersion')})",
                              unit="USD/hour", quantity_unit="hours", segments=[Segment(start, self.now, 1.0)],
                              running={"from": iso(start), "to": None, "state": c.get("status")},
                              note=("GKE 무료 등급: 결제 계정당 영역(zonal)·Autopilot 클러스터에 월 $74.40 크레딧이 적용될 수 "
                                    f"있음({GKE_PRICING_URL}). 적용 여부는 청구 데이터로만 확인되어 정가로 추정"))
            self.lines.append(line)

    def instances(self) -> None:
        for zone, i in self._agg("instances"):
            if i.get("status") not in ("RUNNING", "STAGING", "PROVISIONING"):
                continue
            mt = _base(i["machineType"])
            region = zone.rsplit("-", 1)[0]
            start = _ts(i.get("lastStartTimestamp")) or _ts(i.get("creationTimestamp"))
            spec = self.get(f"{COMPUTE.format(p=self.project)}/zones/{zone}/machineTypes/{mt}")
            vcpu, mem_gib = float(spec["guestCpus"]), float(spec["memoryMb"]) / 1024.0
            family = mt.split("-", 1)[0].upper()
            arm = " Arm" if family in ("C4A", "T2A", "N4A") else ""
            excl = ["Sole Tenancy", "Commitment", "Spot", "Preemptible", "Custom", "Local SSD", "Premium"]
            common = dict(service="Compute Engine", resource_type="gce_instance", resource_id=i.get("selfLink", i["name"]),
                          labels=i.get("labels"), running={"from": iso(start), "to": None, "state": i.get("status")})
            core = self._price("compute", region, [f"{family}{arm} Instance Core running in"], excl)
            ram = self._price("compute", region, [f"{family}{arm} Instance Ram running in"], excl)
            self.lines.append(self._line(core, line_id=f"gcp:gce:{i['name']}:cpu",
                                         resource=f"{i['name']} ({mt}, vCPU {vcpu:g})", unit="USD/vCPU-hour",
                                         quantity_unit="vCPU-hours", segments=[Segment(start, self.now, vcpu)],
                                         **common))
            self.lines.append(self._line(ram, line_id=f"gcp:gce:{i['name']}:ram",
                                         resource=f"{i['name']} ({mt}, RAM {mem_gib:g} GiB)", unit="USD/GiB-hour",
                                         quantity_unit="GiB-hours", segments=[Segment(start, self.now, mem_gib)],
                                         **common))

    def disks(self) -> None:
        names = {"hyperdisk-balanced": "Hyperdisk Balanced", "pd-balanced": "Balanced PD", "pd-ssd": "SSD backed PD",
                 "pd-standard": "Storage PD"}
        excl = ["High Availability", "Confidential", "Storage Pools", "Asynchronous", "Instant Snapshot", "Regional"]
        for zone, d in self._agg("disks"):
            dtype = _base(d["type"])
            region = zone.rsplit("-", 1)[0]
            start = _ts(d.get("creationTimestamp"))
            size = float(d.get("sizeGb", 0))
            label = names.get(dtype)
            common = dict(service="Compute Engine", resource_type="gce_disk", resource_id=d.get("selfLink", d["name"]),
                          labels=d.get("labels"), running={"from": iso(start), "to": None, "state": d.get("status")})
            if label is None:
                self._fail(f"disk {d['name']}", GcpError(f"disk type {dtype} not priced"))
                continue
            cap = self._price("compute", region, [f"{label} Capacity in"], excl)
            self.lines.append(self._line(cap, line_id=f"gcp:disk:{d['name']}", unit="USD/GiB-month",
                                         resource=f"disk {d['name']} ({dtype} {size:g} GB)", quantity_unit="GiB-month",
                                         segments=[Segment(start, self.now, size / GCP_MONTH_HOURS)], **common))
            if dtype == "hyperdisk-balanced":
                for dim, key, unit in (("iops", "provisionedIops", "IOPS"), ("throughput", "provisionedThroughput", "MiBps")):
                    extra = max(0.0, float(d.get(key) or 0) - HYPERDISK_BALANCED_BASELINE[dim])
                    if not extra:
                        continue
                    p = self._price("compute", region, [f"Hyperdisk Balanced {dim.capitalize()} in"], excl)
                    line = self._line(p, line_id=f"gcp:disk:{d['name']}:{dim}", unit=f"USD/{unit}-month",
                                      resource=f"disk {d['name']} ({unit} {extra:g} over baseline "
                                               f"{HYPERDISK_BALANCED_BASELINE[dim]})",
                                      quantity_unit=f"{unit}-month",
                                      segments=[Segment(start, self.now, extra / GCP_MONTH_HOURS)],
                                      note=f"Hyperdisk Balanced 기본 포함량은 디스크당 3,000 IOPS · 140 MiB/s({DISK_PRICING_URL})",
                                      **common)
                    self.lines.append(line)

    def nat(self) -> None:
        nat_ips = [(r, a) for r, a in self._agg("addresses") if a.get("purpose") == "NAT_AUTO" or "nat" in a.get("name", "")]
        vms_by_region: dict[str, int] = {}
        for zone, i in self._agg("instances"):
            if i.get("status") == "RUNNING":
                region = zone.rsplit("-", 1)[0]
                vms_by_region[region] = vms_by_region.get(region, 0) + 1
        for region, router in self._agg("routers"):
            for n in router.get("nats", []) or []:
                start = _ts(router.get("creationTimestamp"))
                vms = min(32, vms_by_region.get(region, 0))
                up = self._price("networking", region, ["Cloud Nat Gateway Uptime"], ["Private"])
                self.lines.append(self._line(
                    up, line_id=f"gcp:nat:{router['name']}:{n['name']}", service="Cloud NAT", labels={},
                    resource_type="cloud_nat", resource_id=f"{router.get('selfLink')}#{n['name']}",
                    resource=f"Cloud NAT {n['name']} (VM {vms}대)", unit="USD/VM-hour", quantity_unit="VM-hours",
                    segments=[Segment(start, self.now, float(vms))],
                    running={"from": iso(start), "to": None, "state": "exists"},
                    note="게이트웨이 시간 요금은 쓰는 VM 수(최대 32)에 비례. VM 수는 지금 RUNNING인 인스턴스 수. "
                         "처리 데이터량(GB)은 측정하지 않음"))
        for region, a in nat_ips:
            start = _ts(a.get("creationTimestamp"))
            ipp = self._price("networking", region, ["Cloud NAT IP Usage"])
            self.lines.append(self._line(
                ipp, line_id=f"gcp:natip:{a['name']}", service="Cloud NAT", labels=a.get("labels"),
                resource_type="nat_ip", resource_id=a.get("selfLink", a["name"]),
                resource=f"NAT 외부 IP {a.get('address')}", unit="USD/hour", quantity_unit="hours",
                segments=[Segment(start, self.now, 1.0)], running={"from": iso(start), "to": None, "state": a.get("status")}))

    def load_balancers(self) -> None:
        for region, fr in self._agg("forwardingRules"):
            if fr.get("loadBalancingScheme") != "INTERNAL_MANAGED":
                continue
            start = _ts(fr.get("creationTimestamp"))
            p = self._price("compute", region, ["Regional Internal Application Load Balancer Proxy Instance Charge"],
                            ["Cross Regional"])
            self.lines.append(self._line(
                p, line_id=f"gcp:ilb:{fr['name']}", service="Cloud Load Balancing", labels=fr.get("labels"),
                resource_type="internal_alb", resource_id=fr.get("selfLink", fr["name"]),
                resource=f"내부 Application LB {fr['name']} (프록시 인스턴스)", unit="USD/proxy-hour",
                quantity_unit="proxy-hours", segments=[Segment(start, self.now, 1.0)],
                running={"from": iso(start), "to": None, "state": "exists"},
                note="프록시 인스턴스 수는 공개 API로 보이지 않아 1개로 추정(트래픽이 늘면 더 청구됨). 처리 데이터량은 측정하지 않음"))

    def artifact_registry(self) -> None:
        for region in self.regions:
            body = self.get(f"https://artifactregistry.googleapis.com/v1/projects/{self.project}/locations/{region}/repositories")
            for repo in body.get("repositories", []):
                start = _ts(repo.get("createTime"))
                gib = float(repo.get("sizeBytes", 0) or 0) / 1024 ** 3
                p = self._price("artifact_registry", region, ["Artifact Registry Storage"])
                self.lines.append(self._line(
                    p, line_id=f"gcp:ar:{_base(repo['name'])}", service="Artifact Registry", labels=repo.get("labels"),
                    resource_type="artifact_repository", resource_id=repo["name"],
                    resource=f"Artifact Registry {_base(repo['name'])} ({gib:.3f} GiB)", unit="USD/GiB-month",
                    quantity_unit="GiB-month", segments=[Segment(start, self.now, gib / GCP_MONTH_HOURS)],
                    running={"from": iso(start), "to": None, "state": "exists"},
                    note="지금 크기를 생성 시각부터 적용(상한 쪽 추정). 월 0.5 GiB 무료 등급은 적용하지 않음"))

    def kms(self) -> None:
        for region in self.regions:
            rings = self.get(f"https://cloudkms.googleapis.com/v1/projects/{self.project}/locations/{region}/keyRings")
            for ring in rings.get("keyRings", []):
                keys = self.get(f"https://cloudkms.googleapis.com/v1/{ring['name']}/cryptoKeys")
                for k in keys.get("cryptoKeys", []):
                    vers = self.get(f"https://cloudkms.googleapis.com/v1/{k['name']}/cryptoKeyVersions")
                    for v in vers.get("cryptoKeyVersions", []):
                        if v.get("state") not in ("ENABLED", "DISABLED"):
                            continue
                        level = (v.get("protectionLevel") or "SOFTWARE").lower()
                        kind = "symmetric" if k.get("purpose") == "ENCRYPT_DECRYPT" else "asymmetric"
                        p = self._price("kms", region, [f"Active {level} {kind} key versions"], ["Autokey"])
                        start = _ts(v.get("createTime"))
                        self.lines.append(self._line(
                            p, line_id=f"gcp:kms:{_base(k['name'])}:{_base(v['name'])}", service="Cloud KMS",
                            labels=k.get("labels"), resource_type="kms_key_version", resource_id=v["name"],
                            resource=f"Cloud KMS {_base(ring['name'])}/{_base(k['name'])} v{_base(v['name'])}",
                            unit="USD/key-version-month", quantity_unit="key-version-month",
                            segments=[Segment(start, self.now, 1.0 / GCP_MONTH_HOURS)],
                            running={"from": iso(start), "to": None, "state": v.get("state")}))
