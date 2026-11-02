# 증거

측정과 시험 결과를 남깁니다. 모든 기록에 실행 ID, 시각, 커밋 또는 이미지 digest, 환경을 붙입니다. 데모에서 "미리 실행한 것"은 여기 있는 실행 ID와 시각을 보입니다(스펙 §10).

기준 시각: 2026-11-03 04:10 KST(문서 갈래). 아래 "환경 · 경로" 칸의 이름은 `deploy/CLOUD-E2E-CONTRACT.md` 0절의 현재 환경표를 따릅니다.

## 읽는 법

- **클라우드 실측**: 출발지 EKS `bravo-source`, 온프렘 RKE2 `bravo-onprem`, GKE `bravo-target-gcp`, 플랫폼 RKE2 `bravo-platform-rke2`에서 잰 것.
- **도구 경로 · 하네스 실행**: 스크립트나 Pod 안 코드가 Executor · 게이트 · 리허설 API를 직접 부른 실행. 콘솔 · 사람 승인 · 상태 머신을 거치지 않았습니다.
- **미리 보기 Pod**: push 전 이미지를 배포와 같은 사양의 Pod로 띄워 잰 것. Service는 그 Pod로 보내지 않습니다.
- **노트북 실행**: 운영자 노트북의 CLI 러너, Docker, 버리는 Lima VM에서 잰 것.
- **파일럿(노트북) 기록**: 내린 Compose 파일럿. 클라우드 결과가 아닙니다.
- `NOT_STARTED`, `NOT_RUN`, 빈 요약, `skipped`는 성공으로 세지 않습니다.

## 폴더

### 장면별 실측

