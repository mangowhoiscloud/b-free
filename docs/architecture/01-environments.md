# 환경 넷

플랫폼 클러스터 하나와 고객 앱 환경 셋을 정합니다. 기준은 스펙 §7.2, §8.8, T1, T2, T6, T7입니다. 클러스터 종류는 결정 D-21, 모든 고객 클러스터에 같은 묶음(랜딩 존)은 결정 D-23입니다. 온프렘 저장소의 대체는 제안 D-22(후보 Rook-Ceph, 검증 중)입니다. 정본은 `docs/spec/TECH-STACK-SPEC.md`, 클러스터별 지도는 `10-cluster-overview.md`, 랜딩 존은 `11-landing-zone.md`입니다. GCP 환경은 제안 D-01입니다. 서브넷, NAT, 환경 사이 연결은 제안 D-16이고 근거는 `research/platform-research/network-design-2026-11-02.md`입니다.

## 1. 한눈에

| 항목 | `platform-aws` | `source-aws` | `target-onprem` | `target-gcp` (D-01) |
|---|---|---|---|---|
| 역할 | 중앙 제어 | 출발지(blue) | 목적지(green) | 두 번째 목적지 |
| 위치 | AWS 서울 2a · 2b · 2c, VPC ② `10.20.0.0/16` | AWS 서울 2a, VPC ① `10.10.0.0/16` | 온프렘 장비의 Ubuntu VM(호텔 NAT 뒤) | GCP 서울(asia-northeast3) |
| K8s | EKS. 제어면은 AWS가 3 AZ에서 운영 | EKS | K3s(CIS 하드닝 설정) | GKE(Autopilot 우선) |
| 노드 | 관리형 노드 그룹: core t4g.large 3대(AZ마다 1), agents t4g.large 1대(2c, taint) | 관리형 노드 그룹 t4g.large 1대 | Lima VM 2대: server(내장 etcd) 1, agent 1(3절) | GKE가 관리 |
| 데이터 | 플랫폼 DB(CNPG) | CNPG, PgBouncer | CNPG, PgBouncer | CNPG, PgBouncer |
| 비밀 · 키 | 플랫폼 전용 | Secrets Manager, KMS(앱이 직접 호출) | OpenBao(+transit), ESO | 온프렘과 같은 금고, ESO(D-23) |
| 블록 저장(`bravo-db`, `bravo-general`) | EBS CSI(gp3) | EBS CSI(gp3) | 후보 Rook-Ceph RBD(제안 D-22, 검증 중) | PD CSI |
| 객체 저장 | 없음(감사 로그는 S3 Object Lock, P1) | S3 | S3 호환 저장소. 후보 Rook-Ceph RGW(제안 D-22, 검증 중) | 온프렘과 같은 저장소(D-23) |
| 정책 | Kyverno(P1) | Kyverno(P1) | Kyverno(P1) | Kyverno(P1) |
| 사용자 진입 | 콘솔용 진입점 | Cloudflare Tunnel | Cloudflare Tunnel | Cloudflare Tunnel |
| 환경 사이 연결(제안 D-16) | 링크(`bravo-link`)로 상대에 접속만 함 | 관리 Tunnel(`cf-edge`) + 링크 | 관리 Tunnel(`cf-edge`) + 링크 | 같은 구성(O13) |
| 아웃바운드(제안 D-16) | 사설 서브넷 3개 + Regional NAT | 사설 서브넷 2개 + Regional NAT | 호텔 NAT. 7844와 443만 | 확인 필요 |
| 작업 | T7 | T1, T6 | T2, T9 | D-01 채택 시 새 작업 |
| 우선 | P0 | P0 | P0 | 실제 컷오버 1회 뒤(D-01). 늦으면 P2 |

## 2. 왜 이렇게 나누나

**플랫폼을 고객 환경과 나눕니다.** 플랫폼은 이전을 지휘하는 쪽이라 출발지나 목적지 안에 있으면 안 됩니다. 출발지가 망가지면 지휘도 같이 멈추기 때문입니다. 데모는 비용과 정산 때문에 출발지와 같은 AWS 계정을 쓰고 VPC만 나눕니다. 운영 권장은 별도 계정이나 다른 클라우드입니다(§7.2).

