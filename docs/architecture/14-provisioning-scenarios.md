# IaC로 환경이 펼쳐지는 과정

플랫폼을 처음 올리는 순간부터 고객 앱이 처음 배포되는 순간까지, 시나리오마다 무엇이 어떤 순서로 펼쳐지는지 적습니다. 각 단계에 2026-11-02 20:57 KST 관측과 21:10 KST 결정(RKE2를 스펙대로, ADR 0010)을 반영한 상태를 함께 둡니다(완료 · 진행 중 · 코드만 · 미착수 · 막힘). 상태는 실제 증거로만 적었습니다. 증거는 파일, 읽기 전용 `kubectl get`, `aws eks describe-*`, `gcloud container clusters list`, `terraform output`, `docker ps`입니다. 노트북 Compose 파일럿은 내렸고, 그 측정은 "파일럿(노트북) 기록"으로만 적습니다(`docs/evidence/pilot-notebook/`). 기술 근거는 `docs/spec/TECH-STACK-SPEC.md`, 클러스터 지도는 `10-cluster-overview.md`, 랜딩 존은 `11-landing-zone.md`입니다.

## 0. 공통 바탕

| 항목 | 내용 | 상태 |
|---|---|---|
| 운영자 도구 | `tools/toolchain/check.sh` 23개 판 확인(Terraform 1.17.1, aws · gcloud · kubectl · helm · argocd · kyverno · cosign · syft · trivy · gitleaks 등) | 완료 |
| Terraform 상태 | S3 버킷 `bravo-tfstate-<account>-apne2`(버전 관리, 암호화, 공개 차단), S3 네이티브 잠금(`use_lockfile`). 환경마다 `envs/<env>.tfstate` | 완료 |
| 입력 원칙 | 고객 입력은 이름 · 지역 · 크기뿐(`terraform.tfvars`). 계정 번호 · 버킷 이름이 든 `backend.hcl`, `*.tfvars`는 저장소에 두지 않음 | 완료 |
| 비밀 | 키 · 비밀은 플랫폼 OpenBao 하나만 보관(D-26, ADR 0007). Terraform 상태 · 출력에 비밀 값 없음. 터널 토큰 · 서비스 토큰은 플랫폼 키 단계(`deploy/pilot/scripts/keys.sh`)가 Cloudflare API에서 받아 OpenBao에 바로 씀. 고객 클라우드 금고 · CI · 노트북 파일에 두지 않음 | 진행 중. 금고가 노트북 `platform-bao`에서 플랫폼 EKS `openbao/openbao-0`로 옮겨지는 중(20:56 Pod Running, `docker ps`에 `platform-bao` 없음) |

## 1. 플랫폼을 처음 올리는 순간(Day 0)

우리 제어면을 처음 세우는 순서입니다. 2026-11-02 21:10 KST에 팀장이 RKE2를 스펙 r2대로 되돌렸습니다("스펙 그대로 전부", "RKE2 관련만", ADR 0010). 플랫폼은 VPC ②의 EC2 1대 + RKE2 단일 노드이고(스펙 §7.2, T7), 온프렘과 같은 공통 cloud-init으로 설치합니다.

