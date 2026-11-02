# 데모 각본(T28)

스펙 10장(정본 사본 `docs/spec/spec-v0.1-r2-snapshot.html`)의 흐름을 실제 환경에서 돌리는 각본입니다. **기준 시각: 2026-11-03 04:10 KST.** 모든 시간은 실측값이고 증거 경로를 붙였습니다. 재지 않은 것은 "미측정", 재는 중인 것은 "측정 중" · "재측정 중"으로 적습니다.

**4절의 5분 안무는 제안입니다(팀 결정 필요).** S2 리허설이 227–315초라 5분 라이브에 들어가지 않습니다. 그래서 S2를 발표 전에 시작하는 안을 시간과 함께 적었습니다.

**규칙.**
- 실제 대상만 씁니다. 출발지 EKS, 온프렘 RKE2(CIS), GKE, 플랫폼 RKE2입니다. 지금 환경의 정본은 `deploy/CLOUD-E2E-CONTRACT.md` 0절입니다.
- 일어나지 않은 결과는 적지 않습니다. 사전 실행 기록은 화면에 "사전 실행"과 실행 ID · 시각을 함께 보입니다. Executor 실행(shift · cutover · failback)은 콘솔 `#/replay`의 `mode: recorded` 재생으로만 보입니다(계약 15 §12).
- 하네스가 넣은 시도(probe)는 에이전트 행동으로 말하지 않습니다(`origin=probe`).
- 노트북 Compose 파일럿 수치는 "파일럿(노트북) 기록, 클라우드 아님"으로만 인용합니다.
- 지난 관측(K3s, 플랫폼 EKS, 노트북 Executor · 임시 링크, CNAME 전환)은 9절 "역사"에만 둡니다. 실행 지침이 아닙니다.

## 0. 시간 상자

킥오프 자료(팀 연구 폴더 `kickoff-2026-10-30/README.md`, 슬라이드 기준)의 일정입니다.

| 날 | 형식 | 데모에 주는 뜻 |
|---|---|---|
| 11/7(토) 14:00–17:00 중간 보고 | 7분: 발표 3분 + 질의 4분, 별실. 슬라이드 없이 설계 자료로 시스템 30초 설명 + 아키텍처 | 라이브 전체 흐름은 들어가지 않음. 제안(팀 결정 필요): 설계 자료 3분, 실행 기록은 질의 때 `#/replay`와 증거 폴더로 |
| 11/8(일) 14:00–16:00 최종 발표 | 5분(통역 제외). **슬라이드 금지.** 설계 문서 + 데모. 넘으면 강제 종료될 수 있음 | 4절 안무. 넘칠 장면은 미리 대체 변형(4.2)으로 넘김 |

## 1. 무대

### 1.1 환경(11/3 04:07 KST 관측)

| 역할 | 실체 | kubectl 컨텍스트 | 공개 이름 → `env.id` |
|---|---|---|---|
| 출발지 `source-aws` | Amazon EKS `bravo-source`, 노드 2(t4g.large, arm64) | `bravo-source` | `b-free-chat-src` → `aws` |
| 목적지 `target-onprem` | Lima VM 2대 위 RKE2 `v1.36.5+rke2r1`(CIS), Rook-Ceph RGW, CNPG PG16 | `bravo-onprem` | `b-free-chat-dst` → `onprem` |
| 목적지 `target-gcp` | GKE `bravo-target-gcp`. 비교 열 · 예외 장면만, 컷오버 대상 아님(D-01) | `bravo-target-gcp`(별도 kubeconfig) | `b-free-chat-gke` → `gcp` |
| 플랫폼 | EC2 1대 + RKE2 단일 노드. 콘솔 · API · 게이트 · 수집기 · 리허설 · `executor-0` · OpenBao | `bravo-platform-rke2` | `b-free` → Access 로그인 |
| 앞단 | Cloudflare LB `b-free-chat`(pool src · dst, 시작 상태 src 100%) | - | `b-free-chat` → `aws` |

