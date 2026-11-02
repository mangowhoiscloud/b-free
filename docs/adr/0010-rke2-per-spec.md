# 0010. RKE2는 스펙 r2대로: 온프렘 Ubuntu VM + RKE2(CIS), 플랫폼 EC2 1대 + RKE2 단일 노드

- 상태: 채택(ADR 0004를 대체). RKE2 판 고정과 CNI · 블록 저장의 구체 방법은 **제안**(실무, 팀장 확인 대기)
- 날짜: 2026-11-02(21:10 KST 결정)
- 정한 사람: 류지환(팀장). 코디네이터가 전한 답: 범위 "스펙 그대로 전부", 되돌릴 편차 "RKE2 관련만"
- 스펙 위치: FR-3, §4, §6 S1(기본 흐름 4, 예외 4a, 인수 조건), §7.2, §7.3, §8.4, §8.5, §8.8, §9, T2, T5, T7, D-21(부분 철회), D-17
- 근거 문서: 정본 사본 `docs/spec/spec-v0.1-r2-snapshot.html`, `docs/spec/DEVIATIONS.md` D-21 첫머리, `docs/spec/TECH-STACK-SPEC.md` 1절, RKE2 GitHub 릴리스 목록(2026-11-02 21:1x KST 조회)

## 맥락

ADR 0004(같은 날 낮)는 RKE2를 모든 환경에서 빼고 플랫폼 EKS, 온프렘 K3s, GCP GKE로 갔습니다. 팀장은 21:10 KST에 RKE2를 스펙 r2대로 따르기로 정했습니다. 스펙 r2의 해당 문장은 이렇습니다.
- §7.2: 플랫폼은 "EC2 1대 + RKE2 단일 노드", 이유는 "EKS 대신 EC2로 비용 절감". 온프렘은 "Ubuntu VM + RKE2(CIS)".
- §6 S1 예외 4a: RKE2 CIS 프로필이 조건 미충족으로 시작을 거부하면, 공통 cloud-init의 조건(sysctl, etcd 사용자)을 고치고 재설치합니다.
- T2 완료 기준: 새 VM에서 RKE2가 CIS 프로필로 뜨고 kube-bench 결과가 나옵니다.
- T7: VPC ②에 EC2 1대 + RKE2(T2와 같은 cloud-init).

되돌리는 범위는 "RKE2 관련만"입니다. 그래서 GKE 목적지, Cilium + Hubble, Rook-Ceph RGW, Cloudflare 링크, 플랫폼 OpenBao 하나, Agent SDK LLM 경로는 그대로 둡니다.

## 선택지

| 안 | 장점 | 단점 |
|---|---|---|
| A. ADR 0004 유지(플랫폼 EKS, 온프렘 K3s) | 이미 떠 있음(EKS `bravo-platform`, K3s `bravo-onprem`). 관리형 제어면 | 스펙과 다름. 팀장이 되돌림 |
| B. 스펙 r2 그대로: 온프렘 Ubuntu VM + RKE2(CIS), 플랫폼 EC2 1대 + RKE2 단일 노드, 공통 cloud-init. 출발지 EKS와 GKE 목적지는 유지 | 스펙 문장 · 인수 조건(S1 4a 포함)과 같음. 플랫폼 비용이 작음(§7.2) | 플랫폼이 노드 하나라 단일 장애점. 제어면(etcd, 인증서)을 우리가 운영 |
| C. 플랫폼 3-AZ RKE2 HA(D-17) | 가용성 | 스펙 §7.2와 다름. "스펙 그대로 전부"와 맞지 않음(해석) |

## 결정

B를 고릅니다.

| 환경 | 클러스터 | 설치 |
|---|---|---|
| 온프렘 목적지 `target-onprem` | Ubuntu VM + RKE2, `profile: cis` | 공통 cloud-init(T2). CIS 조건(sysctl, etcd 사용자)을 cloud-init에 고정 |
| 플랫폼 | VPC ②의 EC2 1대 + RKE2 단일 노드 | 같은 cloud-init(T7) |
| 출발지 `source-aws` | Amazon EKS(바뀌지 않음) | `infra/envs/source-aws` |
| GCP 목적지 `target-gcp` | GKE(D-21에 남는 범위) | `infra/envs/target-gcp` |

