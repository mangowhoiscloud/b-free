# 기술 스택 스펙

Bravo 플랫폼이 쓰는 기술을 한 곳에 고정합니다. 작성 2026-11-02 19시, 판 번호는 이날 공식 저장소와 배포 채널에서 확인했습니다. 스펙 v0.1과 다른 선택은 `DEVIATIONS.md`의 편차 번호를 함께 적습니다.

## 0. 고르는 기준

| 기준 | 뜻 |
|---|---|
| 고객은 몰라도 된다 | 고객은 엔터프라이즈지만 쿠버네티스 초보자입니다. 플랫폼이 클러스터를 만들고 환경을 옮깁니다. 그래서 관리형과 기본값이 안전한 것을 먼저 고르고, 고객 입력은 이름 · 지역 · 크기 · "어디로"뿐입니다 |
| 누가 쓰는지 말할 수 있다 | 칸마다 채택 근거(재단, 벤더 지원, AGENTIC STAR, 일본 금융, 수상작)를 적습니다. 근거를 못 찾은 칸은 "검증 중"으로 두고, 팀 확인 전에는 결정으로 쓰지 않습니다. RKE2 폐기(11/2)에서 배운 규칙입니다 |
| 출구가 있다 | 출구전략 제품이므로 우리 자신도 한 회사에 묶이지 않게, 표준 프로토콜(TCP, TLS, OIDC, OTLP, S3 API, PostgreSQL)로 경계를 긋습니다 |
| 같은 계약 · 같은 경험 | 온프렘 · GKE · EKS 고객 클러스터에서 앱 매니페스트 · 이름 · 통제 시험 · 화면이 같습니다. 엔진은 환경에 맞게 고를 수 있고, 차이는 공급자 어댑터 한 층에만 둡니다(`docs/architecture/11-landing-zone.md`, D-23, 1급 원천 「기술 스택(툴) 조합」) |
| 동등성은 시험으로 | 배포판이나 제품 이름이 아니라 같은 질문의 시험 결과로 보안 동등성을 판단합니다(스펙 설계 원칙) |
| 판은 고정 | 이미지는 digest, 차트와 바이너리는 정확한 판. `latest`를 쓰지 않습니다 |

상태 표기: **결정**(팀장 결정) · **스펙**(스펙 v0.1 고정) · **제안**(회의 전) · **검증 중**(근거 확인 중) · **열림**(아직 정하지 못함)

## 1. 환경별 클러스터

| 환경 | 클러스터 | 쿠버네티스 | 네트워크(CNI) | 스토리지(CSI, 클래스 이름 공통) | 상태 |
|---|---|---|---|---|---|
| 플랫폼(우리 제어면) | Amazon EKS. 제어면은 AWS가 3-AZ로 운영. 역할별 관리형 노드 그룹(core 3-AZ, agents는 taint) | 1.35 권고(EKS 표준 지원 2027-03-27까지. 1.36은 kube-bench가 CIS 판을 자동으로 고르지 못해 통제 비교가 어려움) | Cilium(VPC CNI 체이닝) + Hubble | EBS CSI, gp3, KMS 암호화 | 결정(D-21, CNI는 11/2) |
| 출발지 AWS | Amazon EKS | 같음 | 같음 | 같음 | 스펙 |
| 목적지 온프렘 | K3s(server 1 + agent 1), K3s CIS 하드닝 가이드 설정 | v1.35.9+k3s1 권고 | Cilium(VXLAN) + Hubble | 블록 저장소는 결정 전(임시로 K3s 기본 local-path). 후보 Rook-Ceph RBD | 결정(D-21). 저장소는 **제안(D-22), 검증 중** |
| 목적지 GCP(P2) | GKE Standard(Dataplane V2는 생성 때 지정) | 같은 minor | Dataplane V2(Cilium 기반) | PD CSI(pd-balanced) | 결정(D-21), 범위는 D-01 |