- 데모 앱은 세 클러스터 모두 같은 digest `bravo-demo@sha256:76e8f595…`(0.3.3)입니다(`docs/evidence/app-0.3.3/`, 04:07 다시 확인).
- 플랫폼 서비스의 실행 digest는 `deploy/platform/control/kustomization.yaml`의 고정값과 같습니다(04:0x: api `46edd0d1`, console `a2d8c7df`, gate `c841a75e`, executor `b2732bed`, rehearsal `5c554f5b`, collector `af3c66c1`).

### 1.2 사람과 신원

| 누가 | 신원 | 하는 일 | 04:07 상태 |
|---|---|---|---|
| 운영자(요청자) | naver 메일, 역할 operator | 계획 · 리허설 시작, 예외 요청, "전환 시작", 되돌리기 시작, 장애 주입 | **첫 로그인 전.** 플랫폼 DB `principals` 1행(approver만). 첫 로그인 전에는 역할이 없음 |
| 승인자 | gmail 메일, 역할 approver | change · cutover · failback 승인, 예외 승인 | 로그인함 |

두 메일은 모두 팀장의 것입니다. 무대에서 다른 팀원이 승인자가 될지는 팀 결정입니다(`docs/spec/MEETING-2026-11-03-deviations.md` 4절).

### 1.3 콘솔 화면(`platform/console`, r17 `a2d8c7df`)

| 화면(해시) | 보여 주는 것 | 쓰는 장면 |
|---|---|---|
| `#/home` | 계획 목록, 환경 연결 | 시작 |
| `#/plan` | 상태 진행, 게이트 표(질문마다 시험 방법), 예외, 리허설, change · cutover 승인, "전환 시작", SHIFT 10/50/100 단계 판정, 컷오버 단계 · 쓰기 정지 ms, 영수증(`verdict_parts`, 검사 범위) | S1–S4 |
| `#/approvals` | 승인 기록 | S2–S4 |
| `#/workflow` | 게이트 · 승인 · 실행 · 영수증 흐름, 통제 목록 | S1 |
| `#/cost` | 비용 영수증(`cost/v1`) | 마무리 |
| `#/replay` | 끝난 실행의 기록 재생. 화면 위에 "기록 재생 · 원래 시각"이 늘 보임 | 대체 변형 |
| `#/clusters` | 세 클러스터 노드 · 워크로드 | S1 |
| `#/demo` | 데모 앱 상태, 앞단이 가리키는 환경 | S3 · S4 |
| `#/observability` | 지표 · 트레이스, 환경별 오류율 · 지연 | S4, 마무리 |
| `#/agents` | Trial 진행(`trial.progress`), 도구 거부(`origin` 표시), 판정 | S2 |
| `#/ending` | 남은 의존성 | 마무리 |

### 1.4 스펙 문구 대신 쓰는 결정

| 스펙 r2 문구 | 데모에서 쓰는 것 | 근거 |
|---|---|---|
| 목적지 하나(온프렘) | 온프렘 RKE2 + GKE 비교 열 · 예외 장면. GKE로는 옮기지 않음 | D-01, D-23 |
| SeaweedFS | 같은 S3 계약 + 환경별 엔진(온프렘 RGW, GKE GCS) | D-22, ADR 0005 |
| Tailscale | Cloudflare Tunnel + Access 링크(D-16) | D-16, ADR 0003 |
| 목적지마다 OpenBao | 플랫폼 OpenBao 하나 | D-26, ADR 0007 |
| Actions → GHCR → 같은 digest pull | 플랫폼 빌드 · OpenBao transit 서명 · 목적지별 전달, 결속은 digest | D-28 |
| 리허설 네임스페이스를 직접 띄움, 워커 하나 | 리허설 1회 = Harbor Trial 1회, 역할별 에이전트(데모는 governance · app_migration 둘) | D-02, D-27 |
| 모델 키는 플랫폼에만 | Claude Agent SDK + 팀장 구독 토큰(플랫폼 OpenBao, Trial마다 1회용 발급) | ADR 0009 |
| §8.5 시험 · 도구 | 질문마다 시험 방법(behavioural · config · simulation). 일부는 게이트 밖 | `docs/architecture/16-gate-test-map.md`, D-29 |
| FR-8 단계 기준 값 | 오류율 1%, 목적지 표본 100건, 목적지 p95 1,500 ms(D-30, 컷오버 전 쓰기는 링크를 건넘). 대표 CNAME 0/100은 FR-8을 대신하지 않음 | D-30, D-10 |

