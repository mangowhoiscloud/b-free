# 03. 판정, 궤적, 증거

소유 코드: `src/bravo_rehearsal/verdict.py`, `src/bravo_rehearsal/runner.py`
확인 명령: `make test T=tests/test_verdict.py`

## 판정은 셋입니다

| 판정 | 조건 | 다음 동작 |
|---|---|---|
| PASS | 분리 검증기가 reward 1을 기록 | 상태 머신이 사람 승인을 요청 |
| FAIL | 검증기가 reward 0을 기록 | 시도 횟수가 3 미만이면 다시 시도. 3이면 NEEDS_HUMAN |
| INFRA_ERROR | 환경 시작 실패, timeout, 검증기 미실행 | 같은 입력으로 다시 실행. 시도 횟수에 넣지 않음 |

인프라 잡음만으로 점수가 6pt 움직인 실험이 있습니다. 그래서 인프라 실패를 FAIL과 섞지 않습니다.

## 증거 한 줄

`docs/evidence/rehearsals.tsv`에 리허설 1회를 1행으로 남깁니다. 열은 다음과 같습니다.

- 날짜, 실행 ID, task, task SHA-256, 4사분면 칸
- backend, Harbor(에이전트 샌드박스 · 평가 프레임워크, CNCF 레지스트리 Harbor와 다름) 버전
- 판정, reward, 단계별 초, 실행 경로, 메모

## 채점 분리

- 검증은 분리 검증기에서만 합니다. 에이전트 환경에서 채점하지 않습니다.
- 검증기는 시작 전에 이전 reward 파일을 지웁니다.
- 데모 전에 "에이전트가 미리 reward 파일을 써 두는 공격"을 재현해 막히는지 시험합니다(`../notes/C-SBX-002.md`).

근거: `research/sandbox-research/07-harbor-papers.md` 2.1절과 2.2절