**플랫폼은 EKS입니다(D-21).** 스펙 §7.2는 비용을 줄이려고 EC2 1대를 두었습니다. 팀장 결정으로 관리형 EKS로 바꿨습니다. 제어면(etcd, API 서버)은 AWS가 세 AZ에서 운영하므로 우리가 etcd를 돌보지 않습니다. 고객이 쿠버네티스를 몰라도 되게 한다는 제품 원칙과 같은 방향입니다. 플랫폼도 Terraform으로 만들어 다른 곳에 다시 세울 수 있다는 §7.2의 요구는 그대로 지킵니다. 비용은 6절에 있습니다.

**플랫폼 노드는 역할별 관리형 노드 그룹으로 나눕니다.** 예선 Railshot(server 1 + 빌드 agent 1)에서는 제어면과 워크로드가 한 머신에서 자원을 다퉈 클러스터가 불안정했습니다. EKS는 제어면이 우리 노드 밖에 있어 이 경합이 없습니다. 남는 경합은 제어 서비스와 에이전트 작업 사이입니다. 그래서 노드 그룹을 둘로 나눕니다.

| 층 | 노드 | 무엇이 도나 | 왜 |
|---|---|---|---|
| 제어면 | 없음(AWS 관리) | etcd, API 서버, scheduler, controller-manager | AWS가 세 AZ에서 운영. 우리 워크로드와 자원을 다투지 않음 |
| core 노드 그룹 | t4g.large 3대. AZ마다 관리형 노드 그룹 하나(최소 1) | API, 상태 머신, 게이트, 수집기, 콘솔, 리허설 서비스, LLM 게이트웨이, Executor, 플랫폼 DB(CNPG primary와 replica를 AZ별로), CoreDNS, EBS CSI controller | 제어 서비스와 DB를 세 AZ에 나눔. Executor와 상태 머신은 높은 PriorityClass와 PodDisruptionBudget |
| agents 노드 그룹 | t4g.large 1대(2c), 최소 1. `pool=agents:NoSchedule` taint | Trial 에이전트 Pod, 관측 백엔드 | 자원을 많이 쓰는 작업이 core를 밀어내지 못하게 함. ResourceQuota로 상한 |

노드 그룹은 장애 범위와 자원 경합을 나누는 용도입니다. 보안 격리 단위는 Pod입니다(`07-runtime-mesh-observability.md`). taint는 보안 경계가 아닙니다.

**노드 교체와 add-on.**
- 관리형 노드 그룹이 노드를 만들고 교체합니다. 조인 토큰을 우리가 만들거나 보관하지 않습니다.
- core 노드 그룹을 AZ마다 하나씩 두는 이유는 EBS입니다. 교체 노드가 같은 AZ에 떠야 플랫폼 DB 볼륨을 다시 붙일 수 있습니다.
- 관리형 add-on은 VPC CNI(네트워크 정책 켬), EBS CSI, CoreDNS, kube-proxy, Pod Identity Agent입니다. 판은 EKS 모듈에서 고정합니다.
- 노드 그룹은 IMDSv2 필수, hop limit 1입니다. 플랫폼 Pod의 AWS 역할은 Pod Identity로 받습니다(`platform/README.md`).
- API 엔드포인트는 비공개 전용입니다(`02-network.md` 3절).
- VPC CNI는 노드마다 Pod 수 상한이 있습니다. t4g.large는 35개입니다(prefix delegation을 끈 기본값). core 노드 하나를 잃으면 남은 두 대에 Pod가 몰리므로 Pod 수도 셉니다. 모자라면 prefix delegation을 켭니다(T7).
- T7 완료 기준에 "빈 계정에서 클러스터와 노드 그룹 구축"과 "core 노드 하나를 지운 뒤 같은 AZ에 새 노드가 붙고 플랫폼 DB 인스턴스가 다시 붙음"을 더합니다.
- 11/4(수) 12:00에 노드 배치(core 3 AZ, agents taint)를 확인합니다. 어긋나면 노드 그룹 설정을 고칩니다. AZ 수를 줄이는 일은 회의에서 정합니다.

**플랫폼 DB와 EBS.** EBS 볼륨은 AZ에 묶입니다. CNPG 인스턴스 3개를 `topologySpreadConstraints`로 세 AZ의 core 노드에 하나씩 두고, 각자 그 AZ의 볼륨을 씁니다. 동기 복제는 "standby 둘 중 하나"(any 1, 필수)로 둡니다. 그래서 AZ 하나를 잃어도 남은 둘이 쓰기를 계속합니다. 잃은 인스턴스는 그 AZ의 교체 노드에서 새 볼륨으로 다시 만듭니다. CNPG operator도 2개로 둡니다. 승격과 재구축을 T7에서 각각 시험합니다.

