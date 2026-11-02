# 데모 각본(T28)

스펙 10장(정본 사본 `docs/spec/spec-v0.1-r2-snapshot.html`)의 270초 흐름을 실제 환경에서 돌리는 각본입니다. 목표는 270초, 여유는 30초입니다. 스펙대로 클러스터 생성, 초기 복사, 기준선 통제 시험은 미리 실행하고 실행 ID와 시각을 보입니다. 게이트, AI 수정, 트래픽 전환, 컷오버, 되돌리기는 라이브로 합니다.

**규칙.**
- 실제 대상만 씁니다. 출발지 EKS, 온프렘 RKE2(CIS), GKE, 플랫폼 RKE2입니다. 2026-11-02 21:10 KST에 팀장이 RKE2를 스펙 r2대로 되돌렸습니다("스펙 그대로 전부", "RKE2 관련만", ADR 0010). 1.1절의 관측은 그 전에 만든 K3s · EKS 판입니다. 노트북 Compose 파일럿은 내렸습니다(`deploy/CLOUD-E2E-CONTRACT.md` 6.1).
- 일어나지 않은 결과는 적지 않습니다. 빠진 것이 있는 장면은 "준비 중: <빠진 것>"으로 적습니다.
- Compose 시절 측정은 "파일럿(노트북) 기록"으로만 인용합니다. 클라우드 결과가 아닙니다(`docs/evidence/pilot-notebook/`).
- 관측 시각은 2026-11-02 20:25–20:57 KST입니다. 다른 갈래가 같은 시간에 환경을 올리고 있어 상태가 빠르게 바뀝니다. 데모 전에는 `docs/demo/INTEGRATION-CHECK.md`로 다시 확인합니다.

## 1. 무대

### 1.1 환경

| 역할 | 실체 | 접근 | 관측(2026-11-02 KST, 읽기 전용) |
|---|---|---|---|
| 출발지 `source-aws` | Amazon EKS `bravo-source`, ap-northeast-2, K8s 1.36, t4g.large(arm64) 1대 | kubectl `bravo-source` | 20:25 노드 1 Ready, 앱 네임스페이스 없음. 20:49 Cilium · Hubble Running. 20:57 `cf-edge/cloudflared` 2, `chat/chat-db-1`, ESO, 역방향 링크 `dst-db-link` Running, `chat-pgbouncer`는 Init. 데모 앱 Pod는 아직 없음 |
| 목적지 1 `target-onprem` | 목표: Ubuntu VM + RKE2(CIS), 공통 cloud-init(T2). 지금 떠 있는 것: 결정 전 Lima VM 2대 위 K3s v1.36.5+k3s1(`infra/envs/target-onprem`) | kubectl `bravo-onprem`(RKE2도 같은 이름) | 준비 중: RKE2 설치(cloud-init 없음). K3s 판 관측: 20:49 노드 2 Ready. `chat`의 데모 앱 2, `chat-db-1`, `chat-pgbouncer`, `cf-edge/cloudflared` 2 Running. 20:50 `https://b-free-chat-dst.mangowhois.ai/api/config`가 HTTP 200, `env.id=onprem` |
| 목적지 2 `target-gcp` | GKE `bravo-target-gcp`, 프로젝트 `b-free-target-20261102`, asia-northeast1-b | 컨텍스트 미보고 | 20:49 `RUNNING`, 1.36.4-gke.2377000. 랜딩 존 설치는 확인하지 못함 |
| 플랫폼 | 목표: VPC ②의 EC2 1대 + RKE2 단일 노드(T7, 스펙 §7.2). 지금 떠 있는 것: 결정 전 Amazon EKS `bravo-platform`(1.36), 노드 그룹 core 3개(2a · 2b · 2c)와 agents 1개(taint `pool=agents:NoSchedule`) | RKE2 컨텍스트 없음. EKS 판은 kubectl `bravo-platform` | 준비 중: EC2 + RKE2 생성과 서비스 이전. EKS 판 관측: 20:30 `CREATING`, 20:49 `ACTIVE`. 20:56 `bravo-control`에 api, operator-mcp, console, collector, gate, rehearsal, `executor-0`, CNPG `platform-db`(DB `bravo`, 복제본 생성 중) Running. `openbao/openbao-0`, `bravo-observe`(Prometheus · Tempo · Loki · Grafana · Collector), `cf-edge/cloudflared` Running |
| Executor | 플랫폼 EKS `bravo-control/executor-0`(Service `executor:8081`). 클라우드 E2E 스크립트는 노트북 프로세스(`deploy/cloud/executor.sh`, `http://127.0.0.1:8085`)를 씀 | 둘 중 어느 것이 데모용인지 확인 필요 | 노트북 쪽은 20:56 응답 없음. 플랫폼 쪽은 Pod Running, API 응답은 확인하지 않음 |
| 키 보관소 | 플랫폼 OpenBao. 20:25에는 노트북 `platform-bao` 컨테이너, 20:56에는 플랫폼 EKS `openbao/openbao-0` | `deploy/pilot/scripts/keys.sh status`(BAO_ADDR 기본 127.0.0.1:8200) | 20:56 `docker ps`에 `platform-bao` 없음. 노트북 스크립트가 플랫폼 EKS 금고에 닿으려면 port-forward 필요 |
| 앞단 | Cloudflare `mangowhois.ai` | `infra/envs/edge` 출력 | `b-free`(플랫폼)는 Cloudflare Access 로그인으로 302(20:56). `b-free-chat`(고객 앱 대표)와 `b-free-chat-src`는 20:50에 530, 20:57에 502. `b-free-chat-dst`는 200(20:50). LB pool 없음(`pool_ids = {}`) |

