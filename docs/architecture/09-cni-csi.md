# CNI와 CSI

클러스터 네트워크(CNI), 네트워크 정책 집행, 스토리지(CSI)를 환경마다 정합니다. 기준은 팀 리드 결정(2026-11-02)입니다. 이 결정으로 RKE2와 Calico는 모든 환경에서 빠집니다. 플랫폼과 출발지는 EKS, 온프렘 목적지는 K3s, GCP 목적지는 GKE입니다.

원칙은 셋입니다.

1. 각 플랫폼이 지원하는 기본 CNI를 쓰고, NetworkPolicy 집행을 켭니다. 플랫폼마다 다른 CNI를 직접 운영하지 않습니다.
2. 네트워크 기본선은 Kyverno 정책 하나로 모든 클러스터에 같게 겁니다. CNI 제품 이름이 아니라 시험으로 동등성을 증명합니다.
3. StorageClass 이름은 어디서나 `bravo-db`와 `bravo-general`입니다. 그래서 CNPG, Rook-Ceph 매니페스트를 고치지 않고 옮깁니다.

고객은 Kubernetes를 모른다고 가정합니다. 그래서 환경 입력은 `name`, `region`, `size`(small, medium)뿐입니다. CNI 플래그나 StorageClass 값은 모듈 안에 안전한 기본값으로 둡니다(`infra/README.md`).

## 1. 결정 표

| 항목 | `platform-aws` | `source-aws` | `target-onprem` | `target-gcp` (D-01) |
|---|---|---|---|---|
| 클러스터 | EKS, 3 AZ | EKS | K3s, Lima VM 2대(server 1, agent 1) | GKE Autopilot, 지역형 |
| K8s 판 | 1.35 | 1.35 | v1.35.9+k3s1 | 1.35(STABLE 채널) |
| CNI | Amazon VPC CNI 관리형 add-on | 같음 | flannel VXLAN(K3s 내장) | GKE Dataplane V2(관리형) |
| 정책 집행 | VPC CNI 네트워크 정책(노드 에이전트, eBPF) | 같음 | K3s 내장 네트워크 정책 컨트롤러(kube-router) | Dataplane V2 |
| 기본선 정책 | Kyverno(2절) | 같음 | 같음 | 같음 |
| 캡슐화 | 없음(VPC 주소를 Pod에 줌) | 없음 | VXLAN, UDP 8472 | 없음(VPC 네이티브, GKE 문서 기준) |
| CSI | EBS CSI 관리형 add-on, EKS Pod Identity | 같음 | TopoLVM 0.42.0, 두 번째 VM 디스크의 VG `bravo-vg` | PD CSI(GKE 기본) |
| 디스크 종류 | gp3, KMS CMK 암호화 | 같음 | 노드 로컬 LVM(thick) | pd-balanced, CMEK |
| 스냅숏 | snapshot-controller add-on + VolumeSnapshotClass | 같음 | 없음(4절) | VolumeSnapshotClass |
| IMDS 차단 | 노드 그룹 IMDSv2 필수, hop limit 1 | 같음 | 해당 없음(메타데이터 서버 없음) | Workload Identity 메타데이터 서버 |
| Istio ambient | 있음(`07` 4절) | 없음 | 없음 | 없음 |
| 실행 | validate만 | validate만 | 로컬에서 실제 apply | validate만 |

두 StorageClass의 공통 값은 아래와 같습니다.

| 이름 | 용도 | 파일 시스템 | 바인딩 | 확장 | 회수 |
|---|---|---|---|---|---|
| `bravo-db` | CNPG 데이터와 WAL | xfs | `WaitForFirstConsumer` | 허용 | `Delete` |
| `bravo-general` | 일반 PVC(앱 · 관측 등). 객체 저장은 Rook-Ceph RGW(D-22) | ext4 | `WaitForFirstConsumer` | 허용 | `Delete` |

- 회수 정책 `Delete`는 데모 환경용입니다. 운영은 `Retain`을 씁니다. PVC를 지우면 데이터도 지워지기 때문입니다.
- `WaitForFirstConsumer`는 Pod가 놓일 노드가 정해진 뒤 볼륨을 만듭니다. EBS와 PD는 AZ에, TopoLVM은 노드에 묶이기 때문입니다.
- GKE Autopilot에서 xfs를 쓸 수 있는지는 확인하지 못했습니다. 안 되면 GKE의 `bravo-db`만 ext4로 둡니다.
- K3s 기본 StorageClass(`local-path`)는 끕니다. 용량 기반 스케줄링과 볼륨 확장이 없기 때문입니다.