| # | 하는 일 | 도구 | 결과 | 상태 |
|---|---|---|---|---|
| 1 | VPC②: 사설 서브넷, NAT, 인바운드 0 | Terraform `network/aws` | 네트워크 | 완료(apply, EKS 판 때 만듦. 노드가 10.20.11–13.x에 있었음). EC2가 이 VPC를 그대로 쓸지는 인프라 갈래가 정함 |
| 2 | EC2 1대 + RKE2 단일 노드: 공통 cloud-init, RKE2 v1.36.5+rke2r1(제안) `profile: cis`, sha256 고정 | Terraform + cloud-init | 빈 클러스터 | 미착수. `infra/cloud-init/`가 비어 있음(21:1x) |
| 3 | 첫 설치 경로: 사설 서브넷의 EC2에 닿는 통로 | 설계 필요(EKS 판의 `bootstrap_public_cidrs`는 해당 없음) | 설치 통로 | 미착수 |
| 4 | `cf-edge`(cloudflared, 터널 `bravo-platform`)와 Argo CD 설치 | Helm 1회 | 이후 설치는 Git에서 당겨 옴 | RKE2 위에서는 미착수(EKS 판에서는 cloudflared Running, 아래 이력) |
| 5 | 플랫폼 묶음: Cilium + Hubble, Kyverno + OPA, External Secrets, CNPG 운영자 + 플랫폼 DB, OpenBao(D-26), 관측(Collector · Prometheus · Tempo · Loki · Grafana), 플랫폼 서비스(API · `operator-mcp` · 콘솔 · 상태 머신 · 게이트 · 수집기 · 리허설 서비스(Harbor) · Executor), LLM 게이트웨이(운영 목표) | Argo CD + Helm, 이미지는 digest 고정 | 플랫폼 기동 | RKE2 위에서는 미착수(EKS 판에서는 대부분 Running, 아래 이력) |
| 6 | Cloudflare: 환경별 Tunnel · 진입 이름 · 가중치 LB(`infra/envs/edge`), 콘솔 · MCP Access 앱. 토큰은 플랫폼 OpenBao에서만 꺼내 씀 | Terraform `edge` + `keys.sh` | 운영자 로그인 | 진행 중. Tunnel 셋(출발지 · 목적지 · 플랫폼)이 `terraform output`에 있음. `b-free`가 Cloudflare Access 로그인으로 302(20:56). LB pool 없음(`pool_ids = {}`). 가중치 전환은 코드만, 미실행(감사 B21) |
| 7 | 확인: Access 로그인, `tools/list`, 통제 기준선, 네트워크 시험 일부 | 스크립트 | 증거 파일 | 미착수. 증거 파일 없음 |

### 1.1 이력: 결정 전 EKS 판(ADR 0004, 2026-11-02 20:57 KST 관측)

21:10 결정 전에 플랫폼을 EKS `bravo-platform`으로 세웠습니다. 그 자원은 아직 떠 있습니다. 정리와 RKE2로의 이전은 인프라 갈래가 정하고, 클라우드 자원 삭제는 사람 승인 뒤에 합니다.

