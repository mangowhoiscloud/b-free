# 중간 통합 점검(T28)

스펙 T28의 세부 작업 "중간 통합 점검 2회: S1–S2 연결, S3–S4 연결"의 점검표입니다. 실제 대상(출발지 EKS, 온프렘 RKE2(CIS), GKE, 플랫폼 EC2 + RKE2)에서만 봅니다. 2026-11-02 21:10 KST 결정(ADR 0010) 전의 관측은 K3s · EKS 판이라 "지금" 칸에 그렇게 표시합니다. 노트북 Compose 파일럿은 쓰지 않습니다(`deploy/CLOUD-E2E-CONTRACT.md` 6.1).

**쓰는 법.**
- 항목마다 명령을 그대로 돌리고, "기대하는 실제 신호"와 출력을 비교합니다. 하나라도 다르면 그 항목은 FAIL입니다. 출력을 볼 수 없으면 UNKNOWN이고, UNKNOWN은 통과로 세지 않습니다.
- 클러스터에 쓰는 명령(리허설 시작, 전환, 컷오버)은 표에 "쓰기"로 표시합니다. 다른 갈래가 같은 클러스터를 쓰는 동안에는 돌리지 않습니다.
- 결과는 실행 ID와 시각을 붙여 `docs/evidence/`에 남깁니다. 클라우드 실행 스크립트의 기록은 `deploy/pilot/runs/cloud-*`(Git 제외)에 생기므로 복사합니다(감사 B22).
- "지금" 칸은 2026-11-02 20:25–20:57 KST의 읽기 전용 관측입니다. 이 문서를 쓰면서 점검 명령 가운데 쓰기 명령은 하나도 돌리지 않았습니다.
- **2026-11-03 02:40 KST 정정.** 틀린 사실(환경 이름, 주소, 낡은 "없음 · 열림")만 고쳤고 "(11/3 정정)"을 붙였습니다. 이 표를 다시 돌린 것은 아닙니다. 지금 환경의 정본은 `deploy/CLOUD-E2E-CONTRACT.md` 0절입니다. 실측 시간표로 다시 쓰는 일은 코드 갈래 실측 뒤에 합니다.

**공통 값.** 비밀은 없습니다.

```sh
SRC=bravo-source            # 출발지 EKS 컨텍스트
DST=bravo-onprem            # 온프렘 RKE2 컨텍스트(이름은 K3s 때와 같음, deploy/cloud/cloud.env DST_CONTEXT)
PLAT=bravo-platform-rke2   # 플랫폼 EC2 + RKE2(SSM 포트 포워드 127.0.0.1:26443) (11/3 정정)
GCP=bravo-target-gcp; GKE_KUBECONFIG=infra/envs/target-gcp/.terraform/bravo/kubeconfig   # gke-gcloud-auth-plugin 필요 (11/3 정정)
GCP_PROJECT=b-free-target-20261102
# Executor는 플랫폼 executor-0 하나. 노트북 Executor(127.0.0.1:8085)는 내림 (11/3 정정)
# kubectl --context $PLAT -n bravo-control port-forward svc/executor 18085:8081
EXECUTOR=http://127.0.0.1:18085
FRONT=b-free-chat.mangowhois.ai
# 플랫폼 Service 이름과 포트(ns bravo-control, 11/3 02:3x 관측): api 8080, operator-mcp 8082, console 8080, gate 8084, collector 8083, rehearsal 8090, executor 8081
# 예: kubectl --context $PLAT -n bravo-control port-forward svc/gate 18084:8084
API=<port-forward한 api 주소>  GATE=<gate>  COLLECTOR=<collector>  REHEARSAL=<rehearsal>
```

## 1. S1–S2 연결 점검

S1이 만든 것(환경, 인벤토리, 통제표, 게이트 1차 BLOCK)이 S2(리허설, 차단 기록, 변경 승인, 재대조)로 넘어가는지 봅니다.

### 1.1 환경 전제

