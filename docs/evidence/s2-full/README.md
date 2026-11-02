# S2 전체 사슬 실측: 코드 수정 + 버킷 정책, 플랫폼 리허설 서비스 경로 (2026-11-03 02:36–04:00 KST)

리뷰 24 L2 갈래(B05/P11, P3, B11/P14, B24/P12, B10/P13, B04/P2 S2 몫)의 실측 기록입니다. 스펙 S2를 **사람 승인 직전까지** 실제로 돌렸습니다. 승인 행은 만들지 않았고 실제 목적지(`chat`, `bravo-demo-attachments`, 플랫폼 OpenBao의 `app-*` 정책)에는 아무것도 반영하지 않았습니다. S1의 심은 시나리오(열린 첨부 버킷)는 그대로 둡니다.

## 결론

| 항목 | 결과 | 증거 |
|---|---|---|
| 한 계획에서 두 작업을 0:00에 함께 시작 | 계획 `a0189c2b-…`(실행 18): governance **PASS 227.1초**, app_migration **PASS 315.5초**, 둘 다 첫 시도 | `18-preview4-both.jsonl` |
| 코드 수정(Secrets Manager → OpenBao · ESO) | 에이전트가 `secrets.py` · `config.py`에서 Secrets Manager 호출과 선택 분기를 지움. 검증 Job: ESO가 플랫폼 OpenBao에서 비밀번호를 8.1초에 동기화, 고친 앱이 그 파일만으로 DB에 붙어 `/readyz` 200, 채팅 쓰기 201 · 같은 키 재전송 200(같은 ID), 다른 SA는 Secret 읽기 불가, 고치기 전 코드는 같은 조건에서 `DB_PASSWORD_SOURCE is required`로 종료(대조) | `18-…jsonl`의 app_migration `result` |
| 버킷 정책을 게이트의 OBJ-01–06 코드로 검증 | 게이트 이미지(`gate@sha256:c841a75e…`, 현재 게이트 고정)에서 `bravo_gate.probes.objstore`가 시험 버킷에 6개 모두 PASS. 대조: 열린 정책에서 게이트 OBJ-01 FAIL | 같은 파일 governance `result` |
| 검증기 PASS = T31 반영 가능 | 실행 18의 governance 제안서를 T31 `policies.scoped`에 넣으면 통과(ExternalSecret · SA · Role · RoleBinding, `rgw_bucket_policy`, `openbao_policy` `app-target-onprem-chat-db`) | `19-t31-scope-run18.json` |
| 승인이 묶을 값 | app_migration: diff `sha256:06a8043c…`, 산출물 `sha256:b2fd17e4…`, T31 prepare(buildx → syft → trivy, CRITICAL 0) 이미지 **`bravo.local/bravo-demo@sha256:0a627852…`**. governance: 정책 diff `sha256:9c645329…` | `20-t31-prepare-run18/` |
| T31 정책 반영 단계(P3) | `rgw_bucket_policy`(소유자 키로 PutBucketPolicy → 다시 읽어 같음), `openbao_policy`(`sys/policies/acl/<name>` → 다시 읽어 같음) 추가. 실제 쓰기는 **시험 버킷**에서만 확인: 적용 OK, readback equal, 정리 204, 익명 Allow는 쓰기 전 거부 | `15-t31-rgw-apply-selftest.txt` |
| probe 표시(B10/P13) | 버킷 삭제 두 건은 `POST /v1/trials/{id}/probes`로만 들어가고 기록에 `origin: probe`. 실행 18의 `tool_summary`: governance `agent` 허용 8 · 거부 1, `probe` 거부 2 | `18-…jsonl` `probe` 줄 |
| Hubble DROPPED | 0이 아님. Trial 네임스페이스마다 `POLICY_DENIED` TCP 12건(실행 18: Trial당 36건). 11/2 CLI Trial `t-3ac2e611…`도 36건 | `12-…json`, `12b-…json` |

`16-t31-prepare/` · `20-t31-prepare-run18/`의 trivy 원본 JSON(`trivy.json`)은 넣지 않았습니다. 기반 이미지의 `GPG_KEY`(Python 배포 서명 공개 키 지문) 환경 변수를 gitleaks가 키로 잡습니다. 집계(심각도별 수, DB 판, 정책 결과)는 `prepare.json` · `stages.jsonl`에 있고, SBOM은 `sbom.spdx.json`입니다.

