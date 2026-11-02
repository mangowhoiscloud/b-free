# 플랫폼 게이트 두 쌍, Deployment 기준 (2026-11-03 01:54–01:55 KST)

push(`f147be4`)와 Argo CD 동기화 뒤 Service `gate`로 돌린 결과입니다. 요청은 플랫폼 API가 계획을 평가할 때와 같은 `POST /v1/controls/report`입니다. 이미지 `gate@sha256:ec47f7c3…`, 규칙 `gate-controls/2026-11-03.1`. 비교표 `TABLES.md`, 원본 `report-*.json`, 실행 기록 `run.txt`.

동기화 상태(실행 직전): `control`, `openbao`, `link-d16` 모두 리비전 `f147be4`. OpenBao NetworkPolicy가 게이트를 허용합니다(`["executor","rehearsal","api","gate"]`). `control`의 OutOfSync는 지난 판의 ConfigMap(가지치기 끔)과 마이그레이션 Job뿐입니다.

## 결과

모든 행이 측정값입니다(UNKNOWN 0). 미리 보기에서 UNKNOWN이던 OpenBao 두 행도 이제 Deployment에서 잽니다.

| 통제 | EKS ↔ 온프렘 | EKS ↔ GKE |
|---|---|---|
| SEC-01 | PASS / PASS | PASS / PASS |
| ENC-01 | PASS / PASS | PASS / **FAIL** |
| AUD-01 | PASS / PASS | PASS / PASS |
| NET-01 | **FAIL** / PASS | **FAIL** / PASS |
| IAM-01 | PASS / **FAIL** | PASS / PASS |
| DR-01 | PASS / PASS | PASS / PASS |

OpenBao 두 행(목적지):

| 질문 | 온프렘 | GKE |
|---|---|---|
| SEC-SECRET-STORE-READ | PASS `other_identity_denied`(권한 없는 역할의 값 읽기 403) | PASS `other_identity_denied` |
| SEC-SECRET-AT-REST | PASS(OpenBao 저장 + RKE2 `aescbc`) | PASS(OpenBao 저장 + `databaseEncryption` Cloud KMS) |

남은 FAIL(모두 측정):

| 질문 | 쪽 | 이유 |
|---|---|---|
| OBJ-01, OBJ-05 | 온프렘 | S1 심은 회귀(익명 읽기 Sid). 남겨 둠 |
| OBJ-06 | GKE | GCS 평문 HTTP 수락. 조직 정책 불가(`../obj06-gke-orgpolicy-20261102T143014Z/`). 예외 승인 대상 |
| NET-DEFAULT-DENY | 출발지 | ns `chat`에 기본 차단 없음(온프렘은 이 실행에서 PASS, 다른 갈래가 고침) |
| NET-PATHS | 출발지 | 앱 라벨 시험 Pod에서 `169.254.169.254:80`, `1.1.1.1:80` 연결됨(egress 정책 없음) |

실행 시간: 온프렘 쌍 10초, GKE 쌍 12초.