| # | 확인 | 명령 | 기대하는 실제 신호 | 지금 |
|---|---|---|---|---|
| A1 | 출발지 노드 | `kubectl --context $SRC get nodes` | 모든 노드 `Ready`, `v1.36.*` | 노드 1 Ready, v1.36.5-eks(20:49) |
| A2 | 출발지 Cilium | `kubectl --context $SRC -n kube-system exec ds/cilium -- cilium-dbg status --brief` | `OK` | Cilium · Hubble Pod Running(20:49). 상태 명령은 돌리지 않음 |
| A3 | 출발지 정책 집행 주체 | `aws eks describe-addon --cluster-name bravo-source --addon-name vpc-cni --query addon.configurationValues` 와 A2 | `enableNetworkPolicy=false`이고 A2가 `OK`(엔진은 Cilium 하나) | `enableNetworkPolicy=false`(20:3x). A2 미확인 |
| A4 | 온프렘 노드 · Cilium | `kubectl --context $DST get nodes -o wide`, A2와 같은 명령을 `$DST`로 | 모든 노드 `Ready`, 판 `v1.36.5+rke2r1`, `OK` | (11/3 정정) 노드 2(`bravo-onprem-server`, `bravo-onprem-agent`) Ready, `v1.36.5+rke2r1`(02:3x). Cilium 상태 명령은 돌리지 않음 |
| A4b | 온프렘 RKE2가 CIS 프로필 | VM 안에서 `sudo grep -E '^profile:' /etc/rancher/rke2/config.yaml`(Lima면 `limactl shell <vm> -- ...`), 그리고 kube-bench 결과 파일 | `profile: cis`(또는 그 판의 CIS 값), kube-bench 결과가 있음(T2 완료 기준) | (11/3 정정) cloud-init 있음(`infra/modules/cluster/onprem/cloud-init`). 운영 목적지의 profile · kube-bench 확인은 게이트 갈래가 진행 중 |
| A4c | 예외 4a 사전 기록 | `docs/evidence/`의 RKE2 시작 거부 · 재설치 기록 | 거부 로그, 고친 cloud-init diff, 재설치 뒤 기동 기록 | (11/3 정정) 있음: `docs/evidence/s1-cis-refusal/`(버리는 Lima VM) |
| A5 | GKE | `gcloud container clusters list --project $GCP_PROJECT --format='value(name,status,currentMasterVersion)'` | `bravo-target-gcp RUNNING 1.36.*` | `RUNNING 1.36.4-gke.2377000`(20:49). (11/3 정정) 컨텍스트 `bravo-target-gcp`(별도 kubeconfig, 위 공통 값) |
| A6 | 플랫폼 EC2 + RKE2 | `kubectl --context $PLAT get nodes -o wide`, `kubectl --context $PLAT -n bravo-control get pods` | 노드 1 `Ready`(`+rke2r1`), 플랫폼 서비스 Pod 모두 Running | (11/3 정정) 노드 `bravo-platform` Ready(`v1.36.5+rke2r1`), 플랫폼 서비스 모두 Running, 실행 digest = 고정 값(02:32, `docs/evidence/cloud-runs/platform-snapshot-20261103T0232KST.txt`) |
| A7 | 플랫폼 API | `kubectl --context $PLAT -n bravo-control port-forward svc/api 18080:8080` 뒤 `curl -s localhost:18080/readyz`. 앞단은 `https://b-free.mangowhois.ai`(Access 로그인) | HTTP 200 | Pod Running(20:56). `/readyz`는 확인하지 않음. `b-free`는 Access 로그인으로 302 |
| A8 | 출발지 앱 계약 이름 | `kubectl --context $SRC -n chat get clusters.postgresql.cnpg.io/chat-db deploy/chat-pgbouncer svc/chat-admin` 그리고 `kubectl --context $SRC -n chat get clusters.postgresql.cnpg.io chat-db -o jsonpath='{.status.phase}'` | 세 개 모두 있음, `Cluster in healthy state` | (11/3 정정) `chat-bravo-demo`, `chat-pgbouncer` 있음(02:3x). CNPG 상태 문구와 `chat-admin`은 다시 확인하지 않음 |
| A9 | 온프렘 앱 계약 이름 | A8을 `$DST`로 | A8과 같음 | `chat-db-1`, `chat-pgbouncer` Running(20:49). `chat-admin`, 상태 문구는 확인하지 않음 |
| A10 | 앞단 대표 이름이 출발지 | `curl -s https://$FRONT/api/config \| jq -r .env.id` | `aws` | (11/3 정정) HTTP 200, `aws`(02:32) |
| A11 | 목적지 직접 이름 | `curl -s https://b-free-chat-dst.mangowhois.ai/api/config \| jq -r .env.id` | `onprem` | `onprem`, HTTP 200(20:50) |
| A12 | 금고 | 금고가 플랫폼 클러스터(`openbao/openbao-0`)면 `kubectl --context $PLAT -n openbao port-forward svc/openbao 8200:8200` 뒤 `deploy/pilot/scripts/keys.sh status` | 봉인 풀림, 감사 장치 있음 | 돌리지 않음. 20:56 노트북 `platform-bao` 없음, `openbao-0` Running |
| A13 | D-16 링크 (11/3 정정) | `kubectl --context $PLAT -n bravo-link get deploy` | 모든 링크 Deployment Ready | 노트북 임시 링크(`link.sh`)는 내림. 이 행은 다시 돌리지 않음 |
| A14 | Executor | `curl -s $EXECUTOR/v1/readiness \| jq '{direction, sub_state, lag_bytes}'` | `forward`, `streaming`, `lag_bytes` 작음 | 20:46 응답 없음은 노트북 Executor 기록. (11/3 정정) executor-0의 readiness는 db-tls 작업에서 `forward` · `streaming` · lag 0(`docs/evidence/db-tls-audit-netpol/`) |
| A15 | 정방향 복제 | `deploy/cloud/repl_setup.sh status` | 목적지 `bravo_fwd_sub` enabled에 worker_pid 있음, 출발지 `bravo_rev_sub` 있고 disabled, `app_writer`는 출발지만 LOGIN | (11/3 정정) 정방향 구독 streaming 확인 기록 있음(`docs/evidence/cloud-runs/cloud-repl-eks-rke2-20261102T221751/`, `docs/evidence/repl-timeouts/`) |
| A16 | reset 계획 | `deploy/scripts/reset-demo.sh --dry-run` | 요약 `FAIL 0`, `NOT_CONNECTED 0` | 20:46 `NOT_CONNECTED 7`, 20:56 `PLAN 6`, `NOT_CONNECTED 3`(`SCRIPT.md` 4절). (11/3 정정) reset은 아직 옛 플랫폼 기본값을 씀(리뷰 B19) |