이름은 계약 6절을 따릅니다. `b-free.mangowhois.ai`는 **우리 플랫폼**(콘솔 · API · MCP)입니다. 고객 데모 앱은 `b-free-chat`(대표), `b-free-chat-src`(출발지), `b-free-chat-dst`(목적지)입니다.

### 1.2 콘솔 화면(`platform/console`, 8개)

콘솔은 플랫폼 EKS에서 `https://b-free.mangowhois.ai`로 엽니다(Cloudflare Access 로그인). 20:56에 콘솔 Pod 2개가 Running이고 `b-free`가 Access 로그인으로 넘어갔습니다. 로그인 뒤 8개 화면이 실제 데이터를 띄우는지는 확인하지 않았습니다. 노트북 개발 서버(`VITE_API_BASE=<API 주소> pnpm dev`, `http://localhost:5173`)는 예비 경로입니다. **준비 중: Access 로그인 뒤 8개 화면 확인. 감사 A3(개발용 IdP 스텁)이 열려 있음.**

| # | 화면(주소 해시) | 보여 주는 것 | 데이터 출처 |
|---|---|---|---|
| 1 | `#/plan` 계획 · 승인 콘솔 | 상태 진행, 의존성 3등급, 게이트 판정과 통제 비교표, 승인 칸, 컷오버 단계와 쓰기 정지 ms, 영수증, 이벤트 | API REST · SSE |
| 2 | `#/approvals` 승인(change · cutover · failback) | 승인 대기와 결정, 결속 값 | SSE `approval.granted` |
| 3 | `#/agents` 에이전트 실시간 | Trial 시작, 제안, 차단 기록, 판정 | SSE `trial.*`, `tool.denied`, `analysis.done` |
| 4 | `#/clusters` 클러스터 실시간 | 노드, 네임스페이스, 워크로드 | 수집기 `GET /v1/clusters` |
| 5 | `#/workflow` CI/CD · 워크플로우 | 게이트 · 승인 · 실행 · 영수증 흐름, 통제 목록 | SSE, 게이트 `GET /v1/catalog` |
| 6 | `#/demo` 데모 앱 상태 | 앱 상태, 쓰기 수, 복제 준비, 앞단 이름이 가리키는 환경 | 앱 `healthz` · `api/status`, Prometheus, Executor `GET /v1/readiness`, DNS-over-HTTPS |
| 7 | `#/observe` 관측 | 지표 · 트레이스 링크와 즉시 질의 | Grafana · Prometheus · Tempo |
| 8 | `#/ending` 마무리 장면 | 남은 의존성 | 수집기 `GET /v1/inventory` |

### 1.3 스펙 문구 대신 쓰는 결정