**Multi-AZ를 증명하는 방법.** 심사에서 "설정과 맞는가"를 물으면 다음 셋을 보입니다.
- `docs/evidence/`의 배치 대조 결과: EC2 인스턴스의 AZ, 노드의 `topology.kubernetes.io/zone` 라벨, 주요 Pod의 노드를 한 표로 맞춘 스크립트 출력
- AZ 하나의 서브넷을 NACL로 끊은 파티션 시험 3회의 기록(API 응답, DB 승격 시간, 컷오버 목업 결과)
- 위 결과의 실행 ID와 시각

**설정과 문서를 맞춥니다.** AZ 배치는 Terraform 변수로 고정하고, 노드의 `topology.kubernetes.io/zone` 라벨을 T7 시험으로 확인합니다. T7 완료 기준에는 다음 셋을 더합니다.
- agents 노드에 제어 구성요소가 뜨지 않음(taint와 nodeSelector)
- AZ 하나의 노드를 멈춰도 API와 상태 머신이 응답함
- agents 풀에 부하를 걸어도 core의 응답 시간이 기준 안에 있음

**출발지는 EKS입니다.** 고객이 관리형 K8s와 관리형 서비스(S3, Secrets Manager, KMS)에 기대고 있는 상태가 이 과제의 출발점입니다. 이 의존성이 S2 리허설의 재료가 됩니다(§7.3).

**목적지마다 배포판은 다르고, 위에 올리는 묶음은 같습니다(D-21, D-23).** 온프렘은 K3s, GCP는 GKE입니다. 같은 cloud-init을 나누지 않습니다. 대신 모든 고객 클러스터에 같은 랜딩 존(CNPG + PgBouncer, S3 호환 저장소와 블록 저장(후보 Rook-Ceph, 제안 D-22), 금고 + ESO, Kyverno, Argo CD, `cf-edge` + `bravo-link`, OTel Collector 에이전트)을 설치합니다(`11-landing-zone.md`). 클라우드 차이는 공급자 overlay 한 층에만 둡니다. 같다는 것은 시험으로 보입니다. Kyverno 기본선, StorageClass 이름, 6개 통제 시험이 모든 목적지에서 같습니다. GCP를 더하면 늘어나는 것은 `network/gcp`, `cluster/gke`, `ci_identity/gcp`, `envs/target-gcp`, GKE overlay, GCP용 통제 시험 실행, 삭제 절차입니다.

## 3. 온프렘 장비

스펙 §12는 온프렘 데모 장비를 "Linux VM을 띄울 수 있는 유선 연결 장비(Mac mini 또는 PC)"로 둡니다. 정해지기 전에는 팀원의 Mac(arm64)에서 Lima로 Ubuntu VM을 띄워 개발합니다(`infra/envs/target-onprem/`).

**VM은 두 대입니다.** K3s server 1대(내장 etcd)와 agent 1대입니다. 리허설과 검증 네임스페이스를 운영 앱이 있는 노드와 떼어 둡니다. 크기는 추정입니다. 저장소 첫 후보 Rook-Ceph(제안 D-22, 검증 중)를 올린다고 보고 잡았고, T2 스파이크에서 잽니다.

| VM | K3s 역할 | 크기(추정) | 무엇이 도나 |
|---|---|---|---|
| VM 1 | server(내장 etcd) 겸 앱 노드 | 6 vCPU, 16 GiB. 루트 디스크 40 GiB + 저장소 전용 디스크 50 GiB | K3s 제어면, 앱, CNPG, PgBouncer, OpenBao, ESO, Kyverno, Argo CD, 저장소(후보 Ceph mon · mgr · OSD · RGW), `cf-edge`, `bravo-link` |
| VM 2 | agent. `pool=rehearsal:NoSchedule` taint | 4 vCPU, 12 GiB. 루트 디스크 30 GiB + 저장소 전용 디스크 50 GiB | 리허설 · 검증 네임스페이스, 저장소(후보 Ceph OSD, toleration) |

