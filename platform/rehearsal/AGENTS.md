# bravo-rehearsal: 에이전트 안내

에이전트가 만든 이전 수정안을 Harbor(에이전트 샌드박스 · 평가 프레임워크, CNCF 레지스트리 Harbor와 다름)에서 시험하고, 분리 검증기로 판정하는 Python 프로토타입입니다.
제품 설계의 정본은 Notion 통합 설계 문서 v0.1입니다: https://app.notion.com/p/40b8bee9ada480ecaca6f716e123368b
이 파일은 코드 작업에 필요한 사실만 둡니다.

## 명령

| 목적 | 명령 | 통과 판정 |
|---|---|---|
| 설치 | `make setup` | 종료 코드 0 |
| 정적 검사와 테스트 | `make check` | 종료 코드 0 |
| 테스트 하나 | `make test T=tests/test_tool_layer.py` | 종료 코드 0 |
| task 정답 확인 | `make oracle TASK=<task 폴더>` | 마지막 줄 `reward=1` |
| 리허설 1회 | `make rehearse TASK=<task 폴더>` | `docs/evidence/rehearsals.tsv`에 1행 추가 |

긴 출력은 파일로 보내고 판정 줄만 읽습니다.
`make rehearse TASK=<폴더> > run.log 2>&1; grep -E '^(verdict|reward)=' run.log`

## 어디를 읽을까

| 이 작업이면 | 먼저 읽을 것 |
|---|---|
| `tasks/` 아래 수정 | `tasks/AGENTS.md` |
| Harbor 환경 어댑터 | `docs/architecture/02-harbor-adapter.md` |
| task 생성기 | `docs/architecture/01-task-generator.md` |
| 판정, 궤적, 증거 | `docs/architecture/03-evidence.md` |
| 에이전트 개입 기준(4사분면) | `docs/architecture/00-overview.md` |
| 리허설 에이전트 프롬프트 | `prompts/README.md` |
| 설계 주장과 검증 상태 | `docs/notes/README.md` |
| 구조를 바꾸는 결정 | `docs/adr/README.md` |

## 코드만 봐서는 모르는 것

- Harbor는 0.25.0으로 고정합니다. 계약(메서드, 정책 선언)은 이 판의 소스를 기준으로 합니다.
- 에이전트 모델 루프는 플랫폼에서 돕니다. 샌드박스 안에는 모델 키가 없습니다.
- 리허설은 컷오버 전에만 허용합니다. 컷오버 뒤에는 상태 머신이 거부합니다.
- 결정론 입력과 결정론 출력 작업(게이트, 컷오버, 롤백)은 이 저장소 범위 밖입니다.

## 멈추고 사람에게 묻는 일

- 공유 브랜치 force-push, 클라우드 자원 생성과 삭제, `runs/` 삭제, 외부 메시지 전송
- 실제 자격 증명이 필요해 보이는 모든 작업
- `tasks/*/tests/`와 `src/bravo_rehearsal/verdict.py` 수정. 판정 기준이 바뀌기 때문입니다.

## 완료 조건

- `make check`가 통과합니다. 바꾼 task는 `make oracle`도 통과합니다.
- PR 본문에 실행한 명령과 증거 경로(커밋, `runs/` 경로, TSV 행)를 적습니다.
- 구조 결정이면 ADR을 추가하고 위 라우팅 표를 고칩니다.
- 설계 주장을 바꾸면 `docs/notes/`의 해당 노트 상태를 고칩니다.

## 규칙과 강제 수단

| 규칙 | 강제 수단 | 상태 |
|---|---|---|
| task는 분리 검증기와 에이전트 no-network를 명시 | `scripts/check_tasks.py` | 동작 |
| 문서 색인과 실제 파일 일치 | `scripts/check_docs.py` | 동작 |
| 노트의 ID, 의존, 검증 기록 일치 | `scripts/check_notes.py` | 동작 |
| 도구 계층은 구조화한 작업만 받고 파괴적 동사를 거부 | `tests/test_tool_layer.py` | 동작 |
| 검증기는 산출물을 import하지 않고, 보상 조작이 막힘 | `tests/test_verifier.py` | 동작(같은 UID 실행은 남는 위험) |
| 실제 자격 증명 금지 | gitleaks (CI) | 계획 |