## 2. 실측 시간(장면 길이를 정하는 숫자)

| 무엇 | 측정값 | 증거 | 상태 |
|---|---|---|---|
| 게이트 한 쌍(질문 22개, 규칙 `gate-controls/2026-11-03.2`, 쓰기 시험 포함) | **39–43초.** 03:43–03:45 Deployment 39 · 40초, 02:47–02:55 미리 보기 Pod 40–43초 | `docs/evidence/app-0.3.3/gate-run.txt`, `docs/evidence/gate/platform-gate-20261102T175426Z/run.txt` | 실측. 질문 19개였던 11/3 01:50–02:03 실행은 10–16초 |
| 지금 게이트 FAIL | 온프렘 OBJ-01 · OBJ-05(심은 열린 버킷, `deploy/cloud/rgw_regression.py`), GKE OBJ-06. UNKNOWN 0. 그 밖은 PASS | `docs/evidence/app-0.3.3/gate-report-*.json` | 실측(03:43–03:45) |
| GKE OBJ-06 대체 통제 | 질문 `OBJ-PLAIN-EGRESS` 세 환경 PASS(앱 라벨 Pod에서 저장소 80 차단, 앱 엔드포인트 https) | `docs/evidence/gate/obj-plain-egress-20261103/` | 실측. 예외 승인 0건 |
| CIS 4a 시작 거부 | 거부가 5초 안에 실패로 드러남(버리는 VM) | `docs/evidence/s1-cis-refusal/06-refuse-failfast.log` | 실측(사전 실행) |
| S2 governance(버킷 정책) | **PASS 227초**: 모델 101초, Harbor 정적 검증 44초, 검증 Job 73초 | `docs/evidence/s2-full/README.md`(실행 18) | 실측. API Pod 안에서 API 코드로 부름(콘솔 아님) |
| S2 app_migration(Secrets Manager → OpenBao · ESO 코드 수정) | **PASS 315초**: 모델 161초, Harbor 37초, 검증 Job 107초 | 같음 | 실측. 앱 0.3.3 런타임으로는 다시 재지 않음 |
| S2 단계 범위(PASS 4회) | 모델 101–161초, Harbor 정적 37–44초, 검증 Job 73–107초 | 같음 | 실측 |
| 콘솔에서 두 작업 동시 시작 | 배포된 API(`46edd0d1`)는 계획마다 리허설 하나만 받음(`rehearsal_active`). 역할마다 하나를 받는 코드는 `e1b4fe0`(고정 api `5421e97b`, push 전) | `platform/api/src/bravo_api/rehearsal.py`, S2 갈래 | **측정 중** |
| T31 승인 뒤 반영(빌드 대조 · 서명 · import · 정책 적용 · 게이트 재대조) | 정책 단계(`rgw_bucket_policy`, `openbao_policy`)는 시험 버킷에서만 확인 | `docs/evidence/s2-full/15-t31-rgw-apply-selftest.txt` | **측정 중**(S2 갈래) |
| SHIFT 편도 10/50/100(목적지가 제때 답할 때) | **30.4 · 37.2초**(표본 `GET /api/status`) | `docs/evidence/shift/lb-oneway-20261103T025133/` | 실측 |
| SHIFT 실제 쓰기 판정 통과 | 04:05 배포된 Executor(`b2732bed`, p95 기준 1,500 ms): 10% 단계 목적지 쓰기 109건, 목적지 오류 0, 목적지 p95 1,096 ms였지만 `X-Served-By` 없는 오류 5건(4.39%)으로 0% 복귀. 출발지 Gateway가 Cloudflare 503을 냄 | `docs/evidence/shift/lb-deployed-p95-1500-20261103T040545/` | **재측정 중** |
| SHIFT 0% 복귀(10% 단계에서) | 33.8–38.4초(실행 전체) | `docs/evidence/shift/lb-oneway-20261103T025133/README.md` | 실측 |
| 목적지 쓰기 한 건(컷오버 전, 링크 경유) | 링크 한 왕복 290–340 ms(Cloudflare DFW), 차례 쓰기 p95 705–859 ms | `docs/evidence/shift/write-path-20261103/` | 실측 |
| 클라우드 컷오버(쓰기 정지, 실행 시간) | - | - | **미측정(0회).** 파일럿(노트북) 기록은 쓰기 정지 76–139 ms |
| 클라우드 되돌리기 | - | - | **미측정(0회).** 파일럿 49 ms |
| 출발지 부하 50 쓰기 + 150 읽기/초, 300초(HTTP 읽기, 노드 2) | 쓰기 p95 138.1 ms, 읽기 p95 123.2 ms, NFR-2 OK(15,000건 정확히 1건) | `docs/evidence/load/20261103-035431-job-2nodes-http-50w150r/` | 실측. WebSocket 읽기는 아직 포화(`…040037-job-2nodes-ws-50w150r-60s`) |
| 부하 컷오버 5회(NFR-1) | `deploy/cloud/load_cutover.sh`, 회마다 콘솔 승인 필요 | `docs/evidence/load/README.md` | 미실행 |
| preflight 전체 | 13초, 7초 | `docs/evidence/demo-ops/` | 실측 |
| reset `--plan`(깨끗한 상태) | 실제 실행 9초, dry-run 22–29초 | `docs/evidence/demo-ops/README.md` | 실측. 컷오버 뒤 상태에서는 미측정 |