## 2. 네트워크 기본선(Kyverno)

리허설과 검증 네임스페이스는 Trial마다 생겼다가 지워집니다(`07-runtime-mesh-observability.md` 2절). 그 네임스페이스마다 정책을 손으로 거는 방식은 한 번만 잊어도 격리가 뚫립니다. 그래서 클러스터 범위의 Kyverno 정책이 기본선을 만듭니다.

| 정책 | 대상 | 하는 일 |
|---|---|---|
| 생성(generate) | 라벨 `bravo.io/tier`가 `rehearsal`이나 `verify`인 네임스페이스 | 수신 · 송신 기본 거부 NetworkPolicy와 kube-dns로 가는 DNS(UDP, TCP 53) 허용 NetworkPolicy를 만듦. `synchronize: true`라 지우거나 고쳐도 다시 만듦 |
| 검증(validate) | 위 정책이 만든 NetworkPolicy | Kyverno가 아닌 주체의 삭제와 수정을 거부 |

- 같은 정책을 EKS, GKE, K3s에 똑같이 겁니다. NetworkPolicy는 K8s 표준 API라 세 CNI가 모두 집행합니다.
- 리허설에 필요한 허용(D-12, `05-agent-rehearsal.md`)은 네임스페이스별 NetworkPolicy가 더합니다. NetworkPolicy는 합집합이므로 기본 거부 위에 허용만 늡니다.
- 판은 Kyverno 1.19.2, Helm 차트 3.9.2입니다(2026-11-02 차트 색인 확인).
- 정책은 `kyverno.io/v1` ClusterPolicy로 씁니다. Kyverno 1.19는 이 옛 정책 종류에 폐기 예고 경고를 냅니다(1.19.0 릴리스 노트). `policies.kyverno.io/v1`의 GeneratingPolicy와 ValidatingPolicy로 옮기는 일은 P1입니다.
- Kyverno background controller의 기본 ClusterRole은 NetworkPolicy 생성 권한을 이미 갖습니다(차트 3.9.2 `values.yaml` 확인).

**메타데이터 서버(IMDS) 차단.** RKE2와 Calico를 뺐으므로 Calico GlobalNetworkPolicy로 `169.254.169.254`를 막지 않습니다. 대신 플랫폼마다 다음을 씁니다.
- EKS 노드 그룹은 IMDSv2 필수, hop limit 1입니다. Pod 네트워크 네임스페이스에서 나간 요청은 홉 하나를 더 써서 응답을 받지 못합니다(`02-network.md` N14).
- GKE는 Workload Identity 메타데이터 서버가 노드 메타데이터를 가립니다.
- 온프렘 Lima VM에는 클라우드 메타데이터 서버가 없습니다.

## 3. 판과 고른 이유

| 구성요소 | 판 | 근거(2026-11-02 조회) |
|---|---|---|
| K8s | 1.35 | 아래 표 |
| K3s | v1.35.9+k3s1 | K3s 채널 `v1.35`. 바이너리와 SHA-256(`sha256sum-arm64.txt`)으로 고정 설치 |
| TopoLVM | 0.42.0, 차트 17.3.0 | GitHub 릴리스 2026-10-08. 이미지 `ghcr.io/topolvm/topolvm-with-sidecar:0.42.0`에 arm64 있음 |
| Kyverno | 1.19.2, 차트 3.9.2 | GitHub 릴리스 2026-10-20 |
| EBS CSI, VPC CNI, snapshot-controller | EKS 관리형 add-on | 판은 `infra/modules/cluster/eks`에서 고정 |
| PD CSI, Dataplane V2 | GKE가 관리 | Autopilot이 켬 |

**K8s 1.35를 고른 이유.** 세 플랫폼에 공통으로 들어가는 minor는 1.35입니다.

| 확인 항목 | 1.34 | 1.35 | 1.36 |
|---|---|---|---|
| EKS 표준 지원(AWS 문서: 1.34–1.37) | 지원 | 지원 | 지원 |
| GKE STABLE 채널(GKE 출시 일정) | 있음 | 있음. 표준 지원 종료 2027-04-11 | 2026-11-11쯤 들어옴 |
| K3s 채널 | v1.34.12+k3s1 | v1.35.9+k3s1 | v1.36.5+k3s1 |
| CNPG 1.30(1.34–1.36) | 지원 | 지원 | 지원 |
| Istio 1.31(1.32–1.37) | 지원 | 지원 | 지원 |
| kube-bench 자동 판 대응(1.35까지) | 됨 | 됨 | 안 됨 |