## 경로: 콘솔 대신 무엇을 돌렸나 (B24/P12)

운영자(naver)가 아직 로그인하지 않아 콘솔 → API `POST /v1/plans/{id}/rehearsals`를 쓸 계획이 없습니다. 그래서 **API Pod 안에서 API 자신의 코드로** 같은 상류 호출을 했습니다(`platform/rehearsal/scripts/platform_trial.py`, stdin으로 실행, 이미지에 넣지 않음).

1. 수집기 `GET /v1/inventory` · `/v1/inventory/snapshot`(`CollectorClient`) → 2. 게이트 `POST /v1/controls/report`(`GateControlsClient`, 게이트 Pod가 재시작 중일 때는 `--skip-gate`) → 3. 리허설 `PUT /v1/snapshots/b-free/<plan>` → 4. `POST /v1/trials`(본문 `trial_request`, 증거 `evidence_from`) → 5. 데모 probe `POST /v1/trials/{id}/probes` → 6. `GET /v1/trials/{id}` 1초마다 → 7. FAIL이면 다음 시도(`prior_failures`, 최대 3회).

**콘솔이 대신할 단계:** 계획 행 · 상태 머신 전이 · `rehearsals` 행 쓰기(1 전), 그리고 6의 결과를 SSE 이벤트(`trial.started` · `trial.progress` · `tool.denied` · `trial.verdict`)로 내는 일. 1–6의 호출은 같습니다. API는 지금 계획마다 리허설 하나만 돌리므로(`rehearsal_active`) 두 작업을 0:00에 함께 시작하려면 역할마다 하나를 허용해야 합니다(아래 "남은 것").

리허설 서비스: 배포된 Deployment(이미지 `rehearsal@sha256:1e1337fa…`)는 플랫폼 경로에서 한 번도 Trial을 끝내지 못했습니다(실행 00 · 01, 아래). 고친 이미지는 고정 변경을 팀장이 push해야 배포되므로, 같은 SA · Secret · 설정을 쓰는 **미리 보기 Pod**(`rehearsal-preview-s2full`…`4`, `app.kubernetes.io/name: rehearsal` 라벨이라 네트워크 정책이 같고, readiness가 늘 실패해 Service `rehearsal`에는 들어가지 않음)로 쟀습니다. 마지막 미리 보기 이미지는 `rehearsal@sha256:5c554f5b…`이고 이 커밋의 고정값입니다.

### 플랫폼 경로에서 처음 나온 막힘과 고친 것

| 실행 | 결과 | 원인(실측) | 고친 것 |
|---|---|---|---|
| 00 | INFRA_ERROR `k8s_403` | 실행용 Role이 configmaps update/patch를 주는데 provisioner에 그 권한이 없음(RBAC 승격 방지) | `deploy/platform/link/onprem-rbac/rehearsal.yaml`에 추가, 온프렘에 적용 |
| 01 | INFRA_ERROR `cluster_unreachable` | 링크 Service 이름은 API 서버 인증서에 없음. kubeconfig의 `tls-server-name`이 실행 신원 클라이언트로 복사되지 않음 | `cluster.py` · `harbor_env.py`에서 `tls_server_name` 유지 |
| 02 | governance `job_pod_missing`, app `eso_approle:openbao_denied` | 게이트 이미지 반입 뒤 원래 이름을 지워 CRI가 컨테이너를 못 만듦 / OpenBao 정책의 `rehearsal-eso-+`는 경로 조각 전체만 맞음 | `import-platform-image.sh`(원래 이름 유지, 정규화 이름) / 정책에 대상별 경로를 명시 |
| 06 · 07b · 09 | app `eso_not_synced` | 서비스가 secret_id를 늘 1회용으로 발급(ESO는 저장소 확인과 동기화에 두 번 로그인) / 처음 만든 AppRole이 모든 로그인을 403으로 거부(지우고 다시 만드니 정상, 원인 못 찾음) | `mint_secret_id(num_uses=4)`, `rehearsal-bao.sh eso-setup`에 로그인 점검 |
| 11 | governance `behavioral_job:k8s_400` | 반입한 게이트 이미지의 이름이 `docker.io/` 없이 남아 CRI 조회 실패 | 노드에 정규화 이름 태그, 스크립트 수정 |
| 14 → 17 | governance PASS인데 T31 범위 거부 | OpenBao 정책이 HCL 문자열이라 검증기가 경로를 못 보고 통과, ExternalSecret 이름이 T31 이름 규칙 밖 | 검증기가 HCL 경로와 T31 이름 · 네임스페이스 · 버킷 규칙을 같이 봄, T31은 HCL을 읽음 |

