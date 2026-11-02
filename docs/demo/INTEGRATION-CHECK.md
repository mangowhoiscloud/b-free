# 중간 통합 점검(T28)

스펙 T28의 세부 작업 "중간 통합 점검 2회: S1–S2 연결, S3–S4 연결"의 점검표입니다. **기준 시각: 2026-11-03 04:55 KST.** 실제 대상(출발지 EKS, 온프렘 RKE2(CIS), GKE, 플랫폼 EC2 + RKE2)에서만 봅니다. 지금 환경의 정본은 `deploy/CLOUD-E2E-CONTRACT.md` 0절이고, 장비 · 자격 증명 · 시작 상태는 `docs/demo/ONSITE-CHECKLIST.md`가 다룹니다.

**쓰는 법.**
- 항목마다 명령을 그대로 돌리고 "기대하는 실제 신호"와 비교합니다. 하나라도 다르면 FAIL입니다. 출력을 볼 수 없으면 UNKNOWN이고, UNKNOWN은 통과로 세지 않습니다.
- 클러스터나 플랫폼 DB에 쓰는 조작은 "쓰기"로 표시합니다. 승인과 실행 시작은 콘솔에서 사람만 합니다(운영자 naver, 승인자 gmail, 서로 다른 신원). 스크립트는 승인을 만들거나 실행을 시작하지 않습니다.
- 결과는 실행 ID와 시각을 붙여 `docs/evidence/`에 남깁니다. `deploy/pilot/runs/cloud-*`(Git 제외)에 생긴 기록은 `docs/evidence/cloud-runs/`로 옮깁니다.
- "11/3 상태" 칸의 말: **PASS**(증거 있음), **FAIL**, **측정 중** · **재측정 중**(다른 갈래가 재는 중), **미실행**(사람 승인이나 앞 단계가 필요). 이 문서를 고치면서 쓰기 조작은 하지 않았습니다.

**공통 값.** 비밀은 없습니다.

```sh
SRC=bravo-source; DST=bravo-onprem; PLAT=bravo-platform-rke2   # 플랫폼은 SSM 포트 포워드 127.0.0.1:26443
GCP=bravo-target-gcp; GKE_KUBECONFIG=infra/envs/target-gcp/.terraform/bravo/kubeconfig   # gke-gcloud-auth-plugin 필요
FRONT=b-free-chat.mangowhois.ai
# 플랫폼 Service(ns bravo-control): api 8080, operator-mcp 8082, console 8080, gate 8084, collector 8083, rehearsal 8090, executor 8081
kubectl --context $PLAT -n bravo-control port-forward svc/executor 18085:8081 &   # EXECUTOR=http://127.0.0.1:18085
kubectl --context $PLAT -n bravo-control port-forward svc/gate 18084:8084 &       # GATE=http://127.0.0.1:18084
# 계획 API는 콘솔(https://b-free.mangowhois.ai, Access 로그인)에서 씁니다. PLAN=<콘솔에서 만든 계획 ID>
```

## 0. 먼저

| # | 확인 | 명령 | 기대하는 실제 신호 | 11/3 상태 |
|---|---|---|---|---|
| P1 | 전체 사전 점검 | `deploy/scripts/preflight.sh --out docs/evidence/demo-ops/preflight-<UTC>` | 요약 FAIL 0 | 03:01 PASS 98 · WARN 8 · FAIL 10(Argo 따라오기 전 9줄, operator 신원 1줄). `docs/evidence/demo-ops/README.md` |
| P2 | 두 신원 | 콘솔에 두 브라우저로 로그인, preflight `console` 그룹 | operator(naver) · approver(gmail) 역할이 각자 보임 | **FAIL**: 04:07 플랫폼 DB `principals` 1행(approver만) |
| P3 | 동결 | `deploy/scripts/demo-freeze.sh check` | 이미지 digest · ConfigMap이 동결 기록과 같음 | 미실행(진짜 동결은 데모 전날). 시험 생성 · 비교는 02:43 PASS |

## 1. S1–S2 연결 점검

S1이 만든 것(환경, 인벤토리, 통제표, 게이트 1차 BLOCK)이 S2(리허설, 차단 기록, 변경 승인, 반영, 재대조)로 넘어가는지 봅니다.

### 1.1 환경 전제

