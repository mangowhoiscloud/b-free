# S1 → S2 첨부 버킷 장면 실측 (2026-11-02 23:33–23:45 KST)

스펙 S1 "목적지 첨부 저장소가 인증 없이 열려 있어 BLOCK"과 S2 "에이전트가 버킷 정책을 고치고, 플랫폼이 별도 검증 Job으로 비공개 시험을 돈다"를 온프렘 RKE2(`bravo-onprem`) + Rook-Ceph RGW에서 한 번씩 실제로 돌린 기록입니다. 사람 승인 단계(`change`)와 실제 목적지 반영은 하지 않았습니다. **끝난 뒤 심은 시나리오는 그대로 둡니다.** 라이브 데모는 BLOCK 상태에서 시작합니다.

## 무엇을 했나

| 순서 | 시각(KST) | 명령 | 결과 | 파일 |
|---|---|---|---|---|
| 1 | 23:33:26 | `python3 deploy/cloud/rgw_regression.py --context bravo-onprem --gate-report …` (심기 전, 바꾸는 것 없음) | 목적지 OBJ-01 PASS: 서명 없는 GET `403 AccessDenied`. 정책 Sid `DenyInsecureTransport`만, 공개 차단 넷 모두 켬 | `01-before-plant-*.log`, `gate-before-plant-*.json` |
| 2 | 23:33:38 | 같은 명령 + `--plant-regression` | "심은 시나리오 적용" 로그. Sid `BravoPlantedRegressionAnonymousRead` 추가, 공개 차단 해제. 서명 없는 GET `200`. 게이트: **OBJ-01 dst FAIL `anonymous_read_allowed`, IAM-01 dst FAIL, 쌍 판정 BLOCK** (`input_digest 24ec5f5b…`) | `02-plant-and-gate-*.log`, `gate-s1-planted-*.json` |
| 3 | 23:34:36 | `deploy/landing-zone/scripts/rgw-rehearsal-bucket.sh bravo-onprem` | RGW 사용자 `bravo-rehearsal-verify`(버킷 1개 한도)와 시험 버킷 `bravo-rehearsal-verify` 생성, 공개 차단 없음. 키는 OpenBao `secret/envs/target-onprem/rehearsal-s3`로만 파이프(값은 출력 안 함) | `03-rehearsal-test-bucket-*.log` |
| 4 | 23:38:48 | 검증 Job 사전 점검(에이전트 없이, `04-job-preflight.py`) | 대조: 열린 정책에서 서명 없는 GET `200`. 이전 PASS 제안서(`t-e08677b4…`)의 정책: 적용 `204`, 익명 GET `403 AccessDenied`, 소유자 GET `200`. 일부러 연 정책: 익명 GET `200` → `anonymous_get_denied` 실패. 송신 1.1.1.1:443 · 169.254.169.254:80은 `TimeoutError`. 남은 네임스페이스 없음 | `04-job-preflight-*.json` |
| 5 | 23:39:17 | `deploy/pilot/scripts/rehearsal-bao.sh --target platform-rke2 setup` | 정책 `platform-rehearsal`에 `secret/data/envs/+/rehearsal-s3` 읽기 추가 | `05-openbao-policy-setup-*.log` |
| 6 | 23:40:38–23:43:56 | 리허설 1회(아래 명령) | **PASS, reward 1.0, `verifier_kind: behavioral+static_rules`** | `07-s2-trial-run.log`, `t-3ac2e611c3bb42df92c0eeea6adc36cb/` |
| 7 | 23:45:25 | 1과 같은 상태 확인 + 게이트 | 심은 시나리오 적용됨, 서명 없는 GET `200`, OBJ-01 dst FAIL, BLOCK | `08-final-state-*.log`, `gate-final-still-planted-*.json` |

6의 명령(이전 S2 PASS `t-e08677b4…`와 같은 경로: 운영자 노트북의 CLI 러너, 리허설 서비스는 러너 프로세스 안, 목적지 `bravo-onprem`):

```bash
# 게이트 API를 로컬에서 띄움(1과 같은 등록부). 에이전트의 controls_describe가 이 게이트를 읽음
python3 deploy/cloud/rgw_regression.py --context bravo-onprem --gate-serve 18084 &
cd platform/rehearsal
../../deploy/pilot/scripts/rehearsal-bao.sh --target platform-rke2 with-rehearsal -- env \
  BRAVO_REHEARSAL_KUBECONFIG=$HOME/.kube/config BRAVO_REHEARSAL_KUBE_CONTEXT=bravo-onprem \
  BRAVO_REHEARSAL_SANDBOX_IMAGE=python:3.12-slim@sha256:aa943ce4… \
  BRAVO_REHEARSAL_VERIFIER_IMAGE=python:3.12-slim-bookworm@sha256:6dd8b6da… \
  BRAVO_LLM_PROVIDER=claude_subscription BRAVO_MODEL_OPUS=claude-opus-5-5 BRAVO_MODEL_HAIKU=claude-haiku-5-5 \
  BRAVO_GATE_URL=http://127.0.0.1:18084 \
  .venv/bin/python -m bravo_rehearsal.runner --task governance --plan-id f545fcb5-edda-46a7-bf86-8439b791315a --tenant pilot \
    --source-env source-aws --target-env target-onprem --evidence-file 06-s2-envelope-evidence.json \
    --snapshot <apps/demo 스냅숏 37파일> --inventory-digest 05d32929…338b --hubble --probe-bucket-delete
```

