# 01. task 생성기

소유 코드: `src/bravo_rehearsal/task_generator.py`, `tasks/templates/`
확인 명령: `make test T=tests/test_task_generator.py`

Harbor(에이전트 샌드박스 · 평가 프레임워크, CNCF 레지스트리 Harbor와 다름) 0.25.0은 메모리 안의 task 객체로 Trial을 돌리지 않습니다. Trial은 언제나 task 폴더를 읽습니다. 그래서 이전마다 템플릿에서 폴더를 새로 만듭니다.

## 규칙

- `environment/`는 템플릿 그대로 둡니다. 이미지 캐시를 지키기 위해서입니다.
- 이전마다 다른 값은 지시문의 자리 표시자와 환경 kwargs로 넣습니다. 예는 정책 스냅샷 요약과 DB 스냅샷 ID입니다.
- `tests/`는 플랫폼이 보관한 것을 복사합니다. 에이전트는 이 폴더를 보지 못합니다.
- 생성한 폴더의 SHA-256을 계산해 증거에 남깁니다.

## 입력 자리

| 입력 | 넣는 곳 |
|---|---|
| 이전 목표, 정책 스냅샷 요약 | `instruction.md`의 `{{goal}}`, `{{snapshot_summary}}` |
| DB 스냅샷 ID, 정책 스냅샷 URI | `EnvironmentConfig.kwargs` |
| 산출물 경로 | `task.toml`의 `artifacts` |

## 함정

- 템플릿 안의 자리 표시자가 남으면 생성기는 실패합니다. 빈 값으로 Trial을 돌리지 않습니다.
- `task.toml`의 필드 이름은 Harbor 0.25.0 문서로 확인해야 합니다(`../notes/C-RHS-002.md`).

근거: `research/sandbox-research/08-harbor-k8s-fit.md` 4절
