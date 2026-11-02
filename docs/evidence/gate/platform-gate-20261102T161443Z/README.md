# 플랫폼 게이트 두 쌍 실행 (2026-11-03 01:14–01:15 KST)

출발지 EKS ↔ 온프렘 RKE2, 출발지 EKS ↔ GKE를 플랫폼 게이트로 돌린 결과입니다. 요청은 플랫폼 API와 같은 `GET /v1/controls/report?source=…&target=…`입니다. 통제 ID별 비교표는 `TABLES.md`, 원본은 `report-*.json` · `controls-*.json`, 실행 기록은 `run.txt`입니다.

## 어디서 돌렸나 (숨기지 않음)

- 이 실행은 Deployment `gate`가 아니라 같은 네임스페이스의 미리 보기 Pod `bravo-control/gate-preview`에서 돌렸습니다. 새 설정을 담은 커밋 `0980ff7`이 아직 push되지 않아 Argo CD(`control`, 리비전 `5383381`)가 옛 Deployment를 유지했기 때문입니다.
- 미리 보기 Pod는 커밋된 매니페스트를 `kubectl kustomize`로 렌더한 Deployment `gate`의 Pod 사양 그대로입니다. 이미지(`gate@sha256:00e34755…`, 새 고정값), 서비스 계정 `gate`, 레지스트리, 자격 Secret · ConfigMap, 사이드카, 링크 정책이 같습니다. 다른 점은 둘입니다. 이름 붙은 포트를 빼고 준비 상태를 늘 실패로 두어 Service `gate`가 이 Pod로 요청을 보내지 않게 했습니다.
- push 뒤에는 `docs/evidence/gate/run-platform-gate.sh`(기본 `svc/gate`)로 같은 표를 다시 만듭니다.

## 결과 요약

두 쌍 모두 게이트 판정은 BLOCK입니다. DR-01은 두 쌍 모두 PASS입니다.

| 통제 | EKS ↔ 온프렘 | EKS ↔ GKE |
|---|---|---|
| SEC-01 | 출발지 PASS / 목적지 UNKNOWN | 출발지 PASS / 목적지 UNKNOWN |
| ENC-01 | FAIL / FAIL | FAIL / FAIL |
| AUD-01 | FAIL / FAIL | FAIL / FAIL |
| NET-01 | FAIL / FAIL | FAIL / UNKNOWN |
| IAM-01 | UNKNOWN / FAIL | UNKNOWN / UNKNOWN |
| DR-01 | PASS / PASS | PASS / PASS |

이번 일의 세 질문:

| 질문 | 온프렘 | GKE | 증거 |
|---|---|---|---|
| OBJ-ENC | PASS `customer_managed_key`(RGW SSE-KMS, 키 `rgw-target-onprem`은 플랫폼 OpenBao transit) | PASS `customer_managed_key`(GCS 기본 Cloud KMS 키, JSON API로 읽음) | `../obj-enc-onprem-20261102T152228Z/` |
| DR-RESTORE | PASS `restore_healthy` | PASS `restore_healthy` | `../dr-restore-*-20261102T14*/` |
| OBJ-06 | PASS `plain_http_denied_tls_only`(RGW 평문 수신에서 403) | FAIL `plain_http_accepted`(http GET 200) | `../obj06-gke-orgpolicy-20261102T143014Z/` |

출발지 쪽 세 질문도 PASS입니다(OBJ-ENC 고객 관리 KMS 키, DR-RESTORE healthy, OBJ-06 403).

## UNKNOWN과 FAIL의 이유

측정으로 나온 FAIL(고칠 일):

- OBJ-01 · OBJ-05 온프렘 FAIL: S1 데모 회귀(익명 읽기 Sid)를 일부러 심어 두었습니다. 심은 상태를 유지합니다. 콘솔의 출발지 ↔ 온프렘 실행은 OBJ-01 목적지 FAIL → BLOCK이어야 합니다.
- OBJ-06 GKE FAIL: GCS는 평문 HTTP를 받습니다. 조직 정책은 조직 없는 프로젝트에 걸 수 없습니다(측정). 예외 승인(S1 5b) 대상입니다.
- DB-TLS 세 환경 FAIL `plaintext_accepted`: chat-db `pg_hba`가 `host all all all scram-sha-256`이라 `sslmode=disable` 접속을 받습니다.
- DB-AUDIT 세 환경 FAIL: `log_connections` 꺼짐, 목적지는 pgaudit 미적재.
- NET-DEFAULT-DENY 출발지 · 온프렘 FAIL: ns `chat`에 기본 차단 NetworkPolicy가 없습니다. GKE는 있습니다(PASS).

측정할 수 없어 UNKNOWN(이유 고정):

- OBJ-03 · OBJ-04: HTTP GET은 쓰기 시험을 하지 않습니다(감사 48, `write_probe_disabled`). 출발지 OBJ-04는 앱 자격(IRSA 역할)이 게이트에 없습니다(`app_credential_missing`).
- SEC-SECRET-READ-AWS, SEC-ENC-CMK, AUD-PLATFORM의 목적지: 이 세 질문은 AWS 절 질문입니다. 온프렘 · GKE에는 같은 질문이 없어 `not_configured`입니다.
- NET-PATHS: 대조 · 거부 경로를 정하지 않았습니다(`not_configured`).

이 UNKNOWN이 있는 한 두 쌍은 PASS가 될 수 없습니다. 바꾸려면 게이트 질문 설계를 정해야 합니다(쓰기 시험을 승인된 POST로 돌릴지, 비 AWS 목적지의 대응 질문을 둘지). 팀장 결정 사항입니다.

## 재현

```
deploy/platform/scripts/gate-credentials.sh          # 게이트 자기 신원(RBAC, 토큰, DB 역할, CA, Secret)
docs/evidence/gate/run-platform-gate.sh              # 기본 svc/gate. 이번 실행은 pod/gate-preview
```

원본 JSON에 비밀은 없습니다(오류 문자열은 게이트가 가림, 감사 38). `password`라는 글자는 질문 문장과 판정 이유 코드에만 있습니다.