| 스펙 r2 문구 | 데모에서 쓰는 것 | 근거 |
|---|---|---|
| 목적지 · 플랫폼 RKE2(CIS), 플랫폼 EC2 | **스펙 그대로**: 온프렘 Ubuntu VM + RKE2(CIS), 플랫폼 EC2 1대 + RKE2 단일 노드, 공통 cloud-init. K8s 1.36(RKE2 v1.36.5+rke2r1, 제안). 21:10 전의 K3s · 플랫폼 EKS 결정(ADR 0004)은 대체됨 | D-21 부분 철회, ADR 0010 |
| 목적지 하나(온프렘) | 온프렘 RKE2와 GKE. 같은 계약의 랜딩 존 | D-23, D-01(D-21의 남은 범위), ADR 0006 |
| SeaweedFS 첨부 저장소 | 같은 S3 계약 + 환경별 엔진(온프렘 RGW, GKE Cloud Storage). 게이트는 OBJ-01–06 행동 시험 | D-22, ADR 0005 |
| Tailscale 경로 | 목표는 Cloudflare Tunnel + Access 링크. 지금은 "임시 링크(D-16 전)": 플랫폼의 `kubectl port-forward`(`deploy/cloud/link.sh`) | D-16, ADR 0003, 계약 7절 |
| 목적지마다 OpenBao | 플랫폼 OpenBao 하나. 고객 클러스터는 ESO만 | D-26, ADR 0007 |
| 리허설 네임스페이스를 직접 띄움 | 리허설 1회 = Harbor Trial 1회 | D-02, ADR 0002 |
| 에이전트 워커 하나 | 역할별 에이전트 여섯, 구조화 출력 | D-27 |
| 모델 키는 플랫폼에만 | 데모: Claude Agent SDK + 팀장 구독 토큰(플랫폼 OpenBao, 워커가 한 번 읽음, 데모 전용). 대체 엔진: OpenAI Agents SDK. 운영 목표: 플랫폼 키를 쥔 LLM 게이트웨이 | ADR 0009 |
| Edge 가중치 10, 50, 100% | LB가 없으면 대표 CNAME으로 0%와 100%만. 10 · 50%는 "LB 구독 필요"로 기록하고 적용하지 않음 | `platform/executor/src/bravo_executor/front.py` |
| 네트워크 정책 집행 | 세 환경 Cilium + Hubble, 진입은 Gateway API | ADR 0008 |

## 2. 시작 전(T-15분)

| # | 하는 일 | 명령 | 기대하는 실제 신호 | 지금 |
|---|---|---|---|---|
| 1 | 통합 점검 | `docs/demo/INTEGRATION-CHECK.md` 1 · 2절 | 모든 항목 PASS | 준비 중: 대부분 미통과(그 문서의 "지금" 칸) |
| 2 | 금고 열림 확인 | `deploy/pilot/scripts/keys.sh status` | 봉인 풀림 | 확인하지 않음 |
| 3 | 임시 링크와 Executor | `deploy/cloud/link.sh up`, `deploy/cloud/executor.sh start`(또는 플랫폼 EKS `executor`) | `link.sh status`가 모두 up, `GET /v1/readiness` 응답 | 20:56 온프렘 관리 포트(28081)는 닿음, 출발지(18081)는 안 닿음. 노트북 Executor 응답 없음 |
| 4 | reset 계획 | `deploy/scripts/reset-demo.sh --dry-run` | 요약 줄 `FAIL 0`, `NOT_CONNECTED 0` | 20:56 `PLAN 6`, `NOT_CONNECTED 3`(4절) |
| 5 | reset 실행 | `deploy/scripts/reset-demo.sh` | 종료 코드 0 | 실행하지 않음(다른 갈래가 같은 클러스터에서 작업 중) |
| 6 | 사전 실행 기록 화면 열기 | `docs/evidence/` 행 | 클러스터 생성 · 초기 복사 · 기준선 통제 시험의 실행 ID와 시각 | 준비 중: 클라우드 사전 실행 기록이 없음 |

## 3. 장면

각 장면의 표는 순서대로 진행합니다. "증거"는 그 조작이 남기는 기록입니다.

### 0:00 S1. 목적지와 원본 비교, 게이트 1차 BLOCK(40초)

스펙이 보이라는 것: FR-1–3, FR-6, FR-12. 이 장면이 끝날 때 S2 리허설을 함께 시작합니다.