## 코드 수정 작업 (B05/P11)

- 템플릿 `platform/agent/harbor/app_migration`. `/app`은 이제 스냅숏 작업 복사본으로 시작하고, `make_diff`는 `cd / && diff -ruN src app`(상대 머리말, `-p1`).
- 검증 Job(`behavioral_app.py` + `verify_job_app.py`, 플랫폼 보관): `verify-<plan>-app-<n>-app`에 namespaced SecretStore(AppRole `rehearsal-eso-target-onprem`, secret_id 120초 · 4회, 동기화 뒤 Secret 삭제) + ExternalSecret → `secret/envs/target-onprem/rehearsal-app-db`. Pod 하나: 합성 DB(목적지 chat-db와 같은 `postgresql:16.15-system-bookworm`, 스냅숏 스키마 `01_schema` · `02_roles` · `03_src`), 고친 앱 · 원래 앱(목적지 앱 이미지 `bravo-demo@sha256:b5c0e764…`, 코드는 ConfigMap으로 `PYTHONPATH`에), probe. 앱이 받는 것은 `DB_DSN`과 `DB_PASSWORD_FILE`뿐.
- diff는 플랫폼이 스냅숏에 그대로 적용합니다(문맥 불일치 · 경로 이탈이면 제안서 FAIL). T31은 같은 diff를 `git apply -p1`로 저장소에 적용합니다. 스냅숏 파일의 mtime이 0이라 diff 머리말 날짜가 1970이고, git이 그것을 "새 파일"로 읽어 처음 prepare가 실패했습니다(`16-t31-prepare/attempt1-diff-1970/`). T31은 머리말 날짜를 떼고 적용합니다(결속 digest는 원문 그대로).

## 버킷 정책 작업과 검증기 = 게이트 (B11/P14)

- 검증 Job이 게이트 이미지에서 게이트의 OBJ-01–06을 시험 버킷에 돌립니다(`verify_job_gate.py`). 시험 버킷은 목적지와 같은 SSE-KMS 기본 암호화, "다른 자격"은 권한 없는 RGW 사용자 `bravo-rehearsal-other`.
- **측정한 RGW 동작(03–05):** RGW는 평문 HTTP 요청에 `aws:SecureTransport`를 넣지 않습니다. 그래서 `Bool`로 쓴 Deny는 평문을 막지 못하고(암호화 없는 버킷: 평문 GET 200, SSE-KMS 버킷: 400 InvalidRequest = 게이트 UNKNOWN), `BoolIfExists`만 403으로 막습니다(목적지의 기존 `DenyInsecureTransport`와 같음). 11/2의 PASS 제안서(`Bool`)는 지금 검증기로는 OBJ-06 FAIL입니다.
- 계획 `778b074a`: governance 1–3차가 모두 `Bool`이라 OBJ-06으로 FAIL(08, 스펙 4a: 에이전트는 rationale에 "정책만으로는 못 고칠 수 있음, risk high"라고 적고 멈춤). 실패 요약에 실패한 검사와 관측값을 넣게 API `prior_failures`를 고쳤지만 3차도 같았습니다. 플랫폼이 측정한 엔진 사실을 목적지 프로필(`object_store.engine_facts`)에 넣고 사람이 다시 시작하자 `BoolIfExists`로 PASS(14, 18).
- 비밀 경로는 목적지 프로필에서 옵니다(`deploy/platform/control/config/destination-profiles.json` → 봉투 `inputs.destination_profile`). 검증기는 `envs/target-onprem/` 밖의 OpenBao 경로 · ExternalSecret 키를 `secret_path_outside_profile`로 거부합니다(11/2 제안서의 `envs/onprem/…`는 이제 FAIL).

## 시간 (B04/P2 S2 몫)