- CNI는 모든 환경에서 Cilium입니다(팀장 결정 2026-11-02, 1급 원천 「기술 스택(툴) 조합」). Hubble 흐름 기록이 네트워크 통제(NET-01)의 증거가 됩니다. Tetragon은 P1입니다.
- 공통 안전선: Kyverno `generate` 정책이 리허설 · 검증 네임스페이스에 "기본 차단 + DNS 허용" NetworkPolicy를 만들고 지우지 못하게 합니다. 정책 검사에는 OPA를 함께 씁니다.
- 진입: 환경 사이 가중치는 Cloudflare LB(FR-8), 환경 안 진입점은 Gateway API(Envoy, Cilium 구현)입니다. cloudflared는 앱이 아니라 Gateway로 보냅니다.
- StorageClass 이름 `bravo-db`, `bravo-general`은 모든 환경에서 같습니다.
- 객체 저장(Rook-Ceph RGW)과 비밀 금고는 클라우드 목적지(EKS · GKE)에도 같은 것을 둡니다. 클라우드 관리형 S3 · GCS · Secrets Manager는 "첫 이전 때 번역할 출발지"로만 다룹니다(D-23).
- 근거: EKS standard support 목록(1.34–1.37), K3s 채널(v1.35.9+k3s1), Istio ambient 플랫폼 요건(EKS · GKE · K3s 각각 설정이 있음).

## 2. 구성요소

### 2.1 클러스터와 인프라

| 구성요소 | 판 | 역할 | 누가 쓰나(근거) | 대안과 고르지 않은 이유 | 상태 |
|---|---|---|---|---|---|
| Amazon EKS | 1.35 | 플랫폼 · 출발지 | AGENTIC STAR Marketplace가 EKS · AKS에 설치됨(REA 분석), 2026 예선 Crystal | 자체 운영 제어면은 고객이 몰라도 되게 하기 어려움 | 결정 |
| GKE Standard | 1.35 | GCP 목적지(Autopilot은 kube-bench용 특권 Pod를 막아 쓰지 않음) | 2026 예선 Crystal(도쿄 GKE) | - | 결정, P2 |
| K3s | v1.35.9+k3s1 | 온프렘 목적지 | CNCF 프로젝트(SUSE 기증), 2025 수상 Green · 2026 Crystal 로컬 | RKE2: 일본 공개 채택 사례 못 찾음, 폐기(11/2). OpenShift: 일본 금융 사례 있음(MUFG ROSA, 라쿠텐은행)이나 VM 두 대에 무겁고 구독 필요 | 결정 |
| IaC | Terraform 1.17.1 | 환경 생성 · 삭제 | 업계 표준. 수상작 대부분 Terraform | OpenTofu(MPL)는 쓰지 않음. 설치는 공식 릴리스 + SHA256 확인(`tools/toolchain/install-terraform.sh`) | 결정(류지환, 2026-11-02) |
| VPC | 사설 서브넷만, Regional NAT, 인바운드 0 | 네트워크 | AWS 권장 구성 | 공개 서브넷 + 공인 IP는 노출 위험 | 제안(D-16) |

### 2.2 네트워크와 진입

| 구성요소 | 판 | 역할 | 누가 쓰나 | 대안과 이유 | 상태 |
|---|---|---|---|---|---|
| Cloudflare Tunnel · Access · LB | cloudflared 2026.10.7(N12 통과 판으로 고정) | 사용자 진입, 운영자 로그인, MCP 인가 서버, 환경 사이 링크 | Cloudflare Zero Trust(인증 목록은 검증 중) | Tailscale: 팀장 판단으로 제외. S2S VPN: UDP 필요, 폴백 | 제안(D-16) |
| Istio ambient(L4) | 1.31.3 | 플랫폼 클러스터 안 mTLS와 신원 인가 | CNCF 졸업 프로젝트. ambient는 GA | Cilium: CNI가 관리형마다 달라 뺌. 메시 없음: 서비스 사이 암호화를 앱마다 해야 함 | 제안(D-15), 11/4 판정 |
| Kyverno + OPA | Kyverno 1.19.2 | 정책 적용(digest 강제, 기본 차단 생성) · 계획 · IaC 정책 검사(OPA) | CNCF 프로젝트. 1급 원천이 OPA + Kyverno를 지정 | Calico 전역 정책: 뺌 | 결정 |
| Cilium + Hubble | 판은 인프라 갈래가 고정 | CNI, 네트워크 정책 집행, 흐름 관측, Gateway API(Envoy) | CNCF 졸업. GKE Dataplane V2의 바탕. 1급 원천이 지정 | 환경별 기본 CNI(flannel, VPC CNI 단독): 보안 관측이 환경마다 달라 뺌 | 결정(2026-11-02) |

### 2.3 데이터

