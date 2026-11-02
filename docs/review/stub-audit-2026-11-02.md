# 가짜 · 스텁 감사 (2026-11-02 20:19–20:37 KST)

읽기 전용 감사 결과입니다. 번호는 담당에게 넘길 때 쓰는 ID입니다. 지금 상태는 바로 아래 "현재 상태" 표가 정본입니다. 그 아래 원문 표의 "경과" 칸은 갈래가 적은 기록이라 낡은 문장이 남아 있습니다.

## 현재 상태 (2026-11-03 02:40 KST, 문서 갈래가 git log · 실행 중 digest · 증거로 확인)

상태를 세 단계로 나눕니다. 한 단계가 됐다고 다음 단계가 된 것은 아닙니다.

- **코드**: 고친 코드가 들어간 커밋. 갈래가 큰 체크포인트로 커밋해서, 갈래 기록의 고침 시각 뒤 첫 커밋을 적습니다. 게이트 · 수집기 · Executor · 리허설 · 부하 생성기는 `240856f`(11/2 21:12), 플랫폼 API · 콘솔 로그인은 `d52c8b3`(21:47)입니다.
- **배포**: 그 코드가 든 이미지가 지금 돈다. 02:32에 `bravo-control`의 실행 digest가 모두 `deploy/platform/control/kustomization.yaml` 고정 값과 같았습니다(api `59f6ba2b`, console `91678860`, gate `ec47f7c3`, collector `af3c66c1`, executor `f5ec7dfd`, rehearsal `1e1337fa`). 모두 아래 고침 커밋보다 뒤의 트리에서 빌드했습니다. 원본: `docs/evidence/cloud-runs/platform-snapshot-20261103T0232KST.txt`.
- **실행 확인**: 실제 환경에서 그 고침이 동작한 기록. 단위 시험만 있으면 "없음"입니다.

플랫폼 DB(02:32)에는 계획 · 승인 · 변경 · 리허설 · 영수증이 모두 0건입니다. 그래서 콘솔 · API 경로가 있어야 확인되는 항목은 실행 확인이 "없음"입니다.

