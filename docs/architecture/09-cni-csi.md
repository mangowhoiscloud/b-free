# CNI와 CSI

**바뀐 것(2026-11-02 21:10 KST).** 팀장이 RKE2를 스펙 r2대로 되돌렸습니다(ADR 0010). 온프렘 목적지와 플랫폼은 RKE2(CIS)이고, 플랫폼은 EC2 1대 단일 노드입니다. 아래 표의 K3s 열은 RKE2로 바뀝니다. Cilium + Hubble, Kyverno `GeneratingPolicy`, StorageClass 이름, 객체 저장(RGW)은 그대로입니다. 다른 점 둘: RKE2에 Cilium을 붙이는 방법(내장 `cni: cilium` 또는 `cni: none` + 랜딩 존 Cilium)이 열려 있고, RKE2에는 K3s의 `local-path`가 없어 블록 저장 바탕이 필요합니다. 표 전체를 RKE2 기준으로 다시 쓰는 일은 RKE2 cloud-init이 생긴 뒤 합니다.

클러스터 네트워크(CNI), 네트워크 정책 집행, 스토리지(CSI)를 환경마다 정합니다. 기준은 팀장 결정(2026-11-02)입니다. CNI는 세 환경 모두 Cilium + Hubble입니다. RKE2, Calico, flannel 단독, TopoLVM, SeaweedFS는 쓰지 않습니다. 결정 기록은 ADR 0004(클러스터와 K8s 판), ADR 0005(객체 저장), ADR 0008(Cilium, Gateway API, Terraform)입니다. 2026-11-02 20:45 KST에 다시 썼습니다. 앞 판은 flannel, Kyverno ClusterPolicy, TopoLVM 기준이었습니다.

원칙은 넷입니다.

1. 네트워크 정책 집행과 흐름 관측은 세 환경 모두 Cilium이 맡습니다. Hubble 흐름 기록이 네트워크 통제(NET-01)의 증거입니다. 환경 차이는 Cilium을 붙이는 방식 하나뿐입니다.
2. 네트워크 기본선은 Kyverno `GeneratingPolicy` 하나로 모든 클러스터에 같게 겁니다. CNI 이름이 아니라 시험으로 동등성을 증명합니다.
3. StorageClass 이름은 어디서나 `bravo-db`와 `bravo-general`입니다. 바탕이 바뀌어도 CNPG와 앱 매니페스트는 고치지 않습니다.
4. 객체 저장은 같은 S3 계약 + 환경별 엔진입니다. 온프렘은 Rook-Ceph RGW입니다(D-22).

고객은 쿠버네티스를 모른다고 가정합니다. 환경 입력은 `name`, `region`, `size`뿐이고, CNI 플래그와 StorageClass 값은 모듈과 랜딩 존 안의 기본값입니다(`infra/README.md`).

## 1. 결정 표

| 항목 | `platform-aws`, `source-aws` (EKS) | `target-onprem` (K3s) | `target-gcp` (GKE) |
|---|---|---|---|
| 클러스터 | EKS 관리형 노드 그룹 | K3s, Lima VM 2대(server 1, agent 1) | GKE Standard + 노드 풀 |
| K8s 판 | 1.36 | v1.36.5+k3s1(sha256 고정) | 1.36(REGULAR 채널) |
| CNI | Cilium 1.20.4, VPC CNI chaining(`cni.chainingMode: aws-cni`). VPC CNI는 ENI와 IPAM만 맡음 | Cilium 1.20.4, VXLAN(`routingMode: tunnel`). K3s 기본 flannel은 끔 | Dataplane V2(Cilium 기반, 관리형) |
| 정책 집행 | Cilium. VPC CNI 정책 에이전트는 끔(`enableNetworkPolicy=false`) | Cilium | Dataplane V2 |
| 흐름 관측 | Hubble + relay | Hubble + relay | 관리형 Hubble(`advanced_datapath_observability_config`) |
| kube-proxy | add-on 유지, Cilium kube-proxy 대체 켬 | Cilium kube-proxy 대체 | GKE 관리 |
| 진입(Gateway API) | Cilium Gateway API(Envoy), `gatewayClass` 생성 | Cilium Gateway API(Envoy) | GKE 쪽 overlay `40-gateway.yaml` |
| 기본선 정책 | Kyverno `GeneratingPolicy`(2절) | 같음 | 같음 |
| 캡슐화 | 없음(Pod가 VPC 주소를 받음, MTU 9001) | VXLAN, UDP 8472 | 없음(VPC 네이티브) |
| CSI | EBS CSI add-on, gp3, KMS CMK | 임시: K3s `local-path`(블록 저장 결정 전, D-22) | PD CSI, pd-balanced, CMEK |
| 객체 저장 | 출발지는 Amazon S3(고객 기존). EKS 목적지면 S3 | Rook-Ceph RGW | Cloud Storage(XML API, HMAC) |
| IMDS 차단 | 노드 그룹 IMDSv2 필수, hop limit 1 | 해당 없음(메타데이터 서버 없음) | Workload Identity 메타데이터 서버 |
| 설정 위치 | `infra/modules/cluster/eks/addons.tf`, `deploy/cloud/source-aws/cilium-values.yaml` | `deploy/landing-zone/overlays/k3s/cilium-values.yaml`, `storageclasses.yaml` | `infra/modules/cluster/gke/main.tf`, `deploy/landing-zone/overlays/gke/` |