- 1.36은 오늘 기준 GKE STABLE 채널에 없습니다. kube-bench 자동 대응도 없습니다.
- 1.34는 셋 가운데 지원 종료가 가장 이릅니다(추정, GKE 일정표 기준).
- `06-tech-stack.md`의 기존 제안(세 환경 1.35)과도 맞습니다.

**TopoLVM을 고른 이유.** 결정 조건은 "cert-manager가 필요하거나, arm64 이미지가 없거나, 2026년 릴리스가 없으면 OpenEBS LocalPV-LVM"이었습니다. 셋 다 해당하지 않음을 확인했습니다.
- 차트 17.3.0은 Pod 변경 webhook이 기본으로 꺼져 있습니다. 이때 controller는 `--enable-webhooks=false`로 뜨고 인증서가 필요 없습니다(차트 템플릿 확인).
- 매니페스트 목록에 arm64가 있습니다(GHCR 매니페스트 조회).
- 2026년에 릴리스가 이어집니다(0.40.2, 0.41.0, 0.41.1, 0.42.0).
- CSIStorageCapacity로 노드별 남은 용량을 스케줄러에 알립니다. 그래서 VG가 찬 노드에 Pod를 놓지 않습니다.

## 4. 대안과 쓰지 않는 이유

| 대안 | 쓰지 않는 이유 |
|---|---|
| K3s 위 Calico | 운영자 하나가 더 늡니다. flannel과 K3s 정책 컨트롤러로 NetworkPolicy가 집행됩니다. Calico의 GlobalNetworkPolicy 대신 세 클러스터에 같게 걸리는 Kyverno 기본선을 씁니다 |
| Canal | RKE2 기본값입니다. RKE2를 뺐으므로 해당이 없습니다. 데몬도 둘(flannel, Calico)입니다 |
| Cilium 직접 운영 | Istio ambient와 신원 · mTLS 기능이 겹칩니다. Cilium이 필요한 곳은 GKE Dataplane V2가 관리형으로 줍니다 |
| Kube-OVN | OVN 중앙 Raft와 노드마다 OVS가 늡니다. VPC · 서브넷 기능이 네임스페이스와 정책 모델과 겹칩니다. KubeVirt나 L2 테넌트 VPC가 필요할 때만 맞습니다(P2) |
| 정책 컨트롤러 없는 flannel | NetworkPolicy를 집행하지 않습니다. K3s에서 `--disable-network-policy`를 쓰지 않는 이유입니다 |
| K3s `local-path` | 용량 추적과 볼륨 확장이 없습니다. 끕니다 |
| Longhorn, Rook-Ceph | 복제 블록 스토리지 위에 CNPG를 두면 복제가 두 겹이 됩니다. Postgres가 스스로 복제합니다 |
| OpenEBS LocalPV-LVM | 대체안입니다. TopoLVM이 조건을 모두 만족해 쓰지 않습니다(3절) |

## 5. 실패 유형과 방어 장치

| 실패 | 증상 | 방어 |
|---|---|---|
| Kyverno가 멈춤 | 새 리허설 네임스페이스에 기본선이 생기지 않음 | 리허설 서비스가 네임스페이스를 만든 직후 격리 시험을 돌리고, 실패하면 BLOCK(`05-agent-rehearsal.md` 3절) |
| 기본선 삭제나 수정 | 리허설 Pod가 밖으로 나감 | 검증 정책이 거부. 그래도 지워지면 `synchronize`가 다시 만듦 |
| CNI가 정책을 집행하지 않음 | 정책이 있어도 통과 | 모듈이 집행 플래그를 고정. `verify.sh`가 대조군과 거부 경로를 함께 시험 |
| EKS 정책 적용 전 구간 | 새 Pod가 정책 적용 전에 통신 | VPC CNI 네트워크 정책 strict 모드(`06-tech-stack.md` 2절). 구현 확인 필요 |
| MTU 불일치 | 작은 패킷은 되고 큰 패킷만 사라짐. DB 복제가 멈춘 듯 보임 | flannel이 하위 인터페이스 MTU에서 50을 뺌. `verify.sh`가 DF 핑으로 경계를 시험(6절) |
| VG나 디스크가 참 | PVC가 Pending, 쓰기 실패 | TopoLVM 용량 기반 스케줄링. 네임스페이스별 저장 할당량. VG 여유 20% 미만 경보 |
| 로컬 볼륨의 노드를 잃음 | 그 노드의 TopoLVM 볼륨을 잃음 | CNPG가 다른 인스턴스에서 다시 만듦. 온프렘 데모는 Mac 한 대라 장애 영역이 하나임 |
| EBS가 다른 AZ에 생김 | Pod가 볼륨을 붙이지 못함 | `WaitForFirstConsumer`, CNPG 인스턴스 AZ 분산(`01-environments.md` 2절) |
| KMS 키 권한 없음이나 삭제 예약 | 볼륨 생성과 연결 실패 | IAM 정책을 그 키 하나로 좁힘. 키 삭제 대기 기간 동안 경보 |
| WAL이 데이터 볼륨을 채움 | DB가 쓰기를 멈춤 | CNPG `walStorage`로 WAL 볼륨을 따로 둠(권장) |
| conntrack 표가 참 | 새 연결이 무작위로 실패 | 사용률 80% 경보 |
| Pod IP 고갈 | Pod가 `ContainerCreating`에 멈춤 | EKS는 VPC CNI가 서브넷 IP를 씀. 서브넷 IP 사용률 경보. K3s는 노드 Pod 대역 /24(254개)가 kubelet 상한 110보다 큼 |
| 회수 정책 `Delete` | PVC를 지우면 데이터가 사라짐 | 데모 환경에서만 씀. 운영은 `Retain` |