| # | 코드 | 배포 | 실행 확인 | 남은 것 |
|---|---|---|---|---|
| A1 | 문서 `240856f`(21:12) | 해당 없음(문서) | 해당 없음. 원본 사본 `docs/evidence/pilot-notebook/` | 파일럿 수치를 클라우드 값으로 인용하지 않기 |
| A2 | Executor `approvals.py`, API 마이그레이션 0004: `240856f`(21:12). API 쪽 21:4x 고침: `d52c8b3`(21:47) | 예. executor `f5ec7dfd`, api `59f6ba2b`. 플랫폼 DB 0001–0008 적용 | **없음.** 승인 0건, cutover · failback run 0회 | 콘솔 승인으로 컷오버 1회(사람 승인 필요) |
| A3 | API Access JWT 검증 · 허용 목록 JIT: `d52c8b3`(21:47). 콘솔 자동 로그인 제거: `d52c8b3`(`state.tsx` "Login is Cloudflare Access only") | 예. api `59f6ba2b`, console `91678860` | 부분. `b-free` 302 → Access 로그인(02:32). approver(gmail) 첫 로그인으로 역할이 생김(`principals` 1). 목록 밖 이메일의 403은 기록 없음 | operator(naver) 첫 로그인. 아래 원문의 "콘솔 자동 로그인 열림"은 낡은 표기(닫힘) |
| A4 | API: `d52c8b3`(21:47). 콘솔: `4115767`(23:24) | 예. api, console | 없음. 계획 0건이라 운영 화면에서 본 적 없음(서버 렌더 확인만) | 계획 1건으로 확인 |
| A5 | 게이트 `240856f`(21:12) | 예. gate `ec47f7c3` | 예. `docs/evidence/gate/platform-gate-20261102T170251Z/TABLES.md`: 온프렘 OBJ-05 FAIL `other_credential_read_allowed`, GKE OBJ-06 FAIL `plain_http_accepted`, 나머지는 PASS 사유와 함께 | 없음 |
| A6 | 문서 `240856f` | 해당 없음 | 해당 없음 | N12 시험 결과 |
| B7 | `b7671d6`(23:47) | 예. rehearsal `1e1337fa` | 부분. Trial `t-3ac2e611…` PASS(`behavioral+static_rules`), `docs/evidence/s1-s2-bucket/`. **노트북 CLI 러너**로 돌림. 플랫폼 리허설 서비스 경로는 0회(`rehearsals` 0행) | 콘솔 → API → 리허설 서비스 1회. 평문 HTTP · 앱 PUT · 다른 자격 거부 검사(리뷰 B11) |
| B8 | 리허설 `240856f` | 예. rehearsal | 부분. 같은 Trial이 INFRA_ERROR 없이 채점됨(노트북 CLI) | B7과 같음 |
| B9 | 리허설 `240856f`(`snapshot.py` 추가) | 예. rehearsal | 부분. 같은 Trial이 스냅숏 37파일을 씀(CLI `--snapshot`). `PUT /v1/snapshots` 플랫폼 경로는 0회 | B7과 같음 |
| B10 | Executor `MATCH_PARTIAL` · `verdict_parts`: `240856f`. 콘솔: `0d47efa`(23:26) | 예. executor `f5ec7dfd`, console `91678860` | **없음.** 영수증 0건 | 컷오버 전 첨부 대조, 영수증의 검증 범위 표시(리뷰 B28) |
| B11 | API `d52c8b3`(21:47) | 예. api. API의 OpenBao 토큰(`BRAVO_BAO_TOKEN_FILE`, `deploy/platform/control/api.yaml`)도 배포에 있음. 아래 원문의 "남음: API OpenBao 자격"은 닫힘 | 없음. 환경 등록 0건(`environments` 0행) | API로 환경 등록 1회 |
| B12 | `deploy/cloud/contract.sh` E5: `240856f` | 해당 없음(스크립트) | **없음.** 클라우드 E5 0회(콘솔 cutover 승인 두 개 필요) | E1 · E5 · E6 |
| B13 | `240856f` | 해당 없음(스크립트) | 없음. 클라우드 컷오버 0회 | - |
| B14 | 부하 생성기 `stats.py`: `240856f` | 해당 없음(운영자 도구) | 예. `docs/evidence/cloud-runs/cloud-20261102T225937-cutover/`, `…230116-cutover/`의 부하 요약: 보냄 = 확인, 유실 · 중복 0, `accounted: true` | - |
| B15 | README만 `240856f` | 해당 없음 | 해당 없음 | **요구 미충족.** 쓰기 50 · 읽기 150 req/s 부하가 없음(리뷰 B15) |
| B16 | 게이트 `240856f` | 예. gate `ec47f7c3` | 예. `platform-gate-20261102T170251Z`: 두 쌍 모든 절 측정, UNKNOWN 0 | - |
| B17 | 게이트 `240856f`, 이후 게이트 커밋들 | 예. gate | 예. 같은 실행. 출발지 OBJ-04는 실요청이 아니라 IAM 시뮬레이션이고, 이제 행의 `method`가 `mixed`(`src_method: simulation`)로 그렇게 보임 | 대응표 `docs/architecture/16-gate-test-map.md`, 게이트 밖 시험은 D-29 |
| B18 | 수집기 `240856f` | 예. collector `af3c66c1` | 없음. 시험만(`test_masks_cover_secret_shapes`) | - |
| B19 | 수집기 · 데모 앱 스키마 `240856f` | 해당 없음(DB 초기화 · 스크립트) | 부분. 온프렘 RKE2 역할이 SCRAM(`docs/evidence/cloud-runs/cloud-dbroles-onprem-rke2-20261102T221719/state.txt`) | 구독 conninfo 안 비밀번호(PG16 제약)는 남음 |
| B20 | `1fc9db3`(23:22) | 해당 없음 | 예. `deploy/pilot/.env` 없음(02:30 확인) | - |
| B21 | `TARGET_SHARE` 판정: executor `98f57c71` 고정 `212b930`(22:48) | 예. executor `f5ec7dfd`(같은 판정) | 예, **하네스 실행만.** `docs/evidence/cloud-runs/`: LB 10/50/100 이동 4회(1회는 거짓 DONE 결함), 자동 0% 복귀 2회. 콘솔 · 상태 머신 경로는 0회. 아래 원문의 "증거는 9ca7af4"는 틀림(그 커밋에 실행 기록 없음) | 상태 머신 SHIFT · 콘솔 시작(리뷰 B02), 업무 트래픽 판정(B14) |
| B22 | 파일럿 사본 `240856f`, 클라우드 사본은 이 갱신과 같은 커밋 | 해당 없음 | 예. `docs/evidence/pilot-notebook/`, `docs/evidence/cloud-runs/` | 앞으로 생기는 `deploy/pilot/runs/cloud-*`도 옮기기 |
| G1 | `ac4e1aa`(00:43), 재암호화 · 객체 수준 검사는 이 갈래 L3 커밋 | 예. 온프렘 RGW(Rook) 설정. 객체 수준 검사는 게이트 `c841a75e`(push 전) | 예. `docs/evidence/gate/obj-enc-onprem-20261102T152228Z/`. 기존 객체 4개 재암호화(02:36, 본문 sha256 같음, `docs/evidence/gate/obj-enc-reencrypt-20261103/`), 게이트 OBJ-ENC 객체 수준 4/4(미리 보기 `platform-gate-20261102T175426Z`) | 링크가 끊기면 PUT · GET 500, 이제 canary GET도(리뷰 B17). 출발지 S3는 객체 수준을 못 봄(목록 권한 없음) |
| G2 | `8542cda`(00:11) | 예. 세 환경 CNPG 백업 설정 | 예. `docs/evidence/gate/dr-restore-*`, DR-RESTORE · DR-ARCHIVE PASS(170251Z) | 정기 백업 없음 |
| G3 | 대체 통제 질문 OBJ-PLAIN-EGRESS: 이 갈래 L3 커밋 | 게이트 `c841a75e`(push 전). 온프렘 앱 egress 443만(손으로 patch, 값 파일 커밋) | 예. GKE OBJ-06 FAIL 그대로. OBJ-PLAIN-EGRESS 세 환경 PASS(`docs/evidence/gate/obj-plain-egress-20261103/`, 미리 보기 175426Z) | 예외 승인 0건(콘솔, 사람). 대체 통제 FAIL이 예외를 무르지는 않음 |
| G4 | `db4c4e3` · `1a84e8c` · `0980ff7`(00:55–01:14), 게이트 고정 `ec47f7c3`은 `f147be4`(01:52) | **예(닫힘).** origin/main에 있고 Deployment `gate`가 `ec47f7c3`(02:32). 원문의 "push 필요"는 해소 | 예. `platform-gate-20261102T170251Z`(svc/gate, Deployment 이미지 `ec47f7c3`). 측정 때 Argo `control`은 OutOfSync(리뷰 B20) | - |
| G5 | `50218ea` … `50399a7`, `badb7e2`(01:35–01:49) | 예. gate · api · collector. OpenBao NetworkPolicy `openbao` · `health-from-observe`가 있고 Argo `openbao` Synced. 원문의 "남음"은 해소 | 예. `platform-gate-20261102T165054Z`(미리 보기 Pod), `…170251Z`(Deployment) | - |
| G6 | `624013d`(02:07) | 예. `executor-0`이 02:08:44 KST에 새 topology(verify-full 6곳)로 다시 뜸. 고객 클러스터 설정은 손으로 적용(Argo 밖) | 예. `docs/evidence/db-tls-audit-netpol/`, 170251Z DB-TLS · DB-AUDIT · NET-DEFAULT-DENY PASS. E4는 push 전에 같은 topology로 executor-0 안에서 실행 | E1 · E5 · E6(사람 승인 필요). 복제 타임아웃 60초는 `d532c68`(02:31, `docs/evidence/repl-timeouts/`) |
| G7 | `cd22cc7`(01:57) | 예. 세 클러스터 VAP. GKE 고정 이미지를 `b5c0e764…`로 바꿔 적용(02:53) | 예. `docs/evidence/gate/netprobe-admission-20261102T165631Z/`, Deployment 기준 실행 165445Z · 165646Z UNKNOWN 0. GKE 이미지 바꾼 뒤 NET-PATHS PASS(175426Z) | - |
| G8 | 게이트 규칙 `gate-controls/2026-11-03.2`: 이 갈래 L3 커밋 | 게이트 `c841a75e` 고정, **push 필요** | 예, 미리 보기 Pod. `docs/evidence/gate/platform-gate-20261102T175426Z/`: 질문 22개, UNKNOWN 0, 새 질문 SEC-GIT-PLAINTEXT · OBJ-PLAIN-EGRESS · NET-EXTERNAL-SCAN 두 쌍 양쪽 PASS | push · Argo 동기화 뒤 `svc/gate`로 다시. 회전 · 로그 마스킹 · DB 감사 검색 · Prowler는 게이트 밖(D-29) |
| G9 | 러너 · 랜딩 존: 이 갈래 L3 커밋 | 예. 온프렘에 MutatingPolicy · 기본 SA 패치 적용(02:34) | 예. `docs/evidence/kube-bench/onprem-20261103/`: FAIL 4 → 3, 남은 FAIL마다 판단 | EKS · GKE kube-bench FAIL 미판정 |
| G10 | GKE 이미지 digest 복사: 이 갈래 L3 커밋 | 예. GKE `chat-bravo-demo` `b5c0e764…`(02:52) | 예. `docs/evidence/gke-same-digest-20261103/`, 세 클러스터 같은 digest, `b-free-chat-gke` 200 | - |