| 단계 | 실행 18 governance | 실행 18 app_migration | 다른 PASS(13, 14) |
|---|---|---|---|
| provisioning(네임스페이스 · 격리 시험) | 9.7초 | 9.6초 | 8.6–9.5초 |
| agent_running(모델) | 101.1초 | 160.5초 | 132.8–141.6초 |
| proposed(Harbor 분리 검증기, 정적 규칙) | 43.5초 | 37.4초 | 43.5–43.8초 |
| verifying(검증 Job) | 72.6초 | 106.7초 | 72.5–103.2초 |
| 합계 | **226.9초** | **314.2초** | 258.3 · 297.1초 |

- 비동기 시작: 서비스는 `POST /v1/trials`에 바로 202로 답하고 Trial을 백그라운드로 돌립니다. 두 작업은 0.4초 차이로 시작했습니다. Trial 기록에 `phase_history` · `timings`가 있고, API 감시자는 단계마다 `trial.progress`를 한 번 냅니다(마이그레이션 0010, 이 커밋).
- 0:00에 시작하면 governance는 약 3분 47초, app_migration은 약 5분 15초에 끝납니다. 각본의 S2 장면(0:40–1:30)에는 끝나지 않습니다. 줄일 곳: 정적 검증기 단계(Pod 하나 만드는 데 37–44초)와 검증 Job을 겹치기, 모델 턴 수.

## 모델 경로 실측 (B24/P12)

- 공급자 `claude_subscription`, 모델 `claude-opus-5-5`. 서비스는 Kubernetes 인증(`rehearsal` 역할)으로 OpenBao에 로그인하고, Trial마다 `llm-claude` secret_id(60초 · 1회)를 발급해 워커에 넘깁니다. 워커가 한 번 읽고 토큰을 폐기합니다(`/readyz` `llm_token: ok`).
- 워커 13회: 턴 14–22, API 가격 환산 합계 약 8.52달러(구독 정액이라 청구 없음), 표준 오류 출력에 rate limit · quota · 429 · overloaded 줄 0(`10-…txt`, `10b-…txt`). 구독 한도 자체는 이 측정으로 알 수 없습니다(UNKNOWN).

## 남은 것 (측정한 이유)

- **승인 · 반영 · 게이트 재대조 PASS는 하지 않음**(지시). 승인은 콘솔에서 사람(naver 요청, gmail 승인)이 합니다. 운영자 계획이 없어 API change 행도 없습니다.
- API는 계획마다 리허설 하나만 돌림(`rehearsal_active`). 콘솔에서 두 작업을 0:00에 함께 시작하려면 역할마다 하나를 허용해야 함(L1 상태 머신 몫).
- 미리 보기 Pod는 `prometheus-ingress` 밖이라 서비스 자신의 드롭 수 읽기는 `prometheus_unreachable`. 같은 함수를 노트북에서 port-forward로 돌려 기록함. 이 커밋의 네트워크 정책이 push되면 배포된 서비스가 직접 읽음.
- 게이트 Pod(`c841a75e…`)가 실행 중 재시작을 반복해 07a · 18a는 막혔고, 이후 실행은 `--skip-gate`(봉투에 게이트 행 없음, 에이전트의 `controls_describe`는 `gate_unreachable`).
- AppRole이 처음 만든 상태에서 로그인 403을 낸 원인은 못 찾음(지우고 다시 만들어 해결, 점검을 스크립트에 넣음).
- 실측은 앱 런타임 이미지 `bravo-demo@sha256:b5c0e764…`(측정 시작 때 목적지 이미지)로 했습니다. 03:54에 목적지 앱이 0.3.3(`76e8f595…`)으로 바뀌어 Deployment 설정은 그 digest를 씁니다(0.3.3 런타임으로는 다시 재지 않음). 수집기 스냅숏 고정(`customer-app-source.env`, 240856f)도 0.3.3보다 옛 코드입니다.

## 정리할 것 (팀장)

미리 보기 Pod `rehearsal-preview-s2full`, `…2`, `…3`, `…4`(activeDeadlineSeconds로 스스로 끝남)와 ConfigMap `rehearsal-preview-profiles`(bravo-control). 온프렘 노드의 이미지 별칭 `docker.io/library/bravo-gate-probe:import`, `bravo-import/gate:c841a75e376a`는 CRI 이름 때문에 남겨 둠.