| # | 확인 | 명령 | 기대하는 실제 신호 | 11/3 상태 |
|---|---|---|---|---|
| A1 | 네 클러스터 노드 | `kubectl --context <ctx> get nodes`(GKE는 `KUBECONFIG=$GKE_KUBECONFIG`) | 모두 Ready. 출발지 2, 온프렘 2(`+rke2r1`), 플랫폼 1(`+rke2r1`) | PASS(04:07: 출발지 2, 온프렘 2, 플랫폼 1). GKE는 이 Mac에 인증 플러그인이 없어 preflight 결과로 갈음(03:01 PASS) |
| A2 | 플랫폼 서비스 digest | `kubectl --context $PLAT -n bravo-control get deploy,sts -o wide` | `kustomization.yaml` 고정값과 같음 | PASS(04:0x) |
| A3 | D-16 링크 | preflight `link` 그룹 | 링크 20개 실제 왕복 | PASS(03:01, `docs/evidence/demo-ops/`) |
| A4 | 정방향 복제 | preflight `repl` 그룹 | `bravo_fwd_sub` `latest_end_time` 45초 안, 역방향 슬롯 없음 또는 여유 512MB 위 | PASS(03:01, 17초). 타임아웃 60초 적용(`docs/evidence/repl-timeouts/`) |
| A5 | 공개 이름 | `curl -s https://<이름>/api/config \| jq -r .env.id` | `b-free-chat` · `-src` → `aws`, `-dst` → `onprem`, `-gke` → `gcp` | PASS(04:07) |
| A6 | 같은 앱 digest | 세 클러스터 `chat/chat-bravo-demo` 이미지 | 모두 `sha256:76e8f595…`(0.3.3) | PASS(`docs/evidence/app-0.3.3/`, 04:07 출발지 · 온프렘 다시 확인) |
| A7 | 심은 퇴행 | preflight `s1` 그룹 | 온프렘 첨부 버킷이 `planted`(S1 BLOCK의 재료) | PASS(게이트 보고서의 OBJ-01 · OBJ-05 FAIL로 확인, 03:43) |

### 1.2 S1 산출물

| # | 확인 | 명령 | 기대하는 실제 신호 | 11/3 상태 |
|---|---|---|---|---|
| B1 | 수집기가 클러스터를 읽음 | 콘솔 `#/clusters` 또는 `GET /v1/clusters` | 출발지 · 온프렘 · GKE `ok` | PASS(출발지, `docs/evidence/d16-source-k8s/`). 세 곳 동시는 다시 보지 않음 |
| B2 | 의존성 3등급 | 콘솔 `#/plan` 의존성 칸(`GET /v1/inventory`) | "코드 수정" 1건 이상(Secrets Manager 직접 호출), `input_digest` 있음 | 계획 경로 미실행(계획 0건). S2 실측의 봉투에 인벤토리 결속 있음(`docs/evidence/s2-full/`) |
| B3 | 게이트 두 쌍 | `curl -s -X POST $GATE/v1/controls/report …`(`docs/evidence/gate/run-platform-gate.sh`) | 질문 22개, UNKNOWN 0, FAIL은 온프렘 OBJ-01 · OBJ-05와 GKE OBJ-06뿐. 행마다 `method` | PASS(03:43–03:45, 한 쌍 39–40초, `docs/evidence/app-0.3.3/`). 방법 대응은 `docs/architecture/16-gate-test-map.md` |
| B4 | 계획 게이트 BLOCK. **쓰기** | 콘솔 `#/plan`에서 게이트 실행(`POST /v1/plans/$PLAN/gate`) | `gate.verdict` BLOCK, IAM-01, 한 쌍 39–43초 | 미실행(계획 0건). 리허설(C1)은 이 판정이 있어야 시작되고, 리허설이 시작되면 PROPOSE · REHEARSE에서는 이 명령이 `state_mismatch`로 거부됨(`GATE_STATES`) |
| B5 | 이전 후보 등록이 막힘 | `GET /v1/plans/$PLAN` | AWAIT_APPROVAL로 가지 않음 | 미실행 |
| B6 | CIS 4a 사전 기록 | `docs/evidence/s1-cis-refusal/` | 거부 로그, 고친 cloud-init diff, 재설치, 거부 5초 fail-fast | PASS(버리는 VM, 사전 실행) |
| B7 | GKE 예외(S1 5b). **쓰기** | GKE 쌍 계획에서 운영자가 예외 요청(대체 통제 `OBJ-PLAIN-EGRESS`), 승인자가 승인 | 게이트 행 `EXCEPTION`, 만료 · 대체 통제 표시 | 대체 통제 질문은 PASS(`docs/evidence/gate/obj-plain-egress-20261103/`). 예외 승인 미실행(0건) |

### 1.3 S2 입구와 반영

