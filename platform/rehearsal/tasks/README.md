# task 색인

Harbor(에이전트 샌드박스 · 평가 프레임워크, CNCF 레지스트리 Harbor와 다름)의 task 형식을 따릅니다. 폴더 하나가 task 하나입니다.

| task | 4사분면 칸 | 용도 | 상태 |
|---|---|---|---|
| `templates/fix-secret-injection` | 비결정론 입력 × 결정론 출력 | Secrets Manager 직접 호출을 주입 방식으로 고침(설계 문서 8.4 P0 작업) | 템플릿 |

## 새 task를 만드는 절차
1. 템플릿 폴더를 복사합니다.
2. `instruction.md`의 목표와 지표를 고칩니다.
3. `solution/solve.sh`로 정답을 만듭니다.
4. `make oracle TASK=<폴더>`가 `reward=1`인지 확인합니다.
5. `make check`를 통과시킵니다.

규칙은 `tasks/AGENTS.md`에 있습니다.