- 메모리를 늘린 이유는 첫 후보 Ceph입니다. OSD의 기본 메모리 목표는 4 GiB이고, 데모에서는 낮춰 씁니다. 더 가벼운 후보로 정하면 줄입니다(D-22).
- 합은 10 vCPU, 28 GiB입니다. Mac의 메모리(64 GB)로 충분합니다.
- K3s는 판과 SHA-256으로 고정한 바이너리로 설치하고, CIS 하드닝 가이드의 설정(비밀 암호화, PSA 설정 파일, 감사 정책, `protect-kernel-defaults`와 sysctl)을 겁니다(`infra/README.md`).
- 노드 사이 포트는 TCP 6443(agent에서 server), TCP 10250(kubelet), UDP 8472(flannel VXLAN)입니다. Lima VM 사이 네트워크에만 열고 호스트 밖으로 내놓지 않습니다(`02-network.md` 2.2절).
- T2가 늦어지면 VM 1대로 시작하고 VM 2는 11/5에 더합니다.

- VM은 들어오는 포트를 열지 않습니다. 사용자 진입 Tunnel, 관리 Tunnel, 링크 모두 밖으로 나가는 연결입니다(제안 D-16). 쓰는 포트는 아웃바운드 7844와 443뿐입니다.
- 호스트 LAN, VM 네트워크, Docker 브리지 대역이 아래 클러스터 대역과 겹치지 않는지 T2에서 확인합니다.

## 4. 주소 대역

모든 대역을 한 표에서 대조합니다. EKS Service 대역은 기본값에 맡기지 않고 클러스터 생성 입력으로 명시합니다.

| 구간 | 대역 | 비고 |
|---|---|---|
| VPC ① 출발지 | `10.10.0.0/16`. 사설 서브넷 2개(`10.10.11.0/24` 2a, `10.10.13.0/24` 2c) | EKS 노드와 Pod(VPC CNI가 이 대역에서 줌). 2c 서브넷은 EKS의 서브넷 2개 요건용(확인 필요). 제안 D-16 |
| VPC ② 플랫폼 | `10.20.0.0/16`. 사설 서브넷 3개(`10.20.11.0/24` 2a, `10.20.12.0/24` 2b, `10.20.13.0/24` 2c) | 공개 서브넷과 노드 공인 IP 없음. VPC ①과 경로 없음(피어링은 비상 토글, 기본 꺼짐). 제안 D-16 |
| `source-aws` Service | `172.20.0.0/16` | EKS 생성 입력으로 명시 |
| `platform-aws` Pod · Service | Pod는 VPC ② 서브넷 주소(VPC CNI) · Service `10.63.0.0/16` | Service 대역은 EKS 생성 입력으로 명시 |
| `target-onprem` Pod · Service | `10.52.0.0/16` · `10.53.0.0/16` | K3s 기본값(`10.42.0.0/16`, `10.43.0.0/16`)에서 바꿈. K3s 설치 인자로 명시 |
| `target-gcp` VPC 서브넷 | `10.30.0.0/24` | D-01 |
| `target-gcp` Pod · Service | `10.72.0.0/16` · `10.73.0.0/16` | D-01 |
| 온프렘 호스트 LAN, VM 네트워크, Docker 브리지 | 확인 필요 | T2에서 라우팅 표로 위 대역과 겹치는지 검사 |
| 회장 네트워크 | 확인 필요 | 11/7 도착 직후 N11, 11/8 오전 재점검 |

환경 사이 링크는 TCP를 받아 다시 여는 프록시입니다(제안 D-16). 그래서 Pod 대역 사이 경로를 쓰지 않습니다. 대역을 나누는 이유는 S2S VPN 폴백이나 경로 기반 연결로 바꿀 때 겹치지 않게 하기 위해서입니다. 서브넷과 보안 그룹의 자세한 것은 `02-network.md` 2절에 있습니다.

## 5. 아키텍처와 이미지

- AWS 두 환경은 arm64(t4g)입니다. EKS 관리형 노드 그룹의 AMI는 AL2023 arm64입니다. 온프렘 Mac의 Lima VM도 arm64입니다.
- GKE 서울 리전에서 arm64 노드를 쓸 수 있는지는 확인하지 못했습니다. 쓸 수 없으면 x86 노드를 쓰고, 이미지를 멀티 아키텍처로 빌드합니다.
- 논리 복제는 CPU 아키텍처와 무관합니다(§12).

## 6. 비용

