# 기술 스택

세 칸으로 나눕니다. **고정**은 스펙에 적힌 것이고, 바꾸려면 DEVIATIONS를 거칩니다. **제안**은 스펙이 정하지 않은 구현 선택이고, 영역 담당이 정합니다(`docs/spec/README.md` 규칙 5). **미정**은 스펙 §12가 정할 사람을 지정한 것입니다. 판 번호는 스펙이 고정하지 않았으므로 모두 제안입니다.

## 1. 인프라와 클러스터

| 항목 | 고르는 것 | 칸 | 근거 |
|---|---|---|---|
| IaC | Terraform + cloud-init | 고정 | §8.8, NFR-7 |
| Terraform 판 | 팀 전원이 같은 판으로 고정. `required_version`으로 강제. 로컬 Mac의 1.5.7은 올려야 함 | 제안 | T1에서 정함 |
| Terraform 상태 | S3 백엔드와 상태 잠금. T1에서 가장 먼저 부트스트랩. DB 비밀번호 같은 비밀은 Terraform이 만들지 않고 CNPG와 ESO가 클러스터 안에서 만듦 | 제안 | NFR-6 |
| 목적지 K8s | 온프렘은 K3s(CIS 강화 가이드 플래그). Lima VM 2대(server 1, agent 1). 팀 리드 결정(2026-11-02)으로 RKE2를 뺌. 스펙 §7.2(RKE2 CIS)와 다르므로 편차 등록이 필요함(별도 정리 작업) | 변경(팀 리드). 스펙은 RKE2 | §7.2, T2, `09-cni-csi.md` |
| 출발지 K8s | EKS | 고정 | §7.2, T6 |
| 플랫폼 K8s | EKS(관리형 제어면, 3 AZ). 관리형 노드 그룹은 역할별로 둠: core는 3 AZ, agents는 taint `pool=agents:NoSchedule`. 팀 리드 결정(2026-11-02). 스펙 §7.2(EC2 + RKE2)와 다름 | 변경(팀 리드). 스펙은 EC2 + RKE2 | §7.2, T7, `09-cni-csi.md` |
| K8s 판 | 모든 환경 1.36. EKS 1.36(`bravo-source`가 1.36으로 생성됨, 내릴 수 없음), GKE REGULAR 채널 1.36, K3s v1.36.5+k3s1. CNPG 1.30은 1.34–1.36, Istio 1.31은 1.32–1.37을 지원. kube-bench 자동 대응표는 1.35까지라 벤치마크 이름을 명시함(2026-11-02 조회) | 실무 결정, 팀장 확인 대기(`TECH-STACK-SPEC.md` 3절 7번, ADR 0004) | `09-cni-csi.md` 3절 |
| 노드 아키텍처 | arm64로 통일. GCP에서 안 되면 멀티 아키텍처 이미지 | 미정(인프라 · CI) | §12 |
| GCP 목적지 | GKE Autopilot(지역형). Dataplane V2, PD CSI, Workload Identity. Autopilot이 요구를 깨면 Standard 지역형. validate만 함 | 제안(D-01), 팀 리드 결정 | `09-cni-csi.md` |

## 2. 데이터와 보안 구성요소