## 3. 시작 전

| 시각 | 할 일 | 명령 · 화면 | 기대 |
|---|---|---|---|
| 11/6 | 자격 회전, 두 신원 첫 로그인, 동결 | `docs/demo/ONSITE-CHECKLIST.md` 3절, `deploy/scripts/demo-freeze.sh create` | 회전 `FAIL 0`, preflight 신원 두 줄 PASS |
| T-60분 | 장비 · 로그인 · 전체 점검 · 되돌리기 확인 | ONSITE 5절. `deploy/scripts/preflight.sh --out docs/evidence/demo-ops/preflight-<UTC>`, `deploy/scripts/reset-demo.sh --dry-run --plan <직전 계획>` | FAIL 0, reset 모두 `ALREADY` |
| T-10분 | 빠른 점검, 시작 상태 | ONSITE 6절. `deploy/scripts/preflight.sh --only kube,link,repl,rgw,http,console,host` | LB src 1 / dst 0, `b-free-chat` → `aws`, 심은 퇴행 `planted`, 정방향 구독 45초 안 |
| T-10분 | 부하를 켤지(팀 결정) | `deploy/cloud/load_cutover.sh <plan-id> demo` | 켜면 SHIFT 판정이 사용자 부하로 됨(계약 15 §10.4) |
| T-6분 | 4절 첫 줄(S2 시작) | 콘솔 `#/plan` | 아래 |

## 4. 5분 안무(제안, 팀 결정 필요)

### 4.1 기본 안

S2 두 Trial을 발표 6분 전에 시작합니다. 시작 시각은 화면에 "발표 전 시작"으로 그대로 보입니다. 실측대로면 governance는 T-2:13, app_migration은 T-0:45에 끝납니다. 아래 "남는 위험"은 지금 측정으로 그 칸이 넘칠 수 있는 이유입니다.