중간 항목(23–50)은 문서 · 시험 기준으로 고쳤고, 운영 실행 확인은 따로 하지 않았습니다. 아직 열린 것은 31(`mode: recorded`를 쓰는 코드 없음, 리뷰 P21)과 44(금고 보관 파일 집중)입니다. 26은 API 코드가 `security_holds: null`, `security_holds_reason: not_implemented:D-20`으로 둡니다(D-20 미구현). 계획이 0건이라 운영 응답으로는 보지 못했습니다.

**갱신 규칙.** 항목을 닫는 갈래는 이 표의 세 칸을 갱신합니다. 실행 확인 칸에는 증거 경로와 시각을 적습니다. 아래 원문 표의 "경과" 칸에는 한 줄만 덧붙입니다.

## 원문(2026-11-02 20:19–20:37 감사)과 갈래 경과

| # | 등급 | 위치 | 문제 | 담당 | 경과(갈래 기록) |
|---|---|---|---|---|---|
| A1 | 치명 | `deploy/pilot/README.md:39,40,42` | 증거 파일 없는 수치(18:52 107 ms, 18:58 97 ms). 19:26 불일치 42 ms는 run.json과 모순(`write_pause_ms: null`, 단계 합 66 ms). 실제 DONE 기록: 76, 80, 87, 92, 94, 101, 105, 139 ms | 문서 | 고침(T29, 문서 갈래 21:0x). 실행 ID 표로 바꾸고 "파일럿(노트북) 기록"으로 표시. 원본 사본 `docs/evidence/pilot-notebook/` |
| A2 | 치명 | `platform/executor/src/bravo_executor/app.py:31`, `service.py:175`, `cutover.py:61,152` | Executor가 아무 `approval_id`나 받음(최소 길이만 확인). 승인 표를 확인하지 않음. 스크립트가 가짜 ID(`smoke-approval`, `e1`)에 기댐 | 데이터 · 컷오버 | 고침(Executor 쪽, 21:0x): `approvals.py`가 플랫폼 DB `approvals`를 읽어 계획 · 단계 · 요청자≠승인자 · 만료 · 재사용을 확인, 아니면 403. 표가 없거나 못 읽으면 거부(APPROVAL_SOURCE_UNAVAILABLE). 시험 `tests/test_approvals.py`. 스크립트는 실제 승인 ID를 env로만 받음. **남음**: platform/api가 `bravo_executor`에 `approvals` SELECT · 정책을 주고 `stage`에 `failback`을 더해야 함. executor-0은 아직 예전 이미지(검증 없음)라 새 이미지 `bravo-platform/executor@sha256:32c2b92b…`로 바꿔야 적용됨 / **API 쪽 고침(21:4x)**: 마이그레이션 0004가 `bravo_executor`에 `approvals` SELECT와 RLS 정책 `executor_cell`(읽기만)을 줌. `stage`는 `change` · `cutover` · `failback`. 계약은 `docs/architecture/15-r2-contracts.md` 5절. 시험 `platform/api/tests/test_changes.py::test_executor_role_reads_approvals_for_its_a2_check`. 서명된 승인 토큰은 만들지 않음. 남음: 0003–0006이 플랫폼 DB에 적용돼야 함(EKS 판은 0001–0002만 적용됨) / **닫힘(23:3x)**: 플랫폼 RKE2 DB에 0001–0007 적용 확인(`schema_migrations`). executor-0은 A2 검사가 든 이미지(`kustomization.yaml` 고정 digest)로 동작 |
| A3 | 치명 | `platform/api/src/bravo_api/rest.py:159-171`, `devseed.py`, `console/src/state.tsx:66` | 개발용 IdP가 스텁. 인증 없이 operator · approver JWT를 주는 `/v1/dev/tokens`, 존재하지 않는 발급자 · 사용자, 콘솔 자동 로그인 | 플랫폼 API | 고침(플랫폼 API, 21:3x): Access JWKS를 certs URL에서 받아 캐시, 받기 실패 · 모르는 kid는 거부(fail closed). `/mcp`는 MCP AUD, 나머지는 콘솔 AUD. 설정이 없으면 503 `access_not_connected`("연결 안 됨: Cloudflare Access"). `/v1/dev/tokens` · `BRAVO_DEV_AUTH` · 개발 시드 제거, `deploy/pilot/devauth` 삭제. 첫 로그인 역할은 설정 파일 허용 목록(`BRAVO_ACCESS_MEMBERS_FILE`, 마이그레이션 0006)만, 그 밖은 403 `unknown_principal`. 시험 `tests/test_auth.py`, `tests/test_landing.py`. 콘솔 자동 로그인(`console/src/state.tsx:66`)은 콘솔 갈래 몫, 열림 / 정정(11/3 02:40, 문서 갈래): 콘솔 자동 로그인은 `d52c8b3`에서 없어짐(`state.tsx`는 `GET /v1/me`만 씀) |
| A4 | 치명 | `platform/api/src/bravo_api/service.py:303,336-341`, `console App.tsx:194`, `api/types.ts:45`, `StateProgress.tsx:42` | 수집기가 설정 안 됐는데 "의존성 공백 없음"으로 처리해 ANALYZE를 건너뜀. 콘솔은 ANALYZE · PROPOSE · REHEARSE를 "파일럿 범위 밖"이라고 거짓 표시 | 플랫폼 API · 콘솔 | API 쪽 고침(21:2x): 수집기가 없거나 못 읽으면 ANALYZE에 머물고 `state_reason`=`collector_unconfigured` · `collector_*`. 수집기 실제 형식(`inventory/v1`의 `findings` · `unclassified` `tier_code`)을 읽음. `plan.analysis`는 `GET /v1/plans/{id}`에 있음. 시험 `tests/test_changes.py::test_unconfigured_collector_keeps_analyze_with_the_reason`. 콘솔 고침(23:3x, 닫힘): "파일럿 범위 밖" 표시(`OUT_OF_SCOPE_STATES`) 삭제. 현재 단계에 `state_reason`이 있으면 "멈춤"으로 그리고 끝난 단계로 세지 않음, 이유를 문장과 `state_reason=…`으로 보임(`collector_unconfigured` · `inventory_root_empty` · `collector_*`). 이벤트에 들어간 기록이 없는 앞 단계는 "지남"이 아니라 "기록 없음". 분석 칸은 `plan.analysis`를 읽고 인벤토리가 `ok`가 아니면 "분석 안 됨"과 이유. 홈 계획 표도 "ANALYZE · 멈춤"과 이유. 렌더 확인: `StateProgress`를 서버 렌더로 그려 ANALYZE+`collector_unconfigured`에서 ANALYZE가 `halted`(멈춤)이고 `done`이 아님(콘솔 `src/components/StateProgress.tsx`, `src/App.tsx`, `src/api/normalize.ts`) |
| A5 | 치명 | `platform/gate/src/bravo_gate/probes/objstore.py:27-28,271-273` | OBJ-06은 시간 초과 · 차단도 PASS, OBJ-05는 잘못된 자격도 "거부됨"으로 PASS | 보안 · 게이트 | 고침(20:46). OBJ-05는 `AccessDenied`일 때만 PASS, 자격 오류는 UNKNOWN. OBJ-06은 연결 거부 또는 https로 canary를 읽는 같은 자격이 http에서 `AccessDenied`일 때만 PASS, 시간 초과 · 끊김 · DNS는 UNKNOWN. `tests/test_verdict_rules.py` |
| A6 | 치명 | `docs/spec/TECH-STACK-SPEC.md:50` | "cloudflared 2026.10.7 (N12 통과 판)" 주장. N12는 11/3 예정 | 문서 | 고침(T29). "후보 판, N12 전" |
| B7 | 높음 | `platform/rehearsal/README.md:12`, `agent/harbor/_shared/verify.py:96-129` | 검증기는 제안 JSON 정적 검사뿐인데 README는 재배포 · 비공개 시험이라고 씀 | 리허설 | 고침(23:45, 리허설). 20:46: 정적 검사임을 기록으로 고정(`verifier_kind: static_rules`). 23:45: 동작 시험 추가. 제안서에 `rgw_bucket_policy`가 있으면 플랫폼이 별도 검증 Job(`behavioral.py`, `verify_job_s3.py`)을 `verify-<plan>-<n>-job`에서 돌림: RGW 시험 버킷 `bravo-rehearsal-verify`에 그 정책을 적용하고(버킷 이름 · 앱 주체만 바꿈), 서명 없는 GET은 `AccessDenied`, 소유자 키 GET은 200이어야 함. 대조 단계(열린 정책이면 익명 200), Job 이미지 digest 결속, 송신 차단 기록이 함께 맞아야 함. Job이 실제로 돌았을 때만 `verifier_kind: behavioral+static_rules`, 필요한데 못 돌면 PASS 대신 INFRA_ERROR. 정적 규칙은 그대로. 키는 OpenBao `secret/envs/<env>/rehearsal-s3`에서 Trial마다 읽음(D-26). 실측: Trial `t-3ac2e611c3bb42df92c0eeea6adc36cb`(온프렘 RKE2, Claude Agent SDK) PASS, 익명 GET `403 AccessDenied` · 소유자 GET 200, 열린 정책 사전 점검은 `anonymous_get_denied` 실패로 잡힘. 증거 `docs/evidence/s1-s2-bucket/`. 시험 `tests/test_behavioral.py`. 이미지 `rehearsal@sha256:1e1337fa…`(pin 변경, 푸시는 팀장). 남음: 평문 HTTP 거부(OBJ-06)는 이 Job이 보지 않음 |
| B8 | 높음 | `rehearsal/service.py:259`, `harbor_task.py:162-163` | `expected` 없이 materialize → 모든 역할 리허설이 INFRA_ERROR | 리허설 | 고침(리허설): `service.expected_for(trial)`이 봉투에서 role · plan_id · trial_id · attempt · evidence_ids를 만들어 `materialize`에 넘김. 시험 `test_agent_template_verifier_grades_frozen_artifact_and_binds_digest` |
| B9 | 높음 | `rehearsal/mcp_tools.py:48,144-155` | `repo_read` 루트가 고객 저장소가 아니라 템플릿 폴더 | 리허설 | 고침(리허설): 스냅숏이 없으면 `repo_unconfigured`, 템플릿 폴더는 쓰지 않음(`/src`도 비움). `PUT /v1/snapshots/{tenant}/{plan}`이 수집기 인벤토리 `input_digest`와 같은 파일 집합만 받고 `.env` 계열은 뺌. 올리기 도구 `python -m bravo_rehearsal.snapshot pack` · `upload`(수집기 `scan`과 대조). 시험 `test_snapshot_upload_is_bound_to_collector_inventory` |
| B10 | 높음 | `executor/cutover.py:614-656` | 첨부 객체 `NOT_CHECKED`인데 영수증 MATCH. 콘솔은 objects를 안 보임 | 데이터 · 컷오버, 콘솔 | 고침: 객체 `NOT_CHECKED`면 영수증 `MATCH_PARTIAL`, `verdict_parts`(tables · sequences · objects)를 함께 저장. 콘솔 고침(23:4x): 영수증 본문의 `tables[]` · `sequences[]` · `objects` · `verdict_parts`를 읽어 표별 비교와 부분 판정을 보이고, `MATCH_PARTIAL`은 초록 MATCH가 아니라 "일부만 확인"(첨부 객체 `NOT_CHECKED`와 이유 · 참조 수)으로 표시. 출발 · 목적은 영수증의 `source` · `target`(`src/components/CutoverPanel.tsx`, `src/api/normalize.ts`, `src/live/DemoAppScreen.tsx`) |
| B11 | 높음 | `api/service.py:127-157,262-276` | 환경의 `secret_ref`를 풀지 않음. 아무 ref나 등록해도 동작 | 플랫폼 API | 고침(플랫폼 API, 21:3x): 등록 때 플랫폼 OpenBao KV 메타데이터로 `secret_ref` 확인(값은 읽지 않음). 없으면 422 `secret_ref_not_found`, OpenBao에 못 닿으면 환경 `unverified` + 이유, 그 환경의 계획은 COLLECT에 머묾(`environment_secret_unverified:<이유>`). `envs/<환경 이름>/…` 참조 허용(0006). 시험 `tests/test_changes.py::test_secret_refs_resolve_in_openbao_or_block_collect`. 남음: API가 쓸 OpenBao 자격(AppRole, `secret/metadata/tenants/*` · `secret/metadata/envs/*` 읽기만)을 플랫폼 배포에 넣는 일 |
| B12 | 높음 | `deploy/pilot/scripts/contract_tests.sh:136-141` | E5가 구조상 항상 통과(Executor를 부르지 않음) | 데이터 · 컷오버 | 고침: `deploy/cloud/contract.sh` E5가 낮은 epoch로 `POST /v1/cutover`를 보내 `FENCE_REJECTED`(CONFIRMED_NOT_APPLIED) · ABORT_PRE_WRITE를 확인. Compose 판은 실행하지 않고 끝나게 함. 클라우드 실행은 아직 없음 |
| B13 | 높음 | `contract_tests.sh:147-153` | RESUME 실패를 무시하고 `effects`에 CONFIRMED_APPLIED와 지어낸 문장을 씀 | 데이터 · 컷오버 | 고침: 런북 단계는 RESUME 뒤 실제 `SHOW DATABASES` 행을 저장하고 `paused = 0`일 때만 진행. 스크립트가 `effects`를 쓰지 않음(해소 기록은 플랫폼을 거침) |
| B14 | 높음 | `tools/loadgen/src/bravo_loadgen/stats.py:73-75,92-93` | 응답 0건이어도 ok. 중복 검사가 구조상 0 | 데이터 · 컷오버 | 고침: `ok`는 확인 > 0, 보냄 = 확인 + 실패, 유실 · 중복 · 실패 0. 중복은 본문 표지로 셈. 시험 추가 |
| B15 | 높음 | `tools/loadgen/README.md:3` | 50 쓰기 · 150 읽기 req/s 주장. 실제 기본 20, 읽기 경로 없음 | 데이터 · 컷오버 | 고침: README를 있는 것(쓰기만, 기본 20/s)으로 바꿈 |
| B16 | 높음 | `gate/service.py:67-72`, `controls.py:119` | 설정 안 된 섹션은 질문을 안 만들어 일부만 보고 PASS | 보안 · 게이트 | 고침(20:46). 다섯 절을 늘 모두 시험. 설정 안 된 절은 `not_configured` UNKNOWN 행으로 남음. `test_http_api_contract` |
| B17 | 높음 | `gate/probes/objstore.py:159-175`, `postgres.py:107-150`, `platform.py:68-80,212-257` | OBJ-02/03 canary 없음, 앱 역할 없으면 DB-AUTH PASS, DNS 오류를 차단으로 셈, AUD 선택자 무시, SAR 오류 무시 | 보안 · 게이트 | 고침(20:46). OBJ-02/03도 canary 확인, 앱 역할이 없으면 DB-AUTH UNKNOWN(`app_role_missing`), DNS 오류는 `denied_path_inconclusive`, CloudTrail은 관리 이벤트 쓰기 기록 선택자만 인정, SAR `evaluationError`는 UNKNOWN. 앱 네임스페이스 · Secret이 없으면 K8s 질문 UNKNOWN(추가 발견) |
| B18 | 높음 | `collector/inventory/detector.py:83-88` | 증거 마스킹이 `aws_secret_access_key=`, `DB_PASSWORD=`, JSON password를 못 가림(D-26) | 보안 · 게이트 | 고침(20:46). 키 이름 안 어디에든 비밀 단어가 있으면 가림(`DB_PASSWORD=`, `aws_secret_access_key =`, JSON `"password"`, URL userinfo, `password=`). `test_masks_cover_secret_shapes` |
| B19 | 높음 | `collector/sql/collector_ro.sql:6-10`, `apps/demo/schema/00-bravo-init.sh:30-35`, `repl-setup.sh:35,42,55` | 비밀번호가 psql 인자 · SQL 문자열에 평문(D-26) | 보안 · 게이트, 데모 앱 | 수집기 부분 고침(20:46): `collector_ro.sql`은 `\getenv` + 클라이언트 쪽 SCRAM-SHA-256 검증값만 받고 평문이면 거부(`python -m bravo_collector scram-verifier`). 데모 앱 부분 고침(21:00): `00-bravo-init.sh`가 비밀번호마다 클라이언트 쪽 SCRAM-SHA-256 검증값을 만들고 `02_roles.sql`이 `\getenv`로 읽음(psql 인자 · SQL에 평문 없음, 시험 `test_roles_store_scram_verifiers_made_client_side`). `repl-setup.sh`는 인자 대신 psql 안에서 `\getenv`로 conninfo를 만들고, 그 세션은 `log_statement = none` · `log_min_error_statement = panic`. 남는 것: PG16은 비슈퍼유저 구독 소유자(`executor_ctl`)에게 conninfo 안 비밀번호를 요구해(`passfile=` 거부 실측) `CREATE SUBSCRIPTION` 한 문장과 `pg_subscription.subconninfo`(슈퍼유저만 읽음)에는 남음. 서버 · repl-setup 로그에 비밀번호 0건 실측 |
| B20 | 높음 | `deploy/pilot/.env`, `compose.data.yaml:136-144` | 로컬 파일의 장기 평문 비밀(Compose는 내렸음, 파일 정리 필요) | 플랫폼 EKS | 닫힘(23:3x): `deploy/pilot/.env`의 비밀 값 6개를 플랫폼 OpenBao 값과 해시로 대조해 하나도 쓰이지 않음을 확인(Compose 전용 값). 파일은 휴지통으로 옮김. `CLOUDFLARE_API_TOKEN`은 이미 비어 있었고 `secret/platform/cloudflare`에 있음. `keys.sh status` · `with-cloudflare` 정상 |
| B21 | 높음 | `deploy/CLOUD-E2E-CONTRACT.md:45`, `executor/README.md:31-32`, `14-provisioning-scenarios.md:89` | Cloudflare 가중치 전환을 동작하는 것처럼 씀. 실제는 미실행(`front.py` 주석) | 문서, 데이터 · 컷오버 | 표시는 문서 갈래가 넣음(코드만, 미실행). `front.py`는 `b-free`(플랫폼) 전환을 거부하고 `b-free-chat`만 다룸. 실제 실행(LB 포함)은 클라우드 앱 · 터널 연결 뒤. 문서 갈래(T29): `CLOUD-E2E-CONTRACT.md` 6절, `platform/executor/README.md`, `14-provisioning-scenarios.md`, `docs/demo/SCRIPT.md`에 표시함 / 닫힘: 실제 실행함. Cloudflare LB `b-free-chat` 가중치 10/50/100을 양방향으로 바꾸고 오류 0, 결함 주입 때 `REVERT_DST_0` · `ABORT_PRE_WRITE`로 되돌림(T17, `TARGET_SHARE` 판정). 증거는 9ca7af4의 클라우드 E2E 하네스 실행 기록 / 정정(11/3 02:40, 문서 갈래): 9ca7af4에는 실행 기록 파일이 없음(문서 · 콘솔 2파일). 실행 기록은 `docs/evidence/cloud-runs/`로 옮김. 하네스 실행이고 콘솔 · 상태 머신 경로는 아님 |
| B22 | 높음 | `.gitignore:1`(`runs/`), `smoke-…191253/s4-ac1.json`, `smoke-…194436` | 증거가 커밋되지 않고, 실패한 실행이 섞여 있음 | 문서 | 고침(T29). `docs/evidence/pilot-notebook/`에 스모크 11 · 계약 5 사본, `RUNS.tsv`에 실행별 PASS · FAIL(FAIL 3, 보류 4) |