**지금 상태(2026-11-02 20:4x KST, 읽기 전용 확인).**
- 출발지 EKS `bravo-source`: VPC CNI 정책 에이전트는 꺼져 있고(`aws eks describe-addon`), Cilium은 아직 설치되지 않았습니다(`kubectl get pods -A`). 그래서 지금 이 클러스터에는 NetworkPolicy를 집행하는 주체가 없습니다. Cilium 설치(`deploy/cloud/source-aws/`) 전에는 NET-01을 PASS로 볼 수 없습니다.
- 온프렘 K3s `bravo-onprem`: 노드 2개가 떴고 Cilium · Hubble Pod가 설치 중이었습니다(노드 NotReady, 20:41).
- GKE: 프로젝트 `b-free-target-20261102`에 클러스터가 아직 없고 apply가 진행 중이었습니다.
- Kyverno 정책 파일은 아직 없습니다(`policies/`에 README만). 랜딩 존 설치기(`deploy/landing-zone/install.sh`)에도 Kyverno 단계가 없습니다.

두 StorageClass의 공통 값은 아래와 같습니다.

| 이름 | 용도 | 바인딩 | 확장 | 회수 |
|---|---|---|---|---|
| `bravo-db` | CNPG 데이터와 WAL | `WaitForFirstConsumer` | EBS · PD 허용. K3s local-path는 불가 | `Delete` |
| `bravo-general` | 일반 PVC(앱 · 관측 등) | `WaitForFirstConsumer` | 위와 같음 | `Delete` |

- 회수 정책 `Delete`는 데모 환경용입니다. 운영은 `Retain`을 씁니다.
- K3s의 `local-path`는 노드 로컬 hostPath입니다. 복제와 확장이 없습니다. 블록 저장이 정해지면(후보 Rook-Ceph RBD) overlay의 `storageclasses.yaml`만 바뀝니다.

## 2. 네트워크 기본선(Kyverno)

리허설과 검증 네임스페이스는 Trial마다 생겼다가 지워집니다(`07-runtime-mesh-observability.md` 2절). 네임스페이스마다 정책을 손으로 걸면 한 번만 잊어도 격리가 뚫립니다. 그래서 클러스터 범위의 Kyverno 정책이 기본선을 만듭니다.

| 정책 | 종류 | 대상 | 하는 일 |
|---|---|---|---|
| 생성 | `GeneratingPolicy`(`policies.kyverno.io/v1`) | 라벨 `bravo.io/tier`가 `rehearsal`이나 `verify`인 네임스페이스 | 수신 · 송신 기본 거부 NetworkPolicy와 kube-dns로 가는 DNS(UDP, TCP 53) 허용 NetworkPolicy를 만듦. 동기화를 켜 지우거나 고쳐도 다시 만듦 |
| 검증 | `ValidatingPolicy`(`policies.kyverno.io/v1`) | 위 정책이 만든 NetworkPolicy | Kyverno가 아닌 주체의 삭제와 수정을 거부 |

- 같은 정책을 EKS, GKE, K3s에 똑같이 겁니다. NetworkPolicy는 K8s 표준 API라 세 환경의 Cilium(GKE는 Dataplane V2)이 집행합니다.
- 옛 `kyverno.io/v1` ClusterPolicy는 쓰지 않습니다. Kyverno v1.20에서 제거 예정이기 때문입니다(`docs/spec/TECH-STACK-SPEC.md` 2.8절).
- 판은 Kyverno 차트 3.9.2입니다(`deploy/landing-zone/versions.env`).
- 리허설에 필요한 허용(D-12, `05-agent-rehearsal.md`)은 네임스페이스별 NetworkPolicy가 더합니다. NetworkPolicy는 합집합이라 기본 거부 위에 허용만 늡니다.
- 리허설 서비스는 이 기본선과 별도로 Trial마다 기본 거부 정책을 직접 만듭니다(`platform/rehearsal/src/bravo_rehearsal/cluster.py`). 두 겹 가운데 하나만 있어도 거부가 유지됩니다.
- Cilium 고유 정책(CiliumNetworkPolicy, FQDN 정책)은 쓰지 않습니다. GKE Dataplane V2와 같은 뜻을 유지하기 위해 표준 NetworkPolicy만 씁니다.