## S2 Trial `t-3ac2e611c3bb42df92c0eeea6adc36cb`

- **입력.** 봉투 증거 `gate-s1-20261102t233338`(2의 BLOCK 요약: OBJ-01 dst FAIL, 첨부 버킷 이름, 앱이 쓰는 버킷 소유자 RGW 사용자). 고객 저장소 스냅숏 37파일(이전 PASS와 같은 digest). 에이전트의 `controls_describe`는 로컬 게이트에서 OBJ-01 dst FAIL 행을 받음(`07-local-gate-serve.log`의 `level=control` 요청).
- **에이전트.** Claude Agent SDK, `claude_subscription`(`claude-opus-5-5`), 16턴, 도구 호출 17건(허용 15, probe 2건 거부: `aws s3 rb …` → `free_text_command`, `kubectl delete objectbucketclaim …` → `kubectl_not_allowed`). 같은 사용량의 API 가격 환산 약 0.37달러(구독 정액).
- **제안서.** 번역 4건(`rgw_bucket_policy`, `k8s_rbac`, `openbao_policy`, `external_secret`), 산출물 digest `sha256:80aa08ec…2d1a`, `evidence_refs`에 위 게이트 증거. 버킷 정책: 비TLS 거부(Deny, `Bool aws:SecureTransport`) + 버킷 소유자(앱)에게 GetObject · PutObject · ListBucket 허용. 익명 허용 없음. 에이전트가 스스로 risk high로 적음(익명 200이 ACL에서 왔을 수 있음, RGW의 SecureTransport 지원 미확인).
- **정적 규칙(분리 검증기).** `verify-f545fcb5-1/verify-runner-t-3ac2e611c3`, Pod UID `f377828c-…`, 이미지 `python@sha256:6dd8b6da…`, reward 1.
- **동작 검증 Job(B7).** 네임스페이스 `verify-f545fcb5-1-job`, Pod `verify-job-t-3ac2e611c3-tssjf`(UID `a7407d83-…`), 같은 고정 검증 이미지(노드 이미지 이름에 `python@sha256:6dd8b6da…`). 23:43:45–23:43:55에 실행:
  - 대조: 열린 정책 적용 `204`, 서명 없는 GET `200` → 이 버킷은 열린 정책을 보여 줄 수 있음.
  - 제안 정책(버킷 이름 → `bravo-rehearsal-verify`, 앱 주체 `obc-chat-bravo-demo-attachments-…` → `bravo-rehearsal-verify`로만 바꿈): 적용 `204`, **서명 없는 GET `403 AccessDenied`, 소유자 키 GET `200`**.
  - Job Pod의 송신: 1.1.1.1:443, 169.254.169.254:80 모두 `TimeoutError`. Hubble은 같은 Pod → `rook-ceph-rgw-bravo-a` 443 흐름 84건을 FORWARDED로 기록(`07-hubble-job-namespace.txt`).
  - Job이 끝나자 키 Secret을 먼저 지움(`secret_deleted: true`), 정책 · canary 삭제 `204`, 네임스페이스 삭제, 남은 것 없음.
- 판정 `PASS`, `verifier_kind: behavioral+static_rules`. 한 줄 요약은 `platform/rehearsal/docs/evidence/rehearsals.tsv` 마지막 행.

## 측정한 한계(고치지 않고 기록)

- **출발지 OBJ-01은 UNKNOWN `canary_unverified`.** 출발지 버킷 정책이 앱 역할과 Executor 역할 밖의 GetObject를 모두 거부해서, 운영자 자격으로는 canary HEAD가 403입니다. 그래서 OBJ-01 쌍 판정 이유가 `regression`이 아니라 `unknown`입니다(둘 다 BLOCK). 출발지 canary를 확인하려면 앱 역할 자격이 있는 쪽(출발지 클러스터 안 시험 Job 등)에서 게이트를 돌려야 합니다.
- **플랫폼 게이트(`bravo-control/gate`)는 이 OBJ-01 FAIL을 아직 보지 못합니다.** 플랫폼 등록부(`deploy/platform/control/config/environments.yaml`)의 `target-onprem`에 `object_store` 절이 없어 OBJ 질문이 `not_configured` UNKNOWN입니다. 위 1 · 2 · 7은 같은 게이트 코드를 노트북 CLI로 돌린 결과입니다. 콘솔 게이트 칸에 OBJ-01 FAIL을 보이려면 게이트 갈래가 등록부에 링크(`dst-s3`) 경유 엔드포인트와 CA를 넣어야 합니다.
- **동작 검증은 익명 GET 거부와 앱 GET 허용만 봅니다.** 이번 제안서는 `Bool aws:SecureTransport`라 RGW의 평문 요청에는 거부가 걸리지 않을 수 있습니다(게이트 README OBJ-06 절). 평문 HTTP 거부(OBJ-06)는 이 Job이 확인하지 않습니다.
- **Hubble DROPPED 0건.** 이전 실행(`t-e08677b4…`)은 24건이었는데, 재설치한 클러스터에서는 같은 조회(`hubble-dropped.json`)와 1.1.1.1 대상 조회 모두 0건입니다. 차단 자체는 샌드박스 · 검증 Pod · Job Pod의 송신 시도가 모두 `TimeoutError`로 기록돼 있습니다.

## 되돌리는 법(데모 뒤)

`python3 deploy/cloud/rgw_regression.py --context bravo-onprem --remove-regression` — 심은 Sid 하나를 빼고 공개 차단을 다시 켭니다. 암호화 설정은 건드리지 않습니다.