중간 · 낮음(23–50)은 감사 원문 기준으로 각 담당에게 넘겼습니다.

보안 · 게이트 갈래가 고친 중간 · 낮음 항목(2026-11-02 20:46 KST):

| # | 상태 |
|---|---|
| 36 | 고침. `GET /v1/clusters`에서 읽기에 실패한 목록에 기대는 랜딩 존 행은 `present: null`, 클러스터 `status: partial`과 `failed_reads` |
| 37 | 고침. 코드 안 DSN 리터럴은 `코드 수정` 등급, 읽지 않은 파일은 `skipped[]`에 이유와 함께 |
| 38 | 고침. 수집기 오류 문자열(`model.failed`, `clusters.short_error`)과 게이트 증거(`result.error_text`)를 같은 패턴으로 가림 |
| 48 | 고침. 게이트가 쓴 시험 객체는 지우고 결과를 증거에 남김. HTTP GET(`/v1/controls`, `/v1/controls/report`)은 쓰기 시험을 하지 않음(`write_probe_not_allowed_here`). 쓰기 시험은 CLI `run --write-probes`만 | 요약: 문서 수치 불일치(23–25), 보안 보류를 빈 배열 상수로 반환(26), 호스트 이름 하드코딩(27), 심은 회귀를 스키마에 기본 포함(28), 시험용 충돌 훅이 제품 코드에(29–30, 29 고침: `BRAVO_ENABLE_FAULT_INJECTION=1` 없이 충돌 지점이 있으면 시작을 거부. 30 고침: COMMIT_POINT 관측이 플랫폼 DB의 6a 단계 · 구독 효과를 읽음), 쓰지 않는 RECORDED 모드(31), 워커로 넘기는 환경 변수 범위(32), 증거 행에 상수 값(33), 계정 0번 템플릿 · 로컬 digest(34), `/healthz`만 있고 준비 상태 없음(35), 읽기 실패를 "없음"으로(36), DSN 분류 오류 · 조용한 건너뛰기(37), 오류 문자열 미가림(38), 데모 앱이 DB 확인 전 "live" 표시(39), 링크 ack 없음(40), 차트 예시 값(41), `infra/README.md` 낡음(42), 출발지 ESO 역할 범위 과다(43), devauth 키 · 금고 보관 파일 한 곳 집중(44), 낮은 등급 6건(45–50).