| # | 확인 | 명령 | 기대하는 실제 신호 | 11/3 상태 |
|---|---|---|---|---|
| C1 | 두 작업 시작. **쓰기** | 콘솔 `#/plan`에서 governance · app_migration 리허설 시작(`POST /v1/plans/$PLAN/rehearsals`) | HTTP 202 두 건, `trial.started` 두 건 | 콘솔 클릭은 미실행(운영자 첫 로그인 전). 배포된 API `5421e97b`가 역할마다 리허설 하나를 받음. 같은 상류 호출을 API Pod에서 불러 두 작업을 함께 시작: governance PASS 150–210초, app_migration PASS 180–187초(앱 0.3.3, `docs/evidence/s2-timing/`) |
| C2 | Trial 격리 | `kubectl --context $DST get ns -l bravo.dev/trial-id`, Trial 기록 `isolation` | 대조 exec 성공, 밖으로 나가는 연결 실패, privileged Pod 입장 거부 | PASS(실행 18 `isolation`: 리허설 · 검증 네임스페이스 모두 egress 2건 실패, privileged `rejected`) |
| C3 | 밖으로 나가는 시도 차단 | 플랫폼 Prometheus `hubble_drop_total{reason="POLICY_DENIED"}`, Trial 네임스페이스 | 0이 아님 | PASS: Trial 네임스페이스마다 12건(`docs/evidence/s2-full/12-hubble-drops-prometheus.json`) |
| C4 | 도구 거부 기록 | `#/agents`, SSE `tool.denied` | 거부마다 `origin`(agent · probe) 표시 | PASS: probe 2건(버킷 삭제, `POST /v1/trials/{id}/probes`), agent 1건. probe는 에이전트 행동이 아님 |
| C5 | 분리 검증 | Trial 기록 `verdict`, 검증 Job | governance: 게이트 OBJ-01–06이 시험 버킷에서 PASS. app_migration: ESO 동기화 · 고친 앱 DB 접속 · 쓰기 201 · 다른 SA Secret 읽기 거부 | PASS(`s2-timing` 실행 06, 고정 리허설 이미지 `d78bb0ce`: 210.2초 · 186.8초, 첫 시도. 이전 실행 18은 227초 · 315초) |
| C6 | 변경 승인. **쓰기** | 승인자 콘솔 `#/plan` change | `approval.granted` stage `change`, diff · digest · 정책 diff 결속 | 미실행(승인 0건) |
| C7 | T31 반영. **쓰기** | 승인 뒤 반영 경로(`platform/apply`) | 배포 digest = 승인 digest, `rgw_bucket_policy` · `openbao_policy` 적용 뒤 다시 읽어 같음 | 반영 시간 PASS: 9.6초(이미지 이미 반입), 처음 반입 약 13초. 정책 적용(RGW 버킷 정책 + OpenBao 정책 + k8s) 1.5초, 서명 1.3초, 롤아웃 6.1초(`docs/evidence/s2-timing/05-t31-apply/`). 검증 네임스페이스 · 시험 버킷에 반영했고 승인 행이 없어 `approval.verify`는 안 돎. 실제 목적지 반영은 미실행 |
| C8 | 재대조 PASS | 반영의 `gate.recheck` | `gate.verdict` PASS(온프렘 쌍), 한 쌍 38–63초. 승인부터 PASS까지 50–76초 | 미실행(승인 0건) |
| C9 | 리허설 뒤 정리 | `kubectl --context $DST get ns -l bravo.dev/trial-id -o name` | 빈 출력(S2-AC4) | PASS(실행 18 `leftovers: []`) |

## 2. S3–S4 연결 점검

S3(전환, 컷오버)이 남긴 상태에서 S4(장애, 데이터째 되돌리기)가 출발할 수 있는지, 끝나면 시작 상태로 돌아가는지 봅니다. 1절이 모두 PASS인 뒤에 합니다.