| # | 하는 일(EKS 판) | 도구 | 결과 | 상태(20:57) |
|---|---|---|---|---|
| 1 | VPC②: 사설 서브넷 3개(AZ마다), Regional NAT, 인바운드 0 | Terraform `network/aws` | 네트워크 | 완료(apply). 노드가 세 AZ의 사설 주소 10.20.11–13.x에 있음. 인바운드 0은 확인하지 않음 |
| 2 | EKS 제어면(1.36, ADR 0004), 관리형 노드 그룹 `core`(3-AZ) · `agents`(taint), 관리형 애드온(VPC CNI · EBS CSI · CoreDNS · kube-proxy · Pod Identity), KMS, 서비스별 Pod Identity 역할 | Terraform `cluster/eks` | 빈 클러스터 | 완료. `bravo-platform` ACTIVE(20:49), 노드 그룹 core 2a · 2b · 2c와 agents(taint `pool=agents:NoSchedule`) ACTIVE, 노드 4 Ready, add-on 6개. KMS와 Pod Identity 역할은 확인하지 않음 |
| 3 | 첫 설치 경로: 비공개 API에 닿기 위해 (a) 운영자 IP 하나에만 잠시 공개 엔드포인트를 열거나(`bootstrap_public_cidrs`) (b) VPC 안 CodeBuild를 한 번 돌림 | Terraform 변수 / CodeBuild | 설치 통로 | (a) 쓰는 중: 공개 엔드포인트가 CIDR 1개에 열려 있음(20:5x). (b) 설계 |
| 4 | Argo CD와 `cf-edge`(cloudflared) 설치 → 공개 엔드포인트 닫기 | Helm 1회 | 이후 모든 설치는 Git에서 당겨 옴 | 진행 중. `cf-edge/cloudflared` 2 Running(20:56). Argo CD 네임스페이스 없음. 공개 엔드포인트는 열려 있음 |
| 5 | Argo CD app-of-apps가 플랫폼 묶음 설치: Cilium(체이닝) + Hubble, Istio ambient, Kyverno + OPA, External Secrets, CNPG 운영자 + 플랫폼 DB, 관측(Collector · Prometheus · Tempo · Loki · Grafana), 플랫폼 서비스(API · `operator-mcp` · 콘솔 · 상태 머신 · 게이트 · 수집기 · 리허설 서비스(Harbor) · LLM 게이트웨이 · Executor) | Argo CD + Helm, 이미지는 GHCR digest · cosign 서명을 Kyverno가 확인 | 플랫폼 기동 | 진행 중(Argo CD 없이 설치). 20:56 `bravo-control`에 api, operator-mcp, console, collector, gate, rehearsal, `executor-0`, CNPG `platform-db` Running(복제본 생성 중). `bravo-observe` 관측 5종, `openbao`, Cilium(DaemonSet 4/4) Running. Istio ambient, Kyverno, OPA, ESO, LLM 게이트웨이, Argo CD는 없음 |
| 6 | Cloudflare: 환경별 Tunnel · 진입 이름 · 가중치 LB(`infra/envs/edge`), 콘솔 · MCP Access 앱(Managed OAuth). 토큰은 플랫폼 OpenBao에서만 꺼내 씀 | Terraform `edge` + `keys.sh` | 운영자 로그인 | 진행 중. Tunnel 셋(출발지 · 목적지 · 플랫폼)이 `terraform output`에 있음. `b-free`가 Cloudflare Access 로그인으로 302(20:56). LB pool 없음(`pool_ids = {}`). 가중치 전환은 코드만, 미실행(감사 B21) |
| 7 | 확인: Access 로그인, `tools/list`, 통제 기준선, 네트워크 시험 일부 | 스크립트 | 증거 파일 | 미착수. 증거 파일 없음 |

앞서 노트북 Compose에서 돌린 컷오버 · 되돌리기는 파일럿(노트북) 기록이고 클라우드 결과가 아닙니다(`docs/evidence/pilot-notebook/RUNS.tsv`).

## 2. 출발지 고객 환경(기존 AWS 고객)

실제 고객은 이미 AWS에서 앱을 돌리고 있습니다. 플랫폼은 그 환경을 만들지 않고 **등록**합니다. 데모에서는 그 "기존 고객 환경"을 `infra/envs/source-aws`로 직접 세웁니다.

| # | 하는 일 | 도구 | 상태 |
|---|---|---|---|
| 1 | 상태 버킷 연결(`terraform init -backend-config=backend.hcl`) | Terraform | 완료 |
| 2 | 계획: 자원 52개(VPC① 사설 서브넷 2 · NAT · S3 엔드포인트, EKS · 노드 그룹 · 애드온 6, KMS 3, S3 첨부 버킷(공개 차단 · 버전 관리 · SSE-KMS · TLS 강제 정책), Secrets Manager 그릇, Pod Identity 역할) | `terraform plan` | 완료 |
| 3 | 생성 | `terraform apply` | 완료. EKS `bravo-source` ACTIVE 1.36, 첨부 버킷 · KMS 키 · Pod Identity 역할이 `terraform output`에 있음 |
| 4 | 클러스터 첫 설치: 운영자 IP에만 잠시 연 엔드포인트로 Argo CD · `cf-edge` · CNPG · PgBouncer · 채팅 앱 설치, Secrets Manager에 DB 비밀번호 넣기 | Helm 1회 → Argo CD | 진행 중. 20:57 Cilium · Hubble, `cf-edge/cloudflared` 2, CNPG `chat-db-1`, ESO, `dst-db-link` Running. `chat-pgbouncer` Init. 데모 앱 Pod 없음. Argo CD 없음 |
| 5 | 공개 엔드포인트 닫기(`bootstrap_public_cidrs = []` 후 apply) | Terraform | 미착수. 20:3x에 CIDR 1개로 열려 있음 |
| 6 | 플랫폼에 등록: 고객 계정에 읽기 전용 수집 역할(교차 계정 신뢰)을 만드는 스니펫, 클러스터에 `cf-edge` 연결 | Terraform 스니펫 + 등록 명령 | 설계 |