### 1.2 S1 산출물

| # | 확인 | 명령 | 기대하는 실제 신호 | 지금 |
|---|---|---|---|---|
| B1 | 수집기가 두 클러스터를 읽음 | `curl -s $COLLECTOR/v1/clusters \| jq '.clusters[] \| {cluster_id, status}'` | `bravo-source`, `bravo-onprem`(GKE면 그것도) 모두 `ok` | (11/3 정정) 출발지 `ok`(`docs/evidence/d16-source-k8s/`, 11/2 23:45). 세 클러스터 동시 응답은 다시 확인하지 않음 |
| B2 | 의존성 3등급 | `curl -s $COLLECTOR/v1/inventory \| jq '.summary'` | "코드 수정" 1건 이상(Secrets Manager 직접 호출), `input_digest` 있음 | 확인하지 않음. 탐지기 자체는 Compose에서 응답했음(파일럿(노트북) 기록) |
| B3 | 통제 비교표 | `curl -s "$GATE/v1/controls?source=source-aws&target=target-onprem&level=control" \| jq '.[] \| {control_id, src, dst}'` | 6개 통제(SEC-01, ENC-01, AUD-01, NET-01, IAM-01, DR-01)가 모두 나오고 값은 PASS · FAIL · UNKNOWN 가운데 하나 | (11/3 정정) 플랫폼 게이트 두 쌍 측정 기록(`docs/evidence/gate/platform-gate-20261102T170251Z/`). 감사 A5 · B16 · B17 닫힘 |
| B4 | 게이트 1차 BLOCK | MCP `plan_create` 뒤 `gate_run`(또는 `curl -s -X POST $API/v1/plans/$PLAN/gate -H "Idempotency-Key: $(uuidgen)" ...`). **쓰기** | `verdict: BLOCK`, 이유에 `OBJ-01`(온프렘 버킷의 서명 없는 GET 허용) | (11/3 정정) 온프렘 RGW와 열린 버킷 있음. 게이트 단독 실행에서 OBJ-01 FAIL · BLOCK(170251Z). 계획 경로는 0회(계획 0건) |
| B5 | 이전 후보 등록이 막힘 | `curl -s $API/v1/plans/$PLAN \| jq -r .state` | `GATE`에 머묾. `AWAIT_APPROVAL`로 가지 않음 | 준비 중: B4 |

