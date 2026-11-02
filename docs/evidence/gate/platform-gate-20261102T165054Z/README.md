# 플랫폼 게이트 두 쌍, 공급자 중립 질문 (2026-11-03 01:50–01:51 KST)

규칙 `gate-controls/2026-11-03.1`로 출발지 EKS ↔ 온프렘 RKE2, 출발지 EKS ↔ GKE를 돌린 결과입니다. 요청은 플랫폼 API가 계획을 평가할 때와 같은 `POST /v1/controls/report`입니다. 그래서 쓰기 시험(OBJ-03 · OBJ-04)도 돌았습니다. 비교표는 `TABLES.md`, 원본은 `report-*.json`, 실행 기록은 `run.txt`입니다.

## 어디서 돌렸나 (숨기지 않음)

- 새 게이트(`gate@sha256:ec47f7c3…`, 커밋 `50399a7`)와 새 레지스트리 · 매니페스트(`badb7e2`)가 아직 push 전입니다. 그래서 Deployment `gate` 대신 같은 Pod 사양의 미리 보기 Pod `bravo-control/gate-preview`에서 돌렸습니다. 준비 상태를 늘 실패로 두어 Service `gate`가 이 Pod로 요청을 보내지 않습니다.
- 미리 보기에서 다른 점 하나가 결과에 나옵니다. 플랫폼 OpenBao의 NetworkPolicy는 Argo CD가 관리하고, 게이트를 허용하는 변경(`deploy/platform/openbao/openbao.yaml`)은 동기화 전입니다. 그래서 온프렘 · GKE의 SEC-SECRET-STORE-READ와 SEC-SECRET-AT-REST가 UNKNOWN(`openbao_login_failed`, `openbao_unreachable`)입니다.
- 이 두 질문의 OpenBao 부분은 같은 게이트 신원(SA `bravo-control/gate` 토큰, 역할 `gate-meta` · `gate-other`)과 같은 코드로 운영자 port-forward를 거쳐 따로 쟀습니다: `openbao-gate-identity.json`. 두 환경 모두 PASS입니다(`other_identity_denied`: 값 읽기 403 4건, `secrets_in_openbao`: Shamir, 봉인 해제, 경로 있음). 클러스터 부분은 미리 보기 실행에서 PASS입니다(온프렘 `aescbc` + `--encryption-provider-config`, GKE `databaseEncryption` ENCRYPTED, Cloud KMS 키).
- push 뒤 `docs/evidence/gate/run-platform-gate.sh`(기본 `svc/gate`, POST)로 다시 만듭니다.

## 결과

| 통제 | EKS ↔ 온프렘 | EKS ↔ GKE |
|---|---|---|
| SEC-01 | PASS / UNKNOWN(OpenBao 정책, 동기화 전) | PASS / UNKNOWN(같은 이유) |
| ENC-01 | PASS / PASS | PASS / **FAIL**(OBJ-06 GKE) |
| AUD-01 | PASS / PASS | PASS / PASS |
| NET-01 | **FAIL** / **FAIL** | **FAIL** / PASS |
| IAM-01 | PASS / **FAIL**(심은 회귀) | PASS / PASS |
| DR-01 | PASS / PASS | PASS / PASS |

FAIL만 모으면 이렇습니다. 모두 측정입니다.

| 질문 | 쪽 | 이유 | 누구 몫 |
|---|---|---|---|
| OBJ-01, OBJ-05 | 온프렘 | S1 심은 회귀(익명 읽기 Sid). 남겨 둠 | 데모 |
| OBJ-06 | GKE | GCS가 평문 HTTP를 받음. 조직 없는 프로젝트라 조직 정책 불가(`../obj06-gke-orgpolicy-20261102T143014Z/`) | 예외 승인(S1 5b) |
| NET-DEFAULT-DENY | 출발지, 온프렘 | ns `chat`에 기본 차단 없음 | 다른 갈래(환경 수정 중) |
| NET-PATHS | 출발지 | 앱 Pod 라벨의 시험 Pod에서 `169.254.169.254:80`, `1.1.1.1:80`이 연결됨(출발지 `chat`에 egress 정책 없음) | 같은 갈래. 기본 차단 + egress 규칙이 들어가면 다시 잼 |

DB-TLS · DB-AUDIT는 이 실행에서 세 환경 모두 PASS입니다(다른 갈래가 고침).

## 새로 잰 방법 (요약)

- **SEC-SECRET-STORE-READ**: EKS는 다른 IAM 주체의 `secretsmanager:GetSecretValue` 시뮬레이션. 온프렘 · GKE는 플랫폼 OpenBao에서 게이트의 권한 없는 역할이 앱 비밀 값 읽기에 403.
- **SEC-SECRET-AT-REST**: EKS는 Secrets Manager CMK + EKS `encryptionConfig` CMK. 온프렘은 OpenBao 저장 + kube-apiserver Secret 변환자 `aescbc`(RKE2 secrets-encrypt, 서버 노드의 로컬 키). GKE는 OpenBao 저장 + `databaseEncryption`(Cloud KMS).
- **AUD-CONTROL-PLANE**: EKS는 CloudTrail + EKS `audit` 로그. 온프렘은 `--audit-policy-file` · `--audit-log-path`와 시험 중 `apiserver_audit_event_total` 증가. GKE는 Cloud Audit Logs 관리 활동 최근 1시간 항목.
- **OBJ-03**: 출발지 S3는 서명 없는 PUT을 SSE-KMS 기본값 때문에 `400 InvalidArgument`로 먼저 막습니다. `x-amz-server-side-encryption: AES256`을 붙여 다시 보내면 `403 AccessDenied`입니다(PASS). 온프렘 · GKE는 첫 요청이 403입니다.
- **OBJ-04**: 출발지는 앱 역할의 `iam:SimulatePrincipalPolicy`(canary GET, 시험 키 PUT, 버킷 KMS 키 GenerateDataKey · Decrypt, 버킷 정책 · 키 정책 포함) 모두 allowed. 온프렘 · GKE는 앱 키로 실제 PUT 200, 삭제 `deleted`.
- **NET-PATHS**: 앱 Deployment 라벨 · 이미지의 짧은 시험 Pod. 온프렘 · GKE는 대조 연결, 거부 두 곳 시간 초과(PASS). 출발지는 거부 두 곳이 연결됨(FAIL). Pod는 실행마다 지웠습니다(증거 `cleanup: deleted`).

실행 시간: 쌍마다 약 16초(환경 안 절을 병렬로 돌림. 순차일 때 57–62초였음).
