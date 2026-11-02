# 증거

측정과 시험 결과를 남깁니다. 모든 기록에 실행 ID, 시각, 커밋 또는 이미지 digest, 환경을 붙입니다. 데모에서 "미리 실행한 것"은 여기 있는 실행 ID와 시각을 보입니다(스펙 §10).

기준 시각: 2026-11-03 02:40 KST(문서 갈래). 아래 "환경" 칸의 이름은 `deploy/CLOUD-E2E-CONTRACT.md` 0절의 현재 환경표를 따릅니다.

## 읽는 법

- **클라우드 실측**: 출발지 EKS `bravo-source`, 온프렘 RKE2 `bravo-onprem`, GKE `bravo-target-gcp`, 플랫폼 RKE2 `bravo-platform-rke2`에서 잰 것.
- **하네스 실행**: 스크립트가 Executor · 게이트 API를 직접 부른 실행. 콘솔 · 사람 승인 · 상태 머신을 거치지 않았습니다.
- **노트북 실행**: 운영자 노트북의 CLI 러너, Docker, 버리는 Lima VM에서 잰 것. 플랫폼 서비스 경로가 아닙니다.
- **파일럿(노트북) 기록**: 내린 Compose 파일럿. 클라우드 결과가 아닙니다.
- `NOT_STARTED`, 빈 요약, `skipped`는 성공으로 세지 않습니다.

## 폴더

