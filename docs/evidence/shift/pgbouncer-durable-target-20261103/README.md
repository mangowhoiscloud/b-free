# PgBouncer 활성 대상이 재시작에도 남는가(2026-11-03 03:46–03:47 KST)

Executor가 PgBouncer 대상을 바꾸면 ConfigMap `chat-pgbouncer-target`(`target` = src · dst)도 함께 고칩니다. PgBouncer init 컨테이너는 그 값이 가리키는 `target-<side>.ini`를 고릅니다. 온프렘에서 dst로 바꾸고 재시작하니 dst가, src로 되돌리고 재시작하니 src가 선택됐습니다.

| 순서 | 일 | 기록 |
|---|---|---|
| 1 | 온프렘 · 출발지에 Role 규칙 추가(ConfigMap `chat-pgbouncer-target` 이름 하나에 get · patch), ConfigMap 생성(target=src), PgBouncer 매니페스트 적용(풀 30 · 최소 10, init 컨테이너가 ConfigMap을 읽음). 다른 ConfigMap은 여전히 읽지 못함(`can-i` no) | `apply-onprem.txt`, `apply-source.txt` |
| 2 | executor-0 안에서 Executor 코드와 권한(D-16 링크의 `bravo-executor` 토큰)으로 목적지 풀을 dst로 전환. 활성 파일과 ConfigMap 모두 dst | `restart-keeps-target.txt` |
| 3 | PgBouncer 재시작: init 로그 `selected target-dst.ini`, 활성 파일 host=`chat-db-rw` | 같음 |
| 4 | src로 되돌리고 재시작: `selected target-src.ini`, 활성 파일 host=`src-db-link`. 목적지 `/readyz` 200 | 같음 |

시험하는 동안 LB는 출발지 100%였고 부하 실행은 없었습니다.