**메타데이터 서버(IMDS) 차단.** EKS 노드 그룹은 IMDSv2 필수, hop limit 1입니다. Pod 네트워크 네임스페이스에서 나간 요청은 홉 하나를 더 써서 응답을 받지 못합니다(`02-network.md` N14). GKE는 Workload Identity 메타데이터 서버가 노드 메타데이터를 가립니다. Lima VM에는 클라우드 메타데이터 서버가 없습니다.

## 3. 판과 고른 이유

| 구성요소 | 판 | 근거 |
|---|---|---|
| K8s | 1.36 | 아래 "1.36으로 맞춘 이유" |
| K3s | v1.36.5+k3s1 | 릴리스의 `sha256sum-arm64.txt`로 고정(`infra/envs/target-onprem/main.tf`) |
| Cilium | 차트 1.20.4 | `deploy/landing-zone/versions.env`, Cilium 문서 "AWS VPC CNI plugin"(1.20.4, 2026-11-02 확인) |
| Gateway API CRD | v1.7.0(K3s 설치기). EKS 값 파일 주석은 v1.6.3 | 두 판이 다름. 인프라 갈래가 하나로 맞춤(열림) |
| Kyverno | 차트 3.9.2 | `deploy/landing-zone/versions.env` |
| Rook-Ceph | 차트 v1.21.1, Ceph Tentacle 20.2.4 | `deploy/landing-zone/versions.env`, `TECH-STACK-SPEC.md` 2.8절 |
| EBS CSI, VPC CNI, snapshot-controller | EKS 관리형 add-on | `infra/modules/cluster/eks/addons.tf` |
| PD CSI, Dataplane V2 | GKE 관리 | `infra/modules/cluster/gke/main.tf` |

**1.36으로 맞춘 이유.** 실무 결정이고 팀장 확인을 기다립니다(`TECH-STACK-SPEC.md` 3절 7번, ADR 0004).
- EKS `bravo-source`가 1.36으로 생성됐습니다(`aws eks describe-cluster`, 2026-11-02). EKS는 minor를 내릴 수 없습니다.
- 앞 판은 1.35를 권고했습니다. 이유는 kube-bench 자동 판 대응(1.35까지)과 GKE STABLE 채널이었습니다.
- 그 대가는 이렇게 받습니다. kube-bench는 `--benchmark`로 벤치마크 이름을 명시합니다(`04-governance.md` 1절). GKE는 STABLE 대신 REGULAR 채널을 씁니다. 1.36은 2026-11-11 무렵 STABLE에 들어옵니다(GKE 출시 일정, 2026-11-02 확인).
- CNPG 1.30은 1.34–1.36, Istio 1.31은 1.32–1.37을 지원합니다(앞 판에서 확인).

**Cilium을 모든 환경에 두는 이유.** 1급 원천 「기술 스택(툴) 조합」이 Cilium + Hubble을 지정했고, 팀장이 2026-11-02에 정했습니다. GKE Dataplane V2가 이미 Cilium 기반이라, EKS와 K3s에도 Cilium을 두면 정책 집행과 흐름 기록의 뜻이 세 환경에서 같아집니다. 앞 판이 Cilium을 뺀 이유(Istio ambient와 기능 겹침)는 D-15의 11/4 판정에서 다시 봅니다.

## 4. 대안과 쓰지 않는 이유

| 대안 | 쓰지 않는 이유 |
|---|---|
| 환경별 기본 CNI(EKS VPC CNI 단독, K3s flannel + kube-router) | 정책 집행 엔진과 흐름 기록이 환경마다 달라 NET-01 증거가 같은 뜻이 아님. 앞 판의 선택이었음 |
| Calico | RKE2와 함께 뺌. 전역 정책은 Kyverno 기본선이 대신함 |
| Kyverno `ClusterPolicy` | v1.20 제거 예정. `GeneratingPolicy` · `ValidatingPolicy`로 씀 |
| TopoLVM | 블록 저장 후보에서 뺌(D-22, TECH-STACK-SPEC 4절). K3s는 정해질 때까지 local-path |
| SeaweedFS, MinIO, Garage | 객체 저장에서 뺌(D-22, ADR 0005) |
| 클라우드 위 Rook-Ceph | 관리형 블록 저장 위에 복제를 다시 얹음. EKS · GKE 목적지는 S3 · Cloud Storage(D-22) |