| 순서 | 화면 | 조작(명령 · API) | 증거 | 상태 |
|---|---|---|---|---|
| 1 | `#/demo`, 앱 `/join` | 심사위원이 QR로 `https://b-free-chat.mangowhois.ai/join`에 들어와 채팅 | 출발지 `chat-db`의 `messages` 행, Prometheus `chat_writes_total` | 준비 중: 출발지 EKS의 데모 앱(20:57 Pod 없음, `chat-pgbouncer` Init). `b-free-chat`, `b-free-chat-src`는 20:57에 HTTP 502(커넥터는 붙었고 원점이 아직 없음) |
| 2 | `#/clusters` | 사전 구축 기록을 보임: 빈 Ubuntu VM에 공통 cloud-init으로 RKE2를 CIS 프로필로 세운 실행 기록(스펙 S1 기본 흐름 4, 인수 조건 "RKE2가 CIS 프로필로 뜨고 실행 기록이 남는다"), `terraform -chdir=infra/envs/<env> output`, 수집기 `GET /v1/clusters` | cloud-init 로그, `rke2-server` 기동 기록, kube-bench 결과(T2), Terraform 상태 | 인수 조건의 실행 기록 있음: 새 빈 Lima VM에 공통 cloud-init(`infra/modules/cluster/onprem/cloud-init`)으로 RKE2 CIS, API 준비 75초, 노드 Ready(23:54–23:58 KST, 버리는 VM). `docs/evidence/s1-cis-refusal/04-accept.log`, 설명은 같은 폴더 `README.md`. 남음: `terraform output`과 수집기 `GET /v1/clusters` 화면은 이 기록에 없음 |
| 2a | `#/clusters`, `#/workflow` | 스펙 S1 예외 4a를 사전 실행 기록으로 보임: CIS 프로필이 조건 미충족(sysctl, etcd 사용자)으로 시작을 거부함. 공통 cloud-init의 조건을 고치고 재설치함 | 거부 기록(`rke2-server` 로그), 고친 cloud-init의 diff, 재설치 뒤 기동 기록과 kube-bench 결과 | 사전 실행 기록 있음(23:37–23:52 KST, 버리는 Lima VM `bravo-cis-demo`): `docs/evidence/s1-cis-refusal/README.md`. 거부 원문 `01-refuse.log`(23:37:47 sysctl, 진단 23:48:33 etcd 사용자), 고친 diff `cloud-init-fix.diff`, 재설치 `02-fix.log`(API 92초, 노드 Ready), kube-bench PASS 76 · FAIL 5. 남음: 공통 경로가 거부를 614초 뒤에야 실패로 알림(README 문제 1) |
| 3 | `#/plan` 의존성 칸, `#/ending` | 수집기 `GET /v1/inventory`(앱 코드 · 차트 정적 탐지, 3등급) | 응답의 `input_digest`, `rules_version` | 탐지기는 동작함. 20:30 응답은 설정 교체 2, 어댑터 추가 1, 코드 수정 2, 이전 불필요 2, 미분류 1(파일럿(노트북) 기록, Compose 수집기). 준비 중: 플랫폼 EKS 수집기의 응답 확인, 출발지 설정 수집(S3 정책 · IAM · KMS)의 읽기 전용 역할 |
| 4 | `#/plan` 게이트 칸 | 운영자 MCP `plan_create` 뒤 `env_controls`, 게이트 `GET /v1/controls?source=source-aws&target=target-onprem&level=control`. 환경 등록부는 `deploy/environments/real-targets.yaml` | 통제 ID별 PASS · FAIL · UNKNOWN 표, `rule_version` | 게이트 Pod Running(20:56). 준비 중: 실제 환경 대상 시험 실행 기록. 감사 A5 · B16 · B17(OBJ-05 · 06 거짓 PASS 등)이 열려 있어 그 전에는 결과를 보이지 않음 |
| 5 | `#/plan` 게이트 칸 | MCP `gate_run`(또는 `POST /v1/plans/{id}/gate`) | `gate.verdict` 이벤트 BLOCK, 이유에 OBJ-01(목적지 버킷의 서명 없는 GET 허용) | 열린 버킷 심기 있음: `deploy/cloud/rgw_regression.py --plant-regression`(심은 시나리오, 기본 꺼짐, `--remove-regression`으로 제거). 23:33 게이트 CLI(플랫폼 게이트와 같은 코드)로 OBJ-01 dst FAIL · BLOCK 실측, 23:45 현재 심은 상태로 둠(`docs/evidence/s1-s2-bucket/`). 준비 중: 플랫폼 게이트 등록부에 목적지 `object_store`가 없어 콘솔 경로에서는 OBJ-01이 `not_configured` |
| 6 | `#/agents` | MCP `rehearsal_start`(또는 `POST /v1/plans/{id}/rehearsals`) | `trial.started` 이벤트 | 준비 중: 아래 S2 |

**대체 장면.** 파일럿(노트북) 기록에서 게이트 1차 BLOCK의 재료는 DB 통제 `DB-ANON-READ`입니다. 목적지 FAIL을 `smoke-20261102T194530/controls-before.json`이 보입니다. 열린 첨부 저장소 장면은 아닙니다.

### 0:40 S2. 리허설, 차단 기록, 변경 승인, 재대조 PASS(50초)

스펙이 보이라는 것: FR-4–7, NFR-4.

