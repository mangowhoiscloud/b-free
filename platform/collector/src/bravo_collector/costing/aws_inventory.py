"""AWS resources of this project, read-only, priced from the AWS Pricing API.

Which resources are ours: the NFR-8 ``component`` tag (source / platform / target), else a ``bravo-`` name,
else attachment to one of our instances. The account also holds other projects; they are never counted.
Running time comes from each resource's own timestamps (create / launch / termination). Resources deleted
long enough ago that Describe no longer returns them are not estimated; Cost Explorer covers them (by tag).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from ..redact import redact
from .model import Line, Segment, aws_month_hours, iso, now_utc
from .prices import AwsPrices, PriceError, ut

COMPONENTS = ("source", "platform", "target")
ENV_BY_COMPONENT = {"source": "source-aws", "platform": "platform", "target": "target-aws"}
CFG = Config(retries={"max_attempts": 4, "mode": "standard"}, read_timeout=20, connect_timeout=5)
TERMINATED_AT = re.compile(r"\((\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) GMT\)")
GB = 1024**3


def tag_map(tags: Iterable[dict[str, str]] | None) -> dict[str, str]:
    return {t.get("Key", ""): t.get("Value", "") for t in (tags or [])}


def classify(tags: dict[str, str], name: str | None) -> tuple[str | None, str] | None:
    """(component, how we know) for our resources, None for other projects' resources."""
    comp = tags.get("component")
    if comp in COMPONENTS:
        return comp, "tag"
    n = (name or tags.get("Name") or "").lower()
    for c in COMPONENTS:
        if n.startswith(f"bravo-{c}"):
            return c, "inferred:name"
    eks = tags.get("eks:cluster-name", "")
    for c in COMPONENTS:
        if eks.startswith(f"bravo-{c}"):
            return c, "inferred:eks-cluster"
    if n.startswith("bravo-") or eks.startswith("bravo-"):
        return None, "inferred:name (no component)"
    return None


def env_of(tags: dict[str, str], component: str | None) -> str | None:
    """CLOUD-E2E-CONTRACT 2 environment ids; the platform's Terraform env tag says platform-aws."""
    env = tags.get("env") or ENV_BY_COMPONENT.get(component or "")
    return "platform" if env and env.startswith("platform") else env


def monthly_segments(start: datetime, end: datetime, per_month: float) -> list[Segment]:
    """``per_month`` units for a full calendar month, split at month boundaries (AWS proration)."""
    out: list[Segment] = []
    cur = start
    while cur < end:
        nxt = (cur.replace(day=1, hour=0, minute=0, second=0, microsecond=0) + timedelta(days=32)).replace(day=1)
        stop = min(end, nxt)
        out.append(Segment(cur, stop, per_month / aws_month_hours(cur)))
        cur = stop
    return out