| # | 확인 | 명령 | 기대하는 실제 신호 | 11/3 상태 |
|---|---|---|---|---|
| D1 | 컷오버 직전 준비 | `curl -s $EXECUTOR/v1/readiness \| jq '{direction, sub_state, lag_bytes, schema_hash}'` | `forward`, `streaming`, 스키마 해시 같음 | PASS(`docs/evidence/db-tls-audit-netpol/`, preflight `repl`) |
| D2 | PgBouncer 대상이 출발지이고 재시작에도 남음 | 두 클러스터 ConfigMap `chat/chat-pgbouncer-target` | `target=src` | PASS(재시작 시험 03:46–03:47, `docs/evidence/shift/pgbouncer-durable-target-20261103/`) |
| D3 | 역방향 슬롯이 미리 없음 | preflight `repl`, 컷오버 전 점검 `REVERSE_SLOT_PRESENT` | 슬롯 없음(6a에서 만들어짐) | 코드 배포됨(executor `b2732bed`). 컷오버 실행 0회라 6a 동작은 미실행 |
| D4 | 승인 → 한 번의 시작 → SHIFT → CUTOVER. **쓰기** | 승인자 cutover 승인, 운영자 `#/plan` "전환 시작"(`POST /v1/plans/$PLAN/shift {"stage":"cutover"}`) | SHIFT 10 · 50 · 100% 판정 통과(오류율 1%, 목적지 표본 100건, 목적지 p95 1,500 ms) → 시스템이 CUTOVER 시작 | 콘솔 경로 미실행. 도구 경로 실측: 목적지가 제때 답하면 편도 30.4 · 37.2초. 실제 쓰기 판정: 04:05 배포된 Executor로 10%에서 0% 복귀(목적지 p95 1,096 ms는 기준 안, 헤더 없는 오류 5건, `docs/evidence/shift/lb-deployed-p95-1500-20261103T040545/`). 원인인 출발지 Gateway 노드 사이 503은 04:33에 고침(`docs/evidence/source-gateway-crossnode/`, 고친 뒤 600/600 200, 50 + 150 부하 12,000/12,000). **재측정 중**(D-30) |
| D5 | 부하 중 컷오버. **쓰기**(부하) | `deploy/cloud/load_cutover.sh $PLAN run-1` 뒤 D4 | 실행 `DONE`, `write_pause_ms` 5,000 이하, NFR-2 유실 · 중복 0 | 미실행. 부하 자체는 PASS(50 쓰기 + 150 읽기/초, 쓰기 p95 138.1 ms, `docs/evidence/load/20261103-035431-job-2nodes-http-50w150r/`). 5회는 회마다 승인 |
| D6 | 영수증 | 콘솔 영수증 칸, `GET /v1/plans/$PLAN` | `verdict` MATCH, `verdict_parts` 표 · 시퀀스 · 첨부 모두 MATCH, `scope`(hash_rule v1.1, 첨부 참조 집합 해시) | 미실행(영수증 0건) |
| D7 | 컷오버 뒤 쓰기 DB가 하나 | `deploy/cloud/repl_setup.sh status`, 두 PgBouncer 활성 파일과 ConfigMap | 목적지 `app_writer` LOGIN, 출발지 아님. 두 PgBouncer와 ConfigMap 모두 dst | 미실행 |
| D8 | 장애 주입. **쓰기** | `deploy/cloud/t25.sh fault 0.3` | 목적지 앱 HTTP 30%가 500, 대표 이름 쓰기 20건에 오류. 컷오버 · 되돌리기는 부르지 않음 | 스크립트는 장애 주입만 함(계약 15 §10.8). 컷오버 뒤 상태에서 미실행. 같은 `fault.sh`의 앞단 수준 실행은 11/2 22:54(`docs/evidence/cloud-runs/cloud-front-20261102T225316-t17-revert-98f57c71/error/`) |
| D9 | 되돌리기. **쓰기** | 승인자 failback 승인, 운영자 `#/plan` 되돌리기 시작(`{"stage":"failback"}`) | failback 실행 `DONE`, 마지막 `failback.8_front_to_src`로 LB 출발지 100%, 영수증 MATCH | 미실행 |
| D10 | 목적지에서 쓴 메시지가 출발지에 있음(S4-AC1) | 되돌리기 영수증과 S4-AC1 대조 | 목적지 신규 행의 수와 해시가 출발지와 같음 | 미실행 |
| D11 | 앞단이 출발지로 돌아옴 | `curl -s https://$FRONT/api/config \| jq -r .env.id` | `aws` | 미실행(D9 뒤) |
| D12 | Exit Drill | `deploy/cloud/exit_drill.sh` | 컷오버 뒤 출발지 경로를 끊어도 목적지 앱 재시작 · 새 연결 동작 | 03:09 `NOT_RUN`(컷오버 전, `docs/evidence/exit-drill/`) |
| D13 | 다음 데모 시작 상태 | `deploy/scripts/reset-demo.sh --plan $PLAN` 뒤 `--dry-run --plan $PLAN` | 첫 실행 종료 코드 0, 둘째는 모두 `ALREADY` | 깨끗한 상태 PASS(9초, `docs/evidence/demo-ops/`). 컷오버 · 되돌리기 뒤 상태는 미실행 |

## 3. 판정 기록

| 점검 | 시각 | 결과(PASS · FAIL · UNKNOWN 수) | 증거 | 누가 |
|---|---|---|---|---|
| S1–S2 | - | 아직 돌리지 않음. P2(operator 첫 로그인)가 먼저 | - | - |
| S3–S4 | - | 아직 돌리지 않음 | - | - |

## 4. 역사

11/2 20:25–20:57에 쓴 첫 판은 노트북 Executor(`127.0.0.1:8085`), 노트북 임시 링크(`deploy/cloud/link.sh`), K3s · 플랫폼 EKS 시절의 명령이었습니다. 실행 지침이 아니며, 그때 기록은 `docs/evidence/cloud-runs/history-k3s-20261102T2049-2101/`와 `deploy/CLOUD-E2E-CONTRACT.md` 10절에 있습니다.