현재 막혀 있던 것 하나는 임시로 비켜 갔습니다. `landing_zone` 모듈이 아직 없어 그 블록을 `landing_zone.tf.off`로 잠시 꺼 두었습니다. 이 블록은 Argo CD 파일만 쓰고 AWS 자원은 만들지 않습니다.

## 3. 목적지 환경 만들기(고객이 "온프렘으로"를 고름)

모든 목적지는 같은 상태 머신을 지납니다: 요청 → 구축 → 랜딩 존 설치 → 통제 기준선 · 연결 시험 → 준비 완료(`11-landing-zone.md` 4절).

### 3.1 온프렘 Ubuntu VM + RKE2(CIS)

스펙 §6 S1 기본 흐름 4와 T2입니다. 2026-11-02 21:10 KST 결정(ADR 0010)으로 K3s에서 RKE2로 바뀌었습니다. kubectl 컨텍스트는 그대로 `bravo-onprem`입니다.

| # | 하는 일 | 도구 | 상태 |
|---|---|---|---|
| 1 | 고객 Ubuntu VM 준비(데모는 팀원 Mac의 Lima VM, 절전 차단) | Terraform `terraform_data` + `limactl` | 진행 중. Ubuntu 24.04 Lima VM 2대가 있음(K3s용으로 만듦, `infra/envs/target-onprem`). RKE2용 VM 수와 크기는 T2가 정함 |
| 2 | 공통 cloud-init으로 RKE2 v1.36.5+rke2r1(제안) `profile: cis` 설치. CIS 조건(sysctl, etcd 사용자)을 cloud-init에 고정, 설치 파일 sha256 확인 | cloud-init(플랫폼과 공용) | 미착수. `infra/cloud-init/`가 비어 있음 |
| 3 | 예외 4a: CIS 프로필이 조건 미충족으로 시작을 거부하면 cloud-init 조건을 고치고 재설치 | cloud-init, 재설치 | 미착수. 실행 기록 없음 |
| 4 | 랜딩 존 `rke2` overlay: Cilium + Hubble, CNPG 운영자, PgBouncer, Rook-Ceph RGW(D-22), ESO(값은 플랫폼 OpenBao, D-26), Kyverno `GeneratingPolicy`, Gateway API, `cf-edge`, `bravo-link`, OTel 에이전트 | Argo CD(데모는 설치기) | 미착수. `rke2` overlay 없음(`k3s` overlay만 있음). RKE2에는 기본 local-path가 없어 블록 저장 바탕이 필요(ADR 0010) |
| 5 | 확인: RKE2가 CIS 프로필로 뜨고 kube-bench 결과가 나옴(T2 완료 기준). Cilium 상태, 정책 거부와 Hubble 기록, Gateway 경로, PVC, RGW 버킷 정책(OBJ-01–06) | kube-bench, 시험 스크립트 | 미착수 |
| 6 | 접기와 다시 펴기(같은 스크립트로 재설치 확인, T2 세부) | Terraform | 미착수 |

**이력: 결정 전 K3s 판(ADR 0004, 20:57 KST 관측).** Lima K3s `bravo-onprem`(v1.36.5+k3s1)이 아직 떠 있습니다.