**판(제안).** K8s minor는 세 환경 모두 1.36입니다. RKE2는 v1.36.5+rke2r1로 고정합니다. 1.36 줄의 마지막 안정 릴리스(GitHub 릴리스, 2026-09-30 게시, 2026-11-02 조회)이고, v1.36.6-rc1은 사전 릴리스라 쓰지 않습니다. 설치 파일은 그 릴리스의 체크섬 파일로 sha256을 고정합니다. 이 값은 cloud-init을 쓸 때 인프라 갈래가 넣습니다(아직 없음).

**유지하는 결정을 RKE2에 맞추는 법(제안).**
- CNI: Cilium + Hubble(ADR 0008)을 유지합니다. RKE2 내장 `cni: cilium` 차트를 쓸지, `cni: none`으로 두고 랜딩 존이 Cilium 1.20.4를 깔지 정해야 합니다. 판을 세 환경에서 맞추려면 뒤쪽입니다.
- 블록 저장: RKE2에는 K3s의 `local-path`가 기본으로 없습니다. `bravo-db`, `bravo-general`의 바탕이 필요합니다. 후보는 Rook-Ceph RBD(D-22)이고, 임시로 local-path-provisioner를 따로 설치하는 안이 있습니다.
- 스펙 T2의 Tailscale 설치는 하지 않습니다. 연결은 D-16(Cloudflare 링크)을 유지합니다. 되돌린 범위가 "RKE2 관련만"이기 때문입니다.
- 비밀은 플랫폼 OpenBao 하나(D-26)입니다. 스펙 §7.2의 "목적지 OpenBao"는 따르지 않습니다.

## 결과

- 쉬워지는 것: 스펙 문장과 인수 조건(S1 기본 흐름 4, 예외 4a, T2 · T7 완료 기준)을 그대로 시험할 수 있습니다. 스펙 v0.2에서 RKE2 문구를 고칠 필요가 없습니다.
- 어려워지는 것: 플랫폼 제어면(etcd, 인증서, 업그레이드)을 우리가 운영합니다. 플랫폼 노드 하나가 멈추면 상태 머신과 Executor가 멈춥니다. 상태가 불명확하면 SAFE_HOLD라는 원칙(NFR-3)은 그대로입니다.
- 비용: 스펙 §7.2의 단일 노드는 하루 약 $4입니다(D-17 추정, D-21 비용 표). EKS 제어면 요금(시간당 $0.10)은 플랫폼에서 빠지고 출발지에만 남습니다.
- 통제 비교: 출발지 EKS와 목적지 RKE2를 같은 뜻의 항목으로 비교합니다(T5, `04-governance.md` 1절). kube-bench는 1.36 자동 대응이 없어 벤치마크 이름을 명시합니다.
- 이미 만든 자원: 결정 전에 EKS `bravo-platform`(20:30 생성, 20:56 플랫폼 서비스 Running)과 Lima K3s `bravo-onprem`(v1.36.5+k3s1)이 떴습니다. 정리와 이전은 인프라 갈래가 정하고, 클라우드 자원 삭제(`terraform destroy`)는 사람 승인 뒤에 합니다. 컨텍스트 이름 `bravo-onprem`은 RKE2에서도 그대로 씁니다(`deploy/CLOUD-E2E-CONTRACT.md` 2절).
- 데모: S1 예외 4a(CIS 프로필 시작 거부, cloud-init 조건 수정, 재설치)가 각본에 들어갑니다. 실제 실행 기록이 생기기 전에는 "준비 중"입니다(`docs/demo/SCRIPT.md`).
- 되돌리려면: 클러스터 모듈(`infra/modules/cluster/*`)과 cloud-init만 바꿉니다. 랜딩 존은 공급자 overlay(`rke2`)의 CNI · 저장소 값만 다릅니다(ADR 0006).