| 항목 | 고르는 것 | 칸 | 근거 |
|---|---|---|---|
| DB | PostgreSQL 16 이상(고정), CNPG(고정). CNPG 판은 1.30(제안) | 고정, 판은 제안 | §8.10, §7.3 |
| 커넥션 풀 | PgBouncer, transaction 모드(고정). CNPG Pooler가 아닌 단독 StatefulSet, replica 1(제안). Pod 이름이 고정돼야 Executor의 `pods/exec`를 그 Pod 하나로 묶을 수 있음. 대상 설정 두 벌을 ConfigMap 디렉터리로 마운트하고, 활성 파일은 emptyDir | 고정, 배치는 제안 | §8.6, `03-migration.md` 4.1 |
| 비밀 | 플랫폼 OpenBao 하나만 보관, 고객 클러스터는 ESO로 받음. 출발지 Secrets Manager는 옮겨 올 대상 | 결정(D-26) | §7.3, T9 |
| 암호화 키 | OpenBao transit(목적지), KMS(출발지) | 고정 | §7.3 |
| 객체 저장 | 같은 S3 계약, 환경별 엔진(온프렘 Rook-Ceph RGW, EKS S3, GKE Cloud Storage). 스펙의 SeaweedFS는 D-22로 뺌 | 결정(D-22) | §12 |
| 정책 강제 | Kyverno 1.19.2(차트 3.9.2). 네트워크 기본선 생성 정책은 모든 클러스터에 P0로 깖. 나머지 규칙은 P1 | 고정(P1), 기본선은 제안(P0) | §7.3, `09-cni-csi.md` 2절 |
| NetworkPolicy | 플랫폼 기본 CNI의 집행을 씀. EKS는 VPC CNI 네트워크 정책을 strict 모드로 켬. standard 모드는 Pod 시작 직후 정책 적용 전 허용 구간이 있음. GKE는 Dataplane V2. K3s는 flannel VXLAN과 내장 네트워크 정책 컨트롤러. 리허설 · 검증 네임스페이스의 기본 거부와 DNS 허용은 Kyverno가 만듦. DNS 예외와 거부 기록 수집을 함께 설정 | 제안 | T3, T6, `09-cni-csi.md` |
| 스토리지(CSI) | StorageClass 이름을 `bravo-db`, `bravo-general`로 통일. EKS는 EBS CSI 관리형 add-on(gp3, KMS CMK, Pod Identity). GKE는 PD CSI(pd-balanced, CMEK). K3s는 TopoLVM 0.42.0(두 번째 VM 디스크의 VG `bravo-vg`). 모두 `WaitForFirstConsumer`, 확장 허용. 데모는 회수 `Delete`, 운영은 `Retain` | 제안 | `09-cni-csi.md` 1절 |
| 감사 로그 | P0는 플랫폼 DB의 추가 전용 표. P1은 S3 Object Lock. 데모 버킷은 governance 모드와 짧은 보존 기간으로 둠. compliance 모드는 보존 기간이 끝나기 전 누구도 지울 수 없어 destroy 뒤에도 과금이 남음 | 제안(P0), 고정(P1) | FR-14, T24 |

## 3. 네트워크와 진입점

| 항목 | 고르는 것 | 칸 | 근거 |
|---|---|---|---|
| 사용자 진입 | Cloudflare LB(pool 2개) + 클러스터별 Tunnel | 고정 | §8.7 |
| 환경 사이 연결 | 스펙은 Tailscale ACL. 제안 D-16은 Cloudflare Tunnel + Access 서비스 토큰(`cloudflared access tcp` 링크) | 고정(스펙). 바꾸는 제안 D-16 | §7.5, `02-network.md` 4절 |
| 서비스 노출과 링크 | 노출하는 클러스터는 관리 Tunnel(`cf-edge`, 복제본 2, 구성은 클러스터 안 파일). 소비하는 클러스터는 서비스마다 링크(`bravo-link`, Deployment 복제본 2 + zone anti-affinity + PDB, Service). Pod Security `restricted`, 예외 없음 | 제안(D-16) | `02-network.md` 4절, 6절 |
| 커넥터 판 | `cloudflared`는 11/3에 N12를 통과한 가장 새 판으로 고정하고 이미지 digest도 고정. 보안 수정이 옛 고정 판(2026.5.1)보다 우선. 고정한 판: (11/3 N12 뒤 기록). digest: (11/3 N12 뒤 기록) | 제안(D-16, O9) | `02-network.md` 4.3절 |
| EKS API 경로 | 비공개 전용 엔드포인트 + 링크 `src-k8s`. VPC 피어링은 비상 토글(기본 꺼짐) | 제안(D-16, O3). 정하는 사람은 인프라 · CI | `02-network.md` 3절 |
| 아웃바운드 | 사설 서브넷 + VPC마다 Regional NAT. 활성 AZ마다 과금이라 비용은 AZ마다 NAT Gateway와 같거나 조금 많음. 고르는 이유는 공개 서브넷 없음과 자동 AZ 확장. provider가 지원하지 않으면 AZ마다 NAT Gateway | 제안(D-16, O4) | `02-network.md` 7절 |
| 테넌트 격리 | 플랫폼 DB 모든 테이블에 `tenant_id`와 RLS `FORCE`. 서비스 역할에 `BYPASSRLS` 없음 | 제안(D-16) | `02-network.md` 8절 |

## 4. 플랫폼 서비스