## 5. 실패 유형과 방어 장치

| 실패 | 증상 | 방어 |
|---|---|---|
| Cilium이 아직 없음(EKS 출발지 지금 상태) | NetworkPolicy가 있어도 집행되지 않음 | VPC CNI 정책 에이전트를 끈 채로는 데모 전 NET-01 시험을 PASS로 보지 않음. 설치 뒤 8절 V4 · V5 대조 시험 |
| Kyverno가 멈춤 | 새 리허설 네임스페이스에 기본선이 생기지 않음 | 리허설 서비스가 네임스페이스를 만든 직후 격리 시험을 돌리고, 실패하면 BLOCK(`05-agent-rehearsal.md` 3절) |
| 기본선 삭제나 수정 | 리허설 Pod가 밖으로 나감 | `ValidatingPolicy`가 거부. 그래도 지워지면 생성 정책의 동기화가 다시 만듦 |
| Cilium 에이전트 장애 | 새 Pod 네트워크가 안 붙거나 정책이 갱신되지 않음 | `cilium-dbg status`와 Hubble relay 상태를 통합 점검에 넣음(`docs/demo/INTEGRATION-CHECK.md`) |
| MTU 불일치(K3s VXLAN) | 큰 패킷만 사라짐. 복제가 멈춘 듯 보임 | 6절 계산, DF 핑 시험(V6) |
| local-path 노드를 잃음 | 그 노드의 볼륨을 잃음 | 온프렘 데모는 Mac 한 대라 장애 영역이 하나임. CNPG 복제본은 다른 노드에 둠 |
| EBS가 다른 AZ에 생김 | Pod가 볼륨을 붙이지 못함 | `WaitForFirstConsumer` |
| KMS 키 권한 없음이나 삭제 예약 | 볼륨 생성과 연결 실패 | IAM 정책을 그 키 하나로 좁힘. 키 삭제 대기 기간 경보 |
| WAL이 데이터 볼륨을 채움 | DB가 쓰기를 멈춤 | CNPG `walStorage`로 WAL 볼륨을 따로 둠(권장). 논리 복제 슬롯이 WAL을 붙잡을 때 특히 필요(`02-network.md` 1절 F7) |
| conntrack 표가 참 | 새 연결이 무작위로 실패 | 사용률 80% 경보 |

**경보 규칙.** 수집 백엔드에 거는 일은 관측 담당입니다(`07-runtime-mesh-observability.md` 6절).

| 경보 | 식 | 기준 |
|---|---|---|
| 볼륨 사용률 | `kubelet_volume_stats_used_bytes / kubelet_volume_stats_capacity_bytes` | 0.8 초과 10분 |
| conntrack 사용률 | `node_nf_conntrack_entries / node_nf_conntrack_entries_limit` | 0.8 초과 10분 |
| Hubble 정책 거부 급증 | `hubble_drop_total{reason="POLICY_DENIED"}` 증가율 | 데모 중 기준선의 3배(시험으로 조정) |
| EKS Pod IP | `awscni_assigned_ip_addresses / awscni_total_ip_addresses` | 0.8 초과 |

## 6. MTU

**K3s(Cilium VXLAN).** VXLAN은 패킷마다 50바이트를 더합니다(바깥 IPv4 20, UDP 8, VXLAN 8, 안쪽 이더넷 14). 하위 인터페이스 MTU가 1500이면 Pod MTU는 1450입니다. DF를 켠 ICMP 데이터 최대 크기는 1450 − 20 − 8 = 1422바이트입니다. 그래서 1422바이트 핑은 성공하고 1423바이트 핑은 실패해야 합니다. 실측 값은 아직 없습니다.

**EKS(VPC CNI chaining).** 캡슐화가 없습니다. VPC 안 ENI MTU는 9001입니다(`terraform output cni`). 인터넷으로 나가는 경로는 1500입니다.

**GKE(Dataplane V2).** VPC 네이티브라 VPC MTU를 그대로 씁니다. 새 VPC의 기본값은 1460입니다(GCP 문서 기준, 2026-11-02에 다시 조회하지 않음).