| 구성요소 | 판 | 역할 | 누가 쓰나 | 대안과 이유 | 상태 |
|---|---|---|---|---|---|
| PostgreSQL | 16 | 앱 DB, 플랫폼 DB | 업계 표준 | - | 스펙 |
| CloudNativePG | 1.30.1 | 쿠버네티스 안 PostgreSQL 운영 | CNCF 프로젝트(단계는 검증 중), EDB 주도 | 출발지가 RDS인 고객이 많음 → 출발지 RDS 지원 여부 **열림** | 스펙 |
| PgBouncer | 1.26.x(PGDG 패키지로 직접 빌드) | 쓰기 경로 일시 정지 · 대상 전환 | PostgreSQL 커뮤니티 표준 풀러 | 커뮤니티 이미지(edoburu) 대신 직접 빌드 | 스펙 + 이미지 결정 |
| 논리 복제 | PostgreSQL 내장 | 무중단 이전, 되돌리기 | PostgreSQL 공식 기능 | DMS: AWS로 들어오는 방향 중심 | 스펙 |
| 관리형 PostgreSQL 끝점(RDS · Aurora PostgreSQL, Cloud SQL for PostgreSQL) | 출발지와 같은 메이저(랜딩 존 기본 16) | 첫 이전의 출발지. 고객이 원하면 목적지 DB. 앞에 우리 PgBouncer를 끼워 넣음 | AWS · Google 공식 관리형 서비스. 논리 복제 설정은 공식 문서로 확인(`docs/architecture/12-db-migration-matrix.md` 13절) | AlloyDB: 직접 연결에서 verify-full 불가로 보류. AWS DMS · Google DMS: 되돌리기 경로가 없어 정방향 대안으로만 | 제안(D-24), 검증 중 |
| OpenBao + External Secrets | 2.7.2, 차트 2.14.0 | 키 · 비밀의 유일한 보관(플랫폼 OpenBao, D-26). 고객 클러스터는 ESO만 | OpenBao: 2023년 Vault 포크, 채택 근거 약함(검증 중). ESO: CNCF 프로젝트 | HashiCorp Vault Community: 엔터프라이즈 표준이지만 BSL 1.1 | 스펙. **Vault로 바꿀지 열림** |
| 객체 저장(같은 S3 계약, 환경별 엔진) | - | 첨부 저장, 버킷 정책 | 온프렘: S3 호환 자체 저장소(후보 Rook-Ceph RGW, Rook은 CNCF 졸업). EKS: Amazon S3. GKE: Cloud Storage. 리서치 `research/platform-research/object-storage-cdn-2026-11-02.md` | SeaweedFS · Garage(버킷 정책 없음) · MinIO(저장소 보관 상태) · Cloudflare R2(버킷 정책 · 일본 데이터 위치 보장 없음) 제외. 클라우드 위 Ceph는 안티패턴 | 결정(D-22): 온프렘 엔진 Rook-Ceph RGW는 류지환 결정(2026-11-02), 11/3 실측 |
| S3, Secrets Manager, KMS | AWS | 출발지 | - | - | 스펙 |

### 2.4 플랫폼 서비스

| 구성요소 | 판 | 역할 | 상태 |
|---|---|---|---|
| Python | 3.12, uv 작업 공간 | 서비스 언어 | 제안(06) |
| FastAPI, Pydantic, uvicorn | 0.162.0, 2.14.2, 0.54.0 | API, 운영자 MCP | 제안 |
| psycopg | 3.3.6, ORM 없음, SQL 마이그레이션 | DB 접근 | 제안 |
| MCP Python SDK | 2.3.0(stateless) | 운영자 MCP 서버 | 결정(D-18) |
| PyJWT | 2.15.1 | Access JWT 검증 | 제안 |
| Cutover Executor | Python(파일럿) | 컷오버 실행 | 열림(스펙 §12: 담당이 Go · Python 결정) |
| 콘솔 | React 19, TypeScript 7.0.2, Vite, TanStack Query, nginx 비특권 | FR-13 화면 | 제안 |

### 2.5 AI

| 구성요소 | 판 | 역할 | 누가 쓰나 | 상태 |
|---|---|---|---|---|
| Claude Agent SDK | 0.2.176 | 리허설 에이전트 | Anthropic 공식 | 스펙 |
| LLM 게이트웨이 | - | 모델 키 보관, 비용 상한 | AGENTIC STAR LLM Gateway와 같은 자리 | 제안 |
| Harbor(에이전트 샌드박스 · 평가 프레임워크, CNCF 레지스트리 Harbor와 다름) | 0.25.0 | 리허설 Trial마다 격리 실행 환경과 분리 검증기 | 에이전트 평가 프레임워크(성숙도는 품질 검증 보고에 적음). 발표 첫 언급은 "Harbor(에이전트 샌드박스, CNCF 레지스트리 Harbor와 다름)" | 결정(D-02) |