중간 항목 가운데 리허설 몫의 상태(2026-11-02 20:46):

| # | 상태 |
|---|---|
| 32 | 고침: 워커 환경은 정확한 이름 목록(`sdk_options.ROUTE_VARS` 8개)과 운영자가 고른 `CODEX_HOME` · `HOME`뿐. 접두어 전달 없앰 |
| 33 | 고침: 증거 행의 `harbor_version`은 설치된 판(`importlib.metadata`), `quadrant`는 실제 실행에서 정함(검증기가 채점했으면 FIX, 아니면 ANALYZE), `backend`는 환경 유형, 메모에 `verifier_kind` |
| 34 | 리허설 몫 고침: `prompts/rehearsal-agent.md`(없는 도구 이름)와 `tasks/templates/fix-bucket-policy`(계정 `000000000000`, 옛 도구 설명)를 "준비 중"으로 표시, 코드가 읽지 않음을 명시. `platform/agent/harbor/*` 로컬 digest와 `allowlists/ci.yaml`의 0 digest는 에이전트 갈래 몫으로 넘김 |
| 35 | 고침: `GET /readyz`가 LLM · 클러스터 · 이미지 · 도구 계층 · OpenBao · 게이트를 항목별 이유로 보고(준비되면 200, 아니면 503) |