| 시각 | 장면 | 누가 · 화면 | 조작 | 보이는 증거 | 실측 길이 | 남는 위험 |
|---|---|---|---|---|---|---|
| T-6:00 | S2 시작(발표 전) | 운영자, `#/plan` → `#/agents` | 계획(source-aws → target-onprem)에서 governance · app_migration 리허설을 함께 시작 | `trial.started` 두 건, 시작 시각, "발표 전 시작" 표시 | 227초 · 315초 | 콘솔에서 두 작업 동시 시작은 **측정 중**(배포된 API는 계획당 하나만 받음, 역할별 코드는 push 전). 앱 0.3.3 런타임으로 다시 재지 않음 |
| 0:00–0:40 | 설계 문서 | 발표자, 설계 문서 | 아키텍처 30초(플랫폼 하나가 출발지 · 목적지를 링크로 다룸, 키는 플랫폼 금고 하나, AI는 제안만 하고 판정 · 실행은 코드) | 설계 문서 그림 | 40초 | - |
| 0:40–1:20 | S1 게이트 BLOCK, GKE 예외 | 운영자, `#/plan` 게이트 칸 · `#/workflow` | 이 계획의 게이트 실행(`POST /v1/plans/{id}/gate`) | `gate.verdict` BLOCK, IAM-01(온프렘 OBJ-01 익명 GET 허용 · OBJ-05 다른 자격 읽기 허용). 행마다 시험 방법 | 한 쌍 39–43초 | 40초 칸에 한 쌍만 겨우 들어감. GKE 쌍(ENC-01 BLOCK, OBJ-06)과 예외(대체 통제 `OBJ-PLAIN-EGRESS` PASS)는 GKE 쌍의 별도 계획에서 미리 돌린 결과와 예외 요청 · 승인 기록으로 보임(예외는 계획 단위). 예외 승인은 아직 0건. CIS 4a(5초 거부)는 사전 실행 기록 링크로만 |
| 1:20–2:20 | S2 결과, 변경 승인, 반영, 재대조 PASS | `#/agents` → 승인자 `#/plan` change → `#/plan` | 결과 보이기 → 승인자가 change 승인(diff · 이미지 digest · 정책 diff 결속) → T31 반영 → 게이트 재대조 | Trial PASS 두 건과 단계 시간. 도구 거부: 하네스 probe 2건(`origin=probe`, 버킷 삭제)과 에이전트 거부 1건. Hubble `hubble_drop_total` POLICY_DENIED Trial 네임스페이스마다 12건. 검증 Job이 게이트 OBJ-01–06을 시험 버킷에 돌려 PASS(RGW는 `aws:SecureTransport`에 `BoolIfExists`가 필요). app_migration 검증: ESO 동기화 8.1초, 고친 앱 `/readyz` 200 · 쓰기 201 | 반영 **측정 중**. 재대조 한 쌍 39–43초 | 승인 + 반영 + 재대조 한 쌍이 60초를 넘을 가능성이 큼. 넘으면 4.2 |
| 2:20–3:40 | S3 전환과 컷오버 | 승인자 cutover 승인 → 운영자 "전환 시작"(`#/plan`) → `#/demo` | 한 번의 시작으로 SHIFT 10 → 50 → 100%, 판정 통과면 시스템이 같은 승인으로 컷오버 시작 | 단계마다 목적지 표본 · 오류율 · p95와 기준선(`front.thresholds`), 컷오버 단계 ms · 쓰기 정지 ms, 영수증(`verdict_parts` 표 · 시퀀스 · 첨부, 검사 범위 `scope`), WebSocket 1012 재접속 뒤 "연결된 곳: 온프렘" | SHIFT 30–40초(목적지가 제때 답할 때). 컷오버 **미측정** | 실제 쓰기 판정 통과 **재측정 중**(p95 기준 1,500 ms, D-30). 클라우드 컷오버 0회. 역방향 슬롯은 6a에서 만들어짐, PgBouncer 대상은 ConfigMap `chat-pgbouncer-target`로 재시작에도 남음 |
| 3:40–4:40 | S4 장애와 데이터째 되돌리기 | 운영자 터미널 → `#/observability` → 승인자 failback 승인 → 운영자 되돌리기 시작 | `deploy/cloud/t25.sh fault 0.3`(장애 주입만: 목적지 앱이 HTTP 30%를 500으로) → 오류 확인 → failback 승인 · 시작 | 대표 이름 쓰기 20건의 오류, 목적지 오류율. 되돌리기 단계와 마지막 `failback.8_front_to_src`(LB를 출발지 100%로 한 번에, 5초 판정), 영수증, 목적지에서 쓴 행이 출발지에 있음(S4-AC1) | 되돌리기 **미측정** | "경보"는 화면의 오류율입니다. 경보 규칙 · Alertmanager는 없음(`docs/evidence/demo-ops/README.md`). 클라우드 되돌리기 0회 |
| 4:40–5:00 | 비용과 관측 | `#/cost`, `#/observability` | 이 계획의 비용 영수증, 관측 | 줄마다 단가 출처 · 수량 · 근거(추정 · 정액). 태그 청구는 아직 없음(추정 금액) | 20초 | 비용 화면 실측 기록 없음 |