| 폴더 | 내용 | 환경 · 경로 | 시각(KST) |
|---|---|---|---|
| [cloud-runs/](cloud-runs/README.md) | 클라우드 E2E 스크립트 기록 전부(성공 · 실패). LB 10/50/100 전환 4회(편도 약 185초, 1회는 거짓 DONE 결함), 자동 0% 복귀 2회, T8 첨부 복사 1회, 컷오버 dry run 2회(`NOT_STARTED` · 불완전, 성공 아님), E4, D-16 복제. 플랫폼 DB 집계(계획 · 승인 · 영수증 0) | 클라우드 실측, 하네스 실행. 일부는 K3s 시절 역사 기록, PAUSE 실험은 노트북 Docker | 11/2 20:49–23:02, 집계 11/3 02:32 |
| [gate/](gate/) | 플랫폼 게이트 실행 표(`platform-gate-*/TABLES.md`, `run.txt`), OBJ-ENC 온프렘(RGW SSE-KMS), OBJ-06 GKE 조직 정책 측정, DR 복구 세 환경, NET-PATHS 시험 Pod 제한(VAP). 최신 실행 `platform-gate-20261102T170251Z`: 두 쌍 모두 BLOCK(온프렘 OBJ-01 · OBJ-05 심은 시나리오, GKE OBJ-06) | 플랫폼 게이트(`svc/gate`, `bravo-platform-rke2`). 일부는 push 전 미리 보기 Pod | 11/2 23:30–11/3 02:03 |
| [db-tls-audit-netpol/](db-tls-audit-netpol/README.md) | DB-TLS · DB-AUDIT · NET-DEFAULT-DENY 보완. 홉별 verify-full, pgaudit, 기본 차단, E4 재실행 | 세 고객 클러스터와 executor-0 | 11/3 01:23–02:05 |
| [repl-timeouts/](repl-timeouts/README.md) | 논리 복제 `wal_sender_timeout` · `wal_receiver_timeout` 5초 → 60초. 바꾸기 전 끊김과 바꾼 뒤 관찰 | 세 chat-db, D-16 링크 | 11/3 02:08 |
| [control-exception/](control-exception/README.md) | S1 5b 통제 예외(마이그레이션 0008) 시험과 운영 DB 사전 시험. 운영 예외 승인은 0건(`cloud-runs` 집계) | 단위 시험, 플랫폼 DB 읽기 | 11/2 23:3x–23:4x |
| [d16-source-k8s/](d16-source-k8s/README.md) | 플랫폼이 출발지 EKS API를 D-16 링크 `src-k8s`로 읽음. NAT IP를 허용 목록에서 뺌 | 플랫폼 수집기 · Executor | 11/2 23:38–23:45 |
| [s1-s2-bucket/](s1-s2-bucket/README.md) | S1 열린 첨부 버킷(심은 시나리오) → 게이트 OBJ-01 BLOCK, S2 Trial `t-3ac2e611…` PASS(행동 검증 Job). 사람 승인 · 목적지 반영은 안 함. 버킷 삭제 거부 2건은 하네스가 넣은 probe(`origin=probe`)이고 에이전트 행동이 아님 | 온프렘 RKE2. 게이트 CLI와 리허설 CLI 러너는 **노트북 실행** | 11/2 23:33–23:45 |
| [s1-cis-refusal/](s1-cis-refusal/README.md) | S1 예외 4a(RKE2 `profile: cis` 시작 거부 → 공통 cloud-init 수정 → 재설치)와 S1 인수 조건(빈 VM에서 RKE2 CIS, 노드 Ready). kube-bench `rke2-cis-1.9` PASS 76 · FAIL 5 | 버리는 Lima VM(노트북 실행). 실행 뒤 삭제 | 11/2 23:37–23:58, 11/3 00:07 |
| [traefik-crd-skip/](traefik-crd-skip/README.md) | `rke2-traefik-crd` 차트 멈춤 전후의 Gateway API 자원 비교 | `bravo-onprem`, `bravo-platform-rke2` | 11/3 00:07–00:10 |
| [kube-bench/](kube-bench/) | kube-bench 결과 JSON. 커밋된 것은 플랫폼 RKE2 1회(`platform-rke2-20261102T124326Z.json`, FAIL 5) | 플랫폼 RKE2 | 11/2 21:43 |
| [t31/](t31/t31-20261102T120416Z/) | T31 반영 경로 CLI 1회: 빌드 → SBOM · trivy → OpenBao transit cosign 서명 → 검증 → containerd import → 배포(배포 digest = 준비 digest `a9f7b866`). 승인 digest 불일치 거부 1회. `approval_id: null`(CLI, 콘솔 승인 아님), 정책 적용 `SKIPPED`. 해석은 `docs/spec/DEVIATIONS.md` D-28 | 노트북 실행, 그때 온프렘 K3s | 11/2 21:04–21:10 |
| [pilot-notebook/](pilot-notebook/README.md) | Compose 파일럿 스모크 11회 · 계약 시험 5회 원본과 실행별 PASS · FAIL(`RUNS.tsv`) | **파일럿(노트북) 기록.** 클라우드 결과 아님 | 11/2 18:40–20:30 |

저장소의 다른 증거 폴더: `platform/rehearsal/docs/evidence/`(리허설 Trial 기록 `rehearsals.tsv`, K3s · RKE2 시절 Trial README).

## 아직 없는 것

스펙이 요구하지만 기록이 없는 것입니다(2026-11-03 02:40 KST). 근거는 `cloud-runs/README.md`의 플랫폼 DB 집계입니다.

| 기록 | 작업 | 상태 |
|---|---|---|
| 클라우드 컷오버 · 되돌리기(쓰기 정지 ms, 영수증, S4-AC1) | T12, T20, T25 | 0회 |
| 부하 컷오버 5회(쓰기 50 req/s, 읽기 150 req/s) | T18, NFR-1 | 0회 |
| 콘솔에서 사람이 시작한 실행, change · cutover · failback 승인 | T10, T21 | 0건 |
| 플랫폼 리허설 서비스 경로의 Trial | T15 | 0회(`rehearsals` 0행) |
| Exit Drill | T27 | 0회 |
| 270초 통합 리허설 3회 연속 | T28 | 0회 |

처음 계획했던 묶음 파일(`cutover.tsv`, `controls.tsv`, `rehearsals.tsv`, `isolation.tsv`)은 만들지 않았습니다. 실행마다 폴더를 두고 이 표에서 색인합니다.