| 폴더 | 내용 | 환경 · 경로 | 시각(KST) |
|---|---|---|---|
| [gate/](gate/) | 플랫폼 게이트 실행 표(`platform-gate-*/TABLES.md`, `run.txt`). 최신 규칙 `gate-controls/2026-11-03.2`(질문 22개, 행마다 시험 방법)의 첫 실행 `platform-gate-20261102T175426Z`(한 쌍 41초). OBJ-ENC 온프렘(RGW SSE-KMS), OBJ-06 GKE 조직 정책 측정, DR 복구 세 환경, NET-PATHS 시험 Pod 제한(VAP) | 플랫폼 게이트. 일부는 미리 보기 Pod | 11/2 23:30–11/3 02:55 |
| [gate/obj-plain-egress-20261103/](gate/obj-plain-egress-20261103/README.md) | GKE OBJ-06 예외의 대체 통제를 재는 질문 `OBJ-PLAIN-EGRESS`. 세 환경 PASS(저장소 80 차단, 앱 엔드포인트 https). 온프렘 앱 egress를 443만 남김 | 게이트 미리 보기 Pod | 11/3 02:45–02:55 |
| [gate/obj-enc-reencrypt-20261103/](gate/obj-enc-reencrypt-20261103/README.md) | 온프렘 첨부 버킷의 기존 평문 객체 4개를 SSE-KMS로 다시 저장(리뷰 B13) | 온프렘 RGW | 11/3 02:35–02:36 |
| [app-0.3.3/](app-0.3.3/README.md) | 데모 앱 0.3.3(`76e8f595…`)을 세 클러스터에 같은 digest로. 그 뒤 게이트 두 쌍 실행(한 쌍 39 · 40초, FAIL은 온프렘 OBJ-01 · OBJ-05, GKE OBJ-06뿐, UNKNOWN 0) | 세 고객 클러스터, 배포된 게이트 | 11/3 03:40–03:46 |
| [gke-same-digest-20261103/](gke-same-digest-20261103/README.md) | GKE 앱을 출발지와 같은 digest로 바꿈(그때 `b5c0e764…`, 지금은 0.3.3) | GKE | 11/3 02:51–02:55 |
| [s2-full/](s2-full/README.md) | S2 전체 사슬을 사람 승인 직전까지: governance PASS 227초, app_migration PASS 315초(단계 시간), 검증 Job이 게이트 OBJ-01–06 · ESO 앱 검증, probe 표시(`origin=probe`), Hubble POLICY_DENIED, T31 정책 단계(시험 버킷), RGW `BoolIfExists` 측정 | API Pod 안에서 API 코드로 부름 + 리허설 미리 보기 Pod. 콘솔 · 승인 · 반영 없음 | 11/3 02:36–04:00 |
| [shift/](shift/) | SHIFT 10/50/100 실측: 편도 30.4 · 37.2초(목적지가 제때 답할 때), 실제 쓰기 판정은 아직 통과 못 함, 목적지 쓰기 경로 지연 원인(링크 한 왕복 290–340 ms), PgBouncer 대상이 재시작에도 남음(ConfigMap). `lb-deployed-p95-1500-*`: D-30 기준(1,500 ms)의 배포된 Executor로도 10%에서 0% 복귀(헤더 없는 오류 5건, 출발지 Gateway 503). 재측정 중 | executor-0 도구 경로(계획 · 승인 없음) | 11/3 02:51–04:07 |
| [load/](load/README.md) | 부하 측정. 출발지 50 쓰기 + 150 읽기/초 300초(HTTP 읽기, 노드 2): 쓰기 p95 138.1 ms, 읽기 p95 123.2 ms, NFR-2 OK. WebSocket 읽기는 아직 포화. 노드 1대 · Mac 실행은 대체됨 | 플랫폼 클러스터 Job(일부 Mac) | 11/3 02:36–04:01 |
| [cloud-runs/](cloud-runs/README.md) | 11/2 클라우드 E2E 스크립트 기록 전부(성공 · 실패): LB 전환 4회(60초 보유, 1회 거짓 DONE), 자동 0% 복귀 2회, T8 첨부 복사 1회, 컷오버 dry run 2회(`NOT_STARTED` · 불완전), E4, D-16 복제, K3s 시절 역사. 플랫폼 DB 집계 | 하네스 실행 | 11/2 20:49–23:02, 집계 11/3 02:32 |
| [exit-drill/](exit-drill/README.md) | Exit Drill 스크립트를 D-16 링크 기준으로 다시 씀. 실행은 `NOT_RUN`(컷오버 전), 잔존 의존성 지도만 | executor-0, 세 클러스터 읽기 | 11/3 03:09 |
| [s1-s2-bucket/](s1-s2-bucket/README.md) | 11/2 S1 열린 첨부 버킷 → OBJ-01 BLOCK, S2 Trial `t-3ac2e611…` PASS. 그 제안서의 `Bool` 정책은 지금 검증기로는 OBJ-06 FAIL(`s2-full`) | 노트북 게이트 CLI · 리허설 CLI 러너 | 11/2 23:33–23:45 |
| [s1-cis-refusal/](s1-cis-refusal/README.md) | S1 예외 4a(CIS 시작 거부 → 공통 cloud-init 수정 → 재설치, 거부 5초 fail-fast)와 S1 인수 조건 | 버리는 Lima VM(노트북 실행) | 11/2 23:37–23:58, 11/3 00:07 |
| [t31/](t31/t31-20261102T120416Z/) | T31 반영 CLI 1회(배포 digest = 준비 digest, 불일치 거부 1회). `approval_id: null`, 정책 `SKIPPED`. 해석은 `docs/spec/DEVIATIONS.md` D-28. 정책 단계 추가 뒤 기록은 `s2-full/15-*` · `20-*` | 노트북 실행, 그때 온프렘 K3s | 11/2 21:04–21:10 |
| [control-exception/](control-exception/README.md) | S1 5b 통제 예외(마이그레이션 0008) 시험. 운영 예외 승인은 0건 | 단위 시험, 플랫폼 DB 읽기 | 11/2 23:3x–23:4x |
| [pilot-notebook/](pilot-notebook/README.md) | Compose 파일럿 스모크 11회 · 계약 시험 5회와 실행별 PASS · FAIL(`RUNS.tsv`) | **파일럿(노트북) 기록.** 클라우드 결과 아님 | 11/2 18:40–20:30 |