### 2.6 관측

| 구성요소 | 판 | 역할 | 누가 쓰나 | 상태 |
|---|---|---|---|---|
| OpenTelemetry SDK · Collector | Collector 0.164.0 | 수집 경로 하나 | CNCF. AGENTIC STAR도 OTel Collector 사용 | 결정 |
| Prometheus | 3.15.1(OTLP 수신) | 지표 | CNCF 졸업. AGENTIC STAR 사용 | 결정 |
| Tempo | 2.10.10 | 트레이스 | Grafana Labs. AGENTIC STAR는 Jaeger | 결정 |
| Loki | 3.7.9 | 로그 | AGENTIC STAR 사용 | 결정 |
| Grafana | 13.3.0 | 대시보드 | 업계 표준 | 결정 |

개발용 올인원 이미지(`grafana/otel-lgtm`)는 쓰지 않습니다.

### 2.7 빌드와 배포

| 구성요소 | 역할 | 상태 |
|---|---|---|
| GitHub Actions + OIDC | CI, 장기 키 없음 | 스펙 |
| GHCR, digest 고정 | 이미지 저장 | 스펙 |
| cosign(키 없는 서명), SBOM, Trivy | 공급망 | 제안 |
| Argo CD 3.5.6 | 클러스터가 배포 저장소를 당겨 옴, 컷오버 · 보류 중 배포 거부 창 | 제안(D-16) |
| AWS CodeBuild(VPC 안) | 비공개 클러스터 첫 설치 1회 | 제안 |

## 2.8 품질 검증 반영(2026-11-02, `research/platform-research/enterprise-quality-2026-11-02.md`)

| 구성요소 | 조건 | 상태 |
|---|---|---|
| CIS 통제 비교(kube-bench) | 공개판 v0.16.0은 K3s 최신판을 옛 CIS 1.9로, EKS는 eks-1.8.0까지만 잼. kube-bench를 커밋으로 고정하고 벤치마크를 명시하며, EKS · GKE · K3s 결과를 통제 ID 대응표로 맞추고 "근사"로 표시 | 반영 예정(게이트) |
| EKS | 관리형 노드 그룹(Auto Mode는 kube-bench 검사 불가). 네트워크 정책은 Cilium이 집행하고, 리허설 네임스페이스는 정책을 먼저 만든 뒤 Pod를 띄움 | 반영 |
| GKE | Standard + Dataplane V2(생성 때만 지정 가능) | 반영 |
| K3s | CNCF Sandbox, FIPS 빌드 · STIG 없음, 릴리스 서명 없음 → 하드닝 가이드 전부 적용, sha256으로 확인. 고객이 FIPS를 요구하면 별도 검토 | 반영 |
| Rook-Ceph RGW | Ceph Tentacle 20.2.4 고정. 버킷 생성 직후 Block Public Access 네 항목을 켬. TLS 전용 정책은 `BoolIfExists`로 작성(AWS식 `Bool`은 Ceph에서 HTTP를 막지 못함) | 반영 |
| 금고 | OpenBao(MPL-2.0) 유지 권고. Vault Community는 BSL이고 설치기 내장이 "embedded" 정의에 걸림. 일본 사례는 Vault(Yahoo! JAPAN, Enterprise)만 확인 | 권고: OpenBao 유지 |
| Kyverno | v1.20에서 ClusterPolicy 제거 예정 → 기본 차단 생성은 `GeneratingPolicy`(`policies.kyverno.io/v1`)로 작성 | 반영 |
| PgBouncer | 1.26.1 이상 필수(SCRAM 사용자 혼동 결함 수정, `pgb_admin`에 영향). 서명 · SBOM이 있는 `ghcr.io/cloudnative-pg/pgbouncer` 이미지를 우선, 받을 수 없으면 PGDG 빌드 | 반영 |
| Cloudflare | 무료 요금제는 SLA 없음, 서비스 토큰 50개(테넌트 5개). ISMAP 등록은 정부용 제품만, Access 관리 데이터는 미국 · 암스테르담 저장 | D-16 남는 위험에 추가 |
| LLM 경로 | Anthropic API에 일본 리전 없음, ISMAP 미등록. 운영 경로는 Bedrock 도쿄 `jp.` 프로필(Opus 5.5, Haiku 5.5). 데모는 합성 데이터만 | 반영 |
| Harbor | 0.25.0, 1년 미만, 릴리스 서명 없음, 보안 정책 없음. 이슈 #2551(에이전트가 보상을 미리 써 둘 수 있음) → 검증 경계(V1–V6, T26a)로 막고, 배포 파일은 sha256으로 고정. CLI 이름 `harbor`도 CNCF 레지스트리와 겹침 | 팀장 결정 유지, 조건 반영 |
| Trivy | 2026-03 릴리스 경로 침해(CVE-2026-33634) → CI에서 Action을 커밋 SHA로 고정하고, 검사 작업에 OIDC · 비밀을 주지 않음 | 반영 |
| 쿠버네티스 판 | K3s 1.35 패치 종료 2026-12-28. 보고서는 1.36을 권고 | 데모 주간은 1.35, 데모 뒤 1.36으로 올림(열림) |