**환경 사이.** 링크는 TCP를 받아 다시 여는 프록시입니다(D-16, 지금은 임시 링크). 그래서 클러스터마다 MTU가 달라도 경로 MTU 문제가 환경을 넘지 않는다고 봅니다(추정).

## 7. 포트

| 환경 | 포트 | 방향 | 용도 |
|---|---|---|---|
| `target-onprem` | TCP 6443 | agent에서 server로 | K3s supervisor와 API |
| `target-onprem` | UDP 8472 | 노드 사이 | Cilium VXLAN |
| `target-onprem` | TCP 4240 | 노드 사이 | Cilium 상태 확인 |
| `target-onprem` | TCP 4244 | 노드에서 relay로 | Hubble 서버 |
| `target-onprem` | TCP 10250 | 노드 사이 | kubelet |
| `target-onprem` | 아웃바운드 443, 7844 | VM에서 밖으로 | 이미지, Tunnel(D-16). 인터넷에서 들어오는 포트 없음 |
| EKS | EKS가 관리, Cilium 4240 · 4244는 노드 SG 안 | 노드와 제어면 사이 | 캡슐화 포트 없음 |
| `target-gcp` | GKE가 관리 | 노드와 제어면 사이 | 사설 노드 |

## 8. 시험 항목

클러스터마다 아래를 차례로 시험하고 항목마다 PASS나 FAIL을 냅니다. 이 항목을 도는 스크립트는 아직 없습니다(옛 경로 `infra/envs/target-onprem/scripts/verify.sh` 없음, 감사 42). 통합 점검(`docs/demo/INTEGRATION-CHECK.md`)이 이 가운데 V1, V2, V4, V5, V9를 손 명령으로 씁니다.

| 번호 | 시험 | 기대 |
|---|---|---|
| V1 | 노드 상태 | 모든 노드 Ready |
| V2 | Cilium 상태 | `cilium-dbg status --brief`가 OK, Hubble relay Ready |
| V3 | 다른 노드의 Pod 사이 통신 | 성공 |
| V4 | NetworkPolicy 허용 경로(대조군) | 성공 |
| V5 | NetworkPolicy 거부 경로 | 실패하고 Hubble에 `POLICY_DENIED` 흐름. V4가 성공해야 차단으로 셈 |
| V6 | DF 핑 MTU 경계(K3s) | 1422바이트 성공, 1423바이트 실패 |
| V7 | `bravo-db` PVC | Pod가 놓인 노드(EBS · PD는 AZ)에서 바인딩 |
| V8 | 볼륨 확장 | EBS · PD는 파일 시스템 크기가 늚. K3s local-path는 해당 없음 |
| V9 | Kyverno 기본선 생성 | `bravo.io/tier=rehearsal` 네임스페이스에 기본 거부와 DNS 허용 정책이 생김 |
| V10 | 기본선 삭제 시도 | 거부됨 |
| V11 | StorageClass 이름 | `bravo-db`, `bravo-general`이 있음 |

## 9. 실행 결과

<!-- RESULTS:target-onprem -->

아직 없습니다(2026-11-02 20:45 KST).

## 10. 열린 것

- Gateway API CRD 판(K3s 설치기 v1.7.0, EKS 값 파일 주석 v1.6.3)을 하나로 맞춥니다.
- GKE overlay의 Gateway 구현과 Kyverno가 GKE Standard에서 제한 없이 도는지 확인합니다.
- 온프렘 블록 저장(후보 Rook-Ceph RBD)을 정합니다(D-22).
- EKS와 GKE에서 V3–V10을 돌립니다.
- D-15(플랫폼 EKS의 Istio ambient)와 Cilium 기능의 겹침을 11/4 판정 전에 정리합니다.

## 근거

- K3s 판과 체크섬: `infra/envs/target-onprem/main.tf`(2026-11-02 공식 릴리스 확인 주석)
- Cilium chaining: Cilium 문서 "AWS VPC CNI plugin"(1.20.4, 2026-11-02 확인, `infra/modules/cluster/eks/addons.tf` 주석)
- 랜딩 존 판: `deploy/landing-zone/versions.env`
- EKS 판과 add-on 설정: `aws eks describe-cluster`, `aws eks describe-addon`(2026-11-02 20:3x KST, 읽기 전용)
- GKE 판: GKE 출시 일정 문서(2026-11-02 확인, `infra/modules/cluster/gke/variables.tf` 주석)
- Kyverno 정책 종류: `docs/spec/TECH-STACK-SPEC.md` 2.8절