**저장 할당량.** 네임스페이스마다 ResourceQuota를 둡니다.
- `requests.storage`와 `persistentvolumeclaims` 수를 묶습니다.
- 클래스별 상한(`bravo-db.storageclass.storage.k8s.io/requests.storage`)도 둡니다.
- 리허설 네임스페이스의 할당량은 리허설 프로비저너가 만듭니다(`07-runtime-mesh-observability.md` 3절).

**경보 규칙.** 규칙 정의는 이 표가 기준입니다. 수집 백엔드에 거는 일은 관측 담당입니다(`07-runtime-mesh-observability.md` 6절).

| 경보 | 식 | 기준 |
|---|---|---|
| 볼륨 사용률 | `kubelet_volume_stats_used_bytes / kubelet_volume_stats_capacity_bytes` | 0.8 초과 10분 |
| conntrack 사용률 | `node_nf_conntrack_entries / node_nf_conntrack_entries_limit` | 0.8 초과 10분 |
| TopoLVM VG 여유 | `topolvm_volumegroup_available_bytes / topolvm_volumegroup_size_bytes` | 0.2 미만 |
| EKS Pod IP | `awscni_assigned_ip_addresses / awscni_total_ip_addresses` | 0.8 초과 |

**CNPG WAL 볼륨 권장.** CNPG Cluster에 `storage`와 `walStorage`를 따로 둡니다. 둘 다 `storageClass: bravo-db`입니다. WAL이 늘어도 데이터 볼륨이 차지 않습니다. 논리 복제 슬롯이 WAL을 붙잡는 상황(`02-network.md` 1절 F7)에서 특히 필요합니다.

## 6. MTU 계산

**K3s(flannel VXLAN).** VXLAN은 패킷마다 50바이트를 더합니다.

| 머리 | 바이트 |
|---|---|
| 바깥 IPv4 | 20 |
| UDP | 8 |
| VXLAN | 8 |
| 안쪽 이더넷 | 14 |
| 합 | 50 |

- 하위 인터페이스 MTU가 1500이면 `flannel.1`과 Pod `eth0`은 1450입니다.
- 안쪽 ICMP 데이터 최대 크기는 1450 − 20(IP) − 8(ICMP) = 1422바이트입니다.
- 그래서 DF를 켠 핑은 1422바이트에서 성공하고, 1423바이트에서 실패해야 합니다.
- 실제 값은 8절 실행 결과에 둡니다.

**EKS(VPC CNI).** 캡슐화가 없습니다. VPC 안 ENI MTU는 9001입니다. 인터넷으로 나가는 경로는 1500입니다.

**GKE(Dataplane V2).** VPC 네이티브 경로라 VPC MTU를 그대로 씁니다. 새 VPC의 기본값은 1460입니다(GCP 문서 기준, 오늘 다시 조회하지 않음).

**환경 사이.** 환경 사이 링크는 TCP를 받아 다시 여는 프록시입니다(제안 D-16, `02-network.md` 4절). 그래서 클러스터마다 MTU가 달라도 경로 MTU 문제가 환경을 넘지 않는다고 봅니다(추정).

## 7. 포트