## 3. 열린 결정(팀 회의 안건)

| # | 결정할 것 | 왜 지금 | 권고 |
|---|---|---|---|
| 1 | 플랫폼이 고객 환경을 만드는 실행 경로(Terraform을 어디서, 어떤 자격으로 돌리나. 온프렘은 고객 VM에 무엇을 설치하나) | 제1 가치가 "몰라도 펼쳐 준다"인데 설계에 실행 주체가 없음 | 테넌트별 상태 파일 + 고객 계정의 교차 계정 역할(AWS) · WIF(GCP). 온프렘은 등록 명령 한 줄로 바깥 방향 에이전트 설치 |
| 2 | 출발지 DB로 RDS(PostgreSQL)를 지원하나 | AWS 고객 대부분은 RDS · Aurora. 지원 안 하면 RKE2와 같은 질문이 나옴 | 지원. 논리 복제는 RDS 설정으로 가능(검증 필요) |
| 3 | 플랫폼 DB를 CNPG로 둘지 RDS Multi-AZ로 둘지 | 플랫폼이 EKS로 바뀌면서 관리형 선택지가 생김 | 회의에서 결정 |
| 5 | OpenBao를 Vault로 바꿀지 | 2023년 포크라 채택 근거가 약함 | 품질 검증 보고(라이선스 조건 포함)로 결정 |
| 6 | Executor 언어 | 파일럿은 Python으로 만듦 | 담당이 확정 |

## 4. 바뀐 이력

| 날짜 | 바뀐 것 | 이유 |
|---|---|---|
| 2026-11-02 | RKE2 폐기 → EKS · GKE · K3s(D-21). 3-AZ RKE2 HA(D-17)는 EKS 관리형 제어면으로 대체 | 채택 근거 부족(AGENTIC STAR는 EKS · AKS, 일본 공개 사례 없음), 고객이 몰라도 되는 관리형 우선 |
| 2026-11-02 | Calico 전역 정책 → Kyverno 생성 정책 | CNI가 환경마다 달라짐 |
| 2026-11-02 | `grafana/otel-lgtm` → Collector + Prometheus · Tempo · Loki · Grafana | 개발용 이미지라서 |
| 2026-11-02 | Tailscale 제외(D-16) | 팀장 판단 |
| 2026-11-02 | SeaweedFS · TopoLVM 제외 → Rook-Ceph(RGW · RBD)(D-22) | 채택 근거가 약한 도구는 빼라는 팀장 결정. 도구 수도 줄어듦 |
| 2026-11-02 | Harbor는 에이전트 샌드박스로 확정(D-02) | 팀장 결정 |
| 2026-11-02 | IaC는 Terraform, 온프렘 객체 저장은 Rook-Ceph RGW(같은 S3 계약, EKS는 S3, GKE는 Cloud Storage) | 팀장 결정 |
| 2026-11-02 | CNI는 Cilium + Hubble(모든 환경), 환경 안 진입점은 Gateway API(Envoy), D-23은 "같은 계약 · 같은 경험" | 1급 원천 「기술 스택(툴) 조합」과 맞춤, 팀장 결정 |
| 2026-11-02 | 온프렘 · GKE · EKS에 같은 랜딩 존(D-23) | 같은 사용 경험과 쉬운 멀티 클라우드 이동이라는 팀장 결정 |