데모 앱 몫의 상태(2026-11-02 21:00, 이미지 `bravo-demo:0.3.0`):

| # | 상태 |
|---|---|
| 28 | 고침: 목적지 `GRANT SELECT ON messages TO PUBLIC`은 `BRAVO_PLANT_REGRESSION=1`일 때만(`03_dst.sql` `\if`), 초기화 로그에 "심은 시나리오 적용/없음"을 남김. 기본은 넣지 않음(시험 2개). 파일럿에서 게이트 1차 BLOCK 장면을 쓰려면 `compose.data.yaml`의 `dst-db`에 `BRAVO_PLANT_REGRESSION: "1"`이 필요(데이터 · 컷오버 몫) |
| 39 | 고침: `live` 배지는 서버가 쓰기 DB를 실제로 읽은 뒤(`db_ok`)에만, 그전은 "확인 중". `/readyz`는 PgBouncer로 DB를 읽은 적이 있고 마지막 성공이 30초 안일 때만 200. `APP_ENV_ID` 기본값 없앰(필수). 첨부는 `ObjectStore.ping()`(HeadBucket) 성공 뒤에만 알리고 15초마다 다시 확인, 실패 시 업로드 503 `attachments_unavailable`(시험 3개) |
| 41 | 고침: `chart/ci/*-values.yaml`은 파일 머리와 값마다 `EXAMPLE` 표시, 예시 값은 `*.example.invalid` · 0 digest · 계정 `000000000000`으로 바꿈. 배포 값은 `apps/demo/scripts/render-values.sh`가 `terraform output`과 실제 이미지 참조에서 만들고, 없으면 멈춤 |

문서 갈래(T29)가 고친 중간 항목(2026-11-02 21:0x KST). 41은 데모 앱 갈래가 고쳤습니다(위 표).