| 서비스 | 언어 · 프레임워크 | 칸 | 이유 |
|---|---|---|---|
| API, 상태 머신, 이벤트 스트림 | Python 3.12, FastAPI, Pydantic v2, psycopg 3 | 제안 | 에이전트 SDK와 스키마(Pydantic)가 Python이라 언어를 하나로 줄임 |
| 수집기 | Python, kubernetes 클라이언트, boto3(읽기 전용 역할) | 제안 | 같음 |
| 에이전트 워커 | Python, Claude Agent SDK | 고정(SDK), 제안(언어) | §8.3 |
| 리허설 러너 | Python. Harbor 판은 0.25.0으로 고정(제안) | 제안 | D-02. 프로토타입이 이미 Python |
| 통제 시험기, 게이트 | Python이 CLI 도구(gitleaks, kube-bench, Prowler, nmap, openssl, psql)를 감쌈 | 고정(도구), 제안(언어) | §8.5 |
| Cutover Executor | Go 또는 Python. 계약(§8.6)만 고정 | 미정(데이터 · 컷오버, 11/3까지) | §12 |
| 콘솔 | TypeScript, React, Vite. 진행은 SSE(`EventSource`) | 제안 | 담당이 바꿀 수 있음 |
| 플랫폼 DB | CNPG(플랫폼 클러스터 안) | 제안 | §7.2 "감사 로그 DB" |
| 부하 생성기 | k6 또는 Python asyncio. 요청 ID와 시도 ID를 따로 기록 | 미정(콘솔 · 관측) | T18 |

**멱등 처리.** `Idempotency-Key`는 플랫폼 DB의 유일 제약으로 지킵니다. 같은 키에 다른 입력 해시가 오면 거부합니다. 앱 코드로 잠금을 만들지 않습니다.

**상태 머신.** 워크플로 엔진을 쓰지 않습니다(§8.2). 상태와 전이 조건은 표 하나와 함수 하나로 둡니다.

**Executor 언어 비교.** 쓰기 정지 예산이 5초라 언어의 실행 속도는 결정 요인이 아닙니다. 담당이 PostgreSQL과 K8s 클라이언트를 더 잘 쓰는 언어를 고릅니다. Go는 단일 바이너리와 pgx가, Python은 플랫폼과 같은 언어라는 점이 장점입니다.

## 5. AI

| 항목 | 고르는 것 | 칸 |
|---|---|---|
| SDK | Claude Agent SDK(Python). `tools=[]`, `setting_sources=[]`, `PreToolUse` hook 관문, 구조화 출력. 상태는 system prompt와 hook으로 주입(`07` 7절) | 고정(SDK), 제안(설정) |
| 실행 단위 | Trial마다 에이전트 Pod 1개(Job, TTL 삭제). gVisor RuntimeClass는 P1 후보 | 제안 |
| 모델 | 설정값으로 둠. 기본 `claude-opus-5-5`. 분류 Trial은 비용을 보고 `claude-sonnet-5-5`로 낮출 수 있음 | 제안 |
| 키 위치 | 플랫폼에만. LLM 게이트웨이 경유 | 고정(§7.5) |
| 제안서 스키마 | Pydantic → JSON Schema | 고정(§8.3) |
| 샌드박스 · 평가 | Harbor 0.25.0. 런타임과 평가에 같은 경로 | 고정(P1 평가), 담당 결정(런타임, D-02) |
| 교차 검토 | 다른 모델에 변경안 검토 1회 | 고정(P1) |

## 6. 빌드와 배포

| 항목 | 고르는 것 | 칸 | 근거 |
|---|---|---|---|
| CI | GitHub Actions | 고정 | §8.8 |
| 클라우드 인증 | Actions OIDC로 AWS 역할. GCP는 Workload Identity 연동(D-01) | 고정(AWS), 제안(GCP) | NFR-6 |
| 이미지 | GHCR, digest 고정(고정). 데모 이미지는 공개 패키지로 둬 pull 토큰을 없앰(제안 D-14). GHCR는 비공개 pull에 classic PAT만 받음 | 고정, 공개 여부는 제안 | §8.8, NFR-6 |
| 클러스터 안 배포 | 빌드는 GitHub 호스팅 runner. 모든 클러스터는 그 클러스터의 Argo CD가 배포 저장소를 pull(제안 D-16, O5). 비공개 AWS 클러스터의 첫 설치만 VPC 안 CodeBuild(Actions OIDC로 시작). 온프렘 첫 설치는 운영자가 로컬에서. 폴백은 플랫폼 안 일회용 runner + Helm이 링크로 push | 미정(인프라 · CI) | §12, `02-network.md` 1절 F9, 14절 |
| Python 패키지 | uv 작업 공간(루트 하나에 `platform/*`가 멤버) | 제안 | 잠금 파일 하나 |
| 검사 | ruff, pytest, gitleaks | 제안 | AGENTS.md 강제 수단 표 |

