# cluster/gke

GKE Autopilot 리전 클러스터를 만듭니다. `target-gcp`(제안 D-01)가 씁니다. `terraform validate`만 합니다.

## 왜 Autopilot인가

- 노드 풀, 노드 OS, 업그레이드를 Google이 맡습니다. 고객이 노드를 고르지 않습니다.
- Dataplane V2가 늘 켜져 있어 NetworkPolicy를 집행합니다. PD CSI도 기본으로 켜져 있습니다.
- 목적지에 필요한 CNPG, OpenBao, SeaweedFS, Kyverno는 Autopilot 제약 안에서 돕니다(추정, 실측 전).

## Autopilot 제약(확인할 것)

- 특권 Pod와 호스트 경로를 쓰는 워크로드는 막힙니다. TopoLVM은 GKE에 필요 없습니다.
- Kyverno 웹훅은 GKE 관리 네임스페이스(`kube-system` 등)를 대상으로 삼을 수 없습니다. 차트 기본값이 `kube-system`을 빼므로 맞습니다(추정).
- Istio ambient의 ztunnel은 `NET_ADMIN`이 필요합니다. 메시는 플랫폼 EKS에만 두므로 해당 없습니다.
- 위 셋 가운데 하나라도 막히면 Standard 리전 클러스터로 바꿉니다. 출력 계약은 그대로 둡니다.

## 무엇을 만드나

- 사설 노드, 비공개 IP 엔드포인트, IAM으로 지키는 DNS 엔드포인트.
- 릴리스 채널 `REGULAR`, 최소 판 1.36입니다. 모든 환경이 1.36을 씁니다.
- 1.36은 2026-11-02 현재 Regular 채널에 있습니다. Stable에는 2026-11-11 무렵 들어옵니다(GKE 출시 일정, 2026-11-02 확인). 그 뒤 `release_channel = "STABLE"`로 바꿀 수 있습니다.
- 고객 관리 키 둘: Secret 애플리케이션 계층 암호화, PD(노드 부팅 디스크와 PV).
- Cloud KMS 키링은 지울 수 없습니다. 다시 올릴 때 이름이 겹치지 않게 무작위 접미사를 붙입니다.
- 최소 권한 노드 서비스 계정(`roles/container.defaultNodeServiceAccount`), Workload Identity.
- `deletion_protection` 기본값은 `true`입니다. 지우려면 먼저 `false`로 apply합니다.

## 출력 계약

`cluster/eks`와 같은 이름입니다. `cni`는 `{ name = "gke-dataplane-v2", network_policy = true, encapsulation = "none", mtu = 1460 }`입니다. `storage_platform = "gke"`(landing_zone의 `provider_kind`), `addon_delivery = "argocd"`(landing_zone의 `delivery`), 그리고 `pd_kms_key_id`를 둡니다.
