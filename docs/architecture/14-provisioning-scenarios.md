# IaC로 환경이 펼쳐지는 과정

플랫폼을 처음 올리는 순간부터 고객 앱이 처음 배포되는 순간까지, 시나리오마다 무엇이 어떤 순서로 펼쳐지는지 적습니다. 각 단계에 2026-11-02 20시 기준 상태를 함께 둡니다(완료 · 진행 중 · 코드만 · 미착수 · 막힘). 기술 근거는 `docs/spec/TECH-STACK-SPEC.md`, 클러스터 지도는 `10-cluster-overview.md`, 랜딩 존은 `11-landing-zone.md`입니다.

## 0. 공통 바탕

| 항목 | 내용 | 상태 |
|---|---|---|
| 운영자 도구 | `tools/toolchain/check.sh` 23개 판 확인(Terraform 1.17.1, aws · gcloud · kubectl · helm · argocd · kyverno · cosign · syft · trivy · gitleaks 등) | 완료 |
| Terraform 상태 | S3 버킷 `bravo-tfstate-<account>-apne2`(버전 관리, 암호화, 공개 차단), S3 네이티브 잠금(`use_lockfile`). 환경마다 `envs/<env>.tfstate` | 완료 |
| 입력 원칙 | 고객 입력은 이름 · 지역 · 크기뿐(`terraform.tfvars`). 계정 번호 · 버킷 이름이 든 `backend.hcl`, `*.tfvars`는 저장소에 두지 않음 | 완료 |
| 비밀 | 키 · 비밀은 플랫폼 OpenBao 하나만 보관(D-26). Terraform 상태 · 출력에 비밀 값 없음. 터널 토큰 · 서비스 토큰은 플랫폼 키 단계(`deploy/pilot/scripts/keys.sh`)가 Cloudflare API에서 받아 OpenBao에 바로 씀. 고객 클라우드 금고 · CI · 노트북 파일에 두지 않음 | 진행 중 |

## 1. 플랫폼을 처음 올리는 순간(Day 0)

우리 제어면(`infra/envs/platform-aws`, Amazon EKS)을 처음 세우는 순서입니다.

| # | 하는 일 | 도구 | 결과 | 상태 |
|---|---|---|---|---|
| 1 | VPC②: 사설 서브넷 3개(AZ마다), Regional NAT, 인바운드 0 | Terraform `network/aws` | 네트워크 | 코드만 |
| 2 | EKS 제어면(1.35 · 1.36 결정 대기), 관리형 노드 그룹 `core`(3-AZ) · `agents`(taint), 관리형 애드온(VPC CNI · EBS CSI · CoreDNS · kube-proxy · Pod Identity), KMS, 서비스별 Pod Identity 역할 | Terraform `cluster/eks` | 빈 클러스터 | 코드만 |
| 3 | 첫 설치 경로: 비공개 API에 닿기 위해 (a) 운영자 IP 하나에만 잠시 공개 엔드포인트를 열거나(`bootstrap_public_cidrs`) (b) VPC 안 CodeBuild를 한 번 돌림 | Terraform 변수 / CodeBuild | 설치 통로 | (a) 코드 완료, (b) 설계 |
| 4 | Argo CD와 `cf-edge`(cloudflared) 설치 → 공개 엔드포인트 닫기 | Helm 1회 | 이후 모든 설치는 Git에서 당겨 옴 | 미착수 |
| 5 | Argo CD app-of-apps가 플랫폼 묶음 설치: Cilium(체이닝) + Hubble, Istio ambient, Kyverno + OPA, External Secrets, CNPG 운영자 + 플랫폼 DB, 관측(Collector · Prometheus · Tempo · Loki · Grafana), 플랫폼 서비스(API · `operator-mcp` · 콘솔 · 상태 머신 · 게이트 · 수집기 · 리허설 서비스(Harbor) · LLM 게이트웨이 · Executor) | Argo CD + Helm, 이미지는 GHCR digest · cosign 서명을 Kyverno가 확인 | 플랫폼 기동 | 미착수(서비스는 노트북 Compose에서 동작 확인) |
| 6 | Cloudflare: 환경별 Tunnel · 진입 이름 · 가중치 LB(`infra/envs/edge`), 콘솔 · MCP Access 앱(Managed OAuth). 토큰은 플랫폼 OpenBao에서만 꺼내 씀 | Terraform `edge` + `keys.sh` | 운영자 로그인 | 코드 완료 · validate 통과. 영역 선택, LB 구독, Zero Trust(Access) 켜기 대기 |
| 7 | 확인: Access 로그인, `tools/list`, 통제 기준선, 네트워크 시험 일부 | 스크립트 | 증거 파일 | 미착수 |