### 4.2 대체 변형: 칸을 넘으면 기록 재생

장면이 제 칸을 넘으면 그 단계를 기다리지 않습니다. 운영자가 같은 단계의 사전 실행으로 넘깁니다. 화면 위에는 "기록 재생 · 원래 시각"과 실행 ID가 늘 보입니다(계약 15 §12). 넘긴 사실을 말로도 밝힙니다.

| 장면 | 넘기는 기준 | 보일 것 | 지금 있나(04:07) |
|---|---|---|---|
| S1 게이트 | 1:20에 판정이 안 나옴 | 같은 쌍의 사전 게이트 보고서(실행 시각 · `input_digest` 표시) | 있음: `docs/evidence/app-0.3.3/gate-report-*.json`(03:43–03:45). 콘솔에서 보이려면 리허설 계획의 게이트 기록이 필요(계획 0건) |
| S2 | 0:00에 Trial이 안 끝남, 또는 반영 · 재대조가 2:20을 넘음 | 사전 Trial 기록(단계 시간, 판정, 도구 거부의 `origin`) | 있음: `docs/evidence/s2-full/`(API Pod 경로). 콘솔 경로 기록은 없음 |
| S3 SHIFT · 컷오버 | 3:40에 영수증이 안 나옴 | `#/replay?run=<shift 실행>`, `<cutover 실행>` | **없음.** 재생은 계획에 묶인 실행만 됨. 리허설 계획에서 콘솔로 한 번 돌린 기록이 필요 |
| S4 되돌리기 | 4:40에 영수증이 안 나옴 | `#/replay?run=<failback 실행>` | **없음.** 위와 같음 |

## 5. 대체 장면(사전 실행 기록)

T28은 실패할 때 쓸 사전 실행 기록을 요구합니다. 지금 있는 기록입니다. 화면에는 늘 "사전 실행"과 시각을 함께 보입니다.

| 장면 | 기록 | 범위와 한계 |
|---|---|---|
| S1 구축 · CIS 4a | `docs/evidence/s1-cis-refusal/` | 버리는 Lima VM. 운영 목적지가 아님 |
| S1 게이트 | `docs/evidence/app-0.3.3/`, `docs/evidence/gate/platform-gate-20261102T175426Z/` | 게이트 서비스 단독 실행, 계획에 묶이지 않음 |
| S2 | `docs/evidence/s2-full/`(실행 18) | API Pod 안에서 API 코드로 부름. 승인 · 반영 · 재대조는 안 함 |
| S3 앞단 | `docs/evidence/shift/`, `docs/evidence/cloud-runs/cloud-front-*` | 도구 실행(계획 · 승인 없음). 실제 쓰기 판정 통과는 아직 없음 |
| S3 첨부 동기화 | `docs/evidence/cloud-runs/cloud-t8-attachments-20261102T225707/` | 단독 복사 1회(3개) |
| S3 컷오버 · S4 되돌리기 | 파일럿(노트북) 기록 `docs/evidence/pilot-notebook/`(`smoke-20261102T194530`: 쓰기 정지 87 ms, 되돌리기 49 ms) | **클라우드 아님.** 초당 20건, 가짜 승인 ID. 클라우드 기록은 0건 |
| 부하 | `docs/evidence/load/20261103-035431-job-2nodes-http-50w150r/` | 컷오버 없이 잰 것 |

## 6. 데모 사이 reset

명령은 `deploy/scripts/reset-demo.sh --plan <계획 ID>`입니다. 먼저 `--dry-run`으로 봅니다. 그 계획의 리허설 · 검증 네임스페이스만 지우고, 다른 계획의 것은 `OTHER`로 보이기만 합니다. 컷오버 뒤 상태면 front · data · writes를 FAIL로 멈추고 콘솔의 사람 되돌리기를 기다립니다(스크립트는 되돌리기를 시작하지 않음).