### 1.3 S2 입구

| # | 확인 | 명령 | 기대하는 실제 신호 | 지금 |
|---|---|---|---|---|
| C1 | 리허설 시작. **쓰기** | MCP `rehearsal_start` 또는 `curl -s -X POST $API/v1/plans/$PLAN/rehearsals -H "Idempotency-Key: $(uuidgen)" -d '{"task":"<task>"}'` | HTTP 202, SSE `trial.started`. `llm_unconfigured`, `cluster_unconfigured`, `verifier_unconfigured`가 아님 | (11/3 정정) 리허설 서비스 배포됨. API 경로 Trial 0회(`rehearsals` 0행). 노트북 CLI 러너 Trial은 `docs/evidence/s1-s2-bucket/` |
| C2 | Trial 네임스페이스의 기본 거부 | `kubectl --context $DST get ns -l bravo.dev/trial-id` 뒤 `kubectl --context $DST -n <ns> get networkpolicy default-deny` | 네임스페이스 1개 이상, `default-deny` 있음 | 준비 중: C1 |
| C3 | 밖으로 나가는 시도가 막히고 기록됨 | `kubectl --context $DST -n kube-system exec ds/cilium -- hubble observe --namespace <ns> --verdict DROPPED --last 20` | 메타데이터(169.254.169.254) 또는 인터넷(1.1.1.1)으로 가는 흐름이 `DROPPED`, 이유 `POLICY_DENIED` | 준비 중: C1 |
| C4 | 파괴적 명령 차단 기록 | SSE에서 `tool.denied` 확인(`curl -sN $API/v1/plans/$PLAN/events?ticket=...`) | 버킷 삭제 시도에 대한 `tool.denied` 한 건 이상 | 준비 중: C1 |
| C5 | 분리 검증 판정 | `curl -s $REHEARSAL/v1/trials/<trial_id> \| jq '{verdict, verifier_pod_uid, artifact_digest}'` | `PASS` 또는 `FAIL`(INFRA_ERROR 아님), Pod UID와 digest가 있음 | 준비 중: C1. (11/3 정정) 감사 B7 · B8은 코드에서 닫힘 |
| C6 | 변경 승인 | 콘솔 `#/approvals`에서 change 승인 | `approval.granted` stage `change`, diff · digest 결속 | (11/3 정정) API에 `change` 단계 있음(마이그레이션 0004). 승인 0건 |
| C7 | 재대조 | B4와 같은 명령. **쓰기** | `verdict: PASS` | 준비 중: C6, T31 |
| C8 | 리허설 뒤 정리 | `kubectl --context $DST get ns -l bravo.dev/trial-id -o name` | 빈 출력(S2-AC4) | 준비 중: C1 |

## 2. S3–S4 연결 점검

S3(전환, 컷오버)이 남긴 상태에서 S4(장애, 데이터째 되돌리기)가 출발할 수 있는지, 끝나면 데모 시작 상태로 돌아가는지 봅니다. 1절이 모두 PASS인 뒤에 합니다.