오늘 플랫폼 서비스는 노트북의 Compose(`deploy/pilot/`)에서 돌고, 실제 컷오버 · 되돌리기까지 실측했습니다(`deploy/pilot/README.md`). 플랫폼 EKS 생성은 비용 승인이 따로 필요합니다.

## 2. 출발지 고객 환경(기존 AWS 고객)

실제 고객은 이미 AWS에서 앱을 돌리고 있습니다. 플랫폼은 그 환경을 만들지 않고 **등록**합니다. 데모에서는 그 "기존 고객 환경"을 `infra/envs/source-aws`로 직접 세웁니다.

| # | 하는 일 | 도구 | 상태 |
|---|---|---|---|
| 1 | 상태 버킷 연결(`terraform init -backend-config=backend.hcl`) | Terraform | 완료 |
| 2 | 계획: 자원 52개(VPC① 사설 서브넷 2 · NAT · S3 엔드포인트, EKS · 노드 그룹 · 애드온 6, KMS 3, S3 첨부 버킷(공개 차단 · 버전 관리 · SSE-KMS · TLS 강제 정책), Secrets Manager 그릇, Pod Identity 역할) | `terraform plan` | 완료 |
| 3 | 생성 | `terraform apply` | **진행 중(20시 전후, 승인 받음)** |
| 4 | 클러스터 첫 설치: 운영자 IP에만 잠시 연 엔드포인트로 Argo CD · `cf-edge` · CNPG · PgBouncer · 채팅 앱 설치, Secrets Manager에 DB 비밀번호 넣기 | Helm 1회 → Argo CD | 미착수 |
| 5 | 공개 엔드포인트 닫기(`bootstrap_public_cidrs = []` 후 apply) | Terraform | 미착수 |
| 6 | 플랫폼에 등록: 고객 계정에 읽기 전용 수집 역할(교차 계정 신뢰)을 만드는 스니펫, 클러스터에 `cf-edge` 연결 | Terraform 스니펫 + 등록 명령 | 설계 |

현재 막혀 있던 것 하나는 임시로 비켜 갔습니다. `landing_zone` 모듈이 아직 없어 그 블록을 `landing_zone.tf.off`로 잠시 꺼 두었습니다. 이 블록은 Argo CD 파일만 쓰고 AWS 자원은 만들지 않습니다.

## 3. 목적지 환경 만들기(고객이 "온프렘으로"를 고름)

모든 목적지는 같은 상태 머신을 지납니다: 요청 → 구축 → 랜딩 존 설치 → 통제 기준선 · 연결 시험 → 준비 완료(`11-landing-zone.md` 4절).

### 3.1 온프렘 K3s

| # | 하는 일 | 도구 | 상태 |
|---|---|---|---|
| 1 | 고객 VM 준비(데모는 Lima VM 2대 + 전용 디스크) | Terraform `terraform_data` + `limactl` | 진행 중(인프라 갈래) |
| 2 | 등록 명령 한 줄 → VM 안 바깥 방향 에이전트가 K3s(판 고정, sha256 확인, CIS 설정) 설치. Cilium(flannel 끔) | cloud-init / 등록 에이전트 | 진행 중 |
| 3 | 랜딩 존 `k3s` overlay: CNPG 운영자, PgBouncer, Rook-Ceph RGW(첨부 버킷, 공개 차단, TLS 전용), OpenBao + ESO, Kyverno `GeneratingPolicy`, Gateway API(Cilium), `cf-edge`, `bravo-link`, OTel 에이전트 | Argo CD(데모는 HelmChart 자동 설치) | 진행 중 |
| 4 | 확인: Cilium 상태, 노드 사이 통신, 정책 거부와 Hubble 기록, Gateway 경로, PVC, RGW 버킷 정책(OBJ-01–06) | `verify.sh` | 진행 중 |
| 5 | 접기와 다시 펴기: `terraform destroy` → `apply` | Terraform | 진행 중 |