| # | 위치 | 고친 것 | 상태 |
|---|---|---|---|
| 23 | `deploy/pilot/CONTRACT.md`, `docs/architecture/12-db-migration-matrix.md`, `14-provisioning-scenarios.md` | 파일럿 DB를 "CNPG 이미지"에서 "PostgreSQL 공식 이미지, CNPG 운영자 없음"으로. ms 범위를 실행 파일 기준 76–139 ms로 | 고침 |
| 24 | `README.md` | "구현 전" 상태 문장을 실제 상태로, `.github/workflows/`를 실제 파일 이름으로, 클라우드 명령 문장을 고침 | 고침 |
| 25 | `docs/spec/SPEC-TRACE.md` | 작업 상태를 증거 기준으로("진행", 완료 0). T10 "목업으로" 기준에 목업 금지 지시를 덧붙임. T30 · T31 행 추가 | 고침 |
| 42 | `infra/README.md` | 시험 스크립트 경로(없음 표시), TopoLVM · SeaweedFS 제거, Terraform 1.17.1, D-26 비밀 경로, 랜딩 존 실제 설치 단계 | 고침 |

플랫폼 API 갈래가 고친 중간 항목(2026-11-02 21:4x KST):

| # | 상태 |
|---|---|
| 26 | 고침. `GET /v1/plans/{id}`의 `security_holds`는 `null`, `security_holds_reason`은 `not_implemented:D-20`. 빈 배열 상수를 없앰 |
| 44 | 플랫폼 API 몫 고침. `deploy/pilot/devauth`(키 · 토큰 · JWKS)를 지우고 제품 경로에서 개발 키를 읽는 코드를 없앰. 금고 보관 파일 집중은 열림 |

게이트 통제 갈래(2026-11-03 01:1x KST). 감사 뒤 게이트 실행에서 나온 인프라 공백과 플랫폼 게이트 연결입니다. 결과는 `docs/evidence/gate/`에 있습니다.

| # | 등급 | 문제 | 상태 |
|---|---|---|---|
| G1 | 높음 | 온프렘 RGW OBJ-ENC FAIL `no_default_encryption` | 2026-11-03 02:36 L3: 기존 객체 4개를 SSE-KMS로 다시 저장(본문 sha256 · 메타데이터 같음), 게이트 OBJ-ENC가 객체마다 SSE 헤더를 봄(`docs/evidence/gate/obj-enc-reencrypt-20261103/`). 고침(`ac4e1aa`). RGW SSE-KMS, 키는 플랫폼 OpenBao transit `rgw-target-onprem`(내보내기 불가, 봉투 암호화). 토큰은 그 키의 datakey · decrypt만, TTL 1h, 15분마다 갱신, 168h마다 재발급. OBJ-ENC 온프렘 PASS. 남음: 기존 객체 5개는 암호화 전 그대로, 암호화 객체 PUT · GET은 OpenBao 링크가 끊기면 500(측정) |
| G2 | 높음 | DR-RESTORE UNKNOWN(복구 클러스터 없음) | 고침(`8542cda`). 세 환경 CNPG 백업(barmanObjectStore, system 이미지) → `chat/chat-db-restore`(복원 지점까지 복구, 행 수 같음). DR-RESTORE · DR-ARCHIVE 세 환경 PASS. 남음: 정기 백업 없음, CNPG 1.31 전에 Barman Cloud Plugin으로 옮겨야 함 |
| G3 | 높음 | GKE OBJ-06 FAIL `plain_http_accepted` | 2026-11-03 02:5x L3: 대체 통제를 게이트 질문 OBJ-PLAIN-EGRESS로 잼(세 환경 PASS, `docs/evidence/gate/obj-plain-egress-20261103/`). 예외 요청 `compensating_control`에 이 ID를 적음. 측정 후 FAIL 유지. 프로젝트 수준 `storage.secureHttpTransport`는 403(`orgpolicy.policies.create`), 조직 없는 프로젝트라 `orgpolicy.policyAdmin`을 줄 수 없음(`docs/evidence/gate/obj06-gke-orgpolicy-20261102T143014Z/`). 예외 승인(S1 5b) 대상 |
| G4 | 치명 | 플랫폼 게이트에 자격 · 레지스트리 절이 없어 모든 질문 UNKNOWN, `target-gcp` 404 | 고침(`db4c4e3`, `1a84e8c`, `0980ff7`, 게이트 고정 `00e34755`). 환경마다 게이트 자기 읽기 신원, `target-gcp` 등록, 링크 `t1-dst-s3http` · `t1-gke-db` 추가. push 전이라 미리 보기 Pod로 측정(`docs/evidence/gate/platform-gate-20261102T161443Z/`). **push 필요**: 커밋이 main에 가야 Deployment `gate`가 바뀜 |
| G5 | 중간 | 두 쌍이 늘 BLOCK인 UNKNOWN: OBJ-03 · OBJ-04(GET은 쓰기 시험 안 함), 비 AWS 목적지의 AWS 절 질문 3개, NET-PATHS | 고침(`50218ea` … `50399a7`, `badb7e2`, 팀장 결정 A–D). 공급자 중립 ID `SEC-SECRET-STORE-READ` · `SEC-SECRET-AT-REST` · `AUD-CONTROL-PLANE`(규칙 `gate-controls/2026-11-03.1`), POST 실행에서 쓰기 시험, 출발지 OBJ-04는 IAM 시뮬레이션, NET-PATHS는 앱 라벨 시험 Pod, GKE는 링크 `gke-k8s`(수집기도). 미리 보기 실행에서 모든 행이 측정값(`docs/evidence/gate/platform-gate-20261102T165054Z/`). 남음: OpenBao NetworkPolicy가 Argo 동기화 전이라 Deployment에서는 push 뒤 확인 |
| G6 | 중간 | 측정된 FAIL: DB-TLS 세 환경(`host` 규칙이 평문 접속 허용), DB-AUDIT(`log_connections` 꺼짐, 목적지 pgaudit 없음), NET-DEFAULT-DENY 출발지 · 온프렘 | 고침(2026-11-03 02:0x, 이 갈래). `pg_hba` TLS만(`hostnossl … reject`), 링크 이름 SAN, CA 묶음 `chat/bravo-db-ca`. 앱 · PgBouncer 양쪽 · 양방향 구독 · Executor가 verify-full(verify-ca로 낮춘 홉 없음). 세 곳 pgaudit `ddl, role` + `log_connections` · `log_disconnections`. 출발지 · 온프렘 `chat` 기본 차단 + 허용, Hubble로 확인. 게이트 `platform-gate-20261102T170251Z`에서 DB-TLS · DB-AUDIT · NET-DEFAULT-DENY · NET-PATHS · DR-ARCHIVE · DR-RESTORE 두 쌍 양쪽 PASS. 앱 끝점 200, 정방향 streaming, E4 통과, 백업 completed(`docs/evidence/db-tls-audit-netpol/`). 남음: Executor topology(verify-full)는 push 뒤 적용, E5 · E1 · E6은 사람 승인이 있어야 실행 |
| G7 | 높음 | 게이트 신원의 `chat` Pod 만들기가 아무 이미지 · 사양(NET-PATHS 시험 Pod용 RBAC) | 고침(2026-11-03 01:5x). 세 클러스터에 ValidatingAdmissionPolicy `bravo-gate-netprobe`: 게이트 신원만, 앱 이미지 digest만, SA 토큰 없음 · `default` SA, host 접근 · privileged 없음, 기한 ≤ 90초, 시험 라벨. 거부 · 허용 시험과 실제 NET-PATHS: `docs/evidence/gate/netprobe-admission-20261102T165631Z/`. Deployment 기준 두 쌍 실행 UNKNOWN 0: `docs/evidence/gate/platform-gate-20261102T165445Z/`, `…165646Z/` |
| G8 | 높음 | 스펙 §8.5 시험과 게이트 질문이 1:1이 아니고 시험 방법이 보이지 않음(리뷰 B12 · P16) | 2026-11-03 L3. 대응표 `docs/architecture/16-gate-test-map.md`, 행마다 `method` · `src_method` · `dst_method` · 도구 · `spec_test`. 새 질문 SEC-GIT-PLAINTEXT(gitleaks), NET-EXTERNAL-SCAN(바깥 TCP 스캔), OBJ-PLAIN-EGRESS. kube-bench 요약은 보고서 `cis_benchmark`(판정 안 함). 나머지는 DEVIATIONS D-29. 게이트 `c841a75e`, 미리 보기 `platform-gate-20261102T175426Z`. push 필요 |
| G9 | 높음 | 데모 목적지 `bravo-onprem`에서 kube-bench를 돌린 적 없음(리뷰 P17) | 2026-11-03 02:31–02:34 L3. 러너가 SA 토큰을 붙이게 고침(5.1.x 도구 오류 제거). FAIL 4.3.1 해당 없음, 5.1.3 예외, 5.1.5 고침(공통 랜딩 존 `sa_automount` + Kyverno MutatingPolicy), 5.1.6 37 → 26 + 예외. `docs/evidence/kube-bench/onprem-20261103/` |
| G10 | 중간 | GKE 앱만 다른 빌드(`df08e969…`) | 2026-11-03 02:52 L3. 출발지 digest `b5c0e764…`를 skopeo로 Artifact Registry에 복사, GKE 롤아웃, 게이트 VAP 고정 갱신. `up.sh`는 이제 빌드 대신 digest 복사(`images.sh`). `docs/evidence/gke-same-digest-20261103/` |

