# 런북

사람이 따라 하는 절차입니다. T28에서 채웁니다.

| 파일(계획) | 내용 |
|---|---|
| `cutover.md` | 컷오버 전 확인, 실행, 결과 확인 |
| `failback.md` | 되돌리기 판단 기준과 절차 |
| `safe-hold.md` | SAFE_HOLD에서 사람이 확인할 것. 미해결 효과의 해소(제안 D-19): 효과마다 실제 상태와 근거 기록, PgBouncer `SHOW CLIENTS` · `KILL_CLIENT`, 필요하면 노드 수동 정지. 금지: Executor Pod를 강제 삭제하지 않습니다. 끊긴 노드의 옛 프로세스가 계속 돌 수 있기 때문입니다 |
| `reset.md` | 데모를 처음 상태로 되돌리기 |
| `destroy.md` | 모든 환경 삭제와 비용 확인 |