| 순서 | 화면 | 조작(명령 · API) | 증거 | 상태 |
|---|---|---|---|---|
| 1 | `#/agents` | 상태 머신이 리허설 서비스 `POST /v1/trials`를 부름. 모델 경로 `claude_subscription`(데모 전용, ADR 0009). Trial 네임스페이스 `rehearsal-<plan>-<n>`이 목적지 RKE2에 생김 | Trial 폴더(Harbor 형식), `trial.proposed` 이벤트, `docs/evidence/rehearsals.tsv` 한 줄 | 리허설 서비스 Pod Running(20:56). 준비 중: 실제 Trial 1회, 목적지 kubeconfig(없으면 `cluster_unconfigured`), digest 고정 샌드박스 · 검증기 이미지. 감사 B8(모든 역할 리허설이 INFRA_ERROR), B9(`repo_read` 루트) 열림 |
| 2 | `#/agents` | 에이전트가 정리를 이유로 버킷 삭제를 시도. 도구 계층이 파괴적 동사를 거부. 네임스페이스 밖 접속은 Cilium NetworkPolicy가 막음 | `tool.denied` 이벤트, Hubble 흐름 `POLICY_DENIED` | 도구 계층 거부는 시험으로 확인됨(`platform/rehearsal/tests/test_tool_layer.py`). 준비 중: 실제 Trial에서 난 기록 |
| 3 | `#/agents` | 분리 검증기가 `verify-<plan>-<n>`에서 판정 | `trial.verdict` PASS, 검증 Pod UID, 산출물 digest | 준비 중: 실제 Trial. 감사 B7(검증기는 제안 JSON 정적 검사뿐) 열림 |
| 4 | `#/approvals` change | 승인자가 콘솔에서 승인(요청자와 다른 사람). 반영은 T31 경로(신뢰된 빌드, cosign 서명, 승인 digest와 대조 뒤 배포) | `approval.granted`(stage `change`), 배포 digest | 준비 중: API 승인 단계가 `cutover`, `failback`뿐이고 `change`가 없음(`platform/api/src/bravo_api/migrations/0003_full_path.sql`). T31 반영 경로 없음 |
| 5 | `#/plan` | `gate_run` 다시 | `gate.verdict` PASS | 준비 중: S1 순서 5와 같음 |

**대체 장면.** 파일럿(노트북) 기록의 재대조 PASS는 사람이 `fix-dst-grant.sql`로 고친 결과입니다(`smoke-20261102T194530/controls-after.json`). AI 수정의 기록이 아닙니다.

### 1:30 S3 전반. 부하 중 트래픽 전환(40초)

스펙이 보이라는 것: FR-8.

| 순서 | 화면 | 조작(명령 · API) | 증거 | 상태 |
|---|---|---|---|---|
| 1 | `#/demo`, `#/observe` | 부하 시작. 스펙 기준 쓰기 50 req/s. `deploy/cloud/run_e2e.sh`가 출발지 PgBouncer로 loadgen을, 앞단 `wss://b-free-chat.<zone>/ws`로 채팅 트래픽을 보냄 | loadgen 요약, `chat_writes_total` | 준비 중: 클라우드 실행 기록 없음. 기본 비율은 `LOADGEN_RATE`(`cloud.env`). 감사 B14 · B15(부하 생성기 판정 결함, 50 req/s 주장) 열림 |
| 2 | `#/demo` 앞단 블록 | Executor `POST /v1/front/shift {"target_env":"dst"}`(`Idempotency-Key` 필수). LB가 없으면 10 · 50%는 "LB 구독 필요"로 기록하고 100%만 CNAME 전환 | Executor run(`GET /v1/runs/{run_id}`, kind `shift`), 반영 시간(연속 프로브 5회가 `env.id=onprem`) | **코드만, 미실행**(감사 B21). 준비 중: `b-free-chat-src` 커넥터(530), 실제 전환 1회 기록, LB pool(계정에 LB가 켜졌다는 전달은 있으나 `pool_ids = {}`) |
| 3 | `#/demo` | 오류율이나 p95가 기준을 넘으면 0%로 복귀 | 단계 기록의 판정 | 준비 중: LB 모드에서만 동작하고 실행한 적 없음 |

상태 머신의 SHIFT 상태와 Executor 앞단 전환을 잇는 API 호출은 찾지 못했습니다(`platform/api`에 앞단 호출 없음). 지금은 운영자가 Executor에 직접 요청합니다. 스펙 §8.2가 트래픽 전환을 사람 운영자가 시작하는 구간으로 두므로(D-18) 콘솔 버튼으로 옮겨야 합니다. **준비 중: 콘솔에서 앞단 전환 시작.**

### 2:10 S3 컷오버. 쓰기 정지, 영수증, 채팅 유지, 통제 전후 비교(50초)

스펙이 보이라는 것: FR-9–10, FR-12, NFR-1–2.