| # | 하는 일(K3s 판) | 도구 | 상태(20:57) |
|---|---|---|---|
| 1 | 고객 VM 준비(데모는 Lima VM 2대 + 전용 디스크) | Terraform `terraform_data` + `limactl` | 완료. `terraform output instances`: `bravo-onprem-server`, `bravo-onprem-agent`. Lima 홈은 `infra/envs/target-onprem/.lima` |
| 2 | 등록 명령 한 줄 → VM 안 바깥 방향 에이전트가 K3s(판 고정, sha256 확인, CIS 설정) 설치. Cilium(flannel 끔) | cloud-init / 등록 에이전트 | 완료(K3s). v1.36.5+k3s1 노드 2 Ready(20:49), Cilium · Hubble relay Running. 설치는 등록 에이전트가 아니라 Terraform이 함(등록 에이전트는 설계). CIS 설정 결과(kube-bench)는 없음 |
| 3 | 랜딩 존 `k3s` overlay: CNPG 운영자, PgBouncer, Rook-Ceph RGW(첨부 버킷, 공개 차단, TLS 전용), ESO(값은 플랫폼 OpenBao, D-26), Kyverno `GeneratingPolicy`, Gateway API(Cilium), `cf-edge`, `bravo-link`, OTel 에이전트 | Argo CD(데모는 `deploy/landing-zone/install.sh k3s`) | 진행 중. 20:49 CNPG `chat-db-1`, `chat-pgbouncer`, 데모 앱 2, `cf-edge/cloudflared` 2 Running. `b-free-chat-dst/api/config` HTTP 200(20:50). RGW 설치 단계 없음. Kyverno · ESO는 네임스페이스만 있음. `bravo-link`, OTel 에이전트는 확인하지 않음 |
| 4 | 확인: Cilium 상태, 노드 사이 통신, 정책 거부와 Hubble 기록, Gateway 경로, PVC, RGW 버킷 정책(OBJ-01–06) | 시험 스크립트(옛 `verify.sh`) | 미착수. 스크립트 없음(감사 42), 결과 없음 |
| 5 | 접기와 다시 펴기: `terraform destroy` → `apply` | Terraform | 기록 없음 |

### 3.2 GKE · EKS 목적지

같은 순서이고, 1–2단계만 다릅니다. GKE는 Standard + Dataplane V2를 Workload Identity 연동 자격으로, EKS는 위 모듈을 교차 계정 역할로 만듭니다. 랜딩 존은 overlay만 바뀝니다(객체 저장은 Cloud Storage · S3).

| 목적지 | 상태 |
|---|---|
| GKE | 진행 중. `bravo-target-gcp`(asia-northeast1-b) RUNNING, 1.36.4-gke.2377000(20:49, `gcloud container clusters list`). `gke` overlay 매니페스트는 있음(`deploy/landing-zone/overlays/gke/`). 설치 여부와 kubectl 컨텍스트는 확인하지 못함 |
| EKS 목적지 | 코드만. `eks` overlay는 아직 없음(`deploy/landing-zone/install.sh` 주석) |

### 3.3 아직 없는 것

플랫폼이 고객 대신 이 IaC를 돌리는 실행 주체(테넌트별 실행기, 상태 파일 분리, 자격 위임)는 설계만 있습니다(`TECH-STACK-SPEC.md` 3절 1번). 오늘은 운영자 노트북에서 Terraform을 돌립니다.

## 4. 고객 앱을 처음 배포하는 순간

### 4.1 출발지에 처음 배포(데모 준비)