| 환경 | 포트 | 방향 | 용도 |
|---|---|---|---|
| `target-onprem` | TCP 6443 | agent에서 server로 | K3s supervisor와 API |
| `target-onprem` | UDP 8472 | 노드 사이 | flannel VXLAN(Linux 기본 포트) |
| `target-onprem` | TCP 10250 | 노드 사이 | kubelet |
| `target-onprem` | TCP 2379–2380 | server 사이 | 내장 etcd. server가 하나라 지금은 자기 자신만 |
| `target-onprem` | 아웃바운드 443, 7844 | VM에서 밖으로 | 이미지, Tunnel(D-16). 인터넷에서 들어오는 포트 없음 |
| EKS 두 환경 | EKS가 관리 | 노드와 제어면 사이 | 클러스터 SG와 노드 SG. 캡슐화 포트 없음 |
| `target-gcp` | GKE가 관리 | 노드와 제어면 사이 | 사설 노드 |

- 이 표는 RKE2의 포트(9345, Canal 8472, Calico 4789)를 대신합니다.
- `01`과 `02`의 포트 표는 별도 정리 작업에서 고칩니다.

### Istio ambient와의 관계

메시는 플랫폼 EKS에만 있습니다(`07-runtime-mesh-observability.md` 4절). 고객 클러스터에는 P0에서 메시가 없습니다.
- K3s에 ambient를 넣을 때는 Istio의 K3s 플랫폼 설정(`global.platform=k3s`)을 씁니다. 지금은 해당이 없습니다.
- EKS에서는 VPC CNI 네트워크 정책과 ambient의 공존을 T7에서 실측합니다. HBONE 15008과 프로브 주소 `169.254.7.127`이 대상입니다.

## 8. 시험 항목

`infra/envs/target-onprem/scripts/verify.sh`가 아래를 차례로 시험하고 항목마다 PASS나 FAIL을 냅니다. 클라우드 환경은 같은 시험을 apply 승인 뒤에 돌립니다.

| 번호 | 시험 | 기대 |
|---|---|---|
| V1 | 노드 상태 | server와 agent가 모두 Ready |
| V2 | flannel 백엔드 | VXLAN, 노드마다 `flannel.1` 있음 |
| V3 | 다른 노드의 Pod 사이 통신 | 성공 |
| V4 | NetworkPolicy 허용 경로(대조군) | 성공 |
| V5 | NetworkPolicy 거부 경로 | 실패. V4가 성공해야 차단으로 셈 |
| V6 | DF 핑 MTU 경계 | 1422바이트 성공, 1423바이트 실패 |
| V7 | `bravo-db` PVC | Pod가 놓인 노드에서 바인딩 |
| V8 | 볼륨 확장 | 요청 크기를 늘리면 파일 시스템 크기가 늚 |
| V9 | Kyverno 기본선 생성 | `bravo.io/tier=rehearsal` 네임스페이스에 기본 거부와 DNS 허용 정책이 생김 |
| V10 | 기본선 삭제 시도 | 거부됨 |
| V11 | 기본 StorageClass | `local-path` 없음 |

## 9. 실행 결과

<!-- RESULTS:target-onprem -->

## 10. 확인할 것

- GKE Autopilot에서 Kyverno webhook과 정책이 제한 없이 도는지 확인합니다. 안 되면 GKE Standard 지역형으로 바꿉니다.
- GKE Autopilot의 `bravo-db` xfs 지원을 확인합니다.
- EKS VPC CNI 네트워크 정책의 strict 모드가 `cluster/eks` 구현에 켜져 있는지 validate 출력으로 확인합니다.
- Kyverno 정책을 `policies.kyverno.io/v1`으로 옮깁니다(P1).
- EKS와 GKE의 같은 시험(V3–V10)은 apply 승인 뒤에 돌립니다. 지금은 `terraform validate`까지만 했습니다.
- 스펙 §7.2는 목적지를 RKE2(CIS)로 둡니다. K3s로 바꾼 것은 편차 등록이 필요합니다. 등록은 별도 정리 작업이 맡습니다.

## 근거

- K3s 채널과 체크섬: `https://update.k3s.io/v1-release/channels`, K3s v1.35.9+k3s1 릴리스의 `sha256sum-arm64.txt`(2026-11-02 조회)
- EKS 판: AWS 문서 "Understand the Kubernetes version lifecycle on EKS"(2026-11-02 조회)
- GKE 판: GKE 출시 일정 문서(2026-11-02 조회)
- CNPG 지원 표: `cloudnative-pg/docs/src/supported_releases.md`(2026-11-02 조회)
- Istio 지원 표: istio.io "Supported Releases"(2026-11-02 조회)
- TopoLVM: GitHub 릴리스, 차트 17.3.0 `values.yaml`과 템플릿(2026-11-02 조회)
- Kyverno: 차트 색인 3.9.2, 1.19.0 릴리스 노트(2026-11-02 조회)
- kube-bench: `cfg/config.yaml`의 판 대응표(2026-11-02 조회)