| 순서 | 화면 | 조작(명령 · API) | 증거 | 상태 |
|---|---|---|---|---|
| 1 | `#/approvals` cutover | 승인자가 콘솔에서 승인(`POST /v1/plans/{id}/approvals`). 승인은 계획 revision과 게이트 판정에 묶임 | `approval.granted`(stage `cutover`) | 코드 있음, API Pod Running(20:56). 준비 중: Access 로그인 뒤 콘솔 승인 1회. 감사 A2(Executor가 승인 표를 확인하지 않음), A3(개발용 IdP) 열림 |
| 2 | `#/plan` 컷오버 칸 | 운영자가 콘솔에서 시작(`POST /v1/plans/{id}/shift {"stage":"cutover"}`). API가 Executor `POST /v1/cutover`를 부름. 쓰기 차단(각 앱 `chat-admin`), PgBouncer PAUSE, 펜싱, barrier · 비교, 복제 전환, 확정, 두 PgBouncer 대상 전환(`pods/exec`), 쓰기 확인 | Executor run의 단계별 ms, `write_pause_ms` | 준비 중: 출발지 `chat`(CNPG · PgBouncer · `chat-admin`), 임시 링크, Executor, 정방향 복제(`deploy/cloud/repl_setup.sh setup`). 클라우드 컷오버 기록 없음 |
| 3 | `#/plan` 영수증 칸 | 실행 끝에 영수증 저장 | `receipt.saved` 이벤트, 행 수 · PK 해시 · 내용 해시 · 시퀀스 | 준비 중: 위와 같음. 감사 B10(첨부 객체 `NOT_CHECKED`인데 MATCH) 열림 |
| 4 | 앱 화면, `#/demo` | 열린 WebSocket을 서버가 닫고(코드 1012) 브라우저가 다시 붙음. 심사위원 채팅 기록이 남음 | 메시지 ID 연속, 앱 머리의 "연결된 곳: 온프렘" | 준비 중: 출발지 앱 |
| 5 | `#/plan` 게이트 칸 | 통제를 다시 시험해 전후 비교표를 남김 | 컷오버 뒤 통제 결과 | 준비 중: S1 순서 4와 같음 |

**대체 장면.** 파일럿(노트북) 기록입니다. `smoke-20261102T194530`: 쓰기 정지 87 ms, 영수증 MATCH(`messages` 322행), 부하 800건 ack 800, 채팅 400건 ack 400. 컷오버 DONE 8회의 쓰기 정지는 76–139 ms입니다(`docs/evidence/pilot-notebook/RUNS.tsv`). 부하는 초당 20건이고, PostgreSQL 공식 이미지 위에서 잰 값입니다.

### 3:00 S4. 목적지 장애 주입, 데이터째 되돌리기(50초)

스펙이 보이라는 것: FR-11.

| 순서 | 화면 | 조작(명령 · API) | 증거 | 상태 |
|---|---|---|---|---|
| 1 | `#/demo` | 목적지 앱 장애 주입: `deploy/cloud/t25.sh fault`(목적지 앱 쓰기 차단으로 HTTP 503 · WS nack, 앞단 프로브가 감지) | 프로브 기록, 오류율 | 코드만, 미실행. 준비 중: 컷오버가 끝난 클라우드 상태. 확인 필요: `t25.sh fault`는 감지 뒤 Executor `POST /v1/failback`을 바로 부름(승인 ID는 필요). AGENTS.md와 D-18은 되돌리기를 콘솔의 사람이 시작하게 함. 데모에서는 장애 주입만 쓰고 되돌리기는 순서 2처럼 콘솔에서 시작함 |
| 2 | `#/approvals` failback | 승인자가 failback 승인(컷오버 run에 묶임). 운영자가 `POST /v1/plans/{id}/shift {"stage":"failback"}` | `approval.granted`(stage `failback`), Executor `POST /v1/failback` run | 준비 중: 위와 같음 |
| 3 | `#/plan` 영수증 칸 | 되돌리기 영수증 확인, 목적지에서 쓴 메시지가 출발지에 있는지(S4-AC1) | failback run, 영수증, S4-AC1 비교 | 준비 중: 위와 같음 |
| 4 | `#/demo` | 앞단을 출발지로 되돌림(`POST /v1/front/shift {"target_env":"src"}`) | shift run | 코드만, 미실행 |

**대체 장면.** 파일럿(노트북) 기록 `smoke-20261102T194530`: 되돌리기 49 ms, MATCH, S4-AC1 목적지에서 쓴 496행이 출발지에 같은 해시로 있음. 같은 왕복 시나리오 `smoke-20261102T191253`은 S4-AC1이 314행 대 599행으로 달라 FAIL입니다. 대체 장면으로 쓰지 않습니다.

### 3:50 마무리. 감사 기록, 비용, 잔존 의존성 지도(20초 + 여유)

스펙이 보이라는 것: FR-14, NFR-8. 스펙 조건: T24 · T27이 없으면 비용 조회와 사전 실행 기록 화면으로 대체합니다.