| # | 하는 일 | 도구 | 상태 |
|---|---|---|---|
| 1 | 앱 이미지 빌드, SBOM, 취약점 검사, 서명 → 출발지 ECR digest(계약 5절) | GitHub Actions(OIDC) + syft · trivy · cosign | 진행 중. ECR 저장소 `bravo-source/bravo-demo`, `bravo-source/bravo-pgbouncer`가 있음(`aws ecr describe-repositories`). 이미지 push와 서명은 확인하지 않음. Actions 경로는 저장소 push 전이라 미확인 |
| 2 | 배포 저장소에 digest와 값(DSN Secret 참조, S3 계약 변수, 환경 표시) 커밋 | Git | 미착수(저장소 push 보류) |
| 3 | 출발지 Argo CD가 당겨 옴 → Kyverno가 서명 · digest 확인 → 앱 · CNPG · PgBouncer 기동 | Argo CD | Argo CD · Kyverno 경로는 미착수. CNPG는 다른 경로로 Running, 앱은 없음(20:57) |
| 4 | 앱이 Secrets Manager SDK로 DB 비밀번호를 직접 읽음(의도된 의존성, S2 재료) | 앱 | 코드 있음(`apps/demo`, `passwordSource: aws-secrets-manager`). 출발지 앱 Pod가 없어 실제 동작은 확인 못 함 |
| 5 | 사용자 진입: 대표 이름 `b-free-chat`(LB 전에는 출발지 터널 CNAME) → `b-free-chat-src` 터널 → Gateway → 앱, QR 페이지 | Cloudflare + Gateway API | 진행 중. 출발지 커넥터는 붙었고 원점이 없어 HTTP 502(20:57) |

오늘은 저장소 push가 보류라, 첫 배포는 운영자 노트북에서 Helm으로 한 번 넣는 부트스트랩 경로로 합니다. 같은 차트와 값이라 나중에 Argo CD로 그대로 넘어갑니다.

### 4.2 목적지에 처음 배포(이전 중)

| # | 하는 일 | 누가 | 상태 |
|---|---|---|---|
| 1 | 게이트 1차 BLOCK → 에이전트가 Harbor 샌드박스에서 제안(정책 번역, 코드 수정) → 분리 검증기 → 게이트 재대조 PASS | 상태 머신 + 역할별 에이전트 | 진행 중. 게이트 · 리허설 Pod가 플랫폼 EKS에 Running(20:56). 실제 Trial 기록 없음. 온프렘에 시험이 남긴 `rehearsal-pytest27-23001` 네임스페이스가 있음(20:56). 감사 B7 · B8 · B9 열림 |
| 2 | 코드 수정이 있으면 CI Agent 제안을 신뢰된 파이프라인이 빌드 → 새 digest | Actions | 설계 |
| 3 | 사람이 콘솔에서 `change` 승인 → 플랫폼이 목적지 경로에 승인된 매니페스트를 커밋 | 승인자 + 플랫폼 | 미착수. API 승인 단계는 `cutover`, `failback`뿐이고 `change`가 없음(`migrations/0003_full_path.sql`). 커밋 경로 없음 |
| 4 | 목적지 Argo CD가 당겨 옴 → 앱이 목적지 PgBouncer(SHIFT 동안은 출발지 DB를 가리킴), RGW, 플랫폼 OpenBao 값으로 기동 | Argo CD | Argo CD 경로는 미착수. 온프렘 앱 · PgBouncer는 설치기로 Running(3.1절 3번). RGW 없음 |
| 5 | Cloudflare LB에 목적지 풀 0% 추가 → 10 · 50 · 100% → 컷오버 | 콘솔(사람) + Executor | 가중치 전환: 코드만, 미실행(`front.py`, 감사 B21). 컷오버: 클라우드 실행 없음. 파일럿(노트북) 기록만 있음(쓰기 정지 76–139 ms, `docs/evidence/pilot-notebook/RUNS.tsv`) |

## 5. 접기

환경마다 `terraform destroy`. 링크 토큰은 회전 뒤 폐기하고, 상태 버킷은 남깁니다. 남은 과금은 Cost Explorer와 태그(`component`)로 확인합니다(NFR-8, `docs/runbooks/destroy.md`).
