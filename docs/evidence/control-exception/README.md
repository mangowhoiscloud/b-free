# 통제 예외(스펙 S1 5b) 증거

2026-11-02 23:3x–23:4x KST. 계약은 `docs/architecture/15-r2-contracts.md` 9절, 코드는 커밋 `f1cd770`입니다.

## 확인한 것

| 파일 | 무엇 | 결과 |
|---|---|---|
| `api-tests.txt` | `platform/api` 시험(실제 postgres:16 컨테이너, RLS · 트리거 · 권한 포함) | 157 통과. 예외 흐름 시험 `tests/test_exceptions.py` 1개, 판정 규칙 시험 `tests/test_gate.py` 9개 추가 |
| `gate-tests.txt` | `platform/gate` 시험 | 33 통과, 2 건너뜀(이전과 같음). 증거 digest 시험 1개 추가 |
| `live-db-0008-dryrun.txt` | 플랫폼 DB(`bravo-platform-rke2`)에 0008을 소유자 역할로 한 트랜잭션 안에서 실행하고 ROLLBACK | 모든 문장 성공, 정책 2개. ROLLBACK 뒤 `control_exceptions` 없음, `schema_migrations`는 0007까지 그대로 |
| `images.txt` | api · gate 이미지(arm64) 빌드와 ECR push, 로컬 확인 | api 이미지가 비슈퍼유저 소유자로 0001–0008을 적용함. gate 이미지가 OBJ-06 행에 digest를 붙임 |
| `live-gate-before.txt` | 지금 운영 API Pod에서 게이트를 부른 결과 | 예전 API 호출(`/v1/controls`, 인자 없음)은 422. 게이트 등록부에는 `source-aws`, `target-onprem`만 있고 둘 다 `kubernetes` 절뿐이라 모든 질문이 UNKNOWN. `target-gcp`는 404 `unknown_environment` |

시험이 고정한 것(`tests/test_exceptions.py`):

- 직무 분리: 요청자(operator와 approver를 다 가진 `dual`)는 자기 요청을 승인하지 못함(API 403 `separation_of_duties`, DB 트리거 직접 UPDATE도 거부). operator는 `insufficient_scope`.
- UNKNOWN은 예외 불가(5a): API 422 `exception_not_allowed` `unknown`, DB 직접 INSERT도 트리거가 거부.
- 동등 판정에서 뺌: 승인 뒤 OBJ-06 행이 `EXCEPTION`이고 `reasons`에 없음. 다른 UNKNOWN은 그대로 막고, 나머지가 PASS가 되면 판정 PASS → AWAIT_APPROVAL.
- 증거가 바뀌면 무효: 목적지 digest가 바뀐 판정에서 예외가 `stale`(`evidence_changed`)이 되고 OBJ-06이 다시 `regression`으로 막음. `exception.stale` 이벤트에 전후 digest.
- 만료된 증거로는 요청하지 않음(`evidence_expired`, 스펙 §8.5).

## 아직 안 된 것

- 운영 DB에는 0008이 아직 없습니다. 고정 digest(`deploy/platform/control/kustomization.yaml`)를 main에 올리면 Argo CD Sync 훅 Job `platform-db-migrate`가 새 api 이미지로 0008을 적용합니다. 시도 기록은 위 ROLLBACK 실행뿐입니다.
- GKE OBJ-06 예외는 운영에서 아직 걸 수 없습니다. 플랫폼 게이트 등록부(`deploy/platform/control/config/environments.yaml`)에 `target-gcp`가 없고, `source-aws`에도 `object_store` 절이 없어 OBJ-06이 측정되지 않습니다(UNKNOWN은 예외 불가). 등록부에 두 환경의 `object_store`를 넣어야 OBJ-06 PASS/FAIL과 digest가 나옵니다.
- 같은 통제 ID가 Executor와 게이트 양쪽에서 오면 UNKNOWN입니다(기존 규칙). 게이트 보고서를 실제로 읽게 되면서 `DB-ANON-READ`, `DB-AUTH`가 두 곳에서 와 UNKNOWN으로 막힙니다. 전에도 모든 판정이 `gate_controls_http_422`로 막혔습니다. 규칙을 바꿀지, 게이트 쪽 질문을 뺄지는 게이트 갈래와 정해야 합니다.
- 예외 행과 승인은 이 작업에서 운영 DB에 만들지 않았습니다. 데모에서 사람이 콘솔로 만듭니다.