| 순서 | 화면 | 조작(명령 · API) | 증거 | 상태 |
|---|---|---|---|---|
| 1 | `#/ending` | 감사 기록 조회(T24 Object Lock) | 데모 1회의 전체 기록, 삭제 거부 | 준비 중: T24 없음. 콘솔 칸은 "연결 안 됨" |
| 2 | `#/ending` | `component` 태그별 비용 조회(T1, NFR-8) | 이전 중 추가 비용 | 준비 중: 비용 조회 연결 없음 |
| 3 | `#/ending` | 수집기 `GET /v1/inventory`의 남은 의존성 | 의존성 목록 | 탐지기는 동작함(S1 순서 3). 준비 중: 플랫폼 EKS 수집기의 응답 확인, Exit Drill(T27, `deploy/cloud/exit_drill.sh`) 결과 |

## 4. 데모 사이 reset

명령은 `deploy/scripts/reset-demo.sh`입니다. 같은 명령을 여러 번 돌려도 끝 상태가 같고, 대상마다 한 일을 한 줄씩 냅니다.

| 단계 | 하는 일 | 지우지 않는 것 |
|---|---|---|
| `rehearsal` | 라벨 `bravo.dev/trial-id` 또는 `bravo.io/tier=rehearsal\|verify`인 네임스페이스 삭제 | 그 밖의 네임스페이스 |
| `approvals` | 플랫폼 DB에 열린 승인 · 승인 요청이 0건인지 확인. 남으면 만료(기본 900초)까지 기다린 뒤 다시 읽음 | 승인 기록(감사, append-only). 새 데모는 새 계획을 만들고, 옛 승인은 옛 계획 revision에 묶여 쓰이지 않음 |
| `front` | 고객 앱 대표 이름을 출발지로: Executor `POST /v1/front/shift {"target_env":"src"}` 뒤 `env.id=aws` 확인 | - |
| `data` | PgBouncer 활성 대상이 `target-src.ini`가 아니면 rollout restart. 복제가 컷오버 전 상태가 아니면 `repl_setup rearm`(구독 · 슬롯 정리, 목적지 표 비움, 다시 복사) | 출발지 데이터. rearm은 컷오버 뒤 되돌리기 전이면 거부함(되돌리기는 콘솔에서 사람이) |
| `writes` | 각 환경 `chat-admin`의 쓰기 차단을 끔(토큰은 플랫폼 OpenBao, executor AppRole) | - |

**dry-run 결과(2026-11-02 20:56 KST, 읽기만).** 다른 갈래가 같은 클러스터를 쓰고 있어 실제 실행은 하지 않았습니다. 온프렘의 `rehearsal-pytest27-23001`은 다른 갈래의 시험이 남긴 Trial 네임스페이스로 보입니다. 실제 실행이면 지웁니다.

```text
[reset-demo] rehearsal source-aws               ALREADY       리허설 · 검증 네임스페이스 0개
[reset-demo] rehearsal target-onprem            PLAN          삭제할 것: namespace/rehearsal-pytest27-23001
[reset-demo] rehearsal target-gcp               NOT_CONNECTED 컨텍스트 미설정
[reset-demo] approvals platform-eks             PLAN          열린 승인 · 요청을 읽기 전용 SELECT로 셈. 남으면 만료까지 기다림(최대 900s). 지우지 않음
[reset-demo] front     b-free-chat.mangowhois.ai NOT_CONNECTED 지금 HTTP 530 env.id=-. Executor http://127.0.0.1:8085 응답 없음(deploy/cloud/executor.sh start)
[reset-demo] data      pool:src                 PLAN          active.ini를 target-src.ini와 비교. 다르면 deploy/chat-pgbouncer rollout restart(init 컨테이너가 target-src.ini 복사)
[reset-demo] data      pool:dst                 PLAN          active.ini를 target-src.ini와 비교. 다르면 deploy/chat-pgbouncer rollout restart(init 컨테이너가 target-src.ini 복사)
[reset-demo] data      repl:topology            PLAN          repl_setup status를 읽고 컷오버 전 상태가 아니면 rearm(구독 · 슬롯 정리, 목적지 표 비움, 다시 복사). 링크: 임시 링크(D-16 전)
[reset-demo] writes    entry:aws                NOT_CONNECTED http://127.0.0.1:18081 닿지 않음(임시 링크 port-forward가 없음)
[reset-demo] writes    entry:onprem             PLAN          GET http://127.0.0.1:28081/admin/writes(토큰 OpenBao envs/target-onprem/chat-admin), blocked면 PUT {"blocked": false} 뒤 다시 읽음
[reset-demo] 요약: DONE 0, ALREADY 1, PLAN 6, NOT_CONNECTED 3, FAIL 0
```

20:46의 첫 dry-run은 `NOT_CONNECTED 7`이었습니다. 그 사이 출발지에 `chat-pgbouncer`가 생기고, 플랫폼 EKS 컨텍스트와 온프렘 관리 포트 포워드가 생겼습니다.