AWS 단가는 서울 리전 Price List API로 확인한 값입니다(2026-11-02 조회). EKS 제어면 요금은 AWS EKS 요금 페이지(https://aws.amazon.com/eks/pricing/, 2026-11-02 조회)로 다시 확인했습니다. 하루 24시간 기준입니다.

| 자원 | 단가 | 하루 | 상태 |
|---|---|---|---|
| EKS 제어면 × 2(플랫폼, 출발지) | 클러스터마다 시간당 $0.10(표준 지원 판) | $4.80 | 확인. 노드는 EC2 · EBS 요금과 노드에서 제어면으로 가는 AZ 사이 트래픽 요금(소량, 아래 소량 행)을 냄 |
| 출발지 노드 t4g.large × 1 | 시간당 $0.0832 | $2.00 | 확인 |
| 플랫폼 노드 t4g.large × 4(core 3, agents 1) | 대당 시간당 $0.0832 | $7.99 | 확인(2026-11-02 Price List API) |
| 공인 IPv4 | 개당 시간당 $0.005 | 개당 $0.12. 노드 5개면 $0.60 | 확인. 제안 D-16에서는 노드 공인 IP가 없고, NAT의 EIP는 아래 행에 들어 있음 |
| Regional NAT Gateway(제안 D-16) | 활성 AZ마다 시간당 $0.059 + AZ마다 EIP $0.005 | 활성 AZ 4–5개면 약 $6.1–7.7 | 단가 확인. AZ마다 과금은 AWS VPC 요금 페이지로 확인(2026-11-02). 활성 AZ 수는 추정(`02-network.md` 7절) |
| EBS, S3, CloudWatch 로그, 인터넷 송신, NAT 데이터 처리(GB당 $0.059) | 소량 | $1 미만 | 추정 |
| Cloudflare LB | 월 약 $5(종량 아님) | - | 스펙 §8.7 |
| Cloudflare Zero Trust(Tunnel, Access) | Free 플랜 50명. Tunnel 추가 비용 없음 | $0 | 검색 결과 요약으로만 확인(D-16 설계안) |
| GKE(D-01) | - | - | 확인 필요 |
| 온프렘 K3s(Lima) | 팀원 Mac | $0 | 클라우드 비용 없음 |
| LLM API | - | - | 확인 필요 |

AWS만 하루 약 $15.4입니다(소량 항목 제외). 11/3부터 11/8 발표 뒤 삭제까지 6일이면 약 $92입니다. 제안 D-16을 더하면 노드 공인 IPv4($0.60)가 빠지고 NAT(약 $6.1–7.7)가 더해집니다. 그러면 하루 약 $21–22.5, 6일 약 $125–135입니다(추정). 이전 안(D-17, 하루 약 $16.2)보다 하루 약 $0.60 적습니다. 전용 server 3대($3.00)가 빠지고 EKS 제어면($2.40)이 더해지기 때문입니다(D-21). 어느 쪽이든 팀 지원금(₩500,000) 안에 들어갑니다. 쓰지 않는 시간에는 노드 그룹을 0대로 줄입니다. EKS 제어면은 멈출 수 없어 클러스터를 지울 때까지 과금됩니다. 모든 자원에 `component` 태그를 답니다(NFR-8).

**삭제 뒤 남을 수 있는 것.** `terraform destroy` 뒤에 아래를 확인합니다(`docs/runbooks/destroy.md`).

| 남는 것 | 이유 | 대응 |
|---|---|---|
| KMS 키, Secrets Manager 비밀 | 삭제 대기 기간이 있음. 대기 중 과금은 없음(전문가 리뷰 확인) | 대기 종료 확인 |
| S3 Object Lock 버킷(P1) | 보존 기간 안에는 지울 수 없음 | governance 모드, 짧은 보존 기간 |
| CloudWatch 로그 그룹 | Terraform 밖에서 생기기도 함 | 태그로 찾아 지움 |
| Cloudflare LB | 월 단위 과금 | 발표 뒤 해지 |
| Cloudflare Tunnel, Access 앱, 서비스 토큰(제안 D-16) | `edge_link` 밖에서 만들었거나 destroy가 실패한 경우 | 11/9 토큰 폐기 때 목록과 대조해 지움 |
| 공인 IPv4, EBS 스냅숏 | destroy 범위 밖에서 만든 경우 | 태그로 찾아 지움 |