### 환경 · 보안 · 운영

| 폴더 | 내용 | 환경 · 경로 | 시각(KST) |
|---|---|---|---|
| [kube-bench/](kube-bench/) | `onprem-20261103/`: 데모 목적지 kube-bench(`rke2-cis-1.9`) 서버 PASS 78 · FAIL 3(고친 뒤), 에이전트 FAIL 1, FAIL마다 판단. `source-20261103/` · `gke-20261103/`: 실행 결과만(FAIL 미판정, D-29). `platform-rke2-20261102T124326Z.json`: 플랫폼 1회 | 세 고객 클러스터, 플랫폼 | 11/2 21:43, 11/3 02:31–02:43 |
| [demo-ops/](demo-ops/README.md) | preflight 2회(13초, 7초), 자격 회전 연습, reset `--plan` 시간과 범위 시험 | 운영 Mac, 네 클러스터 | 11/3 02:38–03:02 |
| [edge-https-redirect-20261103/](edge-https-redirect-20261103/README.md) | 공개 이름의 평문 HTTP를 원점 Gateway에서 https 301로(`CF-Visitor` 일치) | 세 고객 클러스터, 플랫폼 | 11/3 03:0x |
| [edge-lb-host-20261103/](edge-lb-host-20261103/README.md) | LB pool이 Host를 덮어쓰지 않게(평문 요청이 `b-free-chat`으로 돌아옴) | Cloudflare | 11/3 03:56 |
| [db-tls-audit-netpol/](db-tls-audit-netpol/README.md) | DB-TLS · DB-AUDIT · NET-DEFAULT-DENY 보완, 홉별 verify-full, E4 재실행 | 세 고객 클러스터와 executor-0 | 11/3 01:23–02:05 |
| [repl-timeouts/](repl-timeouts/README.md) | 논리 복제 타임아웃 5초 → 60초 | 세 chat-db, D-16 링크 | 11/3 02:08 |
| [d16-source-k8s/](d16-source-k8s/README.md) | 플랫폼이 출발지 EKS API를 D-16 링크로 읽음, NAT IP를 허용 목록에서 뺌 | 플랫폼 수집기 · Executor | 11/2 23:38–23:45 |
| [traefik-crd-skip/](traefik-crd-skip/README.md) | `rke2-traefik-crd` 차트 멈춤 전후 비교 | `bravo-onprem`, `bravo-platform-rke2` | 11/3 00:07–00:10 |

저장소의 다른 증거 폴더: `platform/rehearsal/docs/evidence/`(리허설 Trial 기록 `rehearsals.tsv`).

## 아직 없는 것

스펙이 요구하지만 기록이 없는 것입니다(2026-11-03 04:07 KST). 플랫폼 DB에 계획 · 승인 · 영수증이 0건이고, 운영자(naver) 신원은 첫 로그인 전입니다.

| 기록 | 작업 | 상태 |
|---|---|---|
| 콘솔에서 사람이 시작한 실행, change · cutover · failback · 예외 승인 | T10, T21 | 0건 |
| 실제 쓰기 판정으로 통과한 SHIFT | T17 | 재측정 중(D-30) |
| 클라우드 컷오버 · 되돌리기(쓰기 정지 ms, 영수증, S4-AC1) | T12, T20, T25 | 0회 |
| 부하 컷오버 5회 | T18, NFR-1 | 0회. 부하 자체는 측정함(`load/`) |
| 콘솔 경로 S2(역할별 동시 시작, 승인 뒤 T31 반영 시간) | T15, T31 | 측정 중 |
| Exit Drill 실제 실행 | T27 | `NOT_RUN` |
| 270초 · 5분 통합 리허설 3회 연속 | T28 | 0회 |

처음 계획했던 묶음 파일(`cutover.tsv`, `controls.tsv`, `rehearsals.tsv`, `isolation.tsv`)은 만들지 않았습니다. 실행마다 폴더를 두고 이 표에서 색인합니다.