| # | 확인 | 명령 | 기대하는 실제 신호 | 지금 |
|---|---|---|---|---|
| D1 | Executor 앞단 경로 | `curl -s -o /dev/null -w '%{http_code}' -X OPTIONS $EXECUTOR/v1/front/shift` | `405`(경로 있음). `404`면 앞단 전환 코드가 없는 Executor | 20:46 응답 없음은 노트북 Executor 기록. (11/3 정정) executor-0에 경로 있음(앞단 전환 6회 실행) |
| D2 | 쓰기 차단 스위치가 꺼져 있음 | `deploy/scripts/reset-demo.sh --only writes` | 항목마다 `ALREADY blocked=false`. 켜져 있었다면 `DONE` | `NOT_CONNECTED`(노트북 포워드 없음, 20:46). (11/3 정정) 이 행은 다시 돌리지 않음 |
| D3 | 컷오버 직전 준비 | `curl -s $EXECUTOR/v1/readiness \| jq '{lag_bytes, sub_state, schema_hash}'` | `streaming`, `schema_hash.src == schema_hash.dst` | 준비 중: A14 |
| D4 | 앞단 전환. **쓰기** | `curl -s -X POST $EXECUTOR/v1/front/shift -H 'Content-Type: application/json' -H "Idempotency-Key: $(uuidgen)" -d '{"target_env":"dst"}'` 뒤 `curl -s $EXECUTOR/v1/runs/<run_id> \| jq .status` | `DONE`. LB 가중치 10 → 50 → 100%가 모두 `CONFIRMED_APPLIED`. `curl -s https://$FRONT/api/config \| jq -r .env.id`가 `onprem` (11/3 정정) | (11/3 정정) 하네스로 4회 실행(`docs/evidence/cloud-runs/cloud-front-*`, 편도 약 185초). 콘솔 경로 아님 |
| D5 | 부하 중 컷오버. **쓰기** | `PLAN_ID=... APPROVAL_CUTOVER=... SCENARIO=cutover deploy/cloud/run_e2e.sh`(승인 ID는 콘솔 승인 뒤 API가 기록한 값) | 끝 줄 `PASS`, run `DONE`, `write_pause_ms` 5,000 이하, 영수증 `MATCH`, NFR-2 유실 · 중복 0 | (11/3 정정) dry run 2회는 컷오버 `NOT_STARTED` · 기록 불완전(`docs/evidence/cloud-runs/cloud-20261102T2259*`, `…230116-cutover`). 성공 아님. 감사 A2 · B10 · B14는 코드에서 닫힘 |
| D6 | 컷오버 뒤 쓰기 DB가 하나 | `deploy/cloud/repl_setup.sh status` | 목적지 `app_writer` LOGIN, 출발지 LOGIN 아님. 출발지 `bravo_rev_sub` enabled에 worker_pid 있음 | 준비 중: D5 |
| D7 | 두 PgBouncer가 목적지 | `kubectl --context $SRC -n chat exec deploy/chat-pgbouncer -- cat /run/pgbouncer/active.ini`, 같은 명령을 `$DST`로, 그리고 `cat /etc/pgbouncer/targets/target-dst.ini`와 비교 | 두 클러스터 모두 `active.ini`가 `target-dst.ini`와 같음 | 준비 중: D5 |
| D8 | 목적지 장애 주입. **쓰기** | `deploy/cloud/t25.sh fault` | 앞단 프로브가 목적지 앱의 HTTP 503 · WS nack을 기록 | 코드만, 미실행. 확인 필요: 이 스크립트는 감지 뒤 Executor `/v1/failback`을 바로 부름. AGENTS.md · D-18은 되돌리기를 콘솔의 사람이 시작하게 함 |
| D9 | 되돌리기. **쓰기** | 콘솔 `#/approvals`에서 failback 승인 뒤 `#/plan`에서 시작(`POST $API/v1/plans/$PLAN/shift {"stage":"failback"}`) | failback run `DONE`, 영수증 `MATCH` | 준비 중: D5 |
| D10 | 목적지에서 쓴 메시지가 출발지에 있음(S4-AC1) | D5의 기록 폴더 `deploy/pilot/runs/cloud-*/s4-ac1.json` | `written_after_cutover_on_dst`와 `same_rows_on_src`의 행 수와 해시가 같음 | 준비 중: D9 |
| D11 | 앞단이 출발지로 돌아옴 | `curl -s https://$FRONT/api/config \| jq -r .env.id` | `aws` | 준비 중: D4 |
| D12 | 다음 데모 시작 상태 | `deploy/scripts/reset-demo.sh` 뒤 `deploy/scripts/reset-demo.sh --dry-run` | 첫 실행 종료 코드 0. 둘째(dry-run)는 모든 줄이 `ALREADY` | 준비 중: 위 항목들 |

## 3. 판정 기록

| 점검 | 시각 | 결과(PASS · FAIL · UNKNOWN 수) | 증거 | 누가 |
|---|---|---|---|---|
| S1–S2 | - | 아직 돌리지 않음 | - | - |
| S3–S4 | - | 아직 돌리지 않음 | - | - |