**배포 방식(제안 D-16).** 어느 클러스터에도 들어오는 포트가 없습니다. 그래서 배포를 밀어 넣지 않고, 각 클러스터의 Argo CD가 배포 저장소를 당겨 옵니다.
- **첫 설치.** 비공개 출발지 클러스터와 플랫폼 클러스터에는 VPC 안 AWS CodeBuild 프로젝트가 `cf-edge`와 Argo CD를 한 번 설치합니다. GitHub Actions가 OIDC로 받은 역할로 이 프로젝트를 시작합니다. 끝나면 CodeBuild의 클러스터 접근 권한과 SG 규칙을 지웁니다(`02-network.md` 14절).
- 온프렘 첫 설치는 운영자가 같은 LAN에서 로컬로 합니다.
- 그 뒤 배포는 Argo CD pull뿐입니다. 플랫폼 클러스터도 같습니다.
- 플랫폼은 승인 해시와 digest가 맞을 때만 배포 저장소에 커밋합니다.
- pull 방식은 클러스터 상태가 Git과 어긋나면 되돌립니다. 대신 클러스터마다 상주 구성요소가 하나 늡니다.
- 커밋 왕복이 초 단위 이상이라 컷오버 명령에는 쓰지 않습니다.
- 폴백은 플랫폼 안 일회용 runner + Helm이 링크 `*-k8s`로 push하는 방식입니다. 상주 구성요소는 없지만 Git과 어긋나도 되돌리지 않습니다.
- **잠금.** CUTOVER부터 DONE까지와 보안 보류 중에는 배포를 막습니다. 플랫폼이 링크로 그 앱의 AppProject에 deny sync window를 겁니다(수동 sync 포함). 배포 저장소 커밋도 거부합니다. sync 시도가 거부되는지는 N18로 시험합니다(`03-migration.md` 4.1).
- Helm 차트는 PgBouncer 대상 설정 두 벌과 기본 선택만 갖습니다. 실행 중 대상은 Executor만 바꿉니다. 활성 파일은 emptyDir에 있어 Argo CD가 되돌리지 않습니다.

## 7. 메시와 관측성

| 항목 | 고르는 것 | 칸 | 근거 |
|---|---|---|---|
| 서비스 메시 | Istio ambient L4(1.31 기준). 플랫폼 클러스터의 `bravo-control`, `bravo-agents`만 등록. waypoint 없음. Executor는 메시 밖 | 제안(D-15) | `07` 4절 |
| 관측 수집 · 백엔드 | 데모는 Collector가 들어 있는 Grafana 올인원 이미지 1개. 모든 신호를 OTLP로. 운영은 별도 Collector와 OTLP 수용 백엔드로 교체 | 제안(D-15) | `07` 6절 |
| 에이전트 텔레메트리 | Claude Code OTLP export(메트릭 · 이벤트, 트레이스는 beta). 본문 마스킹 기본값 유지. 근거는 `research/.claude/skills/agent-sdk-reference` | 제안 | `07` 6절 |
| 플랫폼 노드 | EKS 관리형 노드 그룹: core t4g.large 3대(AZ마다 1), agents t4g.large 1대(taint). 제어면은 EKS가 관리하므로 server 노드가 없음 | 제안(D-17), 팀 리드 결정(EKS) | `01` 2절, `09-cni-csi.md` |

## 8. 쓰지 않는 것

| 쓰지 않는 것 | 이유 |
|---|---|
| 워크플로 엔진(Temporal, Argo Workflows) | 상태가 열두 개뿐이고, 재기동 시 원격 상태를 다시 읽는 규칙이 더 중요함(§8.2) |
| Argo Rollouts | 클러스터를 넘는 전환을 못 함(§8.7). P2 |
| ECR | 온프렘이 AWS 자격 없이 이미지를 받아야 함(§8.8) |
| 사이드카 메시, 멀티 클러스터 메시, 고객 클러스터 메시(P0) | 계층과 컷오버 변수가 늘어남. 환경 사이는 Cloudflare 링크(제안 D-16, `07` 4절) |
| Transit Gateway, PrivateLink, 상시 VPC 피어링, Network Firewall | 두 VPC 사이에 경로가 필요 없음(제안 D-16, `02-network.md` 12절) |
| 역할별 노드 분리 | 격리 단위를 Pod로 모음(`07` 8절) |
| RKE2, Calico, Canal | 팀 리드 결정(2026-11-02)으로 모든 환경에서 뺌. 각 플랫폼의 기본 CNI와 Kyverno 기본선을 씀(`09-cni-csi.md` 4절) |