### 3.2 GKE · EKS 목적지

같은 순서이고, 1–2단계만 다릅니다. GKE는 Standard + Dataplane V2를 Workload Identity 연동 자격으로, EKS는 위 모듈을 교차 계정 역할로 만듭니다. 랜딩 존은 overlay만 바뀝니다(객체 저장은 Cloud Storage · S3). 지금은 코드와 `terraform validate`까지입니다.

### 3.3 아직 없는 것

플랫폼이 고객 대신 이 IaC를 돌리는 실행 주체(테넌트별 실행기, 상태 파일 분리, 자격 위임)는 설계만 있습니다(`TECH-STACK-SPEC.md` 3절 1번). 오늘은 운영자 노트북에서 Terraform을 돌립니다.

## 4. 고객 앱을 처음 배포하는 순간

### 4.1 출발지에 처음 배포(데모 준비)

| # | 하는 일 | 도구 | 상태 |
|---|---|---|---|
| 1 | 앱 이미지 빌드, SBOM, 취약점 검사, 서명 → GHCR digest | GitHub Actions(OIDC) + syft · trivy · cosign | 앱 코드 진행 중, CI는 push 전이라 미착수 |
| 2 | 배포 저장소에 digest와 값(DSN Secret 참조, S3 계약 변수, 환경 표시) 커밋 | Git | 미착수(저장소 push 보류) |
| 3 | 출발지 Argo CD가 당겨 옴 → Kyverno가 서명 · digest 확인 → 앱 · CNPG · PgBouncer 기동 | Argo CD | 미착수 |
| 4 | 앱이 Secrets Manager SDK로 DB 비밀번호를 직접 읽음(의도된 의존성, S2 재료) | 앱 | 앱 코드 진행 중 |
| 5 | 사용자 진입: Cloudflare LB(출발지 100%) → `t1-src-web` → Gateway → 앱, QR 페이지 | Cloudflare + Gateway API | 막힘(Cloudflare 토큰) |

오늘은 저장소 push가 보류라, 첫 배포는 운영자 노트북에서 Helm으로 한 번 넣는 부트스트랩 경로로 합니다. 같은 차트와 값이라 나중에 Argo CD로 그대로 넘어갑니다.

### 4.2 목적지에 처음 배포(이전 중)

| # | 하는 일 | 누가 | 상태 |
|---|---|---|---|
| 1 | 게이트 1차 BLOCK → 에이전트가 Harbor 샌드박스에서 제안(정책 번역, 코드 수정) → 분리 검증기 → 게이트 재대조 PASS | 상태 머신 + 역할별 에이전트 | 코드 진행 중(모델 키 · 클러스터 · Harbor 어댑터 대기) |
| 2 | 코드 수정이 있으면 CI Agent 제안을 신뢰된 파이프라인이 빌드 → 새 digest | Actions | 설계 |
| 3 | 사람이 콘솔에서 `change` 승인 → 플랫폼이 목적지 경로에 승인된 매니페스트를 커밋 | 승인자 + 플랫폼 | API는 동작(파일럿), 커밋 경로 미착수 |
| 4 | 목적지 Argo CD가 당겨 옴 → 앱이 목적지 PgBouncer(SHIFT 동안은 출발지 DB를 가리킴), RGW, OpenBao로 기동 | Argo CD | 미착수 |
| 5 | Cloudflare LB에 목적지 풀 0% 추가 → 10 · 50 · 100% → 컷오버 | 콘솔(사람) + Executor | 컷오버는 실측(80–107 ms), 가중치는 막힘(Cloudflare 토큰) |

## 5. 접기

환경마다 `terraform destroy`. 링크 토큰은 회전 뒤 폐기하고, 상태 버킷은 남깁니다. 남은 과금은 Cost Explorer와 태그(`component`)로 확인합니다(NFR-8, `docs/runbooks/destroy.md`).