운영 준비 갈래(2026-11-03 03:0x KST, 리뷰 24 L4). 증거 `docs/evidence/demo-ops/`, 점검표 `docs/demo/ONSITE-CHECKLIST.md`.

| # | 리뷰 | 문제 | 상태 |
|---|---|---|---|
| O1 | B16 | 7일 안에 끝나는 자격 증명, 회전 연습 없음, TTL · 갱신 경보 없음 | 고침(연습까지). `deploy/scripts/rotate-credentials.sh`(plan · run · one), `keys.sh rotate-service-token` · `refresh-service-token` · `custody-renew`. 03:0x 측정: 11/9 06:00 KST 전에 끝나는 것 없음, 가장 이른 것 링크 토큰 8개 11/9 20:50. 경로마다 실제 1회: custody 5개, TokenRequest 4개(소비자 확인 포함), 서비스 토큰 1개(`plat-dst-s3http`, 2회). Prometheus에 TTL 지표가 없어 경보는 preflight `cred` 그룹. 남음: 11/6 전체 회전(점검표 3절), RGW 토큰 회전(무대 밖), Cloudflare API 토큰 11/9 23:59(사람) |
| O2 | B17 | RGW SSE-KMS가 OpenBao 링크 · 토큰에 기댐 | 사전 점검에 넣음: 링크 `plat-bao-link` 실제 왕복, RGW 토큰 갱신(CronJob 마지막 성공 · lease), 최대 수명, 암호화 객체 실제 GET(03:01 PASS 382 ms). 키 복제 없음 |
| O3 | B18 | Mac 한 대 · 플랫폼 노드 하나 · Cloudflare에 장애가 몰림 | 점검표와 사전 점검 `host` 그룹(Lima VM, 절전, 전원, 디스크 · 메모리, TCP 포트). 노트북 loadgen이 포트를 채워 kubectl이 실패한 것을 측정(02:44). 남음: 플랫폼 노드 · Lima 재기동 시간 측정 없음 |
| O4 | B19 | reset의 옛 컨텍스트, 900초 대기, dry-run 기대값, NS 삭제 범위 | 고침. 플랫폼 `bravo-platform-rke2`, executor-0(D-16 링크)만 씀, `--plan` 계획 범위만 삭제, 승인은 세기만 하고 기다리지 않음, dry-run이 실제와 같은 읽기를 하고 바꿀 것만 PLAN, 컷오버 뒤면 멈춤. 실측: 깨끗한 상태 실제 실행 9초, dry-run 22–29초, 범위 시험 통과. 남음: 데모 뒤 상태에서 잰 reset 시간 |
| O5 | B20 · P23 | 증거가 OutOfSync 때 찍힘, 동결 절차 없음 | 고침. `deploy/scripts/demo-freeze.sh`(태그 + 이미지 digest + ConfigMap + Argo revision 기록과 비교), `deploy/scripts/argo-state.sh`(증거마다 Argo 상태), `run-platform-gate.sh`가 `argo-state.txt`를 남김. 남음: `deploy/cloud/*` 실행 스크립트에 같은 한 줄(데이터 · 컷오버 갈래 파일), 진짜 동결 태그(데모 전날) |
| O6 | P9 | 운영자 역할은 첫 로그인 뒤에만, OTP, 세션 24시간 | 사전 점검 `console` 그룹: 플랫폼 DB 신원 · 역할(읽기), 쿠키가 있으면 `/v1/me`. 03:01 측정 naver(운영자) 신원 없음 FAIL. 남음: 사람이 첫 로그인 |
| O7 | P6 | 링크 위 복제 끊김 준비 판정 | 사전 점검 `repl` 그룹: `latest_end_time` 경과(PASS 45초, 2배까지 WARN), 역방향 구독 상태, 슬롯 보존 WAL. 측정: 역방향 슬롯 보존 WAL이 부하 중 약 3.6 MB/분으로 늘어 약 7시간이면 2GB 한도. 컷오버 준비 판정(Executor) 안의 경과 기준은 데이터 · 컷오버 갈래 몫 |