| 단계 | 하는 일 |
|---|---|
| `rehearsal` | `rehearsal-<계획 앞 8자>-*`, `verify-<같음>-*` 삭제 |
| `approvals` | 그 계획의 열린 승인 · 요청을 셈(읽기 전용, 기다리지 않음) |
| `front` | LB를 출발지 100%로, `env.id=aws` 확인 |
| `data` | PgBouncer 대상(ConfigMap 포함)을 출발지로, 복제가 컷오버 전 상태가 아니면 `repl_setup rearm`, 남은 역방향 슬롯 삭제 |
| `writes` | 각 환경 쓰기 차단 스위치를 끔 |

측정: 깨끗한 상태 실제 실행 9초, dry-run 22–29초(`docs/evidence/demo-ops/README.md`). 컷오버 · 되돌리기 뒤 상태에서는 아직 재지 않았습니다.

## 7. 준비 중 목록

| 장면 | 빠진 것 | 담당 갈래 |
|---|---|---|
| 공통 | 운영자(naver) 첫 로그인. 플랫폼 DB에 계획 0건, 승인 0건(04:07) | 사람 |
| S1 | GKE 쌍 계획에서 예외 요청 · 승인 1회(대체 통제 `OBJ-PLAIN-EGRESS`) | 사람, 게이트 |
| S2 | 콘솔에서 역할별 두 Trial 동시 시작(코드 `e1b4fe0`, 배포 · 측정 중), 승인 뒤 T31 반영 시간(측정 중), 게이트 재대조 PASS | S2 |
| S3 | 실제 쓰기 판정으로 SHIFT 통과(재측정 중, D-30), 클라우드 컷오버 1회와 영수증, 부하 컷오버 5회(NFR-1) | 컷오버, 사람 |
| S4 | 클라우드 되돌리기 1회와 S4-AC1 | 컷오버, 사람 |
| 마무리 | Exit Drill 실제 실행(11/3 03:09 `NOT_RUN`, 컷오버 뒤에만 가능, `docs/evidence/exit-drill/`), 비용 화면 확인 | 컷오버 |
| 대체 변형 | 리허설 계획에서 S3 · S4를 콘솔로 한 번 돌려 `#/replay` 재생 기록 만들기 | 사람, 컷오버 |
| 시간 | 게이트 한 쌍 39–43초를 S1 40초 칸에 맞추기(질문 수 · 병렬), 4.1 안무 결정 | 게이트, 팀 |
| T28 | 270초(최종 5분) 통합 리허설 3회 연속 성공 | 전원 |

## 8. 리허설 기록

T28 완료 기준은 "제한 시간 안에 끝나는 리허설 3회 연속 성공"입니다.

| 회차 | 시각 | 걸린 시간 | 결과 | 증거 |
|---|---|---|---|---|
| - | - | - | 아직 없음 | - |

## 9. 역사(실행 지침 아님)

- 11/2 20:25–20:57 관측은 결정 전 환경(온프렘 K3s, 플랫폼 EKS, 노트북 Executor `127.0.0.1:8085`, 노트북 임시 링크)이었습니다. 자세한 것은 `deploy/CLOUD-E2E-CONTRACT.md` 10절과 `docs/evidence/cloud-runs/history-k3s-20261102T2049-2101/`입니다.
- 11/2 20:4x 전에는 대표 이름이 CNAME이었고 0%와 100%만 바꿀 수 있었습니다. 이것은 FR-8의 대체가 아닙니다(D-10).
- 11/2 22:26–22:46 LB 전환은 단계마다 60초 보유라 편도 184.8초였습니다. 판정 표본은 `GET /api/config`였고, 목적지가 죽었는데 DONE이 난 결함이 있었습니다(`docs/evidence/cloud-runs/`).
- 11/2의 S2 Trial `t-3ac2e611…`은 노트북 CLI 러너로 돌았고, 그 제안서의 `Bool` 정책은 지금 검증기로는 OBJ-06 FAIL입니다(`docs/evidence/s1-s2-bucket/`, `docs/evidence/s2-full/README.md`).
- 11/2 20:56 reset dry-run 출력은 옛 기본값(플랫폼 EKS, 노트북 Executor)이었습니다. 지금 스크립트는 6절입니다.
