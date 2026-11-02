# task 색인

Harbor(에이전트 샌드박스 · 평가 프레임워크, CNCF 레지스트리 Harbor와 다름)의 task 형식을 따릅니다. 폴더 하나가 task 하나입니다.

| task | 4사분면 칸 | 용도 | 상태 |
|---|---|---|---|
| `templates/fix-secret-injection` | 비결정론 입력 × 결정론 출력 | Secrets Manager 직접 호출을 주입 방식으로 고침(설계 문서 8.4 P0 작업) | 템플릿(oracle 전용) |
| `templates/fix-bucket-policy` | 비결정론 입력 × 결정론 출력 | 버킷 정책 번역(옛 단일 번역 형식) | 준비 중: 비공개 검증기가 옛 제안서 형식을 판정하고(사람이 고칠 일, AGENTS.md), `environment/source`의 계정 `000000000000`은 자리표시입니다. 리허설은 `governance`(`platform/agent/harbor/governance`)를 씁니다. 고객 원본 정책은 수집기 스냅숏으로만 들어갑니다 |

## 새 task를 만드는 절차
1. 템플릿 폴더를 복사합니다.
2. `instruction.md`의 목표와 지표를 고칩니다.
3. `solution/solve.sh`로 정답을 만듭니다.
4. `make oracle TASK=<폴더>`가 `reward=1`인지 확인합니다.
5. `make check`를 통과시킵니다.

규칙은 `tasks/AGENTS.md`에 있습니다.