## 5. 대체 장면(사전 실행 기록)

스펙 T28은 실패할 때 쓸 사전 실행 기록을 요구합니다. 지금 있는 기록은 모두 파일럿(노트북) 기록입니다. 발표에서 쓸 때는 화면에 "파일럿(노트북) 기록, 클라우드 아님"을 함께 보입니다.

| 장면 | 쓸 수 있는 기록 | 판정(`RUNS.tsv`) | 한계 |
|---|---|---|---|
| S1 게이트 1차 BLOCK | `smoke-20261102T194530/controls-before.json`(DB-ANON-READ 목적지 FAIL) | PASS | 열린 버킷이 아니라 DB 권한 퇴행 |
| S2 재대조 PASS | 같은 실행 `controls-after.json` | PASS | 사람이 고침. AI 수정 아님 |
| S3 컷오버 | `smoke-20261102T194530`(87 ms, MATCH) | PASS | 초당 20건, 가짜 승인 ID(감사 A2), 첨부 객체 미대조(B10) |
| S3 불일치 중단 | `smoke-20261102T194650`(ABORT_PRE_WRITE) | PASS(기대한 중단) | 같음 |
| S4 되돌리기 | `smoke-20261102T194530`(49 ms, S4-AC1 496행) | PASS | 같음 |
| 클라우드 기록 | 없음 | - | 준비 중 |

## 6. 준비 중 목록

| 장면 | 빠진 것 | 관련 작업 |
|---|---|---|
| 공통 | 플랫폼을 EC2 1대 + RKE2 단일 노드로 세우고(공통 cloud-init) 지금 EKS 판에서 도는 서비스 · OpenBao · 관측을 옮기기. RKE2 컨텍스트를 계약 2절에 보고 | T7, ADR 0010 |
| 공통 | Access 로그인 뒤 콘솔 화면 확인, 데모용 Executor 하나로 정하기, 노트북 스크립트의 금고 경로(port-forward) | T10, T21 |
| 0:00 S1 | 온프렘 Ubuntu VM + RKE2(CIS) 공통 cloud-init, kube-bench 결과, 사전 실행 기록. `rke2` 랜딩 존 overlay(Cilium 붙이는 방법, 블록 저장 바탕 포함) | T2, ADR 0010 |
| 0:00 S1 | 예외 4a 실행 기록은 있음(`docs/evidence/s1-cis-refusal/`). 남음: 부트스트랩의 `systemctl enable --now`가 시작 실패에도 exit 0이라 거부를 늦게 알림 | T2 |
| 공통 | 클라우드 사전 실행 기록(`docs/evidence/`). 270초 통합 리허설 3회 연속 | T28 |
| 0:00 S1 | 출발지 EKS의 데모 앱과 `chat-admin`, `chat-pgbouncer` 기동. `b-free-chat` 502 해소 | T6 |
| 0:00 S1 | 실제 환경 통제 시험 실행과 감사 A5 · B16 · B17 해소 | T14, T19 |
| 0:00 S1 | 온프렘 Rook-Ceph RGW와 열린 버킷(OBJ-01 BLOCK의 재료) | T9 |
| 0:00 S1 | 출발지 설정 수집의 읽기 전용 역할 | T16 |
| 0:40 S2 | 실제 Harbor Trial(목적지 kubeconfig, 이미지, 모델 경로)과 감사 B7 · B8 · B9 해소 | T15 |
| 0:40 S2 | `change` 승인 단계와 T31 반영 경로 | T10, T31 |
| 1:30 S3 | 클라우드 부하 실행(50 req/s)과 감사 B14 · B15 해소 | T18 |
| 1:30 S3 | 앞단 전환 실제 실행 1회, LB pool, 콘솔에서 전환 시작 | T17, T21 |
| 2:10 S3 | 임시 링크, Executor, 정방향 복제, 클라우드 컷오버 기록. 감사 A2 · B10 해소 | T8, T12, T13, T30 |
| 3:00 S4 | 장애 주입(`t25.sh fault`)과 되돌리기의 클라우드 실행 | T20, T25 |
| 3:50 마무리 | 감사 기록(T24), 비용 조회, Exit Drill 결과 | T1, T24, T27 |
| GKE | GKE 컨텍스트 보고, 랜딩 존 설치 확인. 270초 각본에서 GKE 장면의 자리(목적지 둘째)를 정함 | D-01, T2 |

## 7. 리허설 기록

T28 완료 기준은 "270초 안에 끝나는 리허설 3회 연속 성공"입니다.

| 회차 | 시각 | 걸린 시간 | 결과 | 증거 |
|---|---|---|---|---|
| - | - | - | 아직 없음 | - |
