# S2 시간 재측정 (2026-11-03, 앱 0.3.3)

리허설 두 작업(governance, app_migration)을 0:00에 함께 시작했을 때의 시간과, 승인 뒤 T31 반영 시간을 쟀습니다. 모두 플랫폼 경로(API Pod 안의 `platform/rehearsal/scripts/platform_trial.py`, Claude 구독 워커)입니다. 앱 런타임은 0.3.3(`bravo-demo@sha256:76e8f595…`), 게이트 Pod는 1Gi로 재시작 없이 돌았습니다.

## 리허설 Trial

| 실행 | 이미지 | governance | app_migration |
|---|---|---|---|
| 01 배포된 서비스 | `rehearsal@sha256:5c554f5b…` | PASS 253.8초 | PASS 263.1초 |
| 02 미리 보기 | `…4b219040…` (grace 1초, Job 병렬) | PASS 188.0초 | 1차 FAIL 151.7초(`app:diff_applies_to_snapshot`), 2차 PASS |
| 03 미리 보기 | `…8ab48aba…` (02 + 실제 diff 기록) | PASS 150.5초 | PASS 180.4초 |

단계별 시간(초, Trial의 `timings.phase_s`):

| | provisioning | 모델(agent_running) | 제안 뒤 판정까지 |
|---|---|---|---|
| 01 governance | 11.4 | 124.4 | 118.1 (proposed 44.7 + verifying 73.4) |
| 01 app_migration | 11.7 | 98.9 | 152.5 (43.9 + 108.6) |
| 03 governance | 9.9 | 112.5 | 27.1 |
| 03 app_migration | 10.0 | 118.5 | 51.9 |

제안 뒤 판정까지가 118–152초에서 27–52초로 줄었습니다. 이제 시간 대부분은 모델입니다.

줄인 방법(검사는 그대로):

- **grace 1초.** Harbor 샌드박스 · 검증기 Pod는 PID 1이 `sleep infinity`라 SIGTERM을 무시합니다. 그래서 환경을 지울 때마다 기본 30초를 기다렸습니다(01의 proposed 44초 대부분). 샌드박스, 검증기, 동작 검증 Job Pod에 `terminationGracePeriodSeconds: 1`을 넣었습니다. 증거는 Pod가 멈추기 전에 모두 모읍니다.
- **Job 병렬.** 동작 검증 Job을 Harbor 정적 검증기가 끝난 뒤가 아니라 제안서가 고정되는 `plan_propose` 때 시작합니다. 판정은 그대로 둘 다 PASS여야 PASS입니다.
- **이미지 미리 받기.** 확인만 했습니다. Job 이미지는 digest로 노드에 있었고, 서버 노드의 postgres도 이미 있었습니다(`up to date`). 효과 없음.
- **readiness 폴링 간격.** 바꾸지 않았습니다. 기다린 시간의 큰 몫이 grace 대기였습니다.

게이트 보고(게이트 `POST /v1/controls/report`, 실행 앞에 한 번)는 38.4–41.1초였습니다. Trial 시간에는 들어가지 않습니다. 각 실행 끝의 `result` 줄(270초 무렵)은 드라이버가 Prometheus 드롭 수를 기다린 90초 뒤의 기록이고 Trial 시간이 아닙니다.

## T31 반영 (승인 뒤 부분)

실제 목적지는 건드리지 않았습니다. 온프렘 검증 네임스페이스 `verify-t31timing`(리허설 네트워크 정책, ESO SecretStore, DB, 앱 0.3.3 두 개)과 리허설 시험 버킷, 시간 측정용 OpenBao 정책 이름에 반영했습니다. 반영한 내용은 실행 03의 governance 산출물(번역된 정책)과 prepare가 만든 이미지(`sha256:62192731…`)입니다. 도구는 `platform/apply/tools/t31_apply_timing.py`이고, 끝나면 버킷 정책 · OpenBao 정책을 지웁니다(둘 다 204). 네임스페이스는 측정 뒤 지웠습니다.

prepare(승인 전, `04-t31-prepare/`): 모두 약 13초. `image.build` 9.6초, `sbom.syft` 1.3초, `scan.trivy` 0.7초, 나머지 0.5초.

apply(`05-t31-apply/summary.json`, 단위 ms):

| 단계 | 05 (이미지 이미 반입) | run2 (처음 반입) |
|---|---|---|
| image.locate | 2 | 2 |
| destination.connect | 102 | 102 |
| policies.scope | 0 | 0 |
| policies.dryrun | 105 | (없던 단계) |
| staging.push | 172 | 974 |
| image.sign (cosign + OpenBao transit) | 1,313 | 1,275 |
| signature.verify | 61 | 46 |
| destination.import | 97 | 2,779 |
| deploy.precheck | 94 | 91 |
| deploy.rollout (2개) | 6,099 | 5,775 |
| deploy.verify | 71 | 65 |
| policies.apply (RGW 버킷 정책 + OpenBao 정책 + k8s) | 1,475 | 116 FAIL |
| 합계 | 9.6초 | — |

무대에서 처음 반입하면 약 13초입니다. 여기에 게이트 다시 대조(위 게이트 보고와 같은 호출, 38–41초)를 더하면 승인 뒤 약 52–55초입니다. `approval.verify`는 승인 행이 없어 돌리지 않았습니다(지시). 검증 네임스페이스 준비(13.2초)는 반영 시간이 아닙니다.

## 측정하다 찾은 것

- **`external-secrets.io/v1beta1`(run2).** 제안서의 ExternalSecret이 v1beta1인데 목적지 ESO 2.14에는 v1만 있습니다. 이미지 롤아웃이 끝난 **뒤** `policies.apply`에서 실패해 반쯤 반영된 상태가 됩니다. 고침: T31에 `policies.dryrun`(서버 측 dry run, 반입 전) 단계, 검증기 규칙 `external_secret_api_version`, 목적지 프로필 `secret_store.eso_api_version`.
- **CRI 이름(`destination.import`).** `ctr import` 뒤 아카이브 이름을 지우면 CRI가 이미지를 찾지 못합니다. 지우지 않게 고침.
- **가려진 diff(02 app 1차 FAIL).** 모델이 보는 도구 출력은 비밀 모양 값을 가립니다. `password=`가 들어간 코드 줄도 가려져, 모델이 낸 diff가 자기 스냅숏에 적용되지 않았습니다. 고침: `plan_propose`가 샌드박스의 실제 `make_diff`를 다시 돌려, 가린 결과가 제안서 diff와 같을 때만 받고 산출물에는 실제 diff를 넣습니다(`diff_not_from_workspace`).
- **run1.** 검증 네임스페이스의 DB가 루프백에서만 들어 앱이 준비되지 않음(`run1-db-loopback.log`). 측정 도구의 준비 문제이고 T31과 상관없습니다.

## 파일

`01-…` `02-…` `03-…`: 드라이버 기록(`.jsonl`), 시작 메모(`.started`), stderr. `01-…fetched.jsonl`은 API Pod가 재시작돼 드라이버가 끊긴 뒤 Trial 보기를 직접 받은 것. `04-t31-prepare/`, `05-t31-apply/`: 위 표의 원본.