class AwsInventory:
    def __init__(self, regions: list[str], session: boto3.Session | None = None, prices: AwsPrices | None = None):
        self.regions = regions
        self.session = session or boto3.Session()
        self.prices = prices or AwsPrices(self.session)
        self.lines: list[Line] = []
        self.not_connected: list[dict[str, Any]] = []
        self.notes: list[str] = []
        self.now = now_utc()
        self._instance_component: dict[str, tuple[str | None, str, str | None]] = {}

    # -- plumbing ------------------------------------------------------------------------------------
    def client(self, name: str, region: str):
        return self.session.client(name, region_name=region, config=CFG)

    def _fail(self, what: str, exc: Exception) -> None:
        code = exc.response.get("Error", {}).get("Code") if isinstance(exc, ClientError) else type(exc).__name__
        self.not_connected.append({"provider": "aws", "what": what, "reason": redact(f"{code}: {exc}")[:300]})

    def _price(self, service: str, filters: dict[str, str], want_unit: str | None = None):
        try:
            return self.prices.lookup(service, filters, want_unit=want_unit), None
        except PriceError as exc:
            return None, str(exc)
        except (ClientError, BotoCoreError) as exc:
            code = exc.response.get("Error", {}).get("Code") if isinstance(exc, ClientError) else type(exc).__name__
            return None, redact(f"AWS Pricing API {code}")

    def _line(self, *, price_q: tuple, **kw: Any) -> Line:
        price, err = price_q
        line = Line(
            provider="aws",
            basis_code="estimated",
            unit_price=price.usd if price else None,
            price_source=price.source if price else None,
            **kw,
        )
        if price is None:
            line.status, line.reason = "not_connected", err
        return line

    def collect(self) -> AwsInventory:
        for region in self.regions:
            for step in (
                self.ec2_instances,
                self.ebs_volumes,
                self.eks_clusters,
                self.nat_gateways,
                self.elb,
                self.kms_keys,
                self.ecr,
                self.secrets,
                self.rds,
                self.cloudtrail,
            ):
                try:
                    step(region)
                except (ClientError, BotoCoreError) as exc:
                    self._fail(f"{step.__name__} ({region})", exc)
        try:
            self.s3_buckets()
        except (ClientError, BotoCoreError) as exc:
            self._fail("s3_buckets", exc)
        return self

    # -- compute -------------------------------------------------------------------------------------
    def ec2_instances(self, region: str) -> None:
        ec2 = self.client("ec2", region)
        for page in ec2.get_paginator("describe_instances").paginate():
            for res in page["Reservations"]:
                for i in res["Instances"]:
                    tags = tag_map(i.get("Tags"))
                    cls = classify(tags, tags.get("Name"))
                    if cls is None:
                        continue
                    comp, how = cls
                    iid = i["InstanceId"]
                    self._instance_component[iid] = (comp, how, env_of(tags, comp))
                    state = i["State"]["Name"]
                    start = i["LaunchTime"].astimezone(UTC)
                    if state in ("running", "pending"):
                        end = self.now
                    else:
                        m = TERMINATED_AT.search(i.get("StateTransitionReason") or "")
                        if not m:
                            continue
                        end = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
                    itype = i["InstanceType"]
                    os_ = "Linux" if "Linux" in (i.get("PlatformDetails") or "Linux/UNIX") else i.get("PlatformDetails")
                    price = self._price(
                        "AmazonEC2",
                        {
                            "regionCode": region,
                            "instanceType": itype,
                            "operatingSystem": os_,
                            "tenancy": "Shared",
                            "preInstalledSw": "NA",
                            "capacitystatus": "Used",
                            "licenseModel": "No License required",
                        },
                    )
                    self.lines.append(
                        self._line(
                            price_q=price,
                            line_id=f"aws:ec2:{iid}",
                            service="EC2",
                            resource_type="ec2_instance",
                            resource_id=iid,
                            resource=f"{tags.get('Name', iid)} ({itype})",
                            environment=env_of(tags, comp),
                            component=comp,
                            component_source=how,
                            unit="USD/hour",
                            quantity_unit="hours",
                            segments=[Segment(start, end, 1.0)] if end > start else [],
                            running={
                                "from": iso(start),
                                "to": iso(end) if state not in ("running", "pending") else None,
                                "state": state,
                            },
                            note="LaunchTime부터 계산(정지 후 재시작 전 실행 시간은 빠짐)"
                            if state == "running"
                            else None,
                        )
                    )

    def ebs_volumes(self, region: str) -> None:
        ec2 = self.client("ec2", region)
        for page in ec2.get_paginator("describe_volumes").paginate():
            for v in page["Volumes"]:
                tags = tag_map(v.get("Tags"))
                cls = classify(tags, tags.get("Name"))
                attached = [a.get("InstanceId") for a in v.get("Attachments", []) if a.get("InstanceId")]
                env = None
                if cls is None or cls[0] is None:
                    owner = next((self._instance_component[a] for a in attached if a in self._instance_component), None)
                    if owner is not None:
                        cls, env = (owner[0], "inferred:attached-instance"), owner[2]
                if cls is None:
                    continue
                comp, how = cls
                vid, vtype, size = v["VolumeId"], v["VolumeType"], v["Size"]
                start = v["CreateTime"].astimezone(UTC)
                pvc = tags.get("kubernetes.io/created-for/pvc/name")
                name = tags.get("Name") or (
                    f"pvc {tags.get('kubernetes.io/created-for/pvc/namespace', '?')}/{pvc}" if pvc else vid
                )
                common = dict(
                    service="EBS",
                    resource_type="ebs_volume",
                    resource_id=vid,
                    environment=env or env_of(tags, comp),
                    component=comp,
                    component_source=how,
                    running={"from": iso(start), "to": None, "state": v["State"]},
                )
                price = self._price(
                    "AmazonEC2", {"regionCode": region, "productFamily": "Storage", "volumeApiName": vtype}
                )
                self.lines.append(
                    self._line(
                        price_q=price,
                        line_id=f"aws:ebs:{vid}",
                        resource=f"{name} ({vtype} {size} GB)",
                        unit="USD/GB-month",
                        quantity_unit="GB-month",
                        segments=monthly_segments(start, self.now, float(size)),
                        **common,
                    )
                )
                if vtype == "gp3":
                    extra_iops = max(0, (v.get("Iops") or 3000) - 3000)
                    extra_tp = max(0, (v.get("Throughput") or 125) - 125)
                    if extra_iops:
                        p = self._price(
                            "AmazonEC2",
                            {"regionCode": region, "productFamily": "System Operation", "volumeApiName": "gp3"},
                        )
                        self.lines.append(
                            self._line(
                                price_q=p,
                                line_id=f"aws:ebs:{vid}:iops",
                                resource=f"{name} (gp3 IOPS {extra_iops} over 3000)",
                                unit="USD/IOPS-month",
                                quantity_unit="IOPS-month",
                                segments=monthly_segments(start, self.now, float(extra_iops)),
                                **common,
                            )
                        )
                    if extra_tp:
                        p = self._price(
                            "AmazonEC2",
                            {"regionCode": region, "productFamily": "Provisioned Throughput", "volumeApiName": "gp3"},
                        )
                        self.lines.append(
                            self._line(
                                price_q=p,
                                line_id=f"aws:ebs:{vid}:tp",
                                resource=f"{name} (gp3 {extra_tp} MiB/s over 125)",
                                unit="USD/MiBps-month",
                                quantity_unit="MiBps-month",
                                segments=monthly_segments(start, self.now, float(extra_tp)),
                                **common,
                            )
                        )

    def eks_clusters(self, region: str) -> None:
        eks = self.client("eks", region)
        for page in eks.get_paginator("list_clusters").paginate():
            for name in page["clusters"]:
                c = eks.describe_cluster(name=name)["cluster"]
                tags = c.get("tags") or {}
                cls = classify(tags, name)
                if cls is None:
                    continue
                comp, how = cls
                start = c["createdAt"].astimezone(UTC)
                price = self._price(
                    "AmazonEKS", {"regionCode": region, "usagetype": ut(region, "AmazonEKS-Hours:perCluster")}
                )
                self.lines.append(
                    self._line(
                        price_q=price,
                        line_id=f"aws:eks:{name}",
                        service="EKS",
                        resource_type="eks_cluster",
                        resource_id=c.get("arn", name),
                        resource=f"EKS {name} (k8s {c.get('version')})",
                        environment=env_of(tags, comp),
                        component=comp,
                        component_source=how,
                        unit="USD/hour",
                        quantity_unit="hours",
                        segments=[Segment(start, self.now, 1.0)],
                        running={"from": iso(start), "to": None, "state": c.get("status")},
                        note="표준 지원 판 기준 클러스터 요금. 노드는 EC2 줄에 따로 있음",
                    )
                )

    # -- network -------------------------------------------------------------------------------------
    def nat_gateways(self, region: str) -> None:
        ec2 = self.client("ec2", region)
        cw = self.client("cloudwatch", region)
        for page in ec2.get_paginator("describe_nat_gateways").paginate():
            for n in page["NatGateways"]:
                tags = tag_map(n.get("Tags"))
                cls = classify(tags, tags.get("Name"))
                if cls is None:
                    continue
                comp, how = cls
                nid = n["NatGatewayId"]
                start = n["CreateTime"].astimezone(UTC)
                end = n["DeleteTime"].astimezone(UTC) if n.get("DeleteTime") else self.now
                regional = str(n.get("AvailabilityMode", "")).lower() == "regional" or "regional" in (
                    tags.get("Name", "").lower()
                )
                kind = "RegionalNatGateway" if regional else "NatGateway"
                common = dict(
                    service="VPC",
                    environment=env_of(tags, comp),
                    component=comp,
                    component_source=how,
                    running={"from": iso(start), "to": iso(end) if n.get("DeleteTime") else None, "state": n["State"]},
                )
                self.lines.append(
                    self._line(
                        price_q=self._price(
                            "AmazonEC2",
                            {"regionCode": region, "usagetype": ut(region, f"{kind}-Hours"), "operation": kind},
                        ),
                        line_id=f"aws:nat:{nid}",
                        resource_type="nat_gateway",
                        resource_id=nid,
                        resource=f"{tags.get('Name', nid)} ({'regional' if regional else 'zonal'} NAT, hours)",
                        unit="USD/hour",
                        quantity_unit="hours",
                        segments=[Segment(start, end, 1.0)],
                        note=(
                            "Regional NAT은 활성 AZ마다 시간 과금될 수 있음. 여기서는 게이트웨이 1개 시간으로 추정"
                            if regional
                            else None
                        ),
                        **common,
                    )
                )
                segs, why = self._nat_bytes(cw, nid, max(start, self.now - timedelta(days=14)), end)
                data = self._line(
                    price_q=self._price(
                        "AmazonEC2", {"regionCode": region, "usagetype": ut(region, f"{kind}-Bytes"), "operation": kind}
                    ),
                    line_id=f"aws:nat:{nid}:data",
                    resource_type="nat_gateway_data",
                    resource_id=nid,
                    resource=f"{tags.get('Name', nid)} (처리 데이터)",
                    unit="USD/GB",
                    quantity_unit="GB",
                    segments=segs,
                    note="CloudWatch BytesInFromSource + BytesInFromDestination(시간별 합)",
                    **common,
                )
                if why:
                    data.status, data.reason = "not_connected", why
                self.lines.append(data)
                ipv4 = self._price(
                    "AmazonVPC", {"regionCode": region, "usagetype": ut(region, "PublicIPv4:InUseAddress")}
                )
                for a in n.get("NatGatewayAddresses", []):
                    if not a.get("PublicIp"):
                        continue
                    self.lines.append(
                        self._line(
                            price_q=ipv4,
                            line_id=f"aws:ipv4:{a.get('AllocationId') or a['PublicIp']}",
                            resource_type="public_ipv4",
                            resource_id=a.get("AllocationId") or a["PublicIp"],
                            resource=f"공인 IPv4 {a['PublicIp']} ({tags.get('Name', nid)})",
                            unit="USD/hour",
                            quantity_unit="hours",
                            segments=[Segment(start, end, 1.0)],
                            note="NAT 생성 시각부터 사용 중으로 계산",
                            **common,
                        )
                    )

    def _nat_bytes(self, cw, nid: str, start: datetime, end: datetime) -> tuple[list[Segment], str | None]:
        """Hourly processed bytes. Regional NAT gateways publish per AZ (NatGatewayId + AvailabilityZone)."""
        if end <= start:
            return [], None
        per_hour: dict[datetime, float] = {}
        seen = False
        for metric in ("BytesInFromSource", "BytesInFromDestination"):
            listed = cw.list_metrics(
                Namespace="AWS/NATGateway", MetricName=metric, Dimensions=[{"Name": "NatGatewayId", "Value": nid}]
            ).get("Metrics", [])
            for m in listed:
                r = cw.get_metric_statistics(
                    Namespace="AWS/NATGateway",
                    MetricName=metric,
                    Dimensions=m["Dimensions"],
                    StartTime=start,
                    EndTime=end,
                    Period=3600,
                    Statistics=["Sum"],
                )
                for dp in r.get("Datapoints", []):
                    seen = True
                    t = dp["Timestamp"].astimezone(UTC)
                    per_hour[t] = per_hour.get(t, 0.0) + float(dp["Sum"])
        if not seen:
            return [], "CloudWatch AWS/NATGateway에 이 게이트웨이의 처리량 데이터포인트가 없음"
        return [Segment(t, t + timedelta(hours=1), b / GB) for t, b in sorted(per_hour.items())], None

    def elb(self, region: str) -> None:
        lb = self.client("elbv2", region)
        found = 0
        for page in lb.get_paginator("describe_load_balancers").paginate():
            arns = [x["LoadBalancerArn"] for x in page["LoadBalancers"]]
            tags_by = {}
            for i in range(0, len(arns), 20):
                for d in lb.describe_tags(ResourceArns=arns[i : i + 20])["TagDescriptions"]:
                    tags_by[d["ResourceArn"]] = tag_map(d.get("Tags"))
            for x in page["LoadBalancers"]:
                tags = tags_by.get(x["LoadBalancerArn"], {})
                cls = classify(tags, x["LoadBalancerName"])
                if cls is None:
                    continue
                found += 1
                comp, how = cls
                start = x["CreatedTime"].astimezone(UTC)
                kind = {"application": "LoadBalancerUsage", "network": "LoadBalancerUsage"}.get(x["Type"])
                fam = {"application": "Load Balancer-Application", "network": "Load Balancer-Network"}.get(x["Type"])
                price = (
                    self._price("AWSELB", {"regionCode": region, "productFamily": fam, "usagetype": ut(region, kind)})
                    if kind
                    else (None, f"ELB type {x['Type']} not priced")
                )
                self.lines.append(
                    self._line(
                        price_q=price,
                        line_id=f"aws:elb:{x['LoadBalancerName']}",
                        service="ELB",
                        resource_type="load_balancer",
                        resource_id=x["LoadBalancerArn"],
                        resource=f"{x['LoadBalancerName']} ({x['Type']})",
                        environment=env_of(tags, comp),
                        component=comp,
                        component_source=how,
                        unit="USD/hour",
                        quantity_unit="hours",
                        segments=[Segment(start, self.now, 1.0)],
                        running={"from": iso(start), "to": None, "state": x["State"]["Code"]},
                        note="LCU 요금은 측정하지 않음(시간 요금만)",
                    )
                )
        if not found:
            self.notes.append(f"ELB ({region}): 이 프로젝트의 로드 밸런서 0개(DescribeLoadBalancers)")

    # -- storage, keys, registry, secrets -------------------------------------------------------------
    def s3_buckets(self) -> None:
        s3 = self.client("s3", self.regions[0])
        for b in s3.list_buckets().get("Buckets", []):
            name = b["Name"]
            try:
                tags = tag_map(s3.get_bucket_tagging(Bucket=name).get("TagSet"))
            except ClientError:
                tags = {}
            cls = classify(tags, name)
            if cls is None:
                continue
            comp, how = cls
            region = s3.get_bucket_location(Bucket=name).get("LocationConstraint") or "us-east-1"
            start = b["CreationDate"].astimezone(UTC)
            cw = self.client("cloudwatch", region)
            r = cw.get_metric_statistics(
                Namespace="AWS/S3",
                MetricName="BucketSizeBytes",
                Dimensions=[{"Name": "BucketName", "Value": name}, {"Name": "StorageType", "Value": "StandardStorage"}],
                StartTime=max(start, self.now - timedelta(days=31)),
                EndTime=self.now,
                Period=86400,
                Statistics=["Average"],
            )
            dps = sorted(r.get("Datapoints", []), key=lambda d: d["Timestamp"])
            segs: list[Segment] = []
            for k, dp in enumerate(dps):
                t0 = dp["Timestamp"].astimezone(UTC)
                t1 = dps[k + 1]["Timestamp"].astimezone(UTC) if k + 1 < len(dps) else self.now
                segs.append(Segment(t0, t1, float(dp["Average"]) / GB / aws_month_hours(t0)))
            line = self._line(
                price_q=self._price(
                    "AmazonS3", {"regionCode": region, "productFamily": "Storage", "volumeType": "Standard"}
                ),
                line_id=f"aws:s3:{name}",
                service="S3",
                resource_type="s3_bucket",
                resource_id=name,
                resource=f"S3 {name}",
                environment=env_of(tags, comp),
                component=comp,
                component_source=how,
                unit="USD/GB-month",
                quantity_unit="GB-month",
                segments=segs,
                running={"from": iso(start), "to": None, "state": "exists"},
                note="CloudWatch BucketSizeBytes(StandardStorage, 일별). 요청 요금은 측정하지 않음",
            )
            if not dps:
                line.status = "not_connected"
                line.reason = "S3 BucketSizeBytes 일별 지표가 아직 없음(생성 후 최대 하루 지연)"
            self.lines.append(line)

    def kms_keys(self, region: str) -> None:
        kms = self.client("kms", region)
        aliases: dict[str, str] = {}
        for page in kms.get_paginator("list_aliases").paginate():
            for a in page["Aliases"]:
                if a.get("TargetKeyId"):
                    aliases.setdefault(a["TargetKeyId"], a["AliasName"])
        price = self._price("awskms", {"regionCode": region, "usagetype": f"{region}-KMS-Keys"})
        for page in kms.get_paginator("list_keys").paginate():
            for k in page["Keys"]:
                kid = k["KeyId"]
                alias = aliases.get(kid, "")
                if not alias.startswith("alias/bravo-"):
                    continue  # only our aliases (reading every key's tags would also read other projects')
                md = kms.describe_key(KeyId=kid)["KeyMetadata"]
                if md.get("KeyManager") != "CUSTOMER":
                    continue
                cls = classify({}, alias.removeprefix("alias/"))
                comp, how = cls if cls else (None, "inferred:alias")
                start = md["CreationDate"].astimezone(UTC)
                self.lines.append(
                    self._line(
                        price_q=price,
                        line_id=f"aws:kms:{kid}",
                        service="KMS",
                        resource_type="kms_key",
                        resource_id=kid,
                        resource=f"KMS {alias}",
                        environment=ENV_BY_COMPONENT.get(comp or ""),
                        component=comp,
                        component_source=how.replace("name", "alias"),
                        unit="USD/key-month",
                        quantity_unit="key-month",
                        segments=monthly_segments(start, self.now, 1.0),
                        running={"from": iso(start), "to": None, "state": md.get("KeyState")},
                        note="키 버전 1개 기준(자동 교체 뒤 버전이 늘면 더 청구됨). API 요청 요금은 측정하지 않음",
                    )
                )

    def ecr(self, region: str) -> None:
        ecr = self.client("ecr", region)
        price = self._price("AmazonECR", {"regionCode": region, "usagetype": ut(region, "TimedStorage-ByteHrs")})
        for page in ecr.get_paginator("describe_repositories").paginate():
            for repo in page["repositories"]:
                name = repo["repositoryName"]
                cls = classify({}, name.replace("/", "-"))
                if cls is None:
                    continue
                comp, how = cls
                segs = []
                count = 0
                for ip in ecr.get_paginator("describe_images").paginate(repositoryName=name):
                    for img in ip["imageDetails"]:
                        count += 1
                        t0 = img["imagePushedAt"].astimezone(UTC)
                        segs += monthly_segments(t0, self.now, img.get("imageSizeInBytes", 0) / GB)
                created = repo["createdAt"].astimezone(UTC)
                self.lines.append(
                    self._line(
                        price_q=price,
                        line_id=f"aws:ecr:{name}",
                        service="ECR",
                        resource_type="ecr_repository",
                        resource_id=repo["repositoryArn"],
                        resource=f"ECR {name} (이미지 {count}개)",
                        environment=ENV_BY_COMPONENT.get(comp or ""),
                        component=comp,
                        component_source=how,
                        unit="USD/GB-month",
                        quantity_unit="GB-month",
                        segments=segs,
                        running={"from": iso(created), "to": None, "state": "exists"},
                        note="이미지별 압축 크기를 푸시 시각부터 합산(공유 레이어는 중복으로 세어 상한 쪽 추정)",
                    )
                )

    def secrets(self, region: str) -> None:
        sm = self.client("secretsmanager", region)
        price = self._price(
            "AWSSecretsManager", {"regionCode": region, "usagetype": ut(region, "AWSSecretsManager-Secrets")}
        )
        for page in sm.get_paginator("list_secrets").paginate(IncludePlannedDeletion=True):
            for s in page["SecretList"]:
                tags = tag_map(s.get("Tags"))
                cls = classify(tags, s["Name"].replace("/", "-"))
                if cls is None:
                    continue
                comp, how = cls
                start = s["CreatedDate"].astimezone(UTC)
                end = s["DeletedDate"].astimezone(UTC) if s.get("DeletedDate") else self.now
                self.lines.append(
                    self._line(
                        price_q=price,
                        line_id=f"aws:secret:{s['Name']}",
                        service="Secrets Manager",
                        resource_type="secret",
                        resource_id=s["ARN"],
                        resource=f"Secrets Manager {s['Name']}",
                        environment=env_of(tags, comp),
                        component=comp,
                        component_source=how,
                        unit="USD/secret-month",
                        quantity_unit="secret-month",
                        segments=monthly_segments(start, end, 1.0),
                        running={
                            "from": iso(start),
                            "to": iso(end) if s.get("DeletedDate") else None,
                            "state": "exists",
                        },
                        note="비밀 값은 읽지 않음(목록 메타데이터만)",
                    )
                )

    def rds(self, region: str) -> None:
        rds = self.client("rds", region)
        found = 0
        for page in rds.get_paginator("describe_db_instances").paginate():
            for d in page["DBInstances"]:
                tags = tag_map(d.get("TagList"))
                cls = classify(tags, d["DBInstanceIdentifier"])
                if cls is None:
                    continue
                found += 1
                comp, how = cls
                start = d["InstanceCreateTime"].astimezone(UTC)
                engine = {"postgres": "PostgreSQL"}.get(d["Engine"], d["Engine"])
                common = dict(
                    service="RDS",
                    resource_id=d["DBInstanceArn"],
                    environment=env_of(tags, comp),
                    component=comp,
                    component_source=how,
                    running={"from": iso(start), "to": None, "state": d["DBInstanceStatus"]},
                )
                self.lines.append(
                    self._line(
                        price_q=self._price(
                            "AmazonRDS",
                            {
                                "regionCode": region,
                                "instanceType": d["DBInstanceClass"],
                                "databaseEngine": engine,
                                "deploymentOption": "Multi-AZ" if d.get("MultiAZ") else "Single-AZ",
                            },
                        ),
                        line_id=f"aws:rds:{d['DBInstanceIdentifier']}",
                        resource_type="rds_instance",
                        resource=f"RDS {d['DBInstanceIdentifier']} ({d['DBInstanceClass']})",
                        unit="USD/hour",
                        quantity_unit="hours",
                        segments=[Segment(start, self.now, 1.0)],
                        **common,
                    )
                )
        if not found:
            self.notes.append(
                f"RDS ({region}): 이 프로젝트의 DB 인스턴스 0개. 스파이크 RDS(bravo-spike-rds)는 지금 없음"
            )

    def cloudtrail(self, region: str) -> None:
        ct = self.client("cloudtrail", region)
        price = self._price("AWSCloudTrail", {"regionCode": region, "usagetype": ut(region, "FreeEventsRecorded")})
        for t in ct.describe_trails(includeShadowTrails=False).get("trailList", []):
            cls = classify({}, t["Name"])
            if cls is None:
                continue
            comp, how = cls
            sel = ct.get_event_selectors(TrailName=t["TrailARN"])
            data = any(s.get("DataResources") for s in sel.get("EventSelectors") or []) or any(
                any(
                    f.get("Field") == "eventCategory" and "Data" in (f.get("Equals") or [])
                    for f in s.get("FieldSelectors", [])
                )
                for s in sel.get("AdvancedEventSelectors") or []
            )
            line = self._line(
                price_q=price,
                line_id=f"aws:cloudtrail:{t['Name']}",
                service="CloudTrail",
                resource_type="trail",
                resource_id=t["TrailARN"],
                resource=f"CloudTrail {t['Name']} (관리 이벤트 첫 사본)",
                environment=ENV_BY_COMPONENT.get(comp or ""),
                component=comp,
                component_source=how,
                unit="USD/event",
                quantity_unit="events",
                segments=[],
                note="관리 이벤트 첫 사본은 무료(Pricing API FreeEventsRecorded). 로그 저장은 S3 줄에 있음",
            )
            line.fixed_subtotal = 0.0 if line.unit_price == 0 else None
            self.lines.append(line)
            if data:
                self.not_connected.append(
                    {
                        "provider": "aws",
                        "what": f"CloudTrail {t['Name']} 데이터 이벤트",
                        "reason": "데이터 이벤트가 켜져 있으나 건수를 측정하지 않음",
                    }
                )
