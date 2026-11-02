# 플랫폼 게이트 두 쌍, 규칙 `gate-controls/2026-11-03.2` (2026-11-03 02:54–02:55 KST)

새 게이트(`gate@sha256:c841a75e…`)의 첫 측정입니다. 질문 22개, 행마다 시험 방법(`method`), 보고서에 kube-bench 요약(`cis_benchmark`, 판정 안 함). 비교표 `TABLES.md`, 원본 `report-*.json`, 실행 기록 `run.txt`.

## 어디서 돌렸나 (숨기지 않음)

- 새 이미지 고정과 매니페스트(게이트 초기화 컨테이너 `checkout`, ConfigMap `gate-cis-evidence`, 레지스트리 새 키)는 커밋했지만 push 전입니다. 그래서 Deployment `gate`(아직 `ec47f7c3`) 대신, 커밋한 매니페스트를 `kubectl kustomize`로 렌더한 Pod 사양 그대로의 미리 보기 Pod `bravo-control/gate-preview`에서 `run-platform-gate.sh pod/gate-preview`(POST, 쓰기 시험 포함)로 돌렸습니다. 다른 점은 포트 이름을 빼고 준비 상태를 늘 실패로 둔 것뿐입니다(Service가 이 Pod로 보내지 않음). 새 ConfigMap 두 개(`environments-h4hmg846kd`, `gate-cis-evidence-67b5tdk75b`)는 실행 동안만 만들고 Pod와 함께 지웠습니다.
- 같은 방식의 앞선 실행 `../platform-gate-20261102T174659Z/`(02:47–02:48)는 GKE 앱이 아직 `df08e969…`일 때입니다. 판정은 같습니다.
- push · Argo 동기화 뒤 `run-platform-gate.sh`(기본 `svc/gate`)로 다시 만듭니다.

## 결과

| 통제 | EKS ↔ 온프렘 | EKS ↔ GKE |
|---|---|---|
| SEC-01 | PASS | PASS |
| ENC-01 | PASS | **BLOCK**(OBJ-06 GKE FAIL, 예외 대상) |
| AUD-01 | PASS | PASS |
| NET-01 | PASS | PASS |
| IAM-01 | **BLOCK**(OBJ-01 · OBJ-05 온프렘, S1 심은 회귀) | PASS |
| DR-01 | PASS | PASS |

UNKNOWN 0. FAIL은 의도한 셋뿐입니다.

## 새 질문과 바뀐 증거

| 질문 | 출발지 EKS | 온프렘 | GKE |
|---|---|---|---|
| SEC-GIT-PLAINTEXT | PASS. 커밋 `240856fb`, 51파일, gitleaks v8.30.1 발견 0 | 같음 | 같음 |
| OBJ-PLAIN-EGRESS | PASS. S3 443 연결, 80 시간 초과, 엔드포인트 미설정(SDK 기본 https) | PASS. RGW 443 연결, 80 시간 초과 | PASS. GCS 443 연결, 80 시간 초과, `https://storage.googleapis.com` |
| NET-EXTERNAL-SCAN | PASS. `b-free-chat-src` 80 · 443 · 8080 · 8443(Cloudflare), EKS API 엔드포인트 열린 포트 0(운영자 /32만 허용) | PASS. `b-free-chat-dst`만 | PASS. `b-free-chat-gke`, GKE DNS 엔드포인트 80 · 443, 꺼진 공개 IP 엔드포인트 열린 포트 0 |
| OBJ-ENC 객체 수준 | `not_checked_no_list_right` | 4/4 SSE-KMS | 1/1 CMEK |

방법 표시: OBJ-04는 `mixed`(EKS `simulation`, 목적지 `behavioural`), SEC-SECRET-STORE-READ도 `mixed`, SEC-SECRET-READ-K8S · DB-ANON-READ는 `simulation`. 대응표는 `docs/architecture/16-gate-test-map.md`.

`cis_benchmark`: 온프렘 `rke2-cis-1.9` FAIL 3종 모두 판단 있음(4.3.1 해당 없음, 5.1.3 예외, 5.1.6 일부 고침 + 예외), EKS `eks-1.8.0` FAIL 12 · GKE `gke-1.9.0` FAIL 10은 미판정(`unjudged`). 출처 `docs/evidence/kube-bench/`.

GKE OBJ-06 목적지 증거 digest(예외 결속용): `sha256:15cd6d0aa80287f1f7dadce15d1cc91d38fb9d4ea146d4fabd46adcdba00fc62`.
